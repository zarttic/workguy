"""整合测试：验证产品化三件套真的接进了执行循环。

这组测试存在的理由：内核六件与产品化三件套是分两批、由不同 subagent 写的，
单独测都绿，但**合起来没接线**也照样绿。这里逐条验证接线。

重点看两个安全回归：
- 纯推理代理即便在挂了 MCP 工具之后，依然拿不到任何工具
- 动态工具只对全能/通用代理开放
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from workguy.agents import AgentRegistry
from workguy.audit import AuditLog
from workguy.config import AGENTS, TOOLS
from workguy.kernel import DYNAMIC_TOOL_AGENTS, AgentKernel
from workguy.mcp import MCPRegistry
from workguy.providers import Gateway, MockProvider
from workguy.router import ModelRouter
from workguy.skills import SkillRegistry
from workguy.store import Store
from workguy.tools import ToolRegistry
from workguy.types import (
    ChatResponse,
    MCPServerConfig,
    ProviderError,
    ToolCall,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_SERVER = FIXTURES / "fake_mcp_server.py"


class FailingProvider(MockProvider):
    """总是失败，用于验证 fallback。"""

    name = "failing"

    def complete(self, req):  # noqa: ANN001
        raise ProviderError("模拟 provider 故障")


def build_tools() -> tuple[ToolRegistry, dict]:
    tools = ToolRegistry(TOOLS)
    spy = {"bash": 0, "read": 0}

    def bash(args, caller):
        spy["bash"] += 1
        return f"BASH({args.get('cmd', '')})"

    def read(args, caller):
        spy["read"] += 1
        return f"READ({args.get('path', '')})"

    tools.register("Bash", bash)
    tools.register("Read", read)
    return tools, spy


BODY_MARKER = "__SKILL_BODY_MARKER__"


def write_skill(
    root: Path, name: str, description: str, body: str = BODY_MARKER
) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. Gateway 接线
# ---------------------------------------------------------------------------


class TestGatewayIntegration(unittest.TestCase):
    def test_kernel_runs_through_gateway(self):
        p = MockProvider(response="来自网关", api_key="test")
        gw = Gateway({"mock": p})
        kernel = AgentKernel(gateway=gw, provider_chain=["mock"])
        r = kernel.run("cli", "打个招呼")
        self.assertEqual(r.content, "来自网关")
        self.assertEqual(r.provider, "mock")
        self.assertEqual(len(p.calls), 1, "Gateway 应被真正调用")

    def test_gateway_falls_back_to_second_provider(self):
        bad = FailingProvider(api_key="test")
        good = MockProvider(response="备用接上了", api_key="test")
        gw = Gateway({"failing": bad, "mock": good})
        kernel = AgentKernel(gateway=gw, provider_chain=["failing", "mock"])
        r = kernel.run("cli", "hi")
        self.assertEqual(r.content, "备用接上了")
        self.assertEqual(r.provider, "mock")

    def test_gateway_without_usable_provider_raises(self):
        gw = Gateway({"mock": MockProvider(response="x")})  # 无 api_key
        kernel = AgentKernel(gateway=gw, provider_chain=["mock"])
        with self.assertRaises(RuntimeError):
            kernel.run("cli", "hi")

    def test_kernel_requires_a_backend(self):
        with self.assertRaises(ValueError):
            AgentKernel()

    def test_gateway_receives_agent_tool_whitelist(self):
        """网关调用时要带上该代理的工具白名单——否则模型不知道能用什么。"""
        p = MockProvider(response="ok", api_key="test")
        gw = Gateway({"mock": p})
        kernel = AgentKernel(gateway=gw, provider_chain=["mock"], tools=build_tools()[0])
        kernel.run("cli", "hi")
        offered = p.calls[0].tools
        self.assertIn("Read", offered)
        self.assertNotIn("ImageGen", offered, "懒加载工具不应进默认上下文")


# ---------------------------------------------------------------------------
# 2. Store 接线
# ---------------------------------------------------------------------------


class TestStoreIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "wb.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_session_messages_audit_and_usage_all_persisted(self):
        store = Store(self.db)
        try:
            tools, _ = build_tools()
            p = MockProvider(
                responses=[
                    ChatResponse(
                        content="",
                        tool_calls=[ToolCall(id="c1", name="Read", arguments={"path": "a"})],
                        input_tokens=100,
                        output_tokens=20,
                    ),
                    ChatResponse(content="读完了", input_tokens=120, output_tokens=10),
                ],
                api_key="test",
            )
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}),
                provider_chain=["mock"],
                tools=tools,
                store=store,
                router=ModelRouter(),
            )
            r = kernel.run("cli", "读 a", session_id="sess-1")

            self.assertTrue(r.persisted, "应标记已持久化")
            self.assertEqual(r.session_id, "sess-1")
            self.assertEqual(r.store_errors, [], "不应有落盘错误")

            # 会话落盘
            sessions = [s for s in store.list_sessions()]
            self.assertTrue(any(s.get("session_id") == "sess-1" for s in sessions))

            # 消息落盘
            msgs = store.load_messages("sess-1")
            self.assertGreaterEqual(len(msgs), 3, "user + assistant + tool")

            # 审计链落盘且完好
            self.assertGreater(len(store.load_audit()), 0)
            self.assertTrue(store.verify_audit_chain())

            # 用量落盘
            report = store.usage_report()
            self.assertTrue(report, "应有用量记录")
        finally:
            store.close()

    def test_audit_chain_survives_roundtrip_with_anchor(self):
        store = Store(self.db)
        try:
            p = MockProvider(response="ok", api_key="test")
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}), provider_chain=["mock"], store=store
            )
            kernel.run("cli", "hi")
            anchor = store.anchor()
            self.assertGreater(anchor.length, 0)
            self.assertTrue(store.verify_audit_chain(anchor), "锚点应能校验通过")
        finally:
            store.close()

    def test_run_without_store_still_works(self):
        """不接 Store 时必须退化成纯内存模式，不能崩。"""
        p = MockProvider(response="内存模式", api_key="test")
        kernel = AgentKernel(gateway=Gateway({"mock": p}), provider_chain=["mock"])
        r = kernel.run("cli", "hi")
        self.assertEqual(r.content, "内存模式")
        self.assertFalse(r.persisted)


# ---------------------------------------------------------------------------
# 3. Skill 接线
# ---------------------------------------------------------------------------


class TestSkillIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_skill(self.root, "finance-helper", "金融分析 财报 估值 股票 基金")
        write_skill(self.root, "poster-maker", "海报 设计 视觉")
        self.registry = SkillRegistry()
        self.registry.scan(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_matched_skill_is_injected_as_l0_only(self):
        p = MockProvider(response="ok", api_key="test")
        kernel = AgentKernel(
            gateway=Gateway({"mock": p}),
            provider_chain=["mock"],
            skills=self.registry,
        )
        r = kernel.run("cli", "帮我做个金融分析")

        self.assertIn("finance-helper", r.matched_skills)
        injected = "\n".join(m.content for m in p.calls[0].messages)
        self.assertIn("finance-helper", injected, "技能名应注入上下文")
        self.assertNotIn(BODY_MARKER, injected, "L1 正文不应被注入（渐进加载）")

    def test_no_skill_match_leaves_context_clean(self):
        p = MockProvider(response="ok", api_key="test")
        kernel = AgentKernel(
            gateway=Gateway({"mock": p}),
            provider_chain=["mock"],
            skills=self.registry,
        )
        r = kernel.run("cli", "zzzz 无关查询 qqqq")
        self.assertEqual(r.matched_skills, [])

    def test_skill_match_is_audited(self):
        p = MockProvider(response="ok", api_key="test")
        kernel = AgentKernel(
            gateway=Gateway({"mock": p}),
            provider_chain=["mock"],
            skills=self.registry,
        )
        r = kernel.run("cli", "金融分析")
        events = [(rec.category, rec.event_type) for rec in r.audit.records()]
        self.assertIn(("skill", "match"), events)


# ---------------------------------------------------------------------------
# 4. MCP 接线 + 安全回归
# ---------------------------------------------------------------------------


class TestMCPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FAKE_SERVER.exists():
            raise unittest.SkipTest("缺少 fixtures/fake_mcp_server.py")

    def _registry(self) -> MCPRegistry:
        reg = MCPRegistry()
        reg.add(
            MCPServerConfig(
                name="fake",
                transport="stdio",
                command=sys.executable,
                args=(str(FAKE_SERVER),),
            )
        )
        reg.connect_all()
        return reg

    def test_mcp_tools_mounted_and_visible_to_cli(self):
        mcp = self._registry()
        try:
            tools, _ = build_tools()
            p = MockProvider(response="ok", api_key="test")
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}),
                provider_chain=["mock"],
                tools=tools,
                mcp=mcp,
            )
            self.assertTrue(kernel._dynamic_tools, "应记录到动态工具")
            offered = kernel._effective_tools(AGENTS["cli"])
            self.assertTrue(
                set(kernel._dynamic_tools) & set(offered),
                "cli 应能看到 MCP 工具",
            )
        finally:
            mcp.close_all()

    def test_pure_reasoner_never_gets_dynamic_tools(self):
        """安全回归：挂了 MCP 也不能让零工具代理获得能力。"""
        mcp = self._registry()
        try:
            tools, spy = build_tools()
            p = MockProvider(response="ok", api_key="test")
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}),
                provider_chain=["mock"],
                tools=tools,
                mcp=mcp,
            )
            for name in AgentRegistry(AGENTS).pure_reasoners():
                self.assertEqual(
                    kernel._effective_tools(AGENTS[name]), [],
                    f"{name} 是纯推理代理，不应有任何可用工具",
                )
            self.assertFalse(spy["bash"])
        finally:
            mcp.close_all()

    def test_dynamic_tools_are_restricted_to_authorized_agents(self):
        """动态工具只对 DYNAMIC_TOOL_AGENTS 开放，其余代理即便有白名单也不放行。"""
        mcp = self._registry()
        try:
            tools, _ = build_tools()
            p = MockProvider(response="ok", api_key="test")
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}),
                provider_chain=["mock"],
                tools=tools,
                mcp=mcp,
            )
            dynamic_name = sorted(kernel._dynamic_tools)[0]
            # pulse 有自己的白名单（WebSearch/WebFetch），但不该拿到 MCP 工具
            self.assertNotIn(dynamic_name, kernel._effective_tools(AGENTS["pulse"]))
            self.assertFalse(kernel._may_use(AGENTS["pulse"], dynamic_name))
            # 授权代理则可以
            for allowed in DYNAMIC_TOOL_AGENTS:
                self.assertTrue(kernel._may_use(AGENTS[allowed], dynamic_name))
        finally:
            mcp.close_all()

    def test_pure_reasoner_call_to_mcp_tool_is_denied(self):
        """纯推理代理请求 MCP 工具 → 被拒，且不触达工具。"""
        mcp = self._registry()
        try:
            targets = mcp.tool_names
            if not targets:
                self.skipTest("假 server 未暴露工具")
            target = targets[0]

            tools, _ = build_tools()
            p = MockProvider(
                responses=[
                    ChatResponse(
                        content="",
                        tool_calls=[ToolCall(id="c1", name=target, arguments={})],
                    ),
                    ChatResponse(content="被拒了"),
                ],
                api_key="test",
            )
            kernel = AgentKernel(
                gateway=Gateway({"mock": p}),
                provider_chain=["mock"],
                tools=tools,
                mcp=mcp,
            )
            r = kernel.run("compact", "试试看")
            self.assertEqual(r.denied_count, 1)
            self.assertEqual(r.executed, [])
        finally:
            mcp.close_all()


# ---------------------------------------------------------------------------
# 5. 抽象统一 + 权限绕过回归锁
# ---------------------------------------------------------------------------


class TestAbstractionUnification(unittest.TestCase):
    def test_provider_satisfies_llmclient_protocol(self):
        """Provider 加了 chat() 后应能直接当 llm 传入——两套抽象不再互不相通。"""
        p = MockProvider(response="经协议适配", api_key="test")
        kernel = AgentKernel(llm=p)  # 走 llm= 而非 gateway=
        r = kernel.run("cli", "hi")
        self.assertEqual(r.content, "经协议适配")

    def test_gateway_provider_and_direct_llm_are_interchangeable(self):
        """同一份响应，走 gateway 与走 llm 应得到一致结果。"""
        via_llm = MockProvider(response="X", api_key="test")
        via_gw = MockProvider(response="X", api_key="test")
        k1 = AgentKernel(llm=via_llm)
        k2 = AgentKernel(gateway=Gateway({"mock": via_gw}), provider_chain=["mock"])
        self.assertEqual(k1.run("cli", "hi").content, k2.run("cli", "hi").content)


class TestPermissionBypassRegression(unittest.TestCase):
    """锁定"权限只在 kernel 前置校验"这一设计。

    这组测试的价值不在于验证功能，而在于**防止将来有人以为
    `ToolRegistry.execute` 自带权限检查**，或重构时把校验挪走而无人察觉。
    """

    def test_tool_registry_execute_does_not_enforce_permission(self):
        """已知边界：execute 是执行器，不做权限判断。

        记录这个事实本身很重要——它是设计权衡（职责分离），不是漏洞，
        但必须有测试写明，否则容易被误认为安全边界。
        """
        tools, spy = build_tools()
        tools.execute(
            ToolCall(id="x", name="Bash", arguments={"cmd": "ls"}), caller="compact"
        )
        self.assertEqual(spy["bash"], 1, "execute 不做权限判断（已知边界，非漏洞）")

    def test_every_kernel_path_passes_permission_check(self):
        """经 kernel 的每一条工具调用都必须过权限校验。"""
        tools, spy = build_tools()
        p = MockProvider(
            responses=[
                ChatResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="c1", name="Bash", arguments={"cmd": "a"}),
                        ToolCall(id="c2", name="Read", arguments={"path": "b"}),
                    ],
                ),
                ChatResponse(content="done"),
            ],
            api_key="test",
        )
        kernel = AgentKernel(
            gateway=Gateway({"mock": p}), provider_chain=["mock"], tools=tools
        )
        r = kernel.run("compact", "试图批量调用工具")

        self.assertEqual(r.denied_count, 2, "两个调用都应被拒")
        self.assertEqual(r.executed, [])
        self.assertEqual(spy["bash"] + spy["read"], 0, "底层 handler 零触达")

    def test_kernel_denial_is_audited_not_silent(self):
        """被拒的调用必须留痕，不能静默。"""
        tools, _ = build_tools()
        p = MockProvider(
            responses=[
                ChatResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Bash", arguments={})],
                ),
                ChatResponse(content="done"),
            ],
            api_key="test",
        )
        kernel = AgentKernel(
            gateway=Gateway({"mock": p}), provider_chain=["mock"], tools=tools
        )
        r = kernel.run("compact", "x")
        denies = [rec for rec in r.audit.records() if rec.decision == "deny"]
        self.assertEqual(len(denies), 1, "拒绝必须写进审计链")


if __name__ == "__main__":
    unittest.main()
