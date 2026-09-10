"""布局与滚动 —— 纯逻辑、零 curses 依赖。

根据终端尺寸算出状态栏 / 对话区 / 输入区 / 提示栏的行区间，并提供滚动偏移的
夹取与可见区间计算。渲染层只消费这里的结果，不自己算坐标。

区间约定（半开区间 [start, end)，含端点 start、不含 end）：
    状态栏      : status_row（单行，顶部索引 0）
    对话区      : [chat_start, chat_end)
    输入区      : [input_start, input_end)
    提示栏      : hint_row（单行，底部索引 rows-1）

自上而下排布：状态栏 → 对话区 → 输入区 → 提示栏。
极小终端下退化为：状态栏 + 提示栏占满，中间区域可能收缩为空（半开区间仍合法、
不重叠、不为负）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    """根据终端尺寸算出的各区域行区间（半开区间 [start, end)）。"""

    total_rows: int
    total_cols: int
    status_row: int        # 状态栏（1 行，顶部）
    chat_start: int        # 对话区起
    chat_end: int          # 对话区止（不含）
    input_start: int       # 输入区起
    input_end: int         # 输入区止（不含）
    hint_row: int          # 提示栏（单行，底部）


def compute_layout(rows: int, cols: int, input_lines: int = 1) -> Layout:
    """计算各区域行区间。小终端也要能工作（如 rows=6），不能出负数区间。"""
    rows = max(int(rows), 0)
    cols = max(int(cols), 0)
    input_lines = max(int(input_lines), 0)

    status_row = 0
    hint_row = rows - 1 if rows >= 1 else 0

    # 输入区贴着提示栏上方：半开区间 [input_start, hint_row)
    input_end = hint_row
    input_start = max(0, input_end - input_lines)
    # 输入区必须落在状态栏下方
    if input_start < status_row + 1:
        input_start = status_row + 1
    # 极小终端：上面约束可能让 input_start 越过 input_end，夹回
    if input_start > input_end:
        input_start = input_end

    # 对话区：状态栏下方到输入区上方 [chat_start, input_start)
    chat_start = status_row + 1 if rows >= 1 else 0
    chat_end = input_start
    if chat_end < chat_start:
        chat_end = chat_start

    return Layout(
        total_rows=rows,
        total_cols=cols,
        status_row=status_row,
        chat_start=chat_start,
        chat_end=chat_end,
        input_start=input_start,
        input_end=input_end,
        hint_row=hint_row,
    )


def visible_range(total_lines: int, height: int, offset: int) -> tuple[int, int]:
    """返回可显示的行区间 [start, end)，offset 是顶部已滚过的行数。"""
    total_lines = max(int(total_lines), 0)
    height = max(int(height), 0)
    if total_lines <= 0 or height <= 0:
        return (0, 0)
    offset = clamp_offset(offset, total_lines, height)
    start = offset
    end = min(total_lines, offset + height)
    return (start, end)


def clamp_offset(offset: int, total_lines: int, height: int) -> int:
    """把滚动偏移夹到合法范围 [0, max(0, total_lines - height)]。"""
    total_lines = max(int(total_lines), 0)
    height = max(int(height), 0)
    max_offset = max(0, total_lines - height)
    if offset < 0:
        return 0
    if offset > max_offset:
        return max_offset
    return offset
