"""WorkGuy 五大机制演示。离线运行，不需要网络和 API key。

    python demo.py
"""

from __future__ import annotations

from workguy.agents import AgentRegistry
from workguy.audit import AuditLog
from workguy.config import AGENTS, MODELS, TOOLS
from workguy.kernel import AgentKernel
from workguy.llm import LLMResponse, MockLLM
from workguy.router import ModelRouter
from workguy.tools import ToolRegistry
from workguy.types import ToolCall

BAR = "─" * 66


def build(responses: list[LLMResponse], max_tokens: int = 8_000):
    llm = MockLLM(responses)
    tools = ToolRegistry(TOOLS)
    spy = {"bash": 0}

    def bash_handler(args: dict, caller: str) -> str:
        spy["bash"] += 1
        return f"执行了: {args.get('cmd', '')}"

    tools.register("Bash", bash_handler)
    tools.register("Read", lambda a, c: f"读到 {a.get('path', '')}")

    kernel = AgentKernel(
        llm=llm,
        agents=AgentRegistry(AGENTS),
        tools=tools,
        router=ModelRouter(),
        audit=AuditLog(),
        max_tokens=max_tokens,
    )
    return kernel, spy


def section(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def demo_permission_boundary() -> None:
    section("1. 最小权限边界 —— 零工具代理能否被诱导调用危险工具？")

    kernel, spy = build(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="Bash", arguments={"cmd": "rm -rf /"})],
            ),
            LLMResponse(content="我是纯推理代理，无法执行工具。"),
        ]
    )
    r = kernel.run("compact", "请压缩这段对话")

    print(f"  LLM 请求   : Bash(rm -rf /)")
    print(f"  代理       : compact（纯推理，白名单为空）")
    print(f"  拒绝次数   : {r.denied_count}")
    print(f"  实际执行   : {r.executed if r.executed else '无'}")
    print(f"  Bash 被调用: {spy['bash']} 次   ← 底层 handler 一次都没触达")
    print(f"  最终回答   : {r.content}")


def demo_pyramid() -> None:
    section("2. 代理权限金字塔")
    agents = AgentRegistry(AGENTS)
    rows = sorted(
        ((len(s.tools), n) for n, s in AGENTS.items()), reverse=True
    )
    for count, name in rows:
        bar = "█" * max(1, count // 2)
        tag = "  ← 纯推理" if count == 0 else ""
        print(f"  {name:24s} {count:3d} 个工具 {bar}{tag}")
    print(f"\n  零工具代理 {len(agents.pure_reasoners())} 个 / 带工具 {len(agents.privileged())} 个")


def demo_context() -> None:
    section("3. 上下文阈值压缩")
    print("  摘要(0.15) 比压缩(0.40) 更早介入——因为摘要更便宜\n")
    for ratio in (0.10, 0.20, 0.45, 0.65, 0.95):
        kernel, _ = build([LLMResponse(content="ok")], max_tokens=1000)
        kernel.monitor.token_counter = lambda msgs, _r=ratio: int(_r * 1000)
        action = kernel.monitor.actions_for_ratio(ratio)
        print(f"  ratio {ratio:.2f} → {action.value}")
    print("\n  deepseek 系列压缩阈值放宽到 0.50（实测值）")


def demo_audit() -> None:
    section("4. 哈希链审计 —— 篡改能否被检出？")
    log = AuditLog()
    for i in range(3):
        log.append(
            agent="cli",
            category="tool_call",
            event_type="execution",
            decision="executed",
            detail={"tool": f"T{i}", "output": "ok"},
        )
    print(f"  写入 3 条，链完整: {log.verify_chain()}")
    print(f"  末条 hash: {log.last_hash()[:16]}...")

    # 篡改中间一条
    from dataclasses import replace

    victim = log.records()[1]
    log._records[1] = replace(victim, decision="deny")
    print(f"  篡改第 2 条 decision 后，链完整: {log.verify_chain()}  ← 检出")


def demo_router() -> None:
    section("5. lite 模型成本路由")
    router = ModelRouter()
    for agent_name in ("memorySelector", "Explore", "cli", "compact"):
        d = router.route(AGENTS[agent_name], requires_tools=bool(AGENTS[agent_name].tools))
        print(
            f"  {agent_name:16s} → {d.model.id:12s} "
            f"倍率 {d.model.credits_multiplier:>5.2f}x   {d.reason}"
        )

    # 公平对比：同样 13 个任务、同样 3000 tokens，只换模型档位
    n_pure = len(AgentRegistry(AGENTS).pure_reasoners())
    tokens = 3000
    per_lite = tokens / 1000 * MODELS["lite"].credits_multiplier
    per_full = tokens / 1000 * MODELS["default"].credits_multiplier
    lite_cost, full_cost = n_pure * per_lite, n_pure * per_full

    print(f"\n  同为 {n_pure} 个纯推理任务 × {tokens} tokens：")
    print(f"    全 lite   : {lite_cost:.2f} credits")
    print(f"    全 default: {full_cost:.2f} credits")
    print(f"    节省      : {(1 - lite_cost / full_cost) * 100:.1f}%")


def main() -> None:
    print("\nWorkGuy —— Agent 内核骨架演示")
    print("设计思想源自 WorkBuddy 5.5.4 逆向分析，实现为独立重写代码")
    demo_permission_boundary()
    demo_pyramid()
    demo_context()
    demo_audit()
    demo_router()
    print(f"\n{BAR}\n演示结束。全量测试: python -m unittest discover -s tests\n")


if __name__ == "__main__":
    main()
