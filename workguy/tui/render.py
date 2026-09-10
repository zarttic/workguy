"""TUI 渲染层的「纯文本组装」—— 可测、零 curses 依赖。

本模块只把 ``SessionState`` + 几何信息（行/列）转成「每行文本 + 颜色语义标签」
（``RenderedLine``），不碰任何终端、不调用 curses。真正的上色与画屏由 ``app.py``
（curses 主循环）负责，两者通过 ``RenderedLine.style`` 解耦。

硬约束（渲染层不花屏的前提）：
- **每一个 compose 函数的输出行宽恒 ≤ cols**（用 ``truncate`` / ``pad_to`` 兜底）。
- 中文/全角宽度一律走 ``display_width``，绝不用 ``len``。
- 长输入横向滚动时，视口要跟着光标走（``compose_input``）。

颜色语义的「语义名」与 ``theme.COLORS`` / ``theme.color_for`` 对齐，
此处只产出语义名字符串，具体 256 色/8 色/无色映射交给 theme + app。
"""

from __future__ import annotations

from dataclasses import dataclass

from .art import LOGO_COMPACT, LOGO_FULL, render_guy
from .textutil import char_width, display_width, truncate
from .theme import TAGLINE


# 上下文压力条阈值——与 workguy/context.py 的 TOKEN_THRESHOLDS 呼应
#   compact_emergency = 0.40  → 进 warning
#   input_critical     = 0.70  → 进 error
_METER_WARN_AT: float = 0.40
_METER_ERROR_AT: float = 0.70

# 输入行提示符（宽度 2：❯ + 空格，均 1 列）。app.py 复用此常量算光标列。
PROMPT: str = "❯ "

# 滚动标记（占 1 列）。当输入比视口宽、需要横向卷动时使用。
_SCROLL_LEFT: str = "…"
_SCROLL_RIGHT: str = "…"


@dataclass(frozen=True)
class RenderedLine:
    """渲染层产出的一行：纯文本 + 颜色语义标签。

    ``style`` 是语义名（normal/user/assistant/tool/error/dim/accent/brand/warning），
    供 ``theme.color_for`` 映射成具体颜色对。渲染层（app.py）据此上色。
    """

    text: str
    style: str = "normal"


def context_meter_style(ratio: float) -> str:
    """按上下文占用率返回压力条的语义色名（与 context.py 阈值呼应）。

    - ``ratio < 0.40``           → ``normal``
    - ``0.40 ≤ ratio ≤ 0.70``    → ``warning``
    - ``ratio > 0.70``           → ``error``

    边界：``0.40``、``0.70`` 都落在 warning 档；超过 0.70 才转 error。
    """
    if ratio > _METER_ERROR_AT:
        return "error"
    if ratio >= _METER_WARN_AT:
        return "warning"
    return "normal"


def compose_context_meter(ratio: float, width: int) -> str:
    """生成宽度为 ``width`` 列的上下文压力条（块字符，全部宽度 1）。

    返回串显示宽度恒 == ``width``（width<=0 时返回空串）。
    颜色语义由 ``context_meter_style`` 决定，本函数只负责形状。
    """
    width = max(0, int(width))
    if width == 0:
        return ""
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = round(ratio * width)
    if filled > width:
        filled = width
    return "█" * filled + "░" * (width - filled)


def _status_segments(state) -> list[str]:
    """状态栏的「次要→主要」信息段（不含角标、不含压力条）。"""
    segs: list[str] = []
    if state.agent:
        segs.append(f"·{state.agent}")
    if state.model:
        segs.append(state.model)
    # credits 永远给出（哪怕 0.00），这是成本可见性的核心
    segs.append(f"¢{state.credits:.2f}")
    return segs


def compose_status_bar(
    state,
    cols: int,
    *,
    mood: str = "idle",
    frame: int = 0,
    context_ratio: float = 0.0,
) -> RenderedLine:
    """状态栏（单行）：``◤W◢`` 角标 + agent + 模型 + credits + 上下文压力条。

    降级策略（cols 越小越砍，优先级从低到高砍）：压力条 → credits → 模型 → agent，
    **角标永远最先保留**（除非连角标都放不下才截断角标本身）。保证行宽 ≤ cols。
    """
    cols = max(0, int(cols))
    tag = LOGO_COMPACT  # "◤W◢"，3 列

    # 角标自身都放不下：只能截断角标，保证不超宽（极端小终端，已无法保留完整角标）
    if display_width(tag) > cols:
        return RenderedLine(truncate(tag, cols), "brand")

    line = tag  # 角标常驻
    # 依次尝试塞入 agent / model / credits，放得下才加（前导空格分隔）
    for seg in _status_segments(state):
        trial = f"{line} {seg}"
        if display_width(trial) <= cols:
            line = trial
        else:
            break  # 放不下就停：后面的更次要，直接丢弃

    # 思考/干活态：在状态栏尾部追加一个旋转指示（IP 规定「状态栏的 Guy 循环帧」的
    # 轻量表达；不占多行，避免破坏 compute_layout 的单行状态栏约束）。放得下才加。
    if mood in ("thinking", "working"):
        spin = "◐◓◑◒"[int(frame) % 4]
        trial = f"{line} {spin}"
        if display_width(trial) <= cols:
            line = trial

    # 压力条：用剩余空间（预留 1 列给前导空格）；太窄则放弃
    leftover = cols - display_width(line)
    if leftover >= 4:
        meter_w = leftover - 1  # 前导空格占 1
        if meter_w >= 3:
            meter = compose_context_meter(context_ratio, meter_w)
            line = f"{line} {meter}"

    return RenderedLine(line, "brand")


