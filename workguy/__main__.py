"""WorkGuy 命令行入口（``python -m workguy``）。

零第三方依赖，仅用标准库 ``argparse``。只依赖稳定模块
（skills / config / agents / tools / audit / router / context），
**不 import 正在重构的 kernel / providers**。

子命令：
  version   打印产品名与版本号
  demo      运行内置演示（优先复用根目录 demo.py，失败降级内联精简版）
  skills    扫描目录、列出找到的技能（SKILL.md，经 SkillRegistry）

出错时退出码非 0，错误信息走 stderr。
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from workguy import __product__, __version__


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_version(args: argparse.Namespace) -> int:
    print(f"{__product__} {__version__}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    # 优先复用根目录 demo.py；任何导入/运行异常都降级到内联精简演示。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        demo = importlib.import_module("demo")
        demo.main()
        return 0
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，绝不直接崩溃
        print(
            f"[warn] 未能运行 demo.py（{exc!r}），改用内置精简演示。\n",
            file=sys.stderr,
        )
        return _inline_demo()


def _inline_demo() -> int:
    """仅用稳定模块的内置精简演示，作为 demo.py 不可用时的兜底。"""
    from workguy.agents import AgentRegistry
    from workguy.audit import AuditLog
    from workguy.config import AGENTS
    from workguy.router import ModelRouter

    bar = "─" * 66
    print(f"\n{bar}\nWorkGuy 内置精简演示（仅用稳定模块）\n{bar}")

    # 1. 代理权限金字塔
    agents = AgentRegistry(AGENTS)
    print("\n1. 代理权限金字塔")
    rows = sorted(((len(s.tools), n) for n, s in AGENTS.items()), reverse=True)
    for count, name in rows:
        tag = "  ← 纯推理" if count == 0 else ""
        print(f"  {name:24s} {count:3d} 个工具{tag}")
    print(f"\n  零工具代理 {len(agents.pure_reasoners())} 个 / 带工具 {len(agents.privileged())} 个")

    # 2. 哈希链审计
    print("\n2. 哈希链审计")
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

    # 3. 成本路由
    print("\n3. lite 模型成本路由")
    router = ModelRouter()
    for name in ("memorySelector", "Explore", "cli", "compact"):
        if name not in AGENTS:
            continue
        spec = AGENTS[name]
        d = router.route(spec, requires_tools=bool(spec.tools))
        print(f"  {name:16s} → {d.model.id:12s} 倍率 {d.model.credits_multiplier:>5.2f}x")

    print(f"\n{bar}\n演示结束。\n")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from workguy.skills import SkillRegistry

    root = args.directory
    if not os.path.isdir(root):
        print(f"错误：目录不存在或无访问权限：{root}", file=sys.stderr)
        return 2

    reg = SkillRegistry()
    count = reg.scan(root)
    print(f"扫描目录：{root}")
    print(f"找到技能：{count} 个\n")

    if count == 0:
        print("（未找到任何 SKILL.md）")
        return 0

    for spec in reg.list_specs():
        flag = " [disabled]" if spec.disabled else ""
        print(f"  • {spec.name}{flag}")
        if spec.description:
            first_line = spec.description.splitlines()[0] if spec.description else ""
            if first_line:
                print(f"      {first_line}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """启动全屏 TUI 聊天（需要真实 LLM，key 只从环境变量读）。"""
    from workguy.tui.app import TuiApp
    from workguy.tui.state import SessionState

    # key 缺失：清晰报错，不静默降级到 mock
    key = os.environ.get("LONGCAT_API_KEY")
    if not key:
        print("错误：未设置 LONGCAT_API_KEY 环境变量。TUI 需要真实 LLM 才能对话。", file=sys.stderr)
        print("  export LONGCAT_API_KEY=ak_xxx        # Git Bash", file=sys.stderr)
        print("  $env:LONGCAT_API_KEY='ak_xxx'        # PowerShell", file=sys.stderr)
        return 1

    # 全链路接线（参考 examples/real_llm.py）：网关 → 内核 → 技能 → 审计 → 持久化
    from workguy import AgentKernel, Gateway, LongCatProvider, SkillRegistry, Store
    from workguy.config import TOOLS
    from workguy.tools import ToolRegistry

    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="workguy-"))
    skills = SkillRegistry()
    skills.scan(workdir / "skills")
    store = Store(workdir / "wb.db")
    tools = ToolRegistry(TOOLS)
    provider = LongCatProvider(api_key=key)
    gateway = Gateway({"longcat": provider})

    kernel = AgentKernel(
        gateway=gateway,
        provider_chain=["longcat"],
        tools=tools,
        store=store,
        skills=skills,
    )

    state = SessionState(agent=args.agent)
    app = TuiApp(kernel, state=state, animate=not args.no_animation)
    return app.run()


# ---------------------------------------------------------------------------
# 解析器与入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workguy",
        description=f"{__product__} {__version__} —— Agent 内核命令行入口。",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("version", help="打印产品名与版本号")
    sub.add_parser("demo", help="运行内置演示（优先复用根目录 demo.py）")

    sp = sub.add_parser("skills", help="扫描目录并列出找到的技能（SKILL.md）")
    sp.add_argument("directory", help="待扫描的目录路径")

    cp = sub.add_parser("chat", help="启动全屏 TUI 聊天（需要真实 LLM，key 从环境变量读）")
    cp.add_argument("--agent", default="cli", help="起始 agent（默认 cli）")
    cp.add_argument("--no-animation", action="store_true", help="关闭开场/思考动画")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 无子命令：打印帮助，退出码 1（约定：用法错误）。
    if args.command is None:
        parser.print_help()
        return 1

    handlers = {
        "version": cmd_version,
        "demo": cmd_demo,
        "skills": cmd_skills,
        "chat": cmd_chat,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
