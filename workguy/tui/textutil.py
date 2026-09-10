"""终端文本宽度与处理 —— 纯逻辑、零 curses 依赖。

TUI 里最容易出错的就是「列数 ≠ 字符数」。本模块把宽度相关的计算全部抽成
纯函数，不碰终端、不依赖 curses，方便单测。

约定（务必与测试锁定一致）：
- 单字符显示宽度：
    * CJK / 全角（east_asian_width ∈ {'W','F'}）= 2 列
    * 组合字符 / 零宽控制符（ZWSP、ZWJ、ZWNJ、变体选择符、BOM 等）= 0 列
    * 其余（含 ASCII、ambiguous 宽度的标点如 '…'）= 1 列
    * 控制字符 tab / 换行 / 回车按 0 列处理（简化模型；渲染层可再决定如何展开 tab）
- 折行 / 截断一律以「整字符」为单位，**绝不把一个宽字符劈成两半**。
- 折行时 indent 只加在续行（首行不加），indent 自身宽度从续行可用宽度中扣除。
"""

from __future__ import annotations

import unicodedata

# 显式零宽控制字符集合（这些字符即便不在 combining() 命中范围，也按 0 列）。
_ZERO_WIDTH_CODEPOINTS: frozenset[int] = frozenset(
    [
        0x00AD,  # 软连字符
        0x200B,  # ZWSP 零宽空格
        0x200C,  # ZWNJ 零宽不连字
        0x200D,  # ZWJ 零宽连字
        0x200E,  # LRM
        0x200F,  # RLM
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # 双向格式控制
        0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # 单词连接器等
        0x2066, 0x2067, 0x2068, 0x2069,  # 双向隔离
        0xFEFF,  # BOM / 零宽不换行空格
    ]
)

# 控制字符按 0 列处理
_CONTROL_ZERO_WIDTH: frozenset[str] = frozenset(["\t", "\n", "\r"])


def char_width(ch: str) -> int:
    """单字符显示宽度（按列）。

    - 组合字符（combining 值 > 0）与零宽控制符 → 0
    - CJK / 全角（east_asian_width 为 'W' 或 'F'）→ 2
    - 其余 → 1
    """
    if not ch:
        return 0
    # 组合字符：不占列
    if unicodedata.combining(ch):
        return 0
    code = ord(ch)
    # 零宽控制符 / 变体选择符段（U+FE00–U+FE0F）
    if code in _ZERO_WIDTH_CODEPOINTS or 0xFE00 <= code <= 0xFE0F:
        return 0
    if ch in _CONTROL_ZERO_WIDTH:
        return 0
    ea = unicodedata.east_asian_width(ch)
    if ea in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """字符串显示宽度（按列数，非字符数）。"""
    return sum(char_width(ch) for ch in text)


def wrap_text(text: str, width: int, *, indent: str = "") -> list[str]:
    """按显示宽度折行。

    规则（与测试锁定）：
    - 尊重已有的 ``\\n``：先按换行切成若干段，每段独立折行。
    - 中文按列折行：一行装不下时整体挪到下一行，**不劈半**。
    - 单个字符比 ``width`` 还宽时，独占一行（宁可超宽也不截断字符）。
    - ``indent`` 只加在**续行**（首行不加）；其自身宽度从续行可用宽度中扣除。

    ``width <= 0`` 时每个字符独占一行（退化但确定、不崩）。
    """
    if width < 0:
        width = 0
    indent_w = display_width(indent)

    paragraphs = text.split("\n")
    out: list[str] = []

    for para in paragraphs:
        if para == "":
            # 空段落 → 保留一个空行
            out.append("")
            continue
        # 可用宽度：首行用 width，续行用 width - indent 宽度
        cur = ""
        cur_w = 0
        first_line = True
        for ch in para:
            cw = char_width(ch)
            avail = width if first_line else max(0, width - indent_w)
            if cur_w + cw > avail and cur_w > 0:
                # 当前行已满，收尾并开新行
                out.append((indent if not first_line else "") + cur)
                first_line = False
                # 若被折断处恰好是空白（单词换行），直接丢弃，不带到续行
                if ch.isspace():
                    cur = ""
                    cur_w = 0
                else:
                    cur = ch
                    cur_w = cw
            else:
                cur += ch
                cur_w += cw
        # 收尾最后一行（仅当确有内容，避免多出一个空行）
        if cur or first_line:
            out.append((indent if not first_line else "") + cur)
    return out


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超出时补省略号。

    不变量：**返回串的显示宽度恒 ≤ ``width``**；宽字符不会被截掉一半。
    - 若原串本身不超过 ``width``：原样返回。
    - 若 ``width`` 连省略号都放不下：返回能塞下的前缀（无省略号）。
    """
    if width < 0:
        width = 0
    if display_width(text) <= width:
        return text
    ew = display_width(ellipsis)
    result = ""
    used = 0
    if ew > width:
        # 连省略号都放不下：贪心装前缀，整字符为单位
        for ch in text:
            cw = char_width(ch)
            if used + cw > width:
                break
            result += ch
            used += cw
        return result
    avail = width - ew
    for ch in text:
        cw = char_width(ch)
        if used + cw <= avail:
            result += ch
            used += cw
        else:
            break
    return result + ellipsis


def pad_to(text: str, width: int, align: str = "left") -> str:
    """补齐到指定显示宽度（left / right / center），用于状态栏排版。

    文本已达/超过 ``width`` 时原样返回（不截断）。
    """
    if align not in ("left", "right", "center"):
        raise ValueError(f"未知对齐方式: {align!r}")
    if width < 0:
        width = 0
    dw = display_width(text)
    if dw >= width:
        return text
    pad = width - dw
    if align == "left":
        return text + " " * pad
    if align == "right":
        return " " * pad + text
    # center：左少右多，差 1 列给右边
    left = pad // 2
    right = pad - left
    return " " * left + text + " " * right
