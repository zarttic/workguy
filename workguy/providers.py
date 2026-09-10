"""多模型网关（Multi-Model Gateway）。

解决各 LLM 服务商的「协议方言」差异：

- OpenAI    ：POST {base}/chat/completions，Bearer 鉴权，system 进 messages，
              tool_calls 的 arguments 是 JSON 字符串，用量用 prompt/completion_tokens
- Anthropic ：POST {base}/messages，需 anthropic-version 头，system 是顶层字段，
              tool_use 的 input 已是对象，用量用 input/output_tokens
- LongCat   ：双端点，OpenAI 格式为主；其 Anthropic 端点**不接受 x-api-key**，
              必须用 Bearer（与官方 Anthropic 规范不同）

设计：方言翻译集中在 ``_translate_request`` / ``_translate_response``，
这两个方法可独立测试，不触发网络。真实网络调用只在 ``complete`` 里发生，
测试一律用 ``MockProvider`` 或只测翻译方法，零网络依赖。

零第三方依赖：仅用标准库 urllib / json / ssl。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable

from .types import (
    ChatRequest,
    ChatResponse,
    Message,
    ProviderError,
    ProviderSpec,
    ToolCall,
)

# LongCat 实测端点（见 llm.py 注释）。其 Anthropic 端点必须用 Bearer，不要 x-api-key。
LONGCAT_OPENAI_BASE = "https://api.longcat.chat/openai/v1"
LONGCAT_ANTHROPIC_BASE = "https://api.longcat.chat/anthropic/v1"
LONGCAT_DEFAULT_MODEL = "LongCat-2.0"

# 各 provider 的默认 spec，方便无参构造。
DEFAULT_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o",
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        base_url="https://api.anthropic.com",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-3-5-sonnet-20241022",
    ),
    "longcat": ProviderSpec(
        name="longcat",
        base_url=LONGCAT_OPENAI_BASE,
        env_key="LONGCAT_API_KEY",
        default_model=LONGCAT_DEFAULT_MODEL,
    ),
}


class Provider(ABC):
    """provider 抽象基类。

    子类的 ``name`` 必须是类属性字符串（如 ``name = "openai"``）。

    api_key 解析优先级：构造显式传入 > 环境变量（由 ``spec.env_key`` 指定）。
    key **绝不**出现在任何日志或异常文本里。
    """

    name: str

    def __init__(
        self,
        api_key: str | None = None,
        spec: ProviderSpec | None = None,
        timeout: int = 60,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self.spec = spec or DEFAULT_SPECS.get(self.name) or ProviderSpec(
            name=self.name, base_url="", env_key="", default_model=""
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # sleep 可注入：测试时传假函数避免真实等待
        self.sleep_func = sleep_func or time.sleep
        self.name = self.__class__.name
        env_key = self.spec.env_key
        resolved = api_key or (os.environ.get(env_key) if env_key else None)
        self.api_key = resolved
        self._ctx = ssl.create_default_context()

    @abstractmethod
    def complete(self, req: ChatRequest) -> ChatResponse: ...

    def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[str] | None = None,
    ) -> Any:
        """适配 ``LLMClient`` 协议（见 llm.py），让 Provider 可直接当 ``llm`` 传入。

        存在的意义：原先 ``llm.py`` 的 ``LLMClient`` 与这里的 ``Provider`` 是两套
        互不相通的抽象，``LongCatLLM`` 与 ``LongCatProvider`` 功能近乎重复。
        本方法让 Provider 满足 LLMClient 协议，成为能力更全的唯一真源——它多了
        方言翻译，还能挂进 Gateway 做 fallback。``llm.py`` 保留为极简直连用法。
        """
        from .llm import LLMResponse  # 延迟导入，避免模块级循环依赖

        resp = self.complete(
            ChatRequest(messages=list(messages), tools=list(tools or []))
        )
        return LLMResponse(
            content=resp.content,
            tool_calls=resp.tool_calls,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            model=resp.model or model or self.spec.default_model,
        )

    @abstractmethod
    def _translate_request(self, req: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _translate_response(self, data: dict[str, Any]) -> ChatResponse: ...

    # ---- 共享工具方法 -------------------------------------------------

    def _model(self, req: ChatRequest | None = None) -> str:
        """路由出的模型优先：req.model 非空用它，否则回落 spec 默认。"""
        if req is not None and req.model:
            return req.model
        return self.spec.default_model

    def _require_key(self) -> None:
        """缺 key 时抛 ProviderError。异常文本只含 provider 名与 env 变量名。"""
        if not self.api_key:
            env = self.spec.env_key or "<unknown>"
            raise ProviderError(
                f"{self.name} 缺少 API key：请设置环境变量 {env}"
            )

    def _auth_headers(self) -> dict[str, str]:
        """默认 Bearer 鉴权；子类可按方言覆盖。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _raw_post(self, url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
        """真实网络调用。测试可 monkeypatch 此方法，零网络依赖。"""
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(
            req, timeout=self.timeout, context=self._ctx
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _extract_retry_after(err: urllib.error.HTTPError) -> float | None:
        """从响应体里解析 retry_after（优先于默认退避）。解析失败返回 None。"""
        try:
            body = err.read().decode("utf-8")
        except Exception:
            return None
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        err_obj = parsed.get("error")
        if isinstance(err_obj, dict) and err_obj.get("retry_after") is not None:
            return float(err_obj["retry_after"])
        if parsed.get("retry_after") is not None:
            return float(parsed["retry_after"])
        return None

    def _backoff_for(self, attempt: int, err: urllib.error.HTTPError) -> float:
        """第 n 次重试（n 从 1 起）等待 backoff_base * 2**(n-1)。

        若响应体带 retry_after 则优先采用它。
        """
        retry_after = self._extract_retry_after(err)
        if retry_after is not None:
            return retry_after
        return self.backoff_base * (2 ** (attempt - 1))

    def _http_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_key()
        data = json.dumps(payload).encode("utf-8")
        headers = self._auth_headers()
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._raw_post(url, data, headers)
            except urllib.error.HTTPError as e:
                code = e.code
                # 429 限流与 5xx 服务端错误可重试；其余 4xx 客户端错误不重试
                retryable = code == 429 or 500 <= code < 600
                if not retryable:
                    raise ProviderError(f"{self.name} HTTP {code}") from e
                if attempts > self.max_retries:
                    raise ProviderError(
                        f"{self.name} HTTP {code}：重试 {self.max_retries} 次后仍失败"
                        f"（共 {attempts} 次尝试）"
                    ) from e
                # 第 n 次重试（n == attempts）按指数退避等待
                self.sleep_func(self._backoff_for(attempts, e))


class OpenAIProvider(Provider):
    """OpenAI 兼容端点。system 进 messages，tools 用 function 包装。"""

    name = "openai"

    def _translate_request(self, req: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model(req),
            "messages": [
                {"role": m.role, "content": m.content} for m in req.messages
            ],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t,
                        "description": "",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                for t in req.tools
            ]
        return payload

    def _translate_response(self, data: dict[str, Any]) -> ChatResponse:
        choice = data["choices"][0]["message"]
        content = choice.get("content") or ""
        # 推理模型思维链（OpenAI 格式）
        reasoning = choice.get("reasoning_content") or ""
        raw_calls = choice.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=c.get("id", f"call_{i}"),
                name=c["function"]["name"],
                arguments=json.loads(c["function"].get("arguments") or "{}"),
            )
            for i, c in enumerate(raw_calls)
        ]
        usage = data.get("usage", {}) or {}
        return ChatResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self._model()),
            provider=self.name,
            raw=data,
        )

    def complete(self, req: ChatRequest) -> ChatResponse:
        url = f"{self.spec.base_url.rstrip('/')}/chat/completions"
        payload = self._translate_request(req)
        data = self._http_post(url, payload)
        return self._translate_response(data)


