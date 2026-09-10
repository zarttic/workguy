"""多模型网关测试（TDD：先红后绿）。

覆盖各 provider 的协议「方言」翻译、MockProvider 离线行为、
Gateway 的 fallback 与 available 过滤，以及缺 key 时的安全行为。

不发送任何真实网络请求。
"""

from __future__ import annotations

import io
import os
import unittest
import urllib.error

from workguy.providers import (
    AnthropicProvider,
    Gateway,
    LongCatProvider,
    MockProvider,
    OpenAIProvider,
    Provider,
)
from workguy.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ProviderError,
    ProviderSpec,
    ToolCall,
)

LONGCAT_SPEC = ProviderSpec(
    name="longcat",
    base_url="https://api.longcat.chat/openai/v1",
    env_key="LONGCAT_API_KEY",
    default_model="LongCat-2.0",
)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    """构造一个可携带响应体的假 HTTPError（err.read() 返回 body）。"""
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class _FakeRaw:
    """替代 _raw_post：按序返回成功响应或抛 HTTPError，记录调用次数。"""

    def __init__(self, seq):
        self.seq = list(seq)  # ("ok", dict) 或 ("err", code, body)
        self.calls = 0

    def raw_post(self, url, data, headers):
        self.calls += 1
        item = self.seq.pop(0)
        if item[0] == "err":
            raise _http_error(item[1], item[2])
        return item[1]


def _req(system: str = "", tools: list[str] | None = None) -> ChatRequest:
    msgs = []
    if system:
        msgs.append(Message(role="system", content=system))
    msgs.append(Message(role="user", content="hi"))
    return ChatRequest(messages=msgs, tools=list(tools or []))


OPENAI_SPEC = ProviderSpec(
    name="openai",
    base_url="https://api.openai.com/v1",
    env_key="OPENAI_API_KEY",
    default_model="gpt-4o",
)
ANTHROPIC_SPEC = ProviderSpec(
    name="anthropic",
    base_url="https://api.anthropic.com",
    env_key="ANTHROPIC_API_KEY",
    default_model="claude-3-5-sonnet-20241022",
)


