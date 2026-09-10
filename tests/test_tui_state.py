"""state 单元测试 —— 会话状态机。

TDD：先写测试（全红）→ 实现 → 全绿。

本模块纯数据操作：不碰终端、不碰 kernel、不引入 curses。
- lines() 返回**已按换行拆分的逻辑行**列表（多行回复正确拆成多个 Line）。
  显示层的自动折行（wrap_text）由渲染层基于这些行再做；本层不折行。
- set_agent 校验代理存在（用 workguy.config.AGENTS），非法名拒绝。
- 输入历史有边界，不会越界；翻到最旧 / 最新边界返回 None。
"""

from __future__ import annotations

import sys
import unittest

from workguy.config import AGENTS
from workguy.tui.state import Line, Role, SessionState


class TestRolesAndLine(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(Role.USER.value, "user")
        self.assertEqual(Role.ASSISTANT.value, "assistant")
        self.assertEqual(Role.TOOL.value, "tool")
        self.assertEqual(Role.SYSTEM.value, "system")
        self.assertEqual(Role.ERROR.value, "error")

    def test_line_default_style(self):
        self.assertEqual(Line("hi").style, "normal")


class TestContent(unittest.TestCase):
    def test_add_various_styles(self):
        s = SessionState()
        s.add_user("u")
        s.add_assistant("a")
        s.add_tool_call("Bash", "ls")
        s.add_error("e")
        s.add_system("sys")
        styles = [ln.style for ln in s.lines()]
        self.assertEqual(
            styles,
            ["user", "assistant", "tool", "error", "system"],
        )
        # 工具行含工具名与细节
        tool_line = s.lines()[2]
        self.assertIn("Bash", tool_line.text)
        self.assertIn("ls", tool_line.text)

    def test_multiline_assistant_split(self):
        s = SessionState()
        s.add_assistant("line1\nline2\nline3")
        lines = s.lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([ln.text for ln in lines], ["line1", "line2", "line3"])
        self.assertTrue(all(ln.style == "assistant" for ln in lines))

    def test_user_multiline_split(self):
        s = SessionState()
        s.add_user("a\nb")
        self.assertEqual([ln.text for ln in s.lines()], ["a", "b"])

    def test_clear_resets_lines_only(self):
        s = SessionState()
        s.add_user("u")
        s.add_credits(5.0)
        s.incr_turns()
        s.push_input("hist")
        s.clear()
        self.assertEqual(s.lines(), [])
        # clear 不动累计指标与历史
        self.assertEqual(s.credits, 5.0)
        self.assertEqual(s.turns, 1)
        self.assertEqual(s.history_prev(), "hist")


class TestMetrics(unittest.TestCase):
    def test_credits_accumulate(self):
        s = SessionState()
        self.assertEqual(s.credits, 0.0)
        s.add_credits(1.5)
        s.add_credits(2.5)
        self.assertEqual(s.credits, 4.0)

    def test_turns_increment(self):
        s = SessionState()
        self.assertEqual(s.turns, 0)
        s.incr_turns()
        s.incr_turns()
        self.assertEqual(s.turns, 2)

    def test_agent_and_model(self):
        s = SessionState(agent="cli")
        self.assertEqual(s.agent, "cli")
        self.assertTrue(s.set_agent("Plan"))
        self.assertEqual(s.agent, "Plan")
        # 非法代理被拒，保持原值
        self.assertFalse(s.set_agent("not-a-real-agent"))
        self.assertEqual(s.agent, "Plan")
        s.set_model("default", tier="powerful")
        self.assertEqual(s.model, "default")
        self.assertEqual(s.tier, "powerful")

    def test_invalid_agent_in_constructor_falls_back(self):
        s = SessionState(agent="bogus")
        self.assertNotEqual(s.agent, "bogus")
        self.assertIn(s.agent, AGENTS)


class TestHistory(unittest.TestCase):
    def test_empty_history(self):
        s = SessionState()
        self.assertIsNone(s.history_prev())
        self.assertIsNone(s.history_next())

    def test_prev_walks_back(self):
        s = SessionState()
        s.push_input("a")
        s.push_input("b")
        # 初始在「新行」：prev 取最近一条
        self.assertEqual(s.history_prev(), "b")
        self.assertEqual(s.history_prev(), "a")
        # 到最旧边界返回 None
        self.assertIsNone(s.history_prev())

    def test_next_walks_forward(self):
        s = SessionState()
        s.push_input("a")
        s.push_input("b")
        s.history_prev()  # -> b
        s.history_prev()  # -> a
        self.assertEqual(s.history_next(), "b")
        # 回到新行边界返回 None
        self.assertIsNone(s.history_next())

    def test_next_from_initial_is_none(self):
        s = SessionState()
        s.push_input("a")
        self.assertIsNone(s.history_next())

    def test_no_overflow_index(self):
        s = SessionState()
        s.push_input("a")
        s.push_input("b")
        for _ in range(10):
            s.history_prev()
        # 不会越界到负
        self.assertIsNone(s.history_prev())
        for _ in range(10):
            s.history_next()
        self.assertIsNone(s.history_next())

    def test_empty_input_not_stored(self):
        s = SessionState()
        s.push_input("")
        s.push_input("   ")
        self.assertIsNone(s.history_prev())


class TestNoCursesDependency(unittest.TestCase):
    def test_curses_not_imported(self):
        self.assertNotIn("curses", sys.modules)


if __name__ == "__main__":
    unittest.main()
