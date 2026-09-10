"""WorkGuy —— 一个可运行的 Agent 内核骨架。

机制原型来自对 WorkBuddy Desktop 5.5.4 的逆向分析（仅取设计思想，不含任何原实现代码）：

内核六大件：
1. 六级阈值上下文压缩  —— context.py
2. 代理权限金字塔      —— agents.py / tools.py
3. 最小权限边界        —— agents.py
4. 哈希链审计          —— audit.py
5. lite 模型成本路由   —— router.py
6. ReAct 执行循环      —— kernel.py

产品化三件套：
7. 多模型网关          —— providers.py（OpenAI / Anthropic / LongCat 方言互译 + fallback）
8. 持久化              —— store.py（SQLite WAL + user_version 迁移 + 审计链落盘）
9. 扩展机制            —— skills.py / connectors.py / mcp.py / sandbox.py
                          （渐进式加载 / ${ENV} 凭据占位 / JSON-RPC stdio 客户端 / 命令准入）

三件套已全部接入 kernel.py 的执行循环（会话落盘、网关调用、技能匹配、MCP 工具挂载）。
"""

__version__ = "0.5.0"
__product__ = "WorkGuy"

# 顶层导出，方便 from workguy import AgentKernel, ...
from .agents import AgentRegistry  # noqa: E402
from .audit import AuditAnchor, AuditLog  # noqa: E402
from .connectors import ConnectorRegistry  # noqa: E402
from .context import ContextMonitor  # noqa: E402
from .kernel import DYNAMIC_TOOL_AGENTS, AgentKernel, RunResult  # noqa: E402
from .llm import LongCatLLM, MockLLM  # noqa: E402
from .mcp import MCPClient, MCPRegistry  # noqa: E402
from .providers import (  # noqa: E402
    AnthropicProvider,
    Gateway,
    LongCatProvider,
    MockProvider,
    OpenAIProvider,
    Provider,
)
from .router import ModelRouter  # noqa: E402
from .sandbox import CommandNotAllowedError, validate_command  # noqa: E402
from .skills import SkillRegistry, parse_frontmatter  # noqa: E402
from .store import Store  # noqa: E402
from .tools import ToolRegistry  # noqa: E402

__all__ = [
    # 内核
    "AgentKernel",
    "RunResult",
    "AgentRegistry",
    "ToolRegistry",
    "ContextMonitor",
    "AuditLog",
    "AuditAnchor",
    "ModelRouter",
    "DYNAMIC_TOOL_AGENTS",
    # 多模型网关
    "Gateway",
    "Provider",
    "OpenAIProvider",
    "AnthropicProvider",
    "LongCatProvider",
    "MockProvider",
    # 持久化
    "Store",
    # 扩展机制
    "SkillRegistry",
    "parse_frontmatter",
    "ConnectorRegistry",
    "MCPClient",
    "MCPRegistry",
    "validate_command",
    "CommandNotAllowedError",
    # 兼容旧入口
    "MockLLM",
    "LongCatLLM",
    "__version__",
    "__product__",
]
