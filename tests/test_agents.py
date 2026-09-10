"""代理权限金字塔 + 工具注册表的测试。

TDD 流程：先写测试（全红）→ 实现 → 全绿。

重点证明「最小权限边界」真的生效：
- 13 个纯推理代理即便被提示词注入、LLM 强行返回 tool_call，也必须被运行时拦截。
- 懒加载工具未 load() 之前不可执行，避免 schema 吃光默认上下文。
"""

from __future__ import annotations

import unittest

from workguy.agents import PURE_REASONER_COUNT, AgentRegistry
from workguy.config import AGENTS, TOOLS
from workguy.tools import ToolRegistry
from workguy.types import (
    PermissionDeniedError,
    ToolCall,
    ToolExecutionError,
    ToolSpec,
)


def fresh_registry() -> ToolRegistry:
    """构造一个带状态化 handler 的测试注册表，便于验证 handler 是否真被执行。"""
    reg = ToolRegistry()
    reg._state = {"called": False}

    def handler(arguments: dict, caller: str) -> str:
        reg._state["called"] = True
        reg._state["last_caller"] = caller
        return "ok"

    reg._handler = handler
    return reg


def register_tracked(reg: ToolRegistry, name: str, defer_loading: bool = False) -> None:
    reg.register(name, reg._handler, defer_loading=defer_loading)


class TestAgentPermissions(unittest.TestCase):
    # 1. 权限金字塔规模：13 纯推理 + 6 特权
    def test_reasoner_counts(self):
        reg = AgentRegistry()
        self.assertEqual(len(reg.pure_reasoners()), 13)
        self.assertEqual(len(reg.privileged()), 6)
        # 模块级常量暴露的事实断言
        self.assertEqual(PURE_REASONER_COUNT, 13)
        self.assertEqual(PURE_REASONER_COUNT, len(reg.pure_reasoners()))

    # 2. 纯推理代理调用任何工具都必须抛 PermissionDeniedError（无死角）
    def test_pure_reasoners_deny_everything(self):
        reg = AgentRegistry()
        probes = ["Bash", "Read", "WebSearch", "WebFetch", "Agent",
                  "ToolSearch", "Write", "Edit", "ImageGen", "TaskStop"]
        for agent_name in reg.pure_reasoners():
            spec = reg.get(agent_name)
            self.assertTrue(spec.is_pure_reasoner)
            for tool_name in probes:
                call = ToolCall(id="c", name=tool_name, arguments={})
                with self.subTest(agent=agent_name, tool=tool_name):
                    with self.assertRaises(PermissionDeniedError):
                        reg.assert_may_call(agent_name, call)

    # 3. 带工具的代理：白名单内通过，白名单外被拒
    def test_privileged_allowlist_enforced(self):
        reg = AgentRegistry()
        # pulse 只有 WebSearch / WebFetch
        allow = ToolCall(id="1", name="WebSearch", arguments={})
        reg.assert_may_call("pulse", allow)  # 不抛 = 通过
        deny = ToolCall(id="2", name="Bash", arguments={})
        with self.assertRaises(PermissionDeniedError):
            reg.assert_may_call("pulse", deny)
        # 另一个特权代理的白名单外工具同样被拒
        with self.assertRaises(PermissionDeniedError):
            reg.assert_may_call("statusline-setup", ToolCall(id="3", name="Bash"))

    # 4. cli 持有最多工具（金字塔顶端）
    def test_cli_is_apex(self):
        reg = AgentRegistry()
        cli_count = len(reg.get("cli").tools)
        self.assertEqual(cli_count, max(len(reg.get(a).tools) for a in reg.privileged()))
        # 比其它特权代理都多
        for other in reg.privileged():
            if other == "cli":
                continue
            self.assertGreater(cli_count, len(reg.get(other).tools))

    # 5. 懒加载工具：未 load 直接 execute 抛错；load 后可执行
    def test_lazy_tool_requires_load(self):
        reg = fresh_registry()
        register_tracked(reg, "LazyTool", defer_loading=True)
        call = ToolCall(id="1", name="LazyTool", arguments={})
        self.assertFalse(reg.is_loaded("LazyTool"))
        with self.assertRaises(ToolExecutionError):
            reg.execute(call, "cli")
        reg.load("LazyTool")
        self.assertTrue(reg.is_loaded("LazyTool"))
        self.assertEqual(reg.execute(call, "cli"), "ok")

    # 6. default_context_tools 不含任何 defer_loading 工具
    def test_default_context_excludes_deferred(self):
        reg = ToolRegistry()
        # 用真实 config 注入：默认上下文应是所有非 defer 工具
        for name, spec in TOOLS.items():
            reg.register(name, (lambda a, c: "x"), defer_loading=spec.defer_loading)
        defaults = reg.default_context_tools()
        for name in defaults:
            self.assertFalse(TOOLS[name].defer_loading,
                             f"{name} 是 defer 工具，不应进默认上下文")
        # 所有 defer 工具都不在默认上下文
        deferred = {n for n, s in TOOLS.items() if s.defer_loading}
        self.assertEqual(deferred & set(defaults), set())

    # 7. search 能找到懒加载工具（搜索不要求已装载）
    def test_search_finds_deferred(self):
        reg = ToolRegistry()
        for name, spec in TOOLS.items():
            reg.register(name, (lambda a, c: "x"), defer_loading=spec.defer_loading)
        hits = reg.search("ImageGen")
        self.assertTrue(any(s.name == "ImageGen" for s in hits))
        self.assertTrue(hits[0].defer_loading)
        # 未装载也能搜到
        self.assertFalse(reg.is_loaded("ImageGen"))
        # 模糊匹配：描述/名字
        self.assertTrue(any(s.name == "WebSearch" for s in reg.search("web")))

    # 8. guard_call 串联：权限拒绝时不会执行到底层 handler
    def test_guard_call_blocks_before_handler(self):
        reg = fresh_registry()
        register_tracked(reg, "Bash", defer_loading=True)
        agents = AgentRegistry()
        call = ToolCall(id="x", name="Bash", arguments={})
        # compact 是纯推理代理，必被拒，且 handler 绝不能执行
        with self.assertRaises(PermissionDeniedError):
            agents.guard_call("compact", call, reg)
        self.assertFalse(reg._state["called"])
        # 正向路径：cli 白名单含 Bash，handler 应被执行
        reg2 = fresh_registry()
        register_tracked(reg2, "Bash")
        self.assertEqual(agents.guard_call("cli", call, reg2), "ok")
        self.assertTrue(reg2._state["called"])
        self.assertEqual(reg2._state["last_caller"], "cli")

    # 附加：未知代理 / 未知工具抛对应错误
    def test_unknown_agent_and_tool(self):
        reg = AgentRegistry()
        with self.assertRaises(KeyError):
            reg.get("does-not-exist")
        tr = ToolRegistry()
        with self.assertRaises(ToolExecutionError):
            tr.execute(ToolCall(id="1", name="NoSuchTool"), "cli")


if __name__ == "__main__":
    unittest.main()
