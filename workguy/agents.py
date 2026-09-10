"""代理权限金字塔。

最小权限边界的载体：19 个代理里 13 个工具白名单为空（纯推理）。
即便被提示词注入、LLM 返回了 tool_call，运行时也必须拦截。
这是能力边界，不是配置遗漏。

assert_may_call：白名单不含该工具 → 抛 PermissionDeniedError
guard_call：先 assert_may_call 再 execute —— 给 kernel 用的安全入口
"""

from __future__ import annotations

from typing import Final

from .config import AGENTS
from .tools import ToolRegistry
from .types import AgentSpec, PermissionDeniedError, ToolCall

# 零工具（纯推理）代理数量，由配置实测推导，便于断言「13」这一事实。
PURE_REASONER_COUNT: Final[int] = sum(
    1 for s in AGENTS.values() if s.is_pure_reasoner
)


class AgentRegistry:
    def __init__(self, specs: dict[str, AgentSpec] | None = None) -> None:
        # 默认用 config.AGENTS；也允许注入自定义规格（便于测试/扩展）
        self._specs: dict[str, AgentSpec] = specs if specs is not None else dict(AGENTS)

    def get(self, name: str) -> AgentSpec:
        """取代理规格；未知代理抛 KeyError。"""
        if name not in self._specs:
            raise KeyError(name)
        return self._specs[name]

    def pure_reasoners(self) -> list[str]:
        """所有零工具（纯推理）代理名。"""
        return [n for n, s in self._specs.items() if s.is_pure_reasoner]

    def privileged(self) -> list[str]:
        """所有带工具的（特权）代理名。"""
        return [n for n, s in self._specs.items() if not s.is_pure_reasoner]

    def assert_may_call(self, agent_name: str, call: ToolCall) -> None:
        """白名单不含该工具 → 抛 PermissionDeniedError。

        纯推理代理 tools 为空，调任何工具都必然被拒 —— 无死角。
        """
        spec = self.get(agent_name)  # 未知代理 → KeyError
        if call.name not in spec.tools:
            raise PermissionDeniedError(
                f"agent '{agent_name}' may not call tool '{call.name}'"
            )

    def guard_call(self, agent_name: str, call: ToolCall, registry: ToolRegistry) -> str:
        """给 kernel 的安全入口：先过权限边界，再执行。"""
        self.assert_may_call(agent_name, call)
        return registry.execute(call, caller=agent_name)
