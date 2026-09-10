"""配置层。对标 WorkBuddy/CodeBuddy 的 product.json —— 一个文件驱动全部行为。

数据来源说明：
- 阈值、倍率、代理分层一律取自本机 WorkBuddy Desktop 5.5.4 的实测值
- 标 [骨架设定] 的是逆向时未落到原始数据、为实现完整性而补的合理值
"""

from __future__ import annotations

from typing import Final

from .types import AgentSpec, ModelSpec, ModelTier, ToolSpec

# ---------------------------------------------------------------------------
# 1. 上下文管理：六级阈值
#
# 实测自 tokenUsageThresholds。设计精髓：
# 摘要(0.15) 比压缩(0.40) 更早介入 —— 因为摘要更便宜。
# 不是等上下文爆了再救火，而是用最便宜的手段、在最早的时刻动手。
# ---------------------------------------------------------------------------

TOKEN_THRESHOLDS: Final[dict[str, float]] = {
    "summary_emergency": 0.15,  # 触发摘要生成
    "compact_emergency": 0.40,  # 触发对话压缩
    "compact_emergency_deepseek": 0.50,  # deepseek 系列阈值放宽
    "pre_message": 0.50,  # 每发一条消息前的体检点
    "input_warning": 0.60,
    "input_critical": 0.70,
    "input_emergency": 0.90,
    "request_emergency": 0.90,
}

# 阈值到动作的映射，按触发代价升序。
#
# 硬约束：动作等级必须随 ratio **单调递增**，不得回落。
# （曾踩坑：input_warning 误配成 summarize，导致 0.60~0.70 区间动作等级
#   反而低于 0.40 处的 compact，语义自相矛盾。）
# warning / critical 只是告警状态标记，不改变动作等级——
# 它们的存在是为了可观测性，不是为了降级处理力度。
THRESHOLD_ACTIONS: Final[list[tuple[float, str]]] = [
    (TOKEN_THRESHOLDS["summary_emergency"], "summarize"),
    (TOKEN_THRESHOLDS["compact_emergency"], "compact"),
    (TOKEN_THRESHOLDS["input_warning"], "compact"),
    (TOKEN_THRESHOLDS["input_critical"], "compact"),
    (TOKEN_THRESHOLDS["input_emergency"], "emergency"),
]

# ---------------------------------------------------------------------------
# 2. 模型清单与计费倍率
#
# 实测倍率区间 0.06x ~ 5.00x，差 80 倍。这是成本路由存在的理由。
# ---------------------------------------------------------------------------

MODELS: Final[dict[str, ModelSpec]] = {
    "lite": ModelSpec(
        id="lite",
        name="Lite",
        credits_multiplier=0.05,  # [骨架设定]
        max_input_tokens=128_000,
        tier=ModelTier.LITE,
    ),
    "deepseek-v4-flash": ModelSpec(
        id="deepseek-v4-flash",
        name="DeepSeek-V4-Flash",
        credits_multiplier=0.06,
        max_input_tokens=200_000,
        tier=ModelTier.LITE,
    ),
    "default": ModelSpec(
        id="default",
        name="Default",
        credits_multiplier=2.00,
        max_input_tokens=200_000,
        tier=ModelTier.POWERFUL,
    ),
    "deepseek-v4-pro": ModelSpec(
        id="deepseek-v4-pro",
        name="DeepSeek-V4-Pro",
        credits_multiplier=0.16,
        max_input_tokens=1_000_000,
        tier=ModelTier.POWERFUL,
    ),
    "glm-5.2": ModelSpec(
        id="glm-5.2",
        name="GLM-5.2",
        credits_multiplier=0.79,
        max_input_tokens=200_000,
        tier=ModelTier.POWERFUL,
    ),
    "kimi-k2.7": ModelSpec(
        id="kimi-k2.7",
        name="Kimi-K2.7",
        credits_multiplier=0.57,
        max_input_tokens=200_000,
        tier=ModelTier.POWERFUL,
    ),
    "kimi-k3-1": ModelSpec(
        id="kimi-k3-1",
        name="Kimi-K3-1",
        credits_multiplier=1.62,
        max_input_tokens=200_000,
        tier=ModelTier.POWERFUL,
    ),
    "hunyuan-image-v3.0-art": ModelSpec(
        id="hunyuan-image-v3.0-art",
        name="Hunyuan-Image-V3.0-Art",
        credits_multiplier=5.00,
        max_input_tokens=32_000,
        tier=ModelTier.POWERFUL,
        supports_tools=False,
    ),
}