class AnthropicProvider(Provider):
    """Anthropic 端点。system 抽成顶层字段，tools 用 input_schema。"""

    name = "anthropic"

    def _translate_request(self, req: ChatRequest) -> dict[str, Any]:
        messages = [
            {"role": m.role, "content": m.content}
            for m in req.messages
            if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": self._model(req),
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        system = req.system_prompt
        if system:
            payload["system"] = system
        if req.tools:
            payload["tools"] = [
                {
                    "name": t,
                    "description": "",
                    "input_schema": {"type": "object", "properties": {}},
                }
                for t in req.tools
            ]
        return payload

    def _translate_response(self, data: dict[str, Any]) -> ChatResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                # 推理模型思维链（Anthropic 格式）
                reasoning_parts.append(block.get("thinking", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block["name"],
                        arguments=block.get("input", {}),
                    )
                )
        usage = data.get("usage", {}) or {}
        return ChatResponse(
            content="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=data.get("model", self._model()),
            provider=self.name,
            raw=data,
        )

    def _auth_headers(self) -> dict[str, str]:
        headers = super()._auth_headers()
        headers["anthropic-version"] = "2023-06-01"
        return headers

    def complete(self, req: ChatRequest) -> ChatResponse:
        url = f"{self.spec.base_url.rstrip('/')}/messages"
        payload = self._translate_request(req)
        data = self._http_post(url, payload)
        return self._translate_response(data)


class LongCatProvider(Provider):
    """LongCat 双端点网关。

    OpenAI 格式为主；若 ``spec.dialects["use_anthropic"]`` 为真，则走 Anthropic
    端点。**两个端点都用 Bearer 鉴权**（其 Anthropic 端点不接受 x-api-key）。
    """

    name = "longcat"

    def __init__(
        self,
        api_key: str | None = None,
        spec: ProviderSpec | None = None,
        timeout: int = 60,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        spec = spec or DEFAULT_SPECS["longcat"]
        super().__init__(api_key, spec, timeout, max_retries, backoff_base, sleep_func)
        self.use_anthropic = bool(self.spec.dialects.get("use_anthropic"))

    def _translate_request(self, req: ChatRequest) -> dict[str, Any]:
        if self.use_anthropic:
            return AnthropicProvider._translate_request(self, req)
        return OpenAIProvider._translate_request(self, req)

    def _translate_response(self, data: dict[str, Any]) -> ChatResponse:
        if self.use_anthropic:
            return AnthropicProvider._translate_response(self, data)
        return OpenAIProvider._translate_response(self, data)

    def _auth_headers(self) -> dict[str, str]:
        # LongCat 两个端点都用 Bearer；Anthropic 端点额外带 anthropic-version
        headers = super()._auth_headers()
        if self.use_anthropic:
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def complete(self, req: ChatRequest) -> ChatResponse:
        if self.use_anthropic:
            base = self.spec.base_url or LONGCAT_ANTHROPIC_BASE
            url = f"{base.rstrip('/')}/messages"
        else:
            base = self.spec.base_url or LONGCAT_OPENAI_BASE
            url = f"{base.rstrip('/')}/chat/completions"
        payload = self._translate_request(req)
        data = self._http_post(url, payload)
        return self._translate_response(data)


class MockProvider(Provider):
    """离线可测 provider。返回预设响应，记录调用历史（self.calls）。

    不校验 key、不发网络请求。``response`` 接受单个 ChatResponse 或字符串；
    ``responses`` 接受列表，按序弹出。
    """

    name = "mock"

    def __init__(
        self,
        response: ChatResponse | str | None = None,
        responses: list[ChatResponse | str] | None = None,
        api_key: str | None = None,
        spec: ProviderSpec | None = None,
        timeout: int = 60,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(api_key, spec, timeout, max_retries, backoff_base, sleep_func)
        self.calls: list[ChatRequest] = []
        queue: list[ChatResponse | str] = []
        if responses is not None:
            queue = list(responses)
        elif response is not None:
            queue = [response]
        self._queue: list[ChatResponse] = [self._wrap(r) for r in queue]

    @staticmethod
    def _wrap(r: ChatResponse | str) -> ChatResponse:
        if isinstance(r, ChatResponse):
            return r
        return ChatResponse(content=r, provider="mock")

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.calls.append(req)
        if self._queue:
            return self._queue.pop(0)
        return ChatResponse(content="", provider=self.name)

    def _translate_request(self, req: ChatRequest) -> dict[str, Any]:
        return {}

    def _translate_response(self, data: dict[str, Any]) -> ChatResponse:
        return ChatResponse(provider=self.name)


class Gateway:
    """provider 注册表 + 调用 + fallback。"""

    def __init__(self, providers: dict[str, Provider] | None = None) -> None:
        self._providers: dict[str, Provider] = dict(providers or {})

    def register(self, name: str, provider: Provider) -> None:
        self._providers[name] = provider

    def complete(self, provider_name: str, req: ChatRequest) -> ChatResponse:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderError(f"未注册的 provider：{provider_name}")
        return provider.complete(req)

    def complete_with_fallback(
        self, order: list[str], req: ChatRequest
    ) -> ChatResponse:
        last_err: ProviderError | None = None
        for name in order:
            provider = self._providers.get(name)
            if provider is None:
                last_err = ProviderError(f"未注册的 provider：{name}")
                continue
            try:
                return provider.complete(req)
            except ProviderError as e:
                last_err = e
        if last_err is not None:
            raise last_err
        raise ProviderError("complete_with_fallback 收到空顺序列表")

    def available(self) -> list[str]:
        """仅返回已配置 api_key 的 provider（按注册顺序）。"""
        return [n for n, p in self._providers.items() if p.api_key]