def compose_hint_bar(cols: int, *, busy: bool = False) -> RenderedLine:
    """提示栏（单行）：快捷键说明。busy=True 时显示「思考中」类提示。"""
    cols = max(0, int(cols))
    if busy:
        text = "思考中…（Guy 正在动脑子）  ·  Ctrl+C 中断"
    else:
        text = "Ctrl+C 退出 · ↑↓ 历史 · 回车 发送 · /help 帮助"
    return RenderedLine(truncate(text, cols), "warning" if busy else "dim")


def _input_scroll(buffer: str, cursor: int, avail: int) -> tuple[int, int, str, str, str]:
    """计算长输入横向滚动后的可见窗口（纯逻辑，供 compose_input 与 app 复用）。

    返回 ``(a, b, prefix, suffix, view)``：
    - ``buffer[a:b]`` 是可见文本；
    - 光标字符索引 ``cursor`` 必定落在 ``[a, b)`` 内；
    - ``prefix`` / ``suffix`` 在卷动时各为 1 列标记 ``…``，否则为空串；
    - ``display_width(view) + len(prefix) + len(suffix) ≤ avail``（块字符均 1 列）。

    卷动目标：光标尽量贴右显示（像 readline），左侧保留上下文；右侧若有空间也展示。
    """
    avail = max(0, int(avail))
    cursor = max(0, min(int(cursor), len(buffer)))

    buf_w = display_width(buffer)
    if buf_w <= avail:
        # 整行放得下：不卷动、无标记
        return 0, len(buffer), "", "", buffer

    if avail < 3:
        # 太窄：连「标记 + 1 字」都放不下，放弃标记，直接截断（宽度安全即可）
        view = truncate(buffer, avail)
        return 0, len(view), "", "", view

    # 预留 2 列给左右标记，中间留给 buffer 视口
    avail_buf = avail - 2
    if avail_buf < 1:
        avail_buf = 1

    # 1) 先让视口以光标为右端，向左尽可能多吃上下文
    a = cursor
    used = 0
    while a > 0 and used + char_width(buffer[a - 1]) <= avail_buf:
        a -= 1
        used += char_width(buffer[a])
    # 2) 光标左侧吃满后，右侧若有富余再向右延展（展示光标后的内容）
    b = cursor
    room = avail_buf - used
    while b < len(buffer) and char_width(buffer[b]) <= room:
        room -= char_width(buffer[b])
        b += 1

    prefix = _SCROLL_LEFT if a > 0 else ""
    suffix = _SCROLL_RIGHT if b < len(buffer) else ""
    return a, b, prefix, suffix, buffer[a:b]


def compose_input(state, cols: int, buffer: str, cursor: int) -> RenderedLine:
    """输入行：提示符 + 当前缓冲 + 光标位置。长输入横向滚动（视口跟随光标）。"""
    cols = max(0, int(cols))
    pw = display_width(PROMPT)

    # 提示符都放不下：只尽量保留提示符，buffer 舍弃（极端窄终端，宽度安全）
    if pw > cols:
        return RenderedLine(truncate(PROMPT, cols), "accent")

    avail = cols - pw
    a, b, prefix, suffix, view = _input_scroll(buffer, cursor, avail)
    text = PROMPT + prefix + view + suffix
    # 最终兜底：任何意外都截断到 cols，绝对不花屏
    return RenderedLine(truncate(text, cols), "accent")


def _splash_too_small(rows: int, cols: int) -> bool:
    """终端太小，放不下完整开场画面 → 退回紧凑版。"""
    logo_w = display_width(LOGO_FULL[0]) if LOGO_FULL else 0
    guy_h = len(render_guy("idle", 0))
    # 完整版需要：Logo 行数 + Guy 高度 + 一点留白
    need_rows = len(LOGO_FULL) + guy_h + 1
    return cols < logo_w or rows < need_rows


def compose_splash(rows: int, cols: int, progress: float) -> list[RenderedLine]:
    """开场动画各帧（纯文本）：逐行点亮 Logo + Guy 睁眼。progress ∈ [0,1]。

    - 终端太小时降级为紧凑版（仅 ``◤W◢`` + 标语），不崩、不超宽。
    - 每帧每行都截断到 ``cols``，保证不会把屏冲花。
    - 返回的行列表可能比 ``rows`` 长，由 app 负责裁剪/居中放置。
    """
    rows = max(0, int(rows))
    cols = max(0, int(cols))
    progress = max(0.0, min(1.0, float(progress)))

    if _splash_too_small(rows, cols):
        # 紧凑降级：只给角标 + 标语，照样截断到 cols
        out = [RenderedLine(truncate(LOGO_COMPACT, cols), "brand")]
        if TAGLINE:
            out.append(RenderedLine(truncate(TAGLINE, cols), "brand"))
        return out

    # 完整版：用 animation.splash_frames 的「逐行点亮」逻辑映射到 progress
    from .animation import splash_frames

    frames = splash_frames()
    n = len(frames)
    # progress=0 → 第 0 帧（点亮 1 行）；progress=1 → 末帧（全亮）
    idx = int(progress * (n - 1) + 0.5)
    idx = max(0, min(n - 1, idx))
    frame_lines = frames[idx].lines  # 已点亮 k 行 + 其余留白，行数恒 == len(LOGO_FULL)

    out: list[RenderedLine] = []
    for ln in frame_lines:
        out.append(RenderedLine(truncate(ln, cols), "brand"))

    # Guy：随 progress 睁眼（progress 过半才完全睁眼，否则用思考态表达「正在启动」）
    guy_mood = "idle" if progress >= 0.5 else "thinking"
    for ln in render_guy(guy_mood, 0):
        out.append(RenderedLine(truncate(ln, cols), "brand"))
    return out
