"""animation 单元测试 —— 帧序列、时间线、预置动画、开关判定。

TDD：先写测试（全红）→ 实现 → 全绿。

锁定约定（与 animation.py / IP.md 一致）：
- Frame 为 frozen dataclass；Timeline 支持按已流逝毫秒取当前帧（循环/非循环）
- 超大 elapsed 取模正确；负数不崩
- SPINNER 每帧 120ms（IP 规定思考循环 ~120ms/帧）
- splash_frames() 总时长约 600ms（IP 规定）
- should_animate(False) 为 False（非 TTY 退化为纯文本）
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui.animation import (
    Frame,
    Timeline,
    SPINNER,
    TOOL_PULSE,
    splash_frames,
    should_animate,
)


class TestFrame(unittest.TestCase):
    def test_frame_is_frozen(self):
        f = Frame(lines=("a",), duration_ms=10)
        with self.assertRaises(Exception):
            f.duration_ms = 99  # type: ignore[misc]

    def test_frame_fields(self):
        f = Frame(lines=("x", "y"), duration_ms=50)
        self.assertEqual(f.lines, ("x", "y"))
        self.assertEqual(f.duration_ms, 50)


class TestTimelineBasics(unittest.TestCase):
    def _make(self, loop=True):
        return Timeline(
            [
                Frame(lines=("a",), duration_ms=100),
                Frame(lines=("b",), duration_ms=100),
                Frame(lines=("c",), duration_ms=100),
            ],
            loop=loop,
        )

    def test_total_ms(self):
        tl = self._make()
        self.assertEqual(tl.total_ms(), 300)

    def test_frame_at_basic_loop(self):
        tl = self._make(loop=True)
        self.assertEqual(tl.frame_at(0).lines, ("a",))
        self.assertEqual(tl.frame_at(50).lines, ("a",))
        self.assertEqual(tl.frame_at(100).lines, ("b",))
        self.assertEqual(tl.frame_at(250).lines, ("c",))

    def test_frame_index_at_basic(self):
        tl = self._make()
        self.assertEqual(tl.frame_index_at(0), 0)
        self.assertEqual(tl.frame_index_at(150), 1)
        self.assertEqual(tl.frame_index_at(299), 2)

    def test_loop_wraps(self):
        tl = self._make(loop=True)
        # 总时长 300，elapsed=300 应回到第 0 帧
        self.assertEqual(tl.frame_at(300).lines, ("a",))
        self.assertEqual(tl.frame_index_at(300), 0)
        # 超大 elapsed 取模正确
        big = 100000
        self.assertEqual(
            tl.frame_index_at(big), tl.frame_index_at(big % 300)
        )

    def test_non_loop_stops_at_last(self):
        tl = self._make(loop=False)
        self.assertEqual(tl.frame_at(0).lines, ("a",))
        self.assertEqual(tl.frame_at(250).lines, ("c",))
        # 超出总时长：停在最后一帧，不回卷
        self.assertEqual(tl.frame_at(999).lines, ("c",))
        self.assertEqual(tl.frame_index_at(999), 2)

    def test_negative_elapsed_no_crash(self):
        tl = self._make()
        self.assertEqual(tl.frame_at(-5).lines, ("a",))
        self.assertEqual(tl.frame_index_at(-100), 0)

    def test_frame_at_and_index_consistent(self):
        tl = self._make(loop=True)
        for e in (0, 1, 99, 100, 305, 1000, -10):
            with self.subTest(elapsed=e):
                self.assertEqual(
                    tl.frame_at(e), tl._frames[tl.frame_index_at(e)]
                )

    def test_empty_timeline_rejected(self):
        with self.assertRaises(ValueError):
            Timeline([])


class TestPresetTimelines(unittest.TestCase):
    def test_spinner_each_frame_120ms(self):
        self.assertEqual(len(SPINNER._frames), 4)
        for f in SPINNER._frames:
            self.assertEqual(f.duration_ms, 120)
        self.assertEqual(SPINNER.total_ms(), 480)

    def test_spinner_is_thinking_guy(self):
        # SPINNER 每帧应是 thinking 的 Guy 表情
        from workguy.tui.art import render_guy

        for i, f in enumerate(SPINNER._frames):
            self.assertEqual(list(f.lines), render_guy("thinking", i))

    def test_tool_pulse_exists(self):
        self.assertIsInstance(TOOL_PULSE, Timeline)
        self.assertGreater(len(TOOL_PULSE._frames), 0)
        for f in TOOL_PULSE._frames:
            self.assertIn(f.lines[0], ("░", "▒", "▓", "█"))

    def test_splash_total_about_600ms(self):
        frames = splash_frames()
        total = sum(f.duration_ms for f in frames)
        # IP 规定 ~600ms：给合理区间断言
        self.assertGreaterEqual(total, 550)
        self.assertLessEqual(total, 650)
        self.assertEqual(total, 600)

    def test_splash_progressive_reveal(self):
        frames = splash_frames()
        n = len(frames)
        # 第 k 帧（0-based）应已显现 k+1 行 Logo
        for k, f in enumerate(frames):
            shown = [ln for ln in f.lines if ln.strip()]
            self.assertEqual(len(shown), k + 1)
        # 最后一帧显现全部 Logo 行
        self.assertEqual(len([ln for ln in frames[-1].lines if ln.strip()]), n)

    def test_splash_frames_are_frame_objects(self):
        for f in splash_frames():
            self.assertIsInstance(f, Frame)


class TestShouldAnimate(unittest.TestCase):
    def test_non_tty_false(self):
        self.assertFalse(should_animate(False))

    def test_tty_true(self):
        self.assertTrue(should_animate(True))

    def test_no_animation_flag_false(self):
        self.assertFalse(should_animate(True, no_animation_flag=True))
        self.assertFalse(should_animate(False, no_animation_flag=True))

    def test_bool_coercion(self):
        self.assertFalse(should_animate(0))   # 0 视为非 TTY
        self.assertTrue(should_animate(1))    # 真值视为 TTY


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
