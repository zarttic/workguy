"""核心数据类型。所有模块共享的契约，对标 CodeBuddy/WorkBuddy 的 product.json 结构。

设计原则：
- 不可变优先（frozen dataclass），防止运行时被篡改
- 工具白名单用 tuple 而非 list —— 零工具代理是 frozenset 语义，不可增长
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ModelTier(Enum):
    """模型档位。lite 用于高频低难度任务，是成本控制的核心。"""

    LITE = "lite"
    AUTO = "auto"
    POWERFUL = "powerful"


class ContextAction(Enum):
    """上下文达到某阈值时应采取的动作，按代价从低到高排列。"""

    NONE = "none"
    SUMMARIZE = "summarize"  # 最便宜：只做摘要
    COMPACT = "compact"  # 中等：压缩历史
    EMERGENCY = "emergency"  # 最贵：强制处理


@dataclass(frozen=True)
class ModelSpec:
    """模型规格。credits_multiplier 对应 product.json 里的 credits 倍率。"""

    id: str
    name: str
    credits_multiplier: float
    max_input_tokens: int
    tier: ModelTier = ModelTier.AUTO
    supports_tools: bool = True


@dataclass(frozen=True)
class ToolSpec:
    """工具规格。defer_loading=True 的工具不进默认上下文，需先 ToolSearch 才能加载。"""

    name: str
    description: str
    defer_loading: bool = False


@dataclass(frozen=True)
class AgentSpec:
    """代理规格。

    tools 是**白名单**：
    - 空 tuple 表示纯推理代理，运行时禁止任何工具调用（即使 LLM 请求）
    - 这是最小权限边界的载体，19 个代理中有 13 个是空白名单
    """

    name: str
    description: str
    tools: tuple[str, ...]
    model_tier: ModelTier = ModelTier.AUTO
    as_tool: bool = False  # 可否被主代理当工具递归调用

    @property
    def is_pure_reasoner(self) -> bool:
        """纯推理代理：拿不到任何工具是能力边界，不是配置遗漏。"""
        return len(self.tools) == 0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tokens: int = 0


@dataclass(frozen=True)
class AuditRecord:
    """审计记录。

    prev_hash → hash 构成哈希链：任何一条被篡改都会导致后续所有校验失败。
    这是区别于普通日志的关键。
    """

    sequence: int
    timestamp: float
    agent: str
    category: str
    event_type: str
    decision: str  # allow | deny | require_approval | executed
    detail: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "agent": self.agent,
            "category": self.category,
            "event_type": self.event_type,
            "decision": self.decision,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass
class RouterDecision:
    """模型路由结果，含成本预估。"""

    model: ModelSpec
    reason: str

    def estimate_credits(self, input_tokens: int, output_tokens: int) -> float:
        total = (input_tokens + output_tokens) / 1000.0
        return round(total * self.model.credits_multiplier, 6)


class ToolExecutionError(Exception):
    """工具执行失败。"""


class PermissionDeniedError(Exception):
    """代理试图调用白名单外的工具。这是安全边界被触碰的信号，必须记录审计。"""


# 工具实现签名：(arguments, caller) -> str
ToolHandler = Callable[[dict[str, Any], str], str]


# ---------------------------------------------------------------------------
# 扩展层类型：多模型网关 / 持久化 / Skill / MCP / Connector
# ---------------------------------------------------------------------------


@dataclass
class ChatRequest:
    """网关统一请求格式。各 provider 负责把它翻译成自家协议。"""

    messages: list[Message] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    model: str = ""  # 路由选出的模型 id；为空时 provider 回落 spec.default_model

    @property
    def system_prompt(self) -> str:
        for m in self.messages:
            if m.role == "system":
                return m.content
        return ""


@dataclass
class ChatResponse:
    """网关统一响应格式。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    reasoning: str = ""  # 推理模型思维链（reasoning_content / thinking 块）
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """provider 调用失败，可用于触发 fallback。"""


@dataclass(frozen=True)
class ProviderSpec:
    """一个模型服务商的描述。"""

    name: str  # openai | anthropic | longcat | ...
    base_url: str
    env_key: str  # 从哪个环境变量读 key
    default_model: str
    # 各家协议方言差异在这里声明，由 provider 自行解释
    dialects: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillSpec:
    """技能元数据。

    对应 SKILL.md 的 YAML frontmatter。只有这些字段常驻内存（L0），
    正文与附属资源按需读盘（L1 / L2）—— 这是渐进式加载的核心。
    """

    name: str
    description: str  # 唯一匹配面，写得越像命中条件清单召回越准
    path: str = ""
    description_en: str | None = None
    when_to_use: str | None = None
    allowed_tools: tuple[str, ...] = ()
    license: str | None = None
    version: str | None = None
    author: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class MCPServerConfig:
    """MCP server 配置。对标业界通行的 mcpServers 条目格式。"""

    name: str
    transport: str = "stdio"  # stdio | http | sse
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = 30_000
    disabled: bool = False
    disabled_tools: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """遮蔽 env / headers 的值。

        dataclass 自动生成的 __repr__ 会把字段原样打印，任何 print() / 日志 /
        异常栈里出现这个对象，凭据就泄漏了。契约要求配置只写 ${ENV} 占位，
        但代码层不能依赖调用方守规矩——这里做最后一道兜底。
        """
        return (
            f"MCPServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"command={self.command!r}, args={self.args!r}, "
            f"env=<{len(self.env)} 项已遮蔽>, url={self.url!r}, "
            f"headers=<{len(self.headers)} 项已遮蔽>, "
            f"timeout_ms={self.timeout_ms}, disabled={self.disabled}, "
            f"disabled_tools={self.disabled_tools!r})"
        )


@dataclass(frozen=True)
class ConnectorSpec:
    """连接器 = MCP server 配置 + 生命周期状态 + 凭据引用。

    凭据本身不进这个结构，只放环境变量名引用（业界做法：
    配置文件里写 ${ENV} 占位，真实值从环境读，避免明文落盘）。
    """

    name: str
    mcp: MCPServerConfig
    bound: bool = False
    enabled: bool = False
    credential_env: str | None = None  # 环境变量名，不是值
