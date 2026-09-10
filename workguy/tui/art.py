"""WorkGuy 的字符画资产 —— Logo 与 Guy 方块机器人（IP 规范 v1 的代码化）。

纯数据 + 纯逻辑，**不 import curses**，也不依赖终端。渲染层只消费这里的
字符串数据。

硬约束（对齐 design/IP.md 技术约束）：
- 只用**块字符与 Box Drawing**（`█ ▀ ▄ ░ ▒ ▓ ─ │ ╭ ╮ ╰ ╯ ◤ ◢ ▁` 等），
  **绝不用 emoji**。眼睛用 `● ◐ ◓ ◑ ◒ ▪ ^ ×` 这类宽度 1 的普通符号。
- 每个 mood 的**尺寸必须完全相同**（否则切换表情时画面会跳）。
- 每一帧、**每一行的显示宽度必须一致**（渲染层靠这个对齐）。

设计（12 列 × 7 行）：

       ╷            ← 天线（情绪指示器：竖起=正常，消失=蔫了）
     ╭─────────╮
     │  ●   ●  │      ← 眼睛
     │         │
     │  ╰───╯  │      ← 嘴
     ╰─────────╯
      ▁▁▁▁▁▁▁▁      ← 地面/影子

用**字面模板**而非循环绘制：循环画边框时一旦行号集合写漏，边框就会断裂
（本项目 v1 的 `stuck` 就是这么坏的）。模板则一眼能看出完整性。
"""

from __future__ import annotations

from workguy.tui.textutil import display_width

# 统一画布尺寸：所有 mood / 所有帧都必须是这个大小。
WIDTH: int = 12
HEIGHT: int = 7


def _frame(*lines: str) -> tuple[str, ...]:
    """构造一帧并校验尺寸与行宽（构建期防御，导入即报错）。"""
    if len(lines) != HEIGHT:
        raise AssertionError(f"Guy 应为 {HEIGHT} 行，实际 {len(lines)} 行")
    for ln in lines:
        w = display_width(ln)
        if w != WIDTH:
            raise AssertionError(f"Guy 行宽应为 {WIDTH}，实际 {w}: {ln!r}")
    return tuple(lines)


# —— 行模板（全部 12 列）——

_ANTENNA_UP: str = "   ╷        "  # 天线竖起
_ANTENNA_DOWN: str = " " * WIDTH  # 天线蔫了（整体留白）
_TOP: str = " ╭─────────╮"
_BLANK_MID: str = " │         │"
_BOTTOM: str = " ╰─────────╯"
_GROUND: str = "  ▁▁▁▁▁▁▁▁  "


def _face(
    eyes: str,
    mouth: str,
    antenna: str = _ANTENNA_UP,
) -> tuple[str, ...]:
    """按「眼睛 + 嘴 + 天线」拼一张脸。

    eyes  —— 单字符，左右眼重复使用（如 ``●``、``×``、``^``）
    mouth —— 5 列宽的嘴部内容（如 ``╰───╯``、``· · ·``）
    """
    return _frame(
        antenna,
        _TOP,
        f" │  {eyes}   {eyes}  │",
        _BLANK_MID,
        f" │  {mouth}  │",
        _BOTTOM,
        _GROUND,
    )


# Guy 的表情帧。键为 mood，值是「多帧」字符画（动画用）。
# idle / working / done / stuck 各 1 帧；thinking 4 帧（双眼转圈）。
GUY_MOODS: dict[str, tuple[tuple[str, ...], ...]] = {
    # 待机：圆眼常亮，浅笑
    "idle": (_face("●", "╰───╯"),),
    # 思考：双眼转圈（◐→◓→◑→◒）+ 嘴部嘟囔
    "thinking": tuple(_face(eye, "· · ·") for eye in "◐◓◑◒"),
    # 干活：方眼专注（▪ 是窄字符，视觉上比圆眼更「收」）+ 抿嘴
    "working": (_face("▪", "╭───╮"),),
    # 完成：笑眼 ^^
    "done": (_face("^", "╰───╯"),),
    # 卡住：天线蔫了 + 叉眼 + 撇嘴
    "stuck": (_face("×", "╭───╮", _ANTENNA_DOWN),),
}

# 兜底 mood（未知 mood 回落到此）。
_DEFAULT_MOOD = "idle"


def render_guy(mood: str = "idle", frame: int = 0) -> list[str]:
    """取指定 mood 的第 ``frame`` 帧（frame 对帧数取模，永不越界）。

    未知 mood 回落到 ``idle``；frame 可为任意整数（含负数/超大），取模后安全。
    """
    frames = GUY_MOODS.get(mood, GUY_MOODS[_DEFAULT_MOOD])
    if not frames:
        frames = GUY_MOODS[_DEFAULT_MOOD]
    frame = frame % len(frames)
    return list(frames[frame])


def guy_width(mood: str = "idle", frame: int = 0) -> int:
    """该表情帧的显示宽度（列）。"""
    lines = render_guy(mood, frame)
    return display_width(lines[0]) if lines else 0


def guy_height(mood: str = "idle", frame: int = 0) -> int:
    """该表情帧的显示高度（行）。"""
    return len(render_guy(mood, frame))


def mood_frame_count(mood: str) -> int:
    """该 mood 的帧数（未知 mood 返回 idle 的帧数）。"""
    frames = GUY_MOODS.get(mood, GUY_MOODS[_DEFAULT_MOOD])
    return len(frames) if frames else len(GUY_MOODS[_DEFAULT_MOOD])


# —— Logo ——

# design/IP.md 第六节的全大图 Logo（ANSI Shadow 风格）。
_LOGO_RAW: tuple[str, ...] = (
    "██╗    ██╗ ██████╗ ██████╗ ██╗  ██╗ ██████╗ ██╗   ██╗██╗   ██╗",
    "██║    ██║██╔═══██╗██╔══██╗██║ ██╔╝██╔════╝ ██║   ██║╚██╗ ██╔╝",
    "██║ █╗ ██║██║   ██║██████╔╝█████╔╝ ██║  ███╗██║   ██║ ╚████╔╝",
    "██║███╗██║██║   ██║██╔══██╗██╔═██╗ ██║   ██║██║   ██║  ╚██╔╝",
    "╚███╔███╔╝╚██████╔╝██║  ██║██║  ██╗╚██████╔╝╚██████╔╝   ██║",
    " ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝",
)

# 右补齐到统一宽度，保证每行显示宽度一致（渲染层靠这个对齐）。
_MAX_LOGO_W = max(display_width(line) for line in _LOGO_RAW)
LOGO_FULL: tuple[str, ...] = tuple(
    line + " " * (_MAX_LOGO_W - display_width(line)) for line in _LOGO_RAW
)

# 紧凑一角标（状态栏等窄场景使用）。
LOGO_COMPACT: str = "◤W◢"


def render_logo(compact: bool = False) -> list[str]:
    """返回 Logo 行列表。compact=True 返回紧凑角标 ``◤W◢``。"""
    if compact:
        return [LOGO_COMPACT]
    return list(LOGO_FULL)
