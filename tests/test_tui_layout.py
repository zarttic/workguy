"""layout 单元测试 —— 布局区间与滚动偏移。

TDD：先写测试（全红）→ 实现 → 全绿。

约定（与实现锁定）：
- Layout 各区域用**半开区间** [start, end) 表示；单行锚点（status_row / hint_row）
  是行索引。
- 布局自上而下：状态栏(顶,1 行) → 对话区 → 输入区(底部) → 提示栏(底,1 行)。
- 小终端下必须保证：所有字段 ∈ [0, rows]、区间半开合法、相邻区域不重叠。
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui.layout import (
    Layout,
    clamp_offset,
    compute_layout,
    visible_range,
)


class TestComputeLayout(unittest.TestCase):
    def test_normal_terminal(self):
        lay = compute_layout(24, 80, input_lines=3)
        self.assertEqual(lay.total_rows, 24)
        self.assertEqual(lay.total_cols, 80)
        self.assertEqual(lay.status_row, 0)
        self.assertEqual(lay.hint_row, 23)
        # 状态栏下方开始对话
        self.assertEqual(lay.chat_start, 1)
        # 输入区在提示栏上方
        self.assertEqual(lay.input_end, 23)
        self.assertEqual(lay.input_start, 20)
        # 区间顺序与半开合法
        self.assertLessEqual(lay.chat_start, lay.chat_end)
        self.assertLessEqual(lay.chat_end, lay.input_start)
        self.assertLessEqual(lay.input_start, lay.input_end)
        self.assertLessEqual(lay.input_end, lay.hint_row)

    def test_small_terminal_rows_6(self):
        # 题面点名的极小终端：必须能工作、不重叠、不为负
        lay = compute_layout(6, 40, input_lines=1)
        self.assertEqual(lay.status_row, 0)
        self.assertEqual(lay.hint_row, 5)
        self.assertEqual(lay.chat_start, 1)
        self.assertEqual(lay.chat_end, 4)        # 对话 3 行
        self.assertEqual(lay.input_start, 4)
        self.assertEqual(lay.input_end, 5)        # 输入 1 行
        self.assertGreaterEqual(lay.chat_start, 0)
        self.assertLessEqual(lay.input_end, lay.total_rows)
        # 不重叠
        self.assertLessEqual(lay.chat_end, lay.input_start)
        self.assertLessEqual(lay.input_start, lay.input_end)
        self.assertLessEqual(lay.input_end, lay.hint_row)

    def test_regions_no_overlap_and_nonnegative(self):
        for rows in range(1, 60):
            with self.subTest(rows=rows):
                lay = compute_layout(rows, 80, input_lines=2)
                for name in ("status_row", "chat_start", "chat_end",
                             "input_start", "input_end", "hint_row"):
                    val = getattr(lay, name)
                    self.assertGreaterEqual(val, 0, f"{name}={val} 为负")
                    self.assertLessEqual(val, rows, f"{name}={val} 超出 rows")
                # 各 band 自身半开合法（不依赖终端大小）
                self.assertLessEqual(lay.chat_start, lay.chat_end)
                self.assertLessEqual(lay.input_start, lay.input_end)
        # 区域间不重叠只在 rows>=2 保证（rows=1 退化为单行使跨 band 顺序无意义）
        for rows in range(2, 60):
            with self.subTest(rows=rows):
                lay = compute_layout(rows, 80, input_lines=2)
                self.assertLessEqual(lay.chat_start, lay.chat_end)
                self.assertLessEqual(lay.chat_end, lay.input_start)
                self.assertLessEqual(lay.input_start, lay.input_end)
                self.assertLessEqual(lay.input_end, lay.hint_row)

    def test_input_lines_variation(self):
        lay1 = compute_layout(20, 80, input_lines=1)
        lay3 = compute_layout(20, 80, input_lines=3)
        self.assertEqual(lay3.input_end - lay3.input_start, 3)
        self.assertEqual(lay1.input_end - lay1.input_start, 1)
        # 输入区越高，对话区越小
        self.assertLess(lay3.chat_end - lay3.chat_start,
                        lay1.chat_end - lay1.chat_start)

    def test_cols_zero_safe(self):
        lay = compute_layout(10, 0)
        self.assertEqual(lay.total_cols, 0)
        self.assertGreaterEqual(lay.status_row, 0)

    def test_layout_is_frozen(self):
        lay = compute_layout(24, 80)
        with self.assertRaises(Exception):
            lay.status_row = 99  # type: ignore[misc]


class TestVisibleRange(unittest.TestCase):
    def test_full_screen_fits(self):
        self.assertEqual(visible_range(5, 10, 0), (0, 5))

    def test_scrolled(self):
        self.assertEqual(visible_range(20, 10, 5), (5, 15))

    def test_clamps_to_end(self):
        # offset 过大时，end 不超过 total_lines
        self.assertEqual(visible_range(20, 10, 100), (10, 20))

    def test_total_lines_zero(self):
        self.assertEqual(visible_range(0, 10, 0), (0, 0))

    def test_height_zero(self):
        # height=0 不崩，返回空区间
        self.assertEqual(visible_range(20, 0, 0), (0, 0))

    def test_negative_offset_clamped(self):
        self.assertEqual(visible_range(20, 10, -5), (0, 10))


class TestClampOffset(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(clamp_offset(5, 20, 10), 5)

    def test_negative(self):
        self.assertEqual(clamp_offset(-3, 20, 10), 0)

    def test_overflow(self):
        self.assertEqual(clamp_offset(100, 20, 10), 10)  # max = 20-10

    def test_content_less_than_screen(self):
        # 内容不足一屏：offset 恒为 0
        self.assertEqual(clamp_offset(0, 3, 10), 0)
        self.assertEqual(clamp_offset(5, 3, 10), 0)

    def test_total_lines_zero(self):
        self.assertEqual(clamp_offset(0, 0, 10), 0)
        self.assertEqual(clamp_offset(5, 0, 10), 0)

    def test_height_zero(self):
        # 不崩：max = max(0, total_lines - 0) = total_lines；
        # 合法偏移原样返回，越界夹到 total_lines
        self.assertEqual(clamp_offset(2, 5, 0), 2)
        self.assertEqual(clamp_offset(100, 5, 0), 5)

    def test_exact_fit(self):
        # total_lines == height → 只能 offset 0
        self.assertEqual(clamp_offset(0, 10, 10), 0)
        self.assertEqual(clamp_offset(3, 10, 10), 0)


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
