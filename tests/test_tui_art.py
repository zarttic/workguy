"""art 单元测试 —— Logo 与 Guy 字符画。

TDD：先写测试（全红）→ 实现 → 全绿。

锁定约定（与 art.py / IP.md 一致）：
- Guy 五个 mood：idle / thinking(≥4帧) / working / done / stuck
- 每行显示宽度一致（用 textutil.display_width 逐行校验）— 防画面错乱
- 各 mood 尺寸一致（切换表情画面不跳）
- 只用块字符与 Box Drawing，绝不用 emoji
- Logo 行宽一致
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui import art
from workguy.tui.art import (
    GUY_MOODS,
    LOGO_COMPACT,
    LOGO_FULL,
    guy_height,
    guy_width,
    render_guy,
    render_logo,
)
from workguy.tui.textutil import display_width

# 允许的字符集合：空格 + 两种 Unicode 区块 + 少量语义符号 + 紧凑 Logo 的 W。
# 用于断言「绝不用 emoji」「只用块字符与 Box Drawing」。
_BLOCK = range(0x2580, 0x259F + 1)      # Block Elements
_BOX = range(0x2500, 0x257F + 1)        # Box Drawing
_EXTRA = {
    "⌒", "×", "…", "W", "◤", "◢",  # 弯月眼 / 卡住叉 / 省略号 / 紧凑角标
    "●", "◐", "◓", "◑", "◒",  # 眼睛本体与「转圈」四相
    "▪", "^", "·",  # 专注方眼 / 笑眼 / 思考省略点
}  # 全部是宽度 1 的普通符号，不是 emoji


def _is_allowed(ch: str) -> bool:
    if ch == " ":
        return True
    o = ord(ch)
    if o in _BLOCK or o in _BOX:
        return True
    return ch in _EXTRA


class TestGuyMoodsPresent(unittest.TestCase):
    def test_required_moods_exist(self):
        for mood in ("idle", "thinking", "working", "done", "stuck"):
            self.assertIn(mood, GUY_MOODS)

    def test_thinking_has_at_least_4_frames(self):
        self.assertGreaterEqual(len(GUY_MOODS["thinking"]), 4)

    def test_no_emoji_only_block_box(self):
        for mood, frames in GUY_MOODS.items():
            for frame in frames:
                for line in frame:
                    for ch in line:
                        self.assertTrue(
                            _is_allowed(ch),
                            f"mood={mood} 出现非法字符 {ch!r}（非块/Box 字符）",
                        )


class TestGuyWidthConsistency(unittest.TestCase):
    def test_each_frame_rows_equal_width(self):
        # 题面关键：Guy 各 mood 的每行显示宽度一致（逐行校验）
        for mood, frames in GUY_MOODS.items():
            for frame in frames:
                widths = [display_width(line) for line in frame]
                self.assertEqual(
                    len(set(widths)), 1,
                    f"mood={mood} 某帧行宽不一致: {widths}",
                )

    def test_all_moods_same_dimensions(self):
        # 题面关键：Guy 各 mood 尺寸一致（切换表情画面不跳）
        dims = {(guy_height(m), guy_width(m)) for m in GUY_MOODS}
        self.assertEqual(len(dims), 1, f"各 mood 尺寸不一致: {dims}")
        # 具体锁定为 7 行 × 12 列
        self.assertEqual(dims.pop(), (7, 12))

    def test_thinking_frames_differ(self):
        frames = GUY_MOODS["thinking"]
        self.assertEqual(len({f for f in frames}), len(frames))


class TestRenderGuy(unittest.TestCase):
    def test_frame_modulo_never_overflow(self):
        for mood in GUY_MOODS:
            n = len(GUY_MOODS[mood])
            for big in (0, 1, n, n + 1, 100, 1000):
                lines = render_guy(mood, big)
                self.assertEqual(lines, render_guy(mood, big % n))

    def test_negative_frame_safe(self):
        # 负数取模不应崩，等价于对应正帧
        for mood in GUY_MOODS:
            n = len(GUY_MOODS[mood])
            self.assertEqual(render_guy(mood, -1), render_guy(mood, n - 1))

    def test_unknown_mood_falls_back_to_idle(self):
        self.assertEqual(render_guy("nonexistent"), render_guy("idle"))
        self.assertEqual(render_guy("NONE"), render_guy("idle"))

    def test_render_guy_returns_list_of_str(self):
        out = render_guy("idle", 0)
        self.assertIsInstance(out, list)
        self.assertTrue(all(isinstance(s, str) for s in out))

    def test_guy_width_height(self):
        self.assertEqual(guy_width("idle"), 12)
        self.assertEqual(guy_height("idle"), 7)
        self.assertEqual(guy_width("stuck"), 12)
        self.assertEqual(guy_height("thinking", 2), 7)

    def test_stuck_body_sinks(self):
        # 卡住：天线蔫了（顶部整行留白，视觉上像低头），双眼为 ×
        # 注意：这里断言「语义」而非具体行号——行号属于布局细节，
        # 布局调整不该让测试变红（本项目 v1 就踩过绑定行号的坑）。
        lines = render_guy("stuck")
        self.assertTrue(
            all(ch == " " for ch in lines[0]), "顶部应留白（天线蔫了）"
        )
        joined = "\n".join(lines)
        self.assertIn("×", joined, "双眼应为 ×")
        self.assertEqual(joined.count("×"), 2, "应恰好两只 × 眼")


class TestLogo(unittest.TestCase):
    def test_compact_logo(self):
        self.assertEqual(LOGO_COMPACT, "◤W◢")
        # ◤ W ◢ 共 3 个字符，每个显示宽度 1
        self.assertEqual(display_width(LOGO_COMPACT), 3)

    def test_render_logo_full(self):
        self.assertEqual(render_logo(), list(LOGO_FULL))
        self.assertEqual(render_logo(compact=False), list(LOGO_FULL))

    def test_render_logo_compact(self):
        self.assertEqual(render_logo(compact=True), ["◤W◢"])

    def test_logo_lines_equal_width(self):
        # 题面关键：Logo 行宽一致
        widths = [display_width(line) for line in LOGO_FULL]
        self.assertEqual(len(set(widths)), 1, f"Logo 行宽不一致: {widths}")
        self.assertGreater(widths[0], 0)

    def test_logo_is_block_drawing(self):
        for line in LOGO_FULL:
            for ch in line:
                if ch == " ":
                    continue
                o = ord(ch)
                self.assertTrue(
                    o in _BOX or o in _EXTRA or o in _BLOCK,
                    f"Logo 出现非 Box/块字符 {ch!r}",
                )


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