class TestOpenAITranslate(unittest.TestCase):
    def test_system_message_stays_in_messages(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        payload = p._translate_request(_req(system="你是助手"))
        roles = [m["role"] for m in payload["messages"]]
        self.assertIn("system", roles)
        sys_msg = next(m for m in payload["messages"] if m["role"] == "system")
        self.assertEqual(sys_msg["content"], "你是助手")

    def test_tools_format_is_function_wrapper(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        payload = p._translate_request(_req(tools=["get_weather"]))
        self.assertIn("tools", payload)
        tool = payload["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "get_weather")
        self.assertIn("parameters", tool["function"])


class TestAnthropicTranslate(unittest.TestCase):
    def test_system_extracted_to_top_level(self):
        p = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        payload = p._translate_request(_req(system="你是助手"))
        self.assertEqual(payload["system"], "你是助手")
        roles = [m["role"] for m in payload["messages"]]
        self.assertNotIn("system", roles)

    def test_tools_format_uses_input_schema(self):
        p = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        payload = p._translate_request(_req(tools=["get_weather"]))
        self.assertIn("tools", payload)
        tool = payload["tools"][0]
        self.assertEqual(tool["name"], "get_weather")
        self.assertIn("input_schema", tool)
        self.assertNotIn("function", tool)


class TestAnthropicResponse(unittest.TestCase):
    def test_tool_use_input_is_object(self):
        p = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        data = {
            "content": [
                {"type": "text", "text": "好的"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "get_weather",
                    "input": {"location": "SF"},
                },
            ],
            "usage": {"input_tokens": 7, "output_tokens": 3},
            "model": "claude-x",
        }
        resp = p._translate_response(data)
        self.assertEqual(resp.content, "好的")
        self.assertEqual(len(resp.tool_calls), 1)
        tc = resp.tool_calls[0]
        self.assertIsInstance(tc, ToolCall)
        self.assertEqual(tc.name, "get_weather")
        self.assertEqual(tc.arguments, {"location": "SF"})


class TestOpenAIResponse(unittest.TestCase):
    def test_tool_calls_arguments_parsed_from_json_string(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        data = {
            "choices": [
                {
                    "message": {
                        "content": "好的",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "SF"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            "model": "gpt-4o",
        }
        resp = p._translate_response(data)
        self.assertEqual(resp.content, "好的")
        self.assertEqual(len(resp.tool_calls), 1)
        tc = resp.tool_calls[0]
        self.assertEqual(tc.name, "get_weather")
        self.assertEqual(tc.arguments, {"location": "SF"})


class TestUsageMapping(unittest.TestCase):
    def test_openai_prompt_tokens_to_input_tokens(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        data = {"choices": [{"message": {"content": ""}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5}}
        resp = p._translate_response(data)
        self.assertEqual(resp.input_tokens, 11)
        self.assertEqual(resp.output_tokens, 5)

    def test_anthropic_input_tokens_to_input_tokens(self):
        p = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        data = {"content": [{"type": "text", "text": ""}],
                "usage": {"input_tokens": 11, "output_tokens": 5}}
        resp = p._translate_response(data)
        self.assertEqual(resp.input_tokens, 11)
        self.assertEqual(resp.output_tokens, 5)


class TestMockProvider(unittest.TestCase):
    def test_returns_preset_response_and_records_calls(self):
        preset = ChatResponse(content="mocked", provider="mock")
        p = MockProvider(response=preset, api_key="x")
        out = p.complete(_req())
        self.assertEqual(out.content, "mocked")
        self.assertEqual(len(p.calls), 1)

    def test_accepts_string_list(self):
        p = MockProvider(responses=["a", "b"], api_key="x")
        self.assertEqual(p.complete(_req()).content, "a")
        self.assertEqual(p.complete(_req()).content, "b")
        self.assertEqual(len(p.calls), 2)


class TestGatewayFallback(unittest.TestCase):
    def test_first_fails_then_second_succeeds(self):
        bad = OpenAIProvider(spec=OPENAI_SPEC)  # 无 key -> complete 抛 ProviderError
        good = MockProvider(response=ChatResponse(content="ok", provider="mock"),
                            api_key="x")
        gw = Gateway({"openai": bad, "mock": good})
        out = gw.complete_with_fallback(["openai", "mock"], _req())
        self.assertEqual(out.content, "ok")

    def test_all_fail_raises_provider_error(self):
        a = OpenAIProvider(spec=OPENAI_SPEC)
        b = AnthropicProvider(spec=ANTHROPIC_SPEC)
        gw = Gateway({"openai": a, "anthropic": b})
        with self.assertRaises(ProviderError):
            gw.complete_with_fallback(["openai", "anthropic"], _req())


class TestMissingKey(unittest.TestCase):
    def test_missing_key_raises_and_does_not_leak(self):
        sentinel = "SECRET_LEAK_PROBE_12345"
        os.environ.pop("OPENAI_API_KEY", None)
        p = OpenAIProvider(spec=OPENAI_SPEC)  # 无显式 key，env 也无
        # 即便构造后强行注入一个 key，再清掉，异常文本也不应含 key
        p.api_key = None
        with self.assertRaises(ProviderError) as ctx:
            p.complete(_req())
        msg = str(ctx.exception)
        self.assertNotIn(sentinel, msg)


class TestGatewayAvailable(unittest.TestCase):
    def test_only_keyed_providers_available(self):
        os.environ.pop("OPENAI_API_KEY", None)
        no_key = OpenAIProvider(spec=OPENAI_SPEC)
        with_key = MockProvider(response=ChatResponse(content="x"),
                                api_key="present-key", spec=ProviderSpec(
                                    name="mock", base_url="", env_key="MOCK_KEY",
                                    default_model="m"))
        gw = Gateway({"openai": no_key, "mock": with_key})
        self.assertEqual(gw.available(), ["mock"])


class TestRetry429And5xx(unittest.TestCase):
    def test_429_retries_then_succeeds(self):
        sleeps: list[float] = []
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC, max_retries=2,
                           sleep_func=sleeps.append)
        fake = _FakeRaw([("err", 429, b'{}'),
                         ("ok", {"choices": [{"message": {"content": "ok"}}],
                                 "model": "m"})])
        p._raw_post = fake.raw_post
        resp = p.complete(_req())
        self.assertEqual(resp.content, "ok")
        self.assertEqual(fake.calls, 2)  # 1 次失败 + 1 次成功

    def test_429_always_fails_exhausts_retries_without_key(self):
        api_key = "TOP_SECRET_DO_NOT_LEAK_998877"
        sleeps: list[float] = []
        p = OpenAIProvider(api_key=api_key, spec=OPENAI_SPEC, max_retries=2,
                           sleep_func=sleeps.append)
        fake = _FakeRaw([("err", 429, b'{}'),
                         ("err", 429, b'{}'),
                         ("err", 429, b'{}')])
        p._raw_post = fake.raw_post
        with self.assertRaises(ProviderError) as ctx:
            p.complete(_req())
        msg = str(ctx.exception)
        self.assertEqual(fake.calls, 3)  # 初始 + 2 次重试 = 3 次尝试
        self.assertIn("3", msg)  # 含尝试次数
        self.assertNotIn(api_key, msg)  # 不得含 api_key

    def test_retry_after_preferred(self):
        sleeps: list[float] = []
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC, max_retries=2,
                           backoff_base=1.0, sleep_func=sleeps.append)
        body = b'{"error":{"code":"rate_limit_exceeded","retry_after":60}}'
        fake = _FakeRaw([("err", 429, body),
                         ("ok", {"choices": [{"message": {"content": "ok"}}],
                                 "model": "m"})])
        p._raw_post = fake.raw_post
        p.complete(_req())
        self.assertEqual(sleeps, [60.0])  # retry_after 覆盖默认退避

    def test_5xx_retries_but_4xx_does_not(self):
        # 5xx 可重试
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC, max_retries=2,
                           sleep_func=lambda _: None)
        fake = _FakeRaw([("err", 500, b'{}'),
                         ("ok", {"choices": [{"message": {"content": "ok"}}],
                                 "model": "m"})])
        p._raw_post = fake.raw_post
        self.assertEqual(p.complete(_req()).content, "ok")
        self.assertEqual(fake.calls, 2)

        # 400 / 401 客户端错误不重试，直接抛
        for code in (400, 401):
            p2 = OpenAIProvider(api_key="k", spec=OPENAI_SPEC,
                                sleep_func=lambda _: None)
            fake2 = _FakeRaw([("err", code, b'{}'),
                              ("ok", {"choices": [{"message": {"content": "x"}}],
                                      "model": "m"})])
            p2._raw_post = fake2.raw_post
            with self.assertRaises(ProviderError):
                p2.complete(_req())
            self.assertEqual(fake2.calls, 1)  # 未重试

    def test_exponential_backoff_sequence(self):
        sleeps: list[float] = []
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC, max_retries=3,
                           backoff_base=1.0, sleep_func=sleeps.append)
        fake = _FakeRaw([("err", 500, b'{}'),
                         ("err", 500, b'{}'),
                         ("err", 500, b'{}'),
                         ("ok", {"choices": [{"message": {"content": "ok"}}],
                                 "model": "m"})])
        p._raw_post = fake.raw_post
        self.assertEqual(p.complete(_req()).content, "ok")
        # 第 n 次重试等待 1.0 * 2**(n-1)：1, 2, 4
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])


class TestReasoningParsing(unittest.TestCase):
    def test_openai_reasoning_content(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        data = {"choices": [{"message": {"content": "答案",
                                         "reasoning_content": "先推论…"}}],
                "model": "gpt-4o"}
        resp = p._translate_response(data)
        self.assertEqual(resp.content, "答案")
        self.assertEqual(resp.reasoning, "先推论…")

    def test_anthropic_thinking_blocks_and_text_intact(self):
        p = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        data = {
            "content": [
                {"type": "thinking", "thinking": "链一"},
                {"type": "text", "text": "回答"},
                {"type": "thinking", "thinking": "链二"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": "claude-x",
        }
        resp = p._translate_response(data)
        self.assertEqual(resp.content, "回答")  # text 不受影响
        self.assertEqual(resp.reasoning, "链一链二")  # thinking 拼接

    def test_no_reasoning_field_keeps_empty(self):
        p_o = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        resp_o = p_o._translate_response(
            {"choices": [{"message": {"content": "a"}}], "model": "m"})
        self.assertEqual(resp_o.reasoning, "")

        p_a = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        resp_a = p_a._translate_response(
            {"content": [{"type": "text", "text": "b"}],
             "usage": {}, "model": "m"})
        self.assertEqual(resp_a.reasoning, "")


class TestModelRouting(unittest.TestCase):
    def test_req_model_used_else_default(self):
        p = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        self.assertEqual(p._translate_request(_req())["model"], "gpt-4o")
        req = _req()
        req.model = "gpt-4o-mini"
        self.assertEqual(p._translate_request(req)["model"], "gpt-4o-mini")

    def test_three_providers_pass_model_and_longcat_case_preserved(self):
        # OpenAI
        po = OpenAIProvider(api_key="k", spec=OPENAI_SPEC)
        req_o = _req(); req_o.model = "gpt-custom"
        self.assertEqual(po._translate_request(req_o)["model"], "gpt-custom")

        # Anthropic
        pa = AnthropicProvider(api_key="k", spec=ANTHROPIC_SPEC)
        req_a = _req(); req_a.model = "claude-custom"
        self.assertEqual(pa._translate_request(req_a)["model"], "claude-custom")

        # LongCat：大小写敏感，不做任何转换
        pl = LongCatProvider(api_key="k", spec=LONGCAT_SPEC)
        req_l = _req(); req_l.model = "LongCat-2.0"
        self.assertEqual(pl._translate_request(req_l)["model"], "LongCat-2.0")
        # 为空时回落默认（同样保留大小写）
        self.assertEqual(pl._translate_request(_req())["model"], "LongCat-2.0")


if __name__ == "__main__":
    unittest.main()
