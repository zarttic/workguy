"""六级阈值上下文压缩。

设计精髓（实测自 WorkBuddy Desktop 5.5.4 的 tokenUsageThresholds）：
摘要(0.15) 比压缩(0.40) 更早介入 —— 因为摘要更便宜。
不是等上下文爆了再救火，而是用最便宜的手段、在最早的时刻动手。

阈值来源见 config.TOKEN_THRESHOLDS / THRESHOLD_ACTIONS（契约文件，只读）。
"""

from __future__ import annotations

from typing import Callable

from .config import TOKEN_THRESHOLDS
from .types import ContextAction, Message

# 阈值 -> 动作的默认映射（与 config.THRESHOLD_ACTIONS 同序）。
# 硬约束：动作等级随阈值单调递增，不得回落——warning 档若配成 summarize
# 会让 0.60~0.70 区间的处理力度反而弱于 0.40 处，语义自相矛盾。
# 这里在运行期从 self.thresholds 取值，以支持构造期覆盖与 deepseek 放宽。
_ACTION_PAIRS_DEFAULT: tuple[tuple[str, str], ...] = (
    ("summary_emergency", "summarize"),
    ("compact_emergency", "compact"),
    ("input_warning", "compact"),
    ("input_critical", "compact"),
    ("input_emergency", "emergency"),
)


class ContextMonitor:
    """按 usage_ratio 决定上下文应采取的动作。

    骨架版 token 计数用近似估算（字符数/4 向上取整），并允许注入自定义计数器。
    """

    def __init__(self, max_tokens: int, thresholds: dict | None = None) -> None:
        self.max_tokens = max_tokens
        self.thresholds: dict = dict(TOKEN_THRESHOLDS)
        if thresholds:
            self.thresholds.update(thresholds)
        # 可注入的 token 计数器：Callable[[list[Message]], int]
        self.token_counter: Callable[[list[Message]], int] | None = None

    # -- token 计数 -------------------------------------------------------
    def count_tokens(self, messages: list[Message]) -> int:
        if self.token_counter is not None:
            return self.token_counter(messages)
        # 近似估算：字符数 / 4 向上取整
        return sum((len(m.content) + 3) // 4 for m in messages)

    def usage_ratio(self, messages: list[Message]) -> float:
        if self.max_tokens <= 0:
            return 1.0
        return min(1.0, self.count_tokens(messages) / self.max_tokens)

    # -- 阈值决策 ---------------------------------------------------------
    def _action_pairs(self, model_id: str | None) -> list[tuple[float, str]]:
        """构造 (threshold, action_name) 升序列表，含 deepseek 放宽。"""
        compact_key = "compact_emergency"
        compact_val = self.thresholds[compact_key]
        if model_id and "deepseek" in model_id.lower():
            compact_val = self.thresholds.get(
                "compact_emergency_deepseek",
                TOKEN_THRESHOLDS["compact_emergency_deepseek"],
            )
        pairs: list[tuple[float, str]] = []
        for key, action_name in _ACTION_PAIRS_DEFAULT:
            if key == compact_key:
                pairs.append((compact_val, action_name))
            else:
                pairs.append((self.thresholds[key], action_name))
        return pairs

    def actions_for_ratio(self, ratio: float, model_id: str | None = None) -> ContextAction:
        """纯函数：从低到高匹配阈值，返回最高触发档位的动作。低于最低阈值返回 NONE。"""
        action = ContextAction.NONE
        for thr, action_name in self._action_pairs(model_id):
            if ratio >= thr:
                action = ContextAction(action_name)
        return action

    def evaluate(self, messages: list[Message], model_id: str | None = None) -> ContextAction:
        return self.actions_for_ratio(self.usage_ratio(messages), model_id)

    def check_before_message(self, messages: list[Message]) -> ContextAction:
        """是否达到每发消息前的体检点 pre_message；达到返回对应动作，否则 NONE。"""
        ratio = self.usage_ratio(messages)
        pre = self.thresholds["pre_message"]
        if ratio >= pre:
            return self.actions_for_ratio(ratio)
        return ContextAction.NONE

    def request_exceeds_limit(self, tokens: int) -> bool:
        """单次请求 token 是否超过 request_emergency * max_tokens（严格 >）。"""
        limit = self.thresholds["request_emergency"] * self.max_tokens
        return tokens > limit
