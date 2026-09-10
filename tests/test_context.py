"""六级阈值上下文压缩的测试。

TDD 流程：先写测试（全红）→ 实现 → 全绿。
测试通过注入自定义 token 计数器来精确控制 usage_ratio，避免依赖估算误差。
"""

from __future__ import annotations

import unittest

from workguy.config import TOKEN_THRESHOLDS
from workguy.context import ContextMonitor
from workguy.types import ContextAction, Message


def monitor_with_ratio(ratio: float, max_tokens: int = 1000) -> ContextMonitor:
    """构造一个 usage_ratio 恒为 ratio 的监视器（通过注入计数器）。"""
    m = ContextMonitor(max_tokens)
    m.token_counter = lambda msgs: int(ratio * max_tokens)
    return m


class TestContextMonitor(unittest.TestCase):
    # 1. 空上下文 → NONE
    def test_empty_context_returns_none(self):
        m = ContextMonitor(max_tokens=1000)
        self.assertEqual(m.evaluate([]), ContextAction.NONE)
        self.assertEqual(m.usage_ratio([]), 0.0)
        self.assertEqual(m.count_tokens([]), 0)

    # 2. ratio 低于 0.15 → NONE
    def test_below_summary_threshold_returns_none(self):
        m = monitor_with_ratio(0.10)
        self.assertEqual(m.evaluate([]), ContextAction.NONE)

    # 3. ratio 到 0.15 但低于 0.40 → SUMMARIZE（摘要更早介入）
    def test_summary_threshold_returns_summarize(self):
        m = monitor_with_ratio(0.15)
        self.assertEqual(m.evaluate([]), ContextAction.SUMMARIZE)
        # 略高于 0.15、远低于 0.40 也仍是摘要
        m2 = monitor_with_ratio(0.30)
        self.assertEqual(m2.evaluate([]), ContextAction.SUMMARIZE)

    # 4. ratio 到 0.40 → COMPACT
    def test_compact_threshold_returns_compact(self):
        m = monitor_with_ratio(0.40)
        self.assertEqual(m.evaluate([]), ContextAction.COMPACT)

    # 5. deepseek 差异：0.45 时普通模型已 COMPACT，deepseek 仍为 SUMMARIZE
    def test_deepseek_relaxed_compact_threshold(self):
        normal = monitor_with_ratio(0.45)
        deepseek = monitor_with_ratio(0.45)
        self.assertEqual(normal.evaluate([], model_id="default"), ContextAction.COMPACT)
        self.assertEqual(
            deepseek.evaluate([], model_id="deepseek-v4-flash"),
            ContextAction.SUMMARIZE,
        )
        # 普通模型的 compact 在 0.40，deepseek 在 0.50：两边都到 0.55 时应都为 COMPACT
        normal2 = monitor_with_ratio(0.55)
        deepseek2 = monitor_with_ratio(0.55)
        self.assertEqual(normal2.evaluate([], model_id="default"), ContextAction.COMPACT)
        self.assertEqual(
            deepseek2.evaluate([], model_id="deepseek-v4-pro"),
            ContextAction.COMPACT,
        )

    # 6. ratio 到 0.90 → EMERGENCY
    def test_emergency_threshold_returns_emergency(self):
        m = monitor_with_ratio(0.90)
        self.assertEqual(m.evaluate([]), ContextAction.EMERGENCY)
        # 高于 0.90 仍强制处理（且 usage_ratio 被截断到 1.0）
        m2 = monitor_with_ratio(1.30)
        self.assertEqual(m2.usage_ratio([]), 1.0)
        self.assertEqual(m2.evaluate([]), ContextAction.EMERGENCY)

    # 7. check_before_message 在 pre_message(0.50) 前后行为不同
    def test_check_before_message_pre_message_checkpoint(self):
        below = monitor_with_ratio(0.49)
        at = monitor_with_ratio(0.50)
        # 0.62 触达 input_warning(0.60)。注意：warning 只是告警标记，
        # 动作等级不得回落，故仍为 compact（见 config.THRESHOLD_ACTIONS 注释）。
        above = monitor_with_ratio(0.62)
        crit = monitor_with_ratio(0.71)
        self.assertEqual(below.check_before_message([]), ContextAction.NONE)
        # 达到体检点即返回应采取的动作（compact 已触发）
        self.assertEqual(at.check_before_message([]), ContextAction.COMPACT)
        self.assertEqual(above.check_before_message([]), ContextAction.COMPACT)
        self.assertEqual(crit.check_before_message([]), ContextAction.COMPACT)
        # 动作等级随 ratio 单调不减——防止阈值表再次配出回落
        ratios = [0.0, 0.15, 0.40, 0.62, 0.71, 0.95]
        levels = [ContextAction.NONE, ContextAction.SUMMARIZE,
                  ContextAction.COMPACT, ContextAction.EMERGENCY]
        seen = [levels.index(monitor_with_ratio(r).actions_for_ratio(r)) for r in ratios]
        self.assertEqual(seen, sorted(seen))

    # 8. request_exceeds_limit 边界（request_emergency=0.90 * max_tokens）
    def test_request_exceeds_limit_boundary(self):
        m = ContextMonitor(max_tokens=1000)
        limit = TOKEN_THRESHOLDS["request_emergency"] * 1000  # 900
        self.assertFalse(m.request_exceeds_limit(899))
        self.assertFalse(m.request_exceeds_limit(900))  # 恰好相等不算超
        self.assertTrue(m.request_exceeds_limit(901))
        self.assertTrue(m.request_exceeds_limit(2000))

    # 9. 阈值恰好相等用 >= 触发
    def test_boundary_inclusive(self):
        # 各档位临界点都应触发（含 summary/compact/input_emergency）
        self.assertEqual(monitor_with_ratio(0.15).actions_for_ratio(0.15),
                         ContextAction.SUMMARIZE)
        self.assertEqual(monitor_with_ratio(0.40).actions_for_ratio(0.40),
                         ContextAction.COMPACT)
        self.assertEqual(monitor_with_ratio(0.70).actions_for_ratio(0.70),
                         ContextAction.COMPACT)
        self.assertEqual(monitor_with_ratio(0.90).actions_for_ratio(0.90),
                         ContextAction.EMERGENCY)
        # 略低于阈值不触发
        self.assertEqual(monitor_with_ratio(0.149).actions_for_ratio(0.149),
                         ContextAction.NONE)
        # check_before_message 恰达 0.50 触发
        self.assertEqual(monitor_with_ratio(0.50).check_before_message([]),
                         ContextAction.COMPACT)

    # 额外：默认估算器（字符数/4 向上取整）与注入计数器互不干扰
    def test_default_estimator_counts_chars(self):
        m = ContextMonitor(max_tokens=1000)
        msgs = [Message(role="user", content="0123456789")]  # 10 字符 -> ceil(10/4)=3
        self.assertEqual(m.count_tokens(msgs), 3)
        # 注入计数器优先级更高
        m.token_counter = lambda msgs: 123
        self.assertEqual(m.count_tokens(msgs), 123)


if __name__ == "__main__":
    unittest.main()
