"""textutil 单元测试 —— 终端文本宽度 / 折行 / 截断 / 补齐。

TDD 流程：先写测试（全红）→ 实现 → 全绿。

本层纯逻辑、零 curses 依赖，可在不依赖终端的托管解释器上跑。

关键不变量：
- 显示宽度按「列」算，不是字符数；CJK/全角=2 列，ASCII=1 列。
- 宽字符永不被劈成两半（折行 / 截断都以整字符为单位）。
- 截断后总宽度「不超过」给定 width。
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui.textutil import (
    char_width,
    display_width,
    pad_to,
    truncate,
    wrap_text,
)


class TestCharWidth(unittest.TestCase):
    def test_ascii_is_1(self):
        self.assertEqual(char_width("a"), 1)
        self.assertEqual(char_width("Z"), 1)
        self.assertEqual(char_width(" "), 1)

    def test_cjk_is_2(self):
        self.assertEqual(char_width("中"), 2)
        self.assertEqual(char_width("文"), 2)

    def test_fullwidth_is_2(self):
        # 全角标点也算 2 列
        self.assertEqual(char_width("，"), 2)
        self.assertEqual(char_width("！"), 2)

    def test_combining_is_0(self):
        # 组合字符（声调符）不占列
        self.assertEqual(char_width("\u0301"), 0)  # COMBINING ACUTE ACCENT

    def test_zero_width_controls(self):
        self.assertEqual(char_width("\u200b"), 0)  # ZWSP
        self.assertEqual(char_width("\u200d"), 0)  # ZWJ
        self.assertEqual(char_width("\u200c"), 0)  # ZWNJ
        self.assertEqual(char_width("\ufe0f"), 0)  # 变体选择符-16
        self.assertEqual(char_width("\ufeff"), 0)  # BOM（注意：U+FEFF，非 U+FFEF）

    def test_tab_and_newline_are_0_width(self):
        # 控制字符按 0 列处理（简化模型，渲染层可再自行展开）
        self.assertEqual(char_width("\t"), 0)
        self.assertEqual(char_width("\n"), 0)
        self.assertEqual(char_width("\r"), 0)

    def test_emoji_is_2(self):
        self.assertEqual(char_width("😀"), 2)
        self.assertEqual(char_width("✅"), 2)


class TestDisplayWidth(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(display_width(""), 0)

    def test_pure_ascii(self):
        self.assertEqual(display_width("ab"), 2)
        self.assertEqual(display_width("hello"), 5)

    def test_pure_cjk(self):
        self.assertEqual(display_width("中文"), 4)
        self.assertEqual(display_width("中文字"), 6)

    def test_mixed(self):
        # 中(2)+a(1)+文(2) = 5
        self.assertEqual(display_width("中a文"), 5)
        # 中文(4) + abc(3) = 7
        self.assertEqual(display_width("中文abc"), 7)

    def test_zero_width_ignored(self):
        # 组合符不计入
        self.assertEqual(display_width("e\u0301"), 1)
        # ZWJ 不计入
        self.assertEqual(display_width("a\u200db"), 2)

    def test_embedded_newline(self):
        # 换行按 0 列，故跨行串宽度 = 两段之和
        self.assertEqual(display_width("ab\ncd"), 4)


class TestWrapText(unittest.TestCase):
    def test_respect_existing_newline(self):
        self.assertEqual(wrap_text("a\nb", 10), ["a", "b"])

    def test_cjk_exact_columns(self):
        # 每行 4 列 → 恰好 2 个中文（每个 2 列）
        self.assertEqual(wrap_text("中文中文中文", 4), ["中文", "中文", "中文"])

    def test_cjk_mixed_break(self):
        # 宽度 3：中(2) 落第一行，文(2) 放不下 → 第二行，绝不劈字
        self.assertEqual(wrap_text("中文", 3), ["中", "文"])

    def test_wide_char_not_split(self):
        # 单个中文在 width=3 下是整字返回（不出现半个）
        self.assertEqual(wrap_text("中", 3), ["中"])
        # 即便 width 小于单字宽度，也整体落一行（宁可超宽也不劈半）
        self.assertEqual(wrap_text("中", 1), ["中"])
        out = wrap_text("中文", 1)
        self.assertEqual(out, ["中", "文"])

    def test_overlong_single_word(self):
        # 一个字就比 width 宽：独占一行，不崩
        self.assertEqual(wrap_text("中", 0), ["中"])
        self.assertEqual(wrap_text("中文", 0), ["中", "文"])

    def test_ascii_wrap(self):
        self.assertEqual(wrap_text("hello world", 5), ["hello", "world"])
        self.assertEqual(wrap_text("abcdef", 3), ["abc", "def"])

    def test_indent_on_continuation_only(self):
        # indent 只加在续行（首行不加）；续行可用宽度 = 4 - indent(2) = 2 列，
        # 故每个续行只装 1 个中文（2 列）。
        out = wrap_text("中文中文中文中文", 4, indent="> ")
        self.assertEqual(
            out,
            ["中文", "> 中", "> 文", "> 中", "> 文", "> 中", "> 文"],
        )
        # 首行无 indent，续行有
        self.assertFalse(out[0].startswith("> "))
        self.assertTrue(out[1].startswith("> "))

    def test_empty_input(self):
        self.assertEqual(wrap_text("", 10), [""])
        self.assertEqual(wrap_text("", 0), [""])

    def test_empty_paragraph_preserved(self):
        # 连续换行保留空行
        self.assertEqual(wrap_text("a\n\nb", 10), ["a", "", "b"])

    def test_tab_does_not_crash(self):
        # 含 tab：按 0 列计入，整体在一行内（不崩、行为确定）
        out = wrap_text("a\tb", 10)
        self.assertEqual(out, ["a\tb"])


class TestTruncate(unittest.TestCase):
    def test_no_truncation_needed(self):
        self.assertEqual(truncate("abc", 5), "abc")
        self.assertEqual(truncate("中文", 4), "中文")

    def test_truncated_width_not_exceed(self):
        # 核心不变量：截断后总宽度 ≤ width
        for text, width in [
            ("中文中文中文", 4),
            ("hello world", 5),
            ("abcdef", 3),
            ("中a文b", 3),
            ("x" * 100, 10),
        ]:
            out = truncate(text, width)
            self.assertLessEqual(
                display_width(out), width,
                f"truncate({text!r}, {width}) 超宽: {out!r} -> {display_width(out)}",
            )

    def test_ellipsis_appended(self):
        out = truncate("hello world", 5)
        self.assertTrue(out.endswith("…"))
        # 含省略号后宽度仍 ≤ 5
        self.assertLessEqual(display_width(out), 5)

    def test_wide_char_not_split_in_truncate(self):
        # 截断中文字符串，不能出现半个字
        out = truncate("中文中文", 3)
        # 省略号 1 列，剩 2 列放 1 个中文
        self.assertEqual(out, "中…")
        self.assertLessEqual(display_width(out), 3)

    def test_width_zero(self):
        self.assertEqual(truncate("abc", 0), "")
        self.assertEqual(truncate("中文", 0), "")

    def test_width_smaller_than_ellipsis(self):
        # 给定宽度连省略号都放不下：返回能放下的前缀（无省略号），仍不超宽
        out = truncate("abcdef", 1)
        self.assertLessEqual(display_width(out), 1)

    def test_custom_ellipsis(self):
        out = truncate("hello world", 5, ellipsis="...")
        self.assertEqual(display_width(out), 5)
        self.assertTrue(out.endswith("..."))


class TestPadTo(unittest.TestCase):
    def test_left(self):
        self.assertEqual(pad_to("ab", 5, "left"), "ab   ")
        self.assertEqual(display_width(pad_to("ab", 5, "left")), 5)

    def test_right(self):
        self.assertEqual(pad_to("ab", 5, "right"), "   ab")
        self.assertEqual(display_width(pad_to("ab", 5, "right")), 5)

    def test_center(self):
        padded = pad_to("ab", 6, "center")
        self.assertEqual(display_width(padded), 6)
        self.assertTrue(padded.startswith("  "))
        self.assertTrue(padded.endswith("  "))

    def test_cjk_align(self):
        # 中文 2 列，补齐按列算
        self.assertEqual(display_width(pad_to("中", 4, "left")), 4)
        self.assertTrue(pad_to("中", 4, "left").endswith("  "))

    def test_already_wide(self):
        # 文本已达/超宽：原样返回
        self.assertEqual(pad_to("abcdef", 4, "left"), "abcdef")
        self.assertEqual(pad_to("中文", 4, "left"), "中文")


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        # 本模块不得引入 curses
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
