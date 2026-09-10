"""theme 单元测试 —— 颜色、语义映射、文案、颜色对初始化降级。

TDD：先写测试（全红）→ 实现 → 全绿。

锁定约定（与 theme.py / IP.md 一致）：
- COLORS 的 256 色值：brand=51, accent=208, dim=240, success=46, warning=214, error=196, normal=-1
- init_color_pairs 三条降级链：>=256 用 256 值 / 8..255 用 8 基色 / 0 全部返回 0 且不调用 init_pair
- color_for 未知 style 回落 normal
- message_for 的 denied / error 是「同事口吻」：不道歉、不卖萌、给下一步
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui import theme as theme_mod
from workguy.tui.theme import (
    COLORS,
    PAIR_IDS,
    GREETING,
    THINKING,
    DONE,
    FAREWELL,
    TAGLINE,
    color_for,
    message_for,
)


class MockCurses:
    """可注入的 mock curses 模块：记录 init_pair 的调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def init_pair(self, pair_id: int, fg: int, bg: int) -> None:
        self.calls.append((pair_id, fg, bg))


class TestColorsAndConstants(unittest.TestCase):
    def test_color_values(self):
        self.assertEqual(COLORS["brand"], 51)
        self.assertEqual(COLORS["accent"], 208)
        self.assertEqual(COLORS["dim"], 240)
        self.assertEqual(COLORS["success"], 46)
        self.assertEqual(COLORS["warning"], 214)
        self.assertEqual(COLORS["error"], 196)
        self.assertEqual(COLORS["normal"], -1)

    def test_pair_ids_assigned(self):
        # 语义色名 -> pair_id，从 1 起，避开 curses 保留的 0 号
        self.assertEqual(PAIR_IDS["brand"], 1)
        self.assertEqual(PAIR_IDS["normal"], 7)
        self.assertEqual(set(PAIR_IDS.values()), {1, 2, 3, 4, 5, 6, 7})

    def test_language_constants(self):
        self.assertEqual(GREETING, "在。说吧。")
        self.assertEqual(THINKING, "思考中")
        self.assertEqual(DONE, "好了。")
        self.assertEqual(FAREWELL, "走了。")
        self.assertEqual(TAGLINE, "WorkGuy —— 你的工作搭子")


class TestColorFor(unittest.TestCase):
    def test_known_styles(self):
        self.assertEqual(color_for("normal"), "normal")
        self.assertEqual(color_for("user"), "accent")
        self.assertEqual(color_for("assistant"), "brand")
        self.assertEqual(color_for("tool"), "dim")
        self.assertEqual(color_for("error"), "error")
        self.assertEqual(color_for("dim"), "dim")
        self.assertEqual(color_for("accent"), "accent")
        self.assertEqual(color_for("system"), "dim")

    def test_unknown_style_falls_back_to_normal(self):
        self.assertEqual(color_for("weird-unknown"), "normal")
        self.assertEqual(color_for(""), "normal")
        self.assertEqual(color_for("ASSISTANT"), "normal")  # 大小写不匹配也回落


class TestMessageFor(unittest.TestCase):
    def test_known_messages(self):
        self.assertEqual(message_for("greeting"), GREETING)
        self.assertEqual(message_for("thinking"), THINKING)
        self.assertEqual(message_for("done"), DONE)
        self.assertEqual(message_for("farewell"), FAREWELL)

    def test_denied_tone_is_colleague_like(self):
        msg = message_for("denied")
        self.assertEqual(msg, "不行，compact 没有工具权限")
        # 不道歉不卖萌：不含这些词
        for banned in ("抱歉", "对不起", "不好意思", "～", "~", "🎉", "请原谅"):
            self.assertNotIn(banned, msg)

    def test_error_tone_is_colleague_like(self):
        msg = message_for("error")
        self.assertEqual(msg, "这个挂住了。要重试吗？")
        for banned in ("抱歉", "哎呀", "小问题", "～", "~", "🎉"):
            self.assertNotIn(banned, msg)

    def test_unknown_kind_falls_back(self):
        # 未知 kind 不崩，回落到某个固定文案
        self.assertIsInstance(message_for("nope"), str)
        self.assertTrue(message_for("nope"))


class TestInitColorPairs(unittest.TestCase):
    def test_no_color_mode_returns_zero_and_skips_init(self):
        mock = MockCurses()
        result = theme_mod.init_color_pairs(mock, 0)
        self.assertEqual(mock.calls, [])  # 不调用 init_pair
        self.assertEqual(set(result.values()), {0})
        for name in PAIR_IDS:
            self.assertEqual(result[name], 0)

    def test_256_color_mode_uses_256_values(self):
        mock = MockCurses()
        result = theme_mod.init_color_pairs(mock, 256)
        # 每个语义名都初始化了一对
        self.assertEqual(len(mock.calls), len(PAIR_IDS))
        for name in PAIR_IDS:
            pair_id = result[name]
            # 对应调用里 fg 使用 256 色值
            call = next(c for c in mock.calls if c[0] == pair_id)
            self.assertEqual(call[1], COLORS[name])
            self.assertEqual(call[2], 0)  # bg=0
            self.assertEqual(pair_id, PAIR_IDS[name])

    def test_8_color_mode_uses_base_colors(self):
        mock = MockCurses()
        result = theme_mod.init_color_pairs(mock, 8)
        self.assertEqual(len(mock.calls), len(PAIR_IDS))
        for name in PAIR_IDS:
            pair_id = result[name]
            call = next(c for c in mock.calls if c[0] == pair_id)
            fg = call[1]
            # 8 色模式：fg 必须落在 0-7 基础色范围
            self.assertGreaterEqual(fg, 0)
            self.assertLessEqual(fg, 7)
            # brand 在 8 色下应映射到青 (6)
            if name == "brand":
                self.assertEqual(fg, 6)
            if name == "error":
                self.assertEqual(fg, 1)
            if name == "success":
                self.assertEqual(fg, 2)

    def test_mid_color_count_still_8_mode(self):
        mock = MockCurses()
        result = theme_mod.init_color_pairs(mock, 100)
        self.assertEqual(len(mock.calls), len(PAIR_IDS))
        for name in PAIR_IDS:
            call = next(c for c in mock.calls if c[0] == result[name])
            self.assertGreaterEqual(call[1], 0)
            self.assertLessEqual(call[1], 7)

    def test_no_curses_imported_at_top(self):
        # 模块顶层不应导入真实 curses
        self.assertNotIn("curses", sys.modules or {})


if __name__ == "__main__":
    unittest.main()
