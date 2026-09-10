"""动画帧序列与状态机 —— 纯逻辑、零 curses 依赖（IP 规范 v1 的代码化）。

渲染层只消费这里的 ``Frame`` / ``Timeline`` 数据，自己决定怎么画到屏幕上。
本模块负责「给定已流逝毫秒，算出当前该显示哪一帧」，以及预置几条 IP 规定的
时间线：思考循环、工具脉冲、开场点亮。

约束（对齐 IP.md 技术约束）：
- 非 TTY（管道/重定向）或无动画开关时，``should_animate`` 返回 False，退化为纯文本。
- 动画帧率稳定、不忽快忽慢（每帧时长固定）。
- 所有计算纯函数式，``Frame`` 用 ``frozen`` dataclass，便于测试与并发安全。
"""

from __future__ import annotations

from dataclasses import dataclass

from workguy.tui.art import LOGO_FULL, render_guy


@dataclass(frozen=True)
class Frame:
    """动画中的一帧：若干显示行 + 该帧停留时长（毫秒）。"""

    lines: tuple[str, ...]
    duration_ms: int


class Timeline:
    """一串帧 + 时长，支持按已流逝毫秒取当前帧（默认循环）。"""

    def __init__(self, frames: list[Frame], loop: bool = True) -> None:
        if not frames:
            raise ValueError("Timeline 至少需要一帧")
        self._frames = list(frames)
        self._loop = loop
        self._total_ms = sum(f.duration_ms for f in self._frames)

    def total_ms(self) -> int:
        """整条时间线走完一遍的总时长（毫秒）。"""
        return self._total_ms

    def frame_index_at(self, elapsed_ms: int) -> int:
        """返回 ``elapsed_ms`` 时刻应显示的帧下标（已处理循环/截断/负数）。"""
        n = len(self._frames)
        if elapsed_ms < 0:
            elapsed_ms = 0
        if not self._loop and elapsed_ms >= self._total_ms:
            # 非循环：播完停在最后一帧。
            return n - 1
        # 循环：对总时长取模，得到当前周期内的偏移。
        pos = elapsed_ms % self._total_ms if self._total_ms > 0 else 0
        acc = 0
        for i, f in enumerate(self._frames):
            if pos < acc + f.duration_ms:
                return i
            acc += f.duration_ms
        return n - 1

    def frame_at(self, elapsed_ms: int) -> Frame:
        """返回 ``elapsed_ms`` 时刻应显示的帧（Frame 对象）。"""
        return self._frames[self.frame_index_at(elapsed_ms)]


# —— 预置时间线 ——

# 思考循环：双眼转圈，IP 规定 ~120ms/帧。
SPINNER: Timeline = Timeline(
    [
        Frame(lines=tuple(render_guy("thinking", i)), duration_ms=120)
        for i in range(4)
    ],
    loop=True,
)

# 工具脉冲：工具名前的齿轮/脉冲指示。用块字符的明暗呼吸表达「正在干活」。
# 这里做 4 档强度循环（░→▒→▓→█），每帧 120ms。
TOOL_PULSE: Timeline = Timeline(
    [
        Frame(lines=(ch,), duration_ms=120)
        for ch in ("░", "▒", "▓", "█")
    ],
    loop=True,
)


def splash_frames() -> list[Frame]:
    """开场点亮：Logo 逐行显现，总时长 ~600ms（IP 规定）。

    每步多点亮一行 Logo，先显现的行保持常亮，直到全部露出。
    返回 ``list[Frame]``，渲染层按顺序播放即可（只播一次，不循环）。
    """
    n = len(LOGO_FULL)
    per_ms = 100  # 6 行 × 100ms = 600ms
    frames: list[Frame] = []
    for k in range(1, n + 1):
        # 已显现的 k 行 + 剩余 (n-k) 行留白，保持同一高度，画面不跳。
        lines = LOGO_FULL[:k] + tuple("" for _ in range(n - k))
        frames.append(Frame(lines=lines, duration_ms=per_ms))
    return frames


def should_animate(isatty: bool, no_animation_flag: bool = False) -> bool:
    """是否启用动画。

    - 非 TTY（管道/重定向）→ False（退化为纯文本输出）
    - 显式关闭动画开关（``no_animation_flag``）→ False
    两者皆否 → True

    对齐 IP.md 技术约束：动画在「非 TTY 环境」自动关闭。
    """
    return bool(isatty) and not no_animation_flag
