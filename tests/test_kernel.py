"""执行循环测试。

最要紧的一条是 test_pure_reasoner_cannot_escape_sandbox：
即便 LLM 明确要求调用危险工具，零工具代理也必须拦下来，
且底层 handler **一次都不能被触达**。
"""

from __future__ import annotations

import unittest

from workguy.agents import AgentRegistry
from workguy.audit import AuditLog
from workguy.config import AGENTS, TOOLS
from workguy.kernel import AgentKernel
from workguy.llm import LLMResponse, MockLLM
from workguy.router import ModelRouter
from workguy.tools import ToolRegistry
from workguy.types import Message, ToolCall


def make_kernel(responses: list[LLMResponse], max_tokens: int = 8_000):
    """构造一个可观测的 kernel：Bash 的 handler 会记录自己是否被调用过。"""
    llm = MockLLM(responses)
    tools = ToolRegistry(TOOLS)
    spy = {"bash_called": 0, "read_called": 0}

    def bash_handler(args: dict, caller: str) -> str:
        spy["bash_called"] += 1
        return f"BASH({args.get('cmd', '')})"

    def read_handler(args: dict, caller: str) -> str:
        spy["read_called"] += 1
        return f"READ({args.get('path', '')})"

    tools.register("Bash", bash_handler)
    tools.register("Read", read_handler)

    kernel = AgentKernel(
        llm=llm,
        agents=AgentRegistry(AGENTS),
        tools=tools,
        router=ModelRouter(),
        audit=AuditLog(),
        max_tokens=max_tokens,
    )
    return kernel, spy, llm


class TestKernelBasic(unittest.TestCase):
    def test_single_turn_no_tools(self):
        kernel, _, _ = make_kernel([LLMResponse(content="你好", input_tokens=10, output_tokens=5)])
        r = kernel.run("cli", "打个招呼")
        self.assertEqual(r.content, "你好")
        self.assertEqual(r.iterations, 1)
        self.assertEqual(r.executed, [])
        self.assertEqual(r.denied, [])

    def test_tool_loop_then_answer(self):
        kernel, spy, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Read", arguments={"path": "a.txt"})],
                    input_tokens=100,
                    output_tokens=20,
                ),
                LLMResponse(content="读完了", input_tokens=120, output_tokens=10),
            ]
        )
        r = kernel.run("cli", "读 a.txt")
        self.assertEqual(r.content, "读完了")
        self.assertEqual(r.iterations, 2)
        self.assertEqual(spy["read_called"], 1)
        self.assertEqual(r.executed[0]["tool"], "Read")
        self.assertEqual(r.denied, [])

    def test_max_iterations_guard(self):
        # 永远要调工具，验证不会死循环
        endless = [
            LLMResponse(content="", tool_calls=[ToolCall(id=f"c{i}", name="Read", arguments={})])
            for i in range(20)
        ]
        kernel, _, _ = make_kernel(endless)
        r = kernel.run("cli", "一直读")
        self.assertEqual(r.iterations, 8)


class TestPermissionBoundary(unittest.TestCase):
    def test_pure_reasoner_cannot_escape_sandbox(self):
        """核心安全属性：零工具代理即便被 LLM 要求调危险命令，也必须被拦下。"""
        kernel, spy, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="c1", name="Bash", arguments={"cmd": "rm -rf /"})
                    ],
                    input_tokens=50,
                    output_tokens=10,
                ),
                LLMResponse(content="我无法执行工具", input_tokens=60, output_tokens=8),
            ]
        )
        r = kernel.run("compact", "压缩这段对话")

        self.assertEqual(r.denied_count, 1, "应记录一次权限拒绝")
        self.assertEqual(r.executed, [], "不得有任何工具被执行")
        self.assertEqual(spy["bash_called"], 0, "底层 Bash handler 一次都不能被触达")
        self.assertEqual(r.content, "我无法执行工具")

    def test_all_pure_reasoners_are_sealed(self):
        """遍历全部零工具代理，验证无一漏网。"""
        agents = AgentRegistry(AGENTS)
        for name in agents.pure_reasoners():
            kernel, spy, _ = make_kernel(
                [
                    LLMResponse(
                        content="",
                        tool_calls=[ToolCall(id="c1", name="Bash", arguments={"cmd": "x"})],
                    ),
                    LLMResponse(content="done"),
                ]
            )
            r = kernel.run(name, "test")
            self.assertEqual(r.denied_count, 1, f"{name} 应拒绝工具调用")
            self.assertEqual(spy["bash_called"], 0, f"{name} 不应触达 Bash")

    def test_agent_cannot_exceed_whitelist(self):
        """pulse 只有 WebSearch/WebFetch，调 Bash 必须被拒。"""
        kernel, spy, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Bash", arguments={"cmd": "ls"})],
                ),
                LLMResponse(content="只能用搜索"),
            ]
        )
        r = kernel.run("pulse", "查点东西")
        self.assertEqual(r.denied_count, 1)
        self.assertEqual(spy["bash_called"], 0)


