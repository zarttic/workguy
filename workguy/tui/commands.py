"""斜杠命令解析 —— 纯逻辑、零 curses 依赖。

只负责把用户输入文本解析成 Command 结构；合法性的进一步判断（是否 SUPPORTED）
由调用方完成，本模块对未知命令不报错（返回对应 Command 即可）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 支持的斜杠命令集合（小写）。未知命令由调用方比对此处判定。
SUPPORTED: frozenset[str] = frozenset(
    {"exit", "quit", "clear", "skills", "audit", "agent", "help"}
)


@dataclass(frozen=True)
class Command:
    """解析出的斜杠命令。"""

    name: str
    args: str = ""


def parse_command(text: str) -> Command | None:
    """以 ``/`` 开头（作为首字符）则解析为命令，否则返回 None。

    规则：
    - 首字符必须是 ``/``；前后带空白（如 "  /x"）一律按非命令处理。
    - 命令名取 ``/`` 后第一个空白前的 token；其后内容作为 args，**仅去首尾空格**，
      内部连续空格原样保留（例：``/agent  cli  extra`` → Command('agent','cli  extra')）。
    - 仅有 ``/`` 或 ``/`` 后全是空白 → 返回 None。
    """
    if not text or not text.startswith("/"):
        return None
    body = text[1:]
    if not body.strip():
        return None
    parts = body.split(None, 1)
    name = parts[0]
    # args 仅去首尾空格，内部连续空格保留（例："agent  cli  extra" → "cli  extra"）
    args = parts[1].strip() if len(parts) > 1 else ""
    return Command(name, args)