# ---------------------------------------------------------------------------
# 3. 工具注册表
#
# 实测 55 项，其中 23 项 defer_loading（不进默认上下文，按需 ToolSearch 加载）。
# ---------------------------------------------------------------------------


def _tool(name: str, defer: bool = False) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name} tool", defer_loading=defer)


TOOLS: Final[dict[str, ToolSpec]] = {
    # 默认装载
    "Read": _tool("Read"),
    "Write": _tool("Write"),
    "Edit": _tool("Edit"),
    "Glob": _tool("Glob"),
    "Grep": _tool("Grep"),
    "Bash": _tool("Bash"),
    "PowerShell": _tool("PowerShell"),
    "WebFetch": _tool("WebFetch"),
    "WebSearch": _tool("WebSearch"),
    "Agent": _tool("Agent"),
    "TaskCreate": _tool("TaskCreate"),
    "TaskGet": _tool("TaskGet"),
    "TaskUpdate": _tool("TaskUpdate"),
    "TaskList": _tool("TaskList"),
    "Skill": _tool("Skill"),
    "AskUserQuestion": _tool("AskUserQuestion"),
    "ToolSearch": _tool("ToolSearch"),
    "DeferExecuteTool": _tool("DeferExecuteTool"),
    "SlashCommand": _tool("SlashCommand"),
    # 懒加载：不进默认上下文
    "NotebookEdit": _tool("NotebookEdit", defer=True),
    "ListMcpResources": _tool("ListMcpResources", defer=True),
    "ReadMcpResource": _tool("ReadMcpResource", defer=True),
    "WaitForMcpServers": _tool("WaitForMcpServers", defer=True),
    "EnterPlanMode": _tool("EnterPlanMode", defer=True),
    "ExitPlanMode": _tool("ExitPlanMode", defer=True),
    "KillShell": _tool("KillShell", defer=True),
    "TaskStop": _tool("TaskStop", defer=True),
    "TaskOutput": _tool("TaskOutput", defer=True),
    "SkillManage": _tool("SkillManage", defer=True),
    "LSP": _tool("LSP", defer=True),
    "StructuredOutput": _tool("StructuredOutput", defer=True),
    "ImageGen": _tool("ImageGen", defer=True),
    "ImageEdit": _tool("ImageEdit", defer=True),
    "VideoGen": _tool("VideoGen", defer=True),
    "Artifact": _tool("Artifact", defer=True),
    "ArtifactControl": _tool("ArtifactControl", defer=True),
    "ComputerUse": _tool("ComputerUse", defer=True),
    "TeamCreate": _tool("TeamCreate", defer=True),
    "TeamDelete": _tool("TeamDelete", defer=True),
    "SendMessage": _tool("SendMessage", defer=True),
    "EnterWorktree": _tool("EnterWorktree", defer=True),
    "LeaveWorktree": _tool("LeaveWorktree", defer=True),
    "CronCreate": _tool("CronCreate", defer=True),
    "DelegateTool": _tool("DelegateTool", defer=True),
    "PushNotification": _tool("PushNotification", defer=True),
    "ReportFindings": _tool("ReportFindings", defer=True),
    "Workflow": _tool("Workflow", defer=True),
    "Monitor": _tool("Monitor", defer=True),
    "REPL": _tool("REPL", defer=True),
}

# ---------------------------------------------------------------------------
# 4. 代理权限金字塔
#
# 实测 19 个代理，其中 **13 个工具白名单为空**（纯推理）。
# 这是整套系统最值得学的设计：把"动脑"和"动手"在架构层彻底隔离。
# 即便被提示词注入，零工具代理也拿不到任何能力。
# ---------------------------------------------------------------------------

_CLI_TOOLS: Final[tuple[str, ...]] = (
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "PowerShell",
    "WebFetch", "WebSearch", "Agent", "TaskCreate", "TaskGet", "TaskUpdate",
    "TaskList", "Skill", "AskUserQuestion", "ToolSearch", "DeferExecuteTool",
    "SlashCommand", "NotebookEdit", "EnterPlanMode", "ExitPlanMode", "KillShell",
    "TaskStop", "TaskOutput", "LSP", "StructuredOutput", "ImageGen", "VideoGen",
    "Artifact", "ArtifactControl", "TeamCreate", "SendMessage", "REPL",
)

_GENERAL_TOOLS: Final[tuple[str, ...]] = _CLI_TOOLS[:24]

