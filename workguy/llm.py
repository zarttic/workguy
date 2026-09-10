"""LLM 客户端抽象。

两个实现：
- ``MockLLM``    —— 脚本化的假客户端，离线可测，TDD 用
- ``LongCatLLM`` —— 接真实 LongCat API（OpenAI 兼容格式）

LongCat 实测要点（2026-09-10 探测）：
- OpenAI 兼容端点：https://api.longcat.chat/openai/v1
- Anthropic 兼容端点：https://api.longcat.chat/anthropic/v1/messages
- **两种端点都必须用 ``Authorization: Bearer <key>``**
  —— Anthropic 端不接受标准的 ``x-api-key``，这是它偏离 Anthropic 规范的地方
- /v1/models 无需有效鉴权即可返回，可用作连通性探针
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import Message, ToolCall

LONGCAT_OPENAI_BASE = "https://api.longcat.chat/openai/v1"
LONGCAT_ANTHROPIC_BASE = "https://api.longcat.chat/anthropic/v1"
LONGCAT_DEFAULT_MODEL = "LongCat-2.0"


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[Message],
        model: str,
        tools: list[str] | None = None,
    ) -> LLMResponse: ...


class MockLLM:
    """脚本化假客户端。按预设顺序返回响应，并记录每次调用以供断言。

    用途：让执行循环的全部逻辑（含安全边界）在零网络依赖下可测。
    """

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._queue: list[LLMResponse] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def push(self, response: LLMResponse) -> None:
        self._queue.append(response)

    def chat(
        self,
        messages: list[Message],
        model: str,
        tools: list[str] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": list(tools or []),
            }
        )
        if not self._queue:
            return LLMResponse(content="", tool_calls=[], model=model)
        return self._queue.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class LongCatLLM:
    """LongCat OpenAI 兼容端点客户端。零第三方依赖，用标准库 urllib。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = LONGCAT_OPENAI_BASE,
        model: str = LONGCAT_DEFAULT_MODEL,
        timeout: int = 60,
        verify_ssl: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LongCat API HTTP {e.code}: {body}") from e

    def list_models(self) -> list[str]:
        """连通性探针。实测该端点不校验 key，可用来判断网络是否可达。"""
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("id") for m in data.get("data", [])]

    def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[str] | None = None,
    ) -> LLMResponse:
        model_id = model or self.model
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
        }
        data = self._post("/chat/completions", payload)
        choice = data["choices"][0]["message"]
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
        return LLMResponse(
            content=choice.get("content") or "",
            tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", model_id),
        )
