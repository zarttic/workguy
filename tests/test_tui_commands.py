"""commands 单元测试 —— 斜杠命令解析。

TDD：先写测试（全红）→ 实现 → 全绿。

规则（与实现锁定）：
- 必须以 ``/`` 作为**字符串首字符**才算命令；前后有空白（如 "  /x"）按非命令处理。
- 命令名取第一个 token（``/`` 之后到首个空白）；其余原文作为 args，仅去首尾空格，
  **内部连续空格保留**（如 "/agent  cli  extra" → args="cli  extra"）。
- 未知命令仍解析出 Command，由调用方比对 SUPPORTED 判断。
"""

from __future__ import annotations

import sys
import unittest

from workguy.tui.commands import Command, SUPPORTED, parse_command


class TestParseCommand(unittest.TestCase):
    def test_none_for_plain_text(self):
        self.assertIsNone(parse_command("hello"))
        self.assertIsNone(parse_command(""))

    def test_leading_whitespace_is_not_command(self):
        # 题面特例：首字符不是 '/'（前后带空白）→ 非命令
        self.assertIsNone(parse_command("  /x"))
        self.assertIsNone(parse_command("hello"))
        self.assertIsNone(parse_command(""))
        # 首字符是 '/'（即便尾部带空白）仍正常解析
        self.assertEqual(parse_command("/x  "), Command("x"))

    def test_simple_command(self):
        self.assertEqual(parse_command("/exit"), Command("exit"))
        self.assertEqual(parse_command("/help"), Command("help"))

    def test_command_with_args(self):
        self.assertEqual(parse_command("/agent cli"), Command("agent", "cli"))
        # 多个内空白保留原样（仅去首尾）
        self.assertEqual(
            parse_command("/agent  cli  extra"), Command("agent", "cli  extra")
        )

    def test_args_stripped_ends_only(self):
        self.assertEqual(parse_command("/clear   "), Command("clear"))
        self.assertEqual(parse_command("/audit  foo "), Command("audit", "foo"))

    def test_slash_only_is_none(self):
        self.assertIsNone(parse_command("/"))
        self.assertIsNone(parse_command("/   "))

    def test_unknown_command_parsed(self):
        # 未知命令仍返回 Command，由调用方判断不在 SUPPORTED
        cmd = parse_command("/nope")
        self.assertEqual(cmd, Command("nope"))
        self.assertNotIn(cmd.name, SUPPORTED)

    def test_supported_set_contents(self):
        self.assertEqual(
            SUPPORTED,
            frozenset({"exit", "quit", "clear", "skills", "audit", "agent", "help"}),
        )
        # 已知命令都在 SUPPORTED 中
        for name in ("exit", "quit", "clear", "skills", "audit", "agent", "help"):
            self.assertIn(parse_command("/" + name).name, SUPPORTED)

    def test_command_is_frozen(self):
        cmd = Command("exit")
        with self.assertRaises(Exception):
            cmd.name = "x"  # type: ignore[misc]


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