_PLAN_TOOLS: Final[tuple[str, ...]] = (
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch",
    "TaskCreate", "TaskUpdate", "TaskList", "EnterPlanMode", "ExitPlanMode",
    "SlashCommand", "AskUserQuestion", "Skill", "Agent", "ToolSearch",
)

_EXPLORE_TOOLS: Final[tuple[str, ...]] = (
    "Read", "Glob", "Grep", "Bash", "PowerShell", "WebFetch", "TaskCreate",
    "TaskList", "KillShell", "TaskOutput", "LSP", "SlashCommand",
    "EnterWorktree", "LeaveWorktree", "ToolSearch", "Agent",
)

_STATUSLINE_TOOLS: Final[tuple[str, ...]] = (
    "Read", "Write", "Edit", "ToolSearch", "SlashCommand",
)

_PULSE_TOOLS: Final[tuple[str, ...]] = ("WebSearch", "WebFetch")

AGENTS: Final[dict[str, AgentSpec]] = {
    # ---- 有工具权限的 6 个 ----
    "cli": AgentSpec(
        name="cli",
        description="全能主代理，持有完整工具集",
        tools=_CLI_TOOLS,
        model_tier=ModelTier.POWERFUL,
    ),
    "general-purpose": AgentSpec(
        name="general-purpose",
        description="通用任务代理",
        tools=_GENERAL_TOOLS,
        model_tier=ModelTier.AUTO,
    ),
    "Plan": AgentSpec(
        name="Plan",
        description="架构规划子代理，只读为主",
        tools=_PLAN_TOOLS,
        model_tier=ModelTier.AUTO,
        as_tool=True,
    ),
    "Explore": AgentSpec(
        name="Explore",
        description="代码库探索子代理，绑定 lite 模型省成本",
        tools=_EXPLORE_TOOLS,
        model_tier=ModelTier.LITE,
        as_tool=True,
    ),
    "statusline-setup": AgentSpec(
        name="statusline-setup",
        description="配置状态栏，工具极少",
        tools=_STATUSLINE_TOOLS,
        model_tier=ModelTier.AUTO,
        as_tool=True,
    ),
    "pulse": AgentSpec(
        name="pulse",
        description="联网获取最新信息",
        tools=_PULSE_TOOLS,
        model_tier=ModelTier.AUTO,
    ),
    # ---- 纯推理代理 13 个：白名单为空，运行时禁止任何工具调用 ----
    "compact": AgentSpec(
        name="compact", description="对话压缩", tools=(), model_tier=ModelTier.AUTO
    ),
    "contextSummary": AgentSpec(
        name="contextSummary", description="上下文摘要", tools=(), model_tier=ModelTier.AUTO
    ),
    "contentAnalyzer": AgentSpec(
        name="contentAnalyzer", description="内容分析", tools=(), model_tier=ModelTier.AUTO
    ),
    "terminalTitleGenerator": AgentSpec(
        name="terminalTitleGenerator", description="终端标题生成", tools=(), model_tier=ModelTier.AUTO
    ),
    "promptSuggestion": AgentSpec(
        name="promptSuggestion", description="提示词建议", tools=(), model_tier=ModelTier.AUTO
    ),
    "memorySelector": AgentSpec(
        name="memorySelector", description="记忆筛选，lite", tools=(), model_tier=ModelTier.LITE
    ),
    "summaryGenerator": AgentSpec(
        name="summaryGenerator", description="会话总结", tools=(), model_tier=ModelTier.AUTO
    ),
    "autoModeClassifier": AgentSpec(
        name="autoModeClassifier", description="工具调用风险分级，lite", tools=(), model_tier=ModelTier.LITE
    ),
    "promptHookEvaluator": AgentSpec(
        name="promptHookEvaluator", description="用 LLM 评估 hook 条件，lite", tools=(), model_tier=ModelTier.LITE
    ),
    "insightsAnalyzer": AgentSpec(
        name="insightsAnalyzer", description="使用洞察分析", tools=(), model_tier=ModelTier.AUTO
    ),
    "agentInstructions": AgentSpec(
        name="agentInstructions", description="生成代理指令", tools=(), model_tier=ModelTier.AUTO
    ),
    "handoff-summary": AgentSpec(
        name="handoff-summary", description="任务交接摘要", tools=(), model_tier=ModelTier.AUTO
    ),
    "enhance-prompt": AgentSpec(
        name="enhance-prompt", description="润色用户输入", tools=(), model_tier=ModelTier.AUTO
    ),
}

# 审计用的默认元信息
AUDIT_GENESIS_HASH: Final[str] = "0" * 64
