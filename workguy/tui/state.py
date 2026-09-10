"""会话状态机 —— 纯数据操作、零 curses 依赖、不碰 kernel。

持有一次 TUI 会话的全部状态：对话内容（已按换行拆分的逻辑行）、累计指标
（credits / turns）、当前代理与模型、输入历史。渲染层只读取这些状态来画屏。

设计要点：
- lines() 返回**已按换行拆分的逻辑行**列表（多行助手回复正确拆成多个 Line）。
  显示层若需按终端宽度自动折行，应基于这些行再调用 textutil.wrap_text，
  本层不做显示折行（职责分离：内容模型 vs 显示布局）。
- set_agent 校验代理存在（workguy.config.AGENTS）；非法名拒绝并返回 False。
- clear() 只清空对话内容，保留累计指标、代理/模型、输入历史。
- 输入历史用索引游标管理，越界安全：翻到最旧/最新边界返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from workguy.config import AGENTS


class Role(Enum):
    """消息角色语义标签（同时是 Line 的着色依据）。"""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
    ERROR = "error"


@dataclass
class Line:
    """已渲染好的一行（含颜色语义标签，渲染层据此上色）。"""

    text: str
    style: str = "normal"  # normal | user | assistant | tool | error | system


class SessionState:
    """一次 TUI 会话的全部状态。"""

    def __init__(self, agent: str = "cli", model: str = "", session_id: str = "") -> None:
        # 非法 agent 在构造时回落到默认 cli
        self._agent = agent if agent in AGENTS else "cli"
        self._model = model
        self._tier = ""
        self._session_id = session_id
        self._lines: list[Line] = []
        self._credits: float = 0.0
        self._turns: int = 0
        self._history: list[str] = []
        self._hist_index: int = 0  # 指向「新行」末尾；0..len

    # —— 内容 ——
    def add_user(self, text: str) -> None:
        for ln in text.split("\n"):
            self._lines.append(Line(ln, style=Role.USER.value))

    def add_assistant(self, text: str) -> None:
        for ln in text.split("\n"):
            self._lines.append(Line(ln, style=Role.ASSISTANT.value))

    def add_tool_call(self, tool: str, detail: str = "") -> None:
        text = tool if not detail else f"{tool} {detail}"
        self._lines.append(Line(text, style=Role.TOOL.value))

    def add_error(self, text: str) -> None:
        for ln in text.split("\n"):
            self._lines.append(Line(ln, style=Role.ERROR.value))

    def add_system(self, text: str) -> None:
        for ln in text.split("\n"):
            self._lines.append(Line(ln, style=Role.SYSTEM.value))

    def lines(self) -> list[Line]:
        """全部已渲染的逻辑行（已按换行拆分，未做显示折行）。"""
        return list(self._lines)

    def clear(self) -> None:
        """清空对话内容；保留累计指标、代理/模型、输入历史。"""
        self._lines = []

    # —— 累计指标（状态栏用）——
    @property
    def credits(self) -> float:
        return self._credits

    @property
    def turns(self) -> int:
        return self._turns

    def add_credits(self, amount: float) -> None:
        self._credits += amount

    def incr_turns(self) -> None:
        self._turns += 1

    # —— 代理 / 会话 ——
    @property
    def agent(self) -> str:
        return self._agent

    @property
    def model(self) -> str:
        return self._model

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def session_id(self) -> str:
        return self._session_id

    def set_agent(self, name: str) -> bool:
        """切换代理；不存在的代理返回 False 并保持不变。"""
        if name in AGENTS:
            self._agent = name
            return True
        return False

    def set_model(self, model: str, tier: str = "") -> None:
        self._model = model
        self._tier = tier

    # —— 输入历史 ——
    def push_input(self, text: str) -> None:
        """记录一条输入历史；空串忽略。记录后游标回到「新行」末尾。"""
        if text and text.strip():
            self._history.append(text)
        self._hist_index = len(self._history)

    def history_prev(self) -> str | None:
        """上翻一条；已在最旧项时返回 None（不越界）。"""
        if not self._history:
            return None
        if self._hist_index <= 0:
            return None
        self._hist_index -= 1
        return self._history[self._hist_index]

    def history_next(self) -> str | None:
        """下翻一条；已回到新行时返回 None（不越界）。"""
        if not self._history:
            return None
        if self._hist_index >= len(self._history):
            return None
        self._hist_index += 1
        if self._hist_index >= len(self._history):
            return None
        return self._history[self._hist_index]
