"""成本路由与计费。

设计思想（照 WorkBuddy Desktop 5.5.4 的设计，不含原实现）：
- 高频、低难度任务全部走 lite 模型，配合 0.06x~5.00x 的倍率差省下可观成本。
- 4 个代理绑定 lite 档（memorySelector / autoModeClassifier / promptHookEvaluator /
  Explore），其余按 POWERFUL/AUTO 走 default。
- requires_tools=True 时硬性排除 supports_tools=False 的模型（如图像模型），
  哪怕其倍率再合适也不能跑工具。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import MODELS
from .types import AgentSpec, ModelSpec, ModelTier, RouterDecision


@dataclass
class _Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    credits: float = 0.0


class ModelRouter:
    """按代理档位选模型，并累计计费。"""

    def __init__(
        self,
        models: dict[str, ModelSpec] | None = None,
        default_model_id: str = "default",
    ) -> None:
        self.models: dict[str, ModelSpec] = dict(models if models is not None else MODELS)
        if default_model_id not in self.models:
            raise ValueError(f"default_model_id {default_model_id!r} 不在 models 之中")
        self.default_model_id: str = default_model_id
        self._usage: dict[str, _Usage] = {}

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def route(self, agent: AgentSpec, requires_tools: bool = False) -> RouterDecision:
        candidates = list(self.models.values())
        if requires_tools:
            candidates = [m for m in candidates if m.supports_tools]

        if agent.model_tier == ModelTier.LITE:
            lite_models = [m for m in candidates if m.tier == ModelTier.LITE]
            if lite_models:
                # 优先名为 'lite' 的模型；否则取倍率最低者
                chosen = next((m for m in lite_models if m.id == "lite"), None)
                if chosen is None:
                    chosen = min(lite_models, key=lambda m: m.credits_multiplier)
                    reason = (
                        f"LITE tier 且没有名为 'lite' 的模型，"
                        f"退回倍率最低的 LITE 模型 {chosen.id}"
                        f"（multiplier={chosen.credits_multiplier}）"
                    )
                else:
                    reason = (
                        f"LITE tier 代理，优先选择显式 'lite' 模型"
                        f"（multiplier={chosen.credits_multiplier}，全场最低档之一）"
                    )
                return RouterDecision(model=chosen, reason=reason)

        # POWERFUL / AUTO / LITE 无可用候选 → 走 default
        chosen = self.models[self.default_model_id]
        if agent.model_tier == ModelTier.POWERFUL:
            reason = (
                f"POWERFUL tier 代理，路由到 default"
                f"（multiplier={chosen.credits_multiplier}）"
            )
        elif agent.model_tier == ModelTier.AUTO:
            reason = (
                f"AUTO tier 代理，默认路由到 default"
                f"（multiplier={chosen.credits_multiplier}）"
            )
        else:  # LITE 但无 lite 候选（例如 requires_tools 把所有 lite 都过滤掉了）
            reason = (
                f"LITE tier 但无可用 LITE 候选（requires_tools="
                f"{requires_tools}），退回 default"
                f"（multiplier={chosen.credits_multiplier}）"
            )
        if requires_tools:
            reason += " [requires_tools=True，已排除不支持工具的模型]"
        return RouterDecision(model=chosen, reason=reason)

    # ------------------------------------------------------------------
    # 计费
    # ------------------------------------------------------------------
    def charge(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        if model_id not in self.models:
            raise ValueError(f"未知 model_id {model_id!r}")
        multiplier = self.models[model_id].credits_multiplier
        cost = round((input_tokens + output_tokens) / 1000.0 * multiplier, 6)
        entry = self._usage.setdefault(model_id, _Usage())
        entry.tokens_in += input_tokens
        entry.tokens_out += output_tokens
        entry.credits = round(entry.credits + cost, 6)
        return cost

    @property
    def total_credits(self) -> float:
        return round(sum(u.credits for u in self._usage.values()), 6)

    def usage_report(self) -> dict[str, dict[str, float]]:
        return {
            mid: {
                "tokens_in": float(u.tokens_in),
                "tokens_out": float(u.tokens_out),
                "credits": u.credits,
            }
            for mid, u in self._usage.items()
        }

    def reset_usage(self) -> None:
        self._usage.clear()