class TestAuditAndBilling(unittest.TestCase):
    def test_audit_chain_intact_after_run(self):
        kernel, _, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Read", arguments={})],
                ),
                LLMResponse(content="ok"),
            ]
        )
        r = kernel.run("cli", "读")
        self.assertTrue(r.audit.verify_chain(), "运行结束后审计链必须完好")
        decisions = [rec.decision for rec in r.audit.records()]
        self.assertIn("executed", decisions)
        # run.start + tool_call.execution + run.end
        self.assertEqual(decisions.count("executed"), 3)

    def test_denial_is_audited(self):
        kernel, _, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Bash", arguments={})],
                ),
                LLMResponse(content="done"),
            ]
        )
        r = kernel.run("compact", "压缩")
        self.assertIn("deny", [rec.decision for rec in r.audit.records()])

    def test_credits_accumulate(self):
        # 首轮必须带 tool_call，否则循环第一轮就直接 break，测不到累加
        kernel, _, _ = make_kernel(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c1", name="Read", arguments={})],
                    input_tokens=500,
                    output_tokens=500,
                ),
                LLMResponse(content="done", input_tokens=500, output_tokens=500),
            ]
        )
        r = kernel.run("cli", "hi")
        self.assertEqual(r.iterations, 2)
        # default 倍率 2.00，两轮各 1000 tokens → 各 2.0，合计 4.0
        self.assertAlmostEqual(r.credits, 4.0, places=5)

    def test_lite_agent_costs_less(self):
        """同类任务下，lite 路由应显著便宜。"""
        def run_for(agent_name: str) -> float:
            kernel, _, _ = make_kernel(
                [LLMResponse(content="x", input_tokens=1000, output_tokens=1000)]
            )
            return kernel.run(agent_name, "hi").credits

        cheap = run_for("memorySelector")  # LITE
        pricey = run_for("cli")  # POWERFUL → default 2.00
        self.assertLess(cheap, pricey * 0.1)


class TestContextCompression(unittest.TestCase):
    def test_compression_triggers_on_long_context(self):
        # max_tokens 调小，并把 token 计数直接拉满
        kernel, _, _ = make_kernel(
            [LLMResponse(content="done", input_tokens=10, output_tokens=10)],
            max_tokens=100,
        )
        kernel.monitor.token_counter = lambda msgs: 95  # ratio 0.95
        r = kernel.run("cli", "长对话")
        self.assertTrue(r.context_actions, "应触发上下文动作")
        self.assertEqual(r.context_actions[0], "emergency")

    def test_no_compression_when_short(self):
        kernel, _, _ = make_kernel(
            [LLMResponse(content="done")], max_tokens=10_000
        )
        kernel.monitor.token_counter = lambda msgs: 10
        r = kernel.run("cli", "短对话")
        self.assertEqual(r.context_actions, [])


class TestLazyToolLoading(unittest.TestCase):
    def test_deferred_tool_not_in_default_context(self):
        kernel, _, llm = make_kernel([LLMResponse(content="done")])
        kernel.run("cli", "hi")
        offered = llm.calls[0]["tools"]
        self.assertNotIn("ImageGen", offered, "懒加载工具不应进默认上下文")
        self.assertIn("Read", offered)


if __name__ == "__main__":
    unittest.main()
