"""用真实 LongCat LLM 跑一次 WorkGuy 端到端。

用法：
    # Windows (Git Bash)
    export LONGCAT_API_KEY=ak_xxxxxxxx
    python examples/real_llm.py "用一句话说明什么是哈希链审计"

    # Windows (PowerShell)
    $env:LONGCAT_API_KEY="ak_xxxxxxxx"
    python examples/real_llm.py "你的问题"

设计要点：
- **API key 只从环境变量读**，不写进代码、不落盘、不入库
- 若环境变量缺失，直接报错退出而不是静默降级到 mock
- 展示全链路接线：网关 → 内核 → 技能匹配 → 审计 → 持久化
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 允许直接 `python examples/real_llm.py` 运行（把项目根加进 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workguy import (  # noqa: E402
    AgentKernel,
    Gateway,
    LongCatProvider,
    SkillRegistry,
    Store,
)
from workguy.config import TOOLS  # noqa: E402
from workguy.tools import ToolRegistry  # noqa: E402

MODEL = "LongCat-2.0"


def build_skill_dir(base: Path) -> Path:
    """造一个技能目录，用来演示渐进加载的 L0 匹配确实生效。"""
    d = base / "skills" / "hash-audit"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: hash-audit\n"
        "description: 哈希链 审计 防篡改 完整性校验 日志\n"
        "---\n\n"
        "（L1 正文：只有真正选中该技能时才应被读取）\n",
        encoding="utf-8",
    )
    return base / "skills"


def main() -> int:
    key = os.environ.get("LONGCAT_API_KEY")
    if not key:
        print("错误：未设置 LONGCAT_API_KEY 环境变量。", file=sys.stderr)
        print("  export LONGCAT_API_KEY=ak_xxx   # Git Bash", file=sys.stderr)
        return 1

    question = " ".join(sys.argv[1:]) or "用一句话说明什么是哈希链审计的防篡改原理"

    workdir = Path(tempfile.mkdtemp(prefix="workguy-"))
    skills = SkillRegistry()
    skills.scan(build_skill_dir(workdir))

    store = Store(workdir / "wb.db")

    tools = ToolRegistry(TOOLS)
    tools.register("Read", lambda a, c: f"[示例] 读到 {a.get('path', '')}")

    # 关键一步：Provider 通过 chat() 适配满足 LLMClient 协议，可直接挂进 Gateway
    provider = LongCatProvider(api_key=key)
    gateway = Gateway({"longcat": provider})

    kernel = AgentKernel(
        gateway=gateway,
        provider_chain=["longcat"],
        tools=tools,
        store=store,
        skills=skills,
    )

    print(f"提问：{question}\n")
    result = kernel.run("cli", question, session_id="example-1")

    print("─" * 62)
    print(f"回复      : {result.content.strip()}")
    print(f"provider  : {result.provider}  |  模型: {result.model}  档位: {result.model_tier}")
    print(f"credits   : {result.credits:.4f}")
    print(f"命中技能  : {result.matched_skills or '无'}")
    print(f"迭代轮数  : {result.iterations}")
    print(f"会话落盘  : {result.persisted}  (session={result.session_id})")
    print(f"审计条数  : {len(store.load_audit())}   链完好: {store.verify_audit_chain()}")
    print(f"用量      : {store.usage_report()}")
    if result.store_errors:
        print(f"落盘错误  : {result.store_errors}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
