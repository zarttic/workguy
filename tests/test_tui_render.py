"""render.py 纯函数层的单元测试（用托管解释器跑，render.py 不依赖 curses）。

核心断言：所有 compose 输出行宽 ≤ cols（防花屏的生命线）。本文件也顺带验证
``app.py`` 能在「curses 为 None」的托管解释器下被 import 而不报错。
"""

from __future__ import annotations

import importlib
import unittest

from workguy.tui.render import (
    PROMPT,
    RenderedLine,
    compose_context_meter,
    compose_hint_bar,
    compose_input,
    compose_splash,
    compose_status_bar,
    context_meter_style,
)
from workguy.tui.state import SessionState
from workguy.tui.textutil import display_width

# 遍历的 cols 集合：覆盖极窄到超宽
_COLS_SET = [5, 10, 20, 40, 80, 200]


class TestComposeStatusBar(unittest.TestCase):
    def test_width_le_cols(self):
        for cols in _COLS_SET:
            st = SessionState(agent="cli", model="LongCat-2.0")
            st.add_credits(12.34)
            rl = compose_status_bar(st, cols, context_ratio=0.5)
            self.assertLessEqual(display_width(rl.text), cols, f"cols={cols} 状态栏超宽: {rl.text!r}")
            self.assertEqual(rl.style, "brand")

    def test_tiny_cols_no_crash(self):
        st = SessionState(agent="cli", model="LongCat-2.0")
        for cols in (1, 2, 3, 4, 5):
            rl = compose_status_bar(st, cols, context_ratio=0.9)
            self.assertLessEqual(display_width(rl.text), cols)
            self.assertIsInstance(rl, RenderedLine)

    def test_keeps_tag_when_small(self):
        # cols 足够放下角标时，必须含一角标 ◤W◢
        st = SessionState(agent="cli", model="LongCat-2.0")
        rl = compose_status_bar(st, 10)
        self.assertIn("◤W◢", rl.text)
        # cols=5（角标 3 列 + 余 2）也应含角标
        rl5 = compose_status_bar(st, 5)
        self.assertIn("◤W◢", rl5.text)

    def test_degrade_drops_secondary(self):
        # 极窄下砍掉次要信息（model / credits），但仍保留角标
        st = SessionState(agent="cli", model="LongCat-2.0")
        st.add_credits(99.99)
        rl = compose_status_bar(st, 6)
        # 6 列放不下 agent 段(·cli=4) + 角标(3) + 空格 = 8，故 agent 被砍
        self.assertIn("◤W◢", rl.text)
        self.assertNotIn("LongCat", rl.text)
        self.assertNotIn("¢", rl.text)

    def test_thinking_spinner_fits(self):
        st = SessionState(agent="cli")
        rl = compose_status_bar(st, 20, mood="thinking", frame=2)
        self.assertLessEqual(display_width(rl.text), 20)
        # 思考态应带一个旋转指示（◐◓◑◒ 其一）
        self.assertTrue(any(c in rl.text for c in "◐◓◑◒"))


class TestComposeHintBar(unittest.TestCase):
    def test_width_le_cols(self):
        for cols in _COLS_SET:
            for busy in (False, True):
                rl = compose_hint_bar(cols, busy=busy)
                self.assertLessEqual(display_width(rl.text), cols, f"cols={cols} busy={busy} 超宽")
                self.assertEqual(rl.style, "warning" if busy else "dim")

    def test_tiny_cols(self):
        for cols in (1, 3, 5):
            rl = compose_hint_bar(cols, busy=True)
            self.assertLessEqual(display_width(rl.text), cols)


