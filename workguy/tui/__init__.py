"""WorkGuy 全屏 TUI 的可测逻辑层。

本包只放「能算的」纯逻辑：宽度/文本处理、布局/滚动、斜杠命令解析、会话状态机。
不引入 curses、不碰终端 —— 渲染层（另一 agent 负责）只调用这里的接口。

模块：
- textutil：宽度与文本处理
- layout：布局与滚动
- commands：斜杠命令解析
- state：会话状态机
"""

from __future__ import annotations

from .commands import Command, SUPPORTED, parse_command
from .layout import Layout, clamp_offset, compute_layout, visible_range
from .render import (
    RenderedLine,
    compose_context_meter,
    compose_hint_bar,
    compose_input,
    compose_splash,
    compose_status_bar,
    context_meter_style,
)
from .state import Line, Role, SessionState
from .textutil import char_width, display_width, pad_to, truncate, wrap_text

__all__ = [
    "char_width",
    "display_width",
    "wrap_text",
    "truncate",
    "pad_to",
    "Layout",
    "compute_layout",
    "visible_range",
    "clamp_offset",
    "Command",
    "SUPPORTED",
    "parse_command",
    "Line",
    "Role",
    "SessionState",
    "RenderedLine",
    "compose_status_bar",
    "compose_hint_bar",
    "compose_input",
    "compose_context_meter",
    "context_meter_style",
    "compose_splash",
]