class TestComposeInput(unittest.TestCase):
    def test_width_le_cols(self):
        st = SessionState()
        for cols in _COLS_SET:
            buf = "你好world" * 20
            rl = compose_input(st, cols, buf, cursor=len(buf))
            self.assertLessEqual(display_width(rl.text), cols)

    def test_tiny_cols(self):
        st = SessionState()
        for cols in (1, 2, 3, 5):
            rl = compose_input(st, cols, "abcdefghij" * 5, cursor=7)
            self.assertLessEqual(display_width(rl.text), cols)

    def test_cursor_middle_scrolls(self):
        # 长缓冲 + 光标在中间 → 视口应卷动（出现左滚动标记），且不从 buffer 开头
        st = SessionState()
        buf = "x" * 50
        cols = 20  # 提示符 2 + 视口 18
        rl = compose_input(st, cols, buf, cursor=25)
        self.assertIn("…", rl.text)  # 卷动标记出现

    def test_cursor_end_shows_tail(self):
        st = SessionState()
        buf = "x" * 50
        cols = 20
        rl = compose_input(st, cols, buf, cursor=50)
        # 视口应以 buffer 末尾若干字符呈现（光标贴右），且最后字符可见
        self.assertTrue(rl.text.endswith("x") or "…" in rl.text)
        self.assertLessEqual(display_width(rl.text), cols)

    def test_short_buffer_no_scroll(self):
        st = SessionState()
        buf = "hi"
        rl = compose_input(st, 40, buf, cursor=2)
        self.assertNotIn("…", rl.text)
        self.assertTrue(rl.text.startswith(PROMPT))
        self.assertLessEqual(display_width(rl.text), 40)

    def test_cjk_cursor(self):
        # 中文宽字符下的光标滚动不应劈半字符、且不超宽
        st = SessionState()
        buf = "中" * 30
        for cols in (10, 20, 40):
            rl = compose_input(st, cols, buf, cursor=15)
            self.assertLessEqual(display_width(rl.text), cols)


class TestContextMeter(unittest.TestCase):
    def test_width_equals_param(self):
        for w in (0, 1, 3, 8, 20):
            for ratio in (0.0, 0.3, 0.5, 1.0):
                bar = compose_context_meter(ratio, w)
                self.assertEqual(display_width(bar), w, f"w={w} ratio={ratio} 宽不符: {bar!r}")

    def test_ratio_boundaries(self):
        self.assertEqual(compose_context_meter(0.0, 4), "░░░░")
        self.assertEqual(compose_context_meter(1.0, 4), "████")
        # 中间值：约一半填充
        bar = compose_context_meter(0.5, 10)
        self.assertEqual(bar.count("█"), 5)
        self.assertEqual(bar.count("░"), 5)

    def test_style_switch(self):
        # <0.4 → normal；0.4..0.7 → warning；>0.7 → error
        self.assertEqual(context_meter_style(0.0), "normal")
        self.assertEqual(context_meter_style(0.39), "normal")
        self.assertEqual(context_meter_style(0.40), "warning")
        self.assertEqual(context_meter_style(0.70), "warning")
        self.assertEqual(context_meter_style(0.71), "error")
        self.assertEqual(context_meter_style(1.0), "error")

    def test_meter_zero_width(self):
        self.assertEqual(compose_context_meter(0.5, 0), "")


class TestComposeSplash(unittest.TestCase):
    def test_reasonable_line_count_at_extremes(self):
        # progress=0 与 1 都应返回合理行数，且不崩
        for progress in (0.0, 1.0):
            lines = compose_splash(24, 80, progress)
            self.assertGreater(len(lines), 0)
            for rl in lines:
                self.assertLessEqual(display_width(rl.text), 80)

    def test_tiny_terminal_degrades(self):
        # 极小终端退回紧凑版（◤W◢ + 标语），行数很少且不超宽
        lines = compose_splash(3, 10, 0.5)
        self.assertTrue(any("◤W◢" in rl.text for rl in lines))
        for rl in lines:
            self.assertLessEqual(display_width(rl.text), 10)
        # 紧凑版行数应远小于完整版（完整版约 13 行）
        self.assertLess(len(lines), 6)

    def test_progress_clamps(self):
        # progress 越界被夹到 [0,1]
        for progress in (-1.0, 2.0):
            lines = compose_splash(30, 120, progress)
            self.assertGreater(len(lines), 0)


class TestAppImportable(unittest.TestCase):
    def test_app_imports_without_curses(self):
        # 关键：用托管解释器（curses=None）也能 import app.py，不抛错
        import workguy.tui.app as app_mod

        self.assertTrue(hasattr(app_mod, "TuiApp"))
        # importlib 重新加载也应成功（确保无模块级副作用）
        reloaded = importlib.reload(app_mod)
        self.assertTrue(hasattr(reloaded, "TuiApp"))


if __name__ == "__main__":
    unittest.main()
