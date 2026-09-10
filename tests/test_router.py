"""ModelRouter 测试：lite 成本路由与计费。

TDD 顺序：先写本文件 → 确认失败 → 实现 workguy/router.py → 跑全绿。
"""

from __future__ import annotations

import unittest

from workguy.config import MODELS, AGENTS
from workguy.router import ModelRouter
from workguy.types import AgentSpec, ModelTier, ModelSpec


def _agent(name: str, tier: ModelTier, tools: tuple[str, ...] = ()) -> AgentSpec:
    return AgentSpec(name=name, description=name, tools=tools, model_tier=tier)


class LiteRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")

    def test_lite_tier_routes_to_lite_model(self):
        """LITE tier 代理应路由到 lite 模型，且倍率极低。"""
        # 用真实的 lite 代理（如 memorySelector）做端到端验证
        decision = self.router.route(AGENTS["memorySelector"])
        self.assertEqual(decision.model.id, "lite")
        # lite 倍率 0.05，是全场最低档之一
        self.assertLess(decision.model.credits_multiplier, 0.1)
        self.assertIn("LITE", decision.reason.upper())

    def test_lite_tier_lowest_multiplier_when_no_explicit_lite(self):
        """当字典里没有名为 lite 的模型时，应退而选倍率最低的 LITE 模型。"""
        models = {k: v for k, v in MODELS.items() if k != "lite"}
        router = ModelRouter(models=models, default_model_id="default")
        decision = router.route(_agent("x", ModelTier.LITE))
        self.assertEqual(decision.model.id, "deepseek-v4-flash")
        self.assertEqual(decision.model.credits_multiplier, 0.06)


class PowerfulAutoRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")

    def test_powerful_tier_routes_to_default(self):
        decision = self.router.route(_agent("cli", ModelTier.POWERFUL, ("Read",)))
        self.assertEqual(decision.model.id, "default")
        self.assertIn("default", decision.reason.lower())

    def test_auto_tier_routes_to_default(self):
        decision = self.router.route(AGENTS["compact"])  # model_tier == AUTO
        self.assertEqual(decision.model.id, "default")


class RequiresToolsExclusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")

    def test_never_returns_supports_tools_false(self):
        """requires_tools=True 时，绝不返回 supports_tools=False 的模型。"""
        # 遍历所有已知不支持工具的模型，断言它们永远不会被选中
        no_tool_ids = [
            mid for mid, m in MODELS.items() if not m.supports_tools
        ]
        self.assertTrue(no_tool_ids, "config 里应至少存在一个 supports_tools=False 的模型")
        for agent in AGENTS.values():
            decision = self.router.route(agent, requires_tools=True)
            self.assertTrue(
                decision.model.supports_tools,
                f"requires_tools=True 却返回了不支持工具的 {decision.model.id}",
            )
            self.assertNotIn(decision.model.id, no_tool_ids)

    def test_image_model_excluded_when_tools_required(self):
        """hunyuan-image-v3.0-art 是图像模型，requires_tools 时不能被选。"""
        router = ModelRouter(models=dict(MODELS), default_model_id="default")
        # 即便只有图像模型 + default 在候选中，也应排除图像模型
        decision = router.route(_agent("cli", ModelTier.POWERFUL, ("Read",)), requires_tools=True)
        self.assertNotEqual(decision.model.id, "hunyuan-image-v3.0-art")
        self.assertTrue(decision.model.supports_tools)


class ChargeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")

    def test_charge_default_formula(self):
        """charge('default', 1000, 1000) == (2000/1000)*2.00 == 4.0。"""
        cost = self.router.charge("default", 1000, 1000)
        self.assertAlmostEqual(cost, 4.0, places=6)
        self.assertAlmostEqual(self.router.total_credits, 4.0, places=6)

    def test_multiplier_spread_about_80x(self):
        """同样的 token 量，deepseek-v4-flash(0.06) 与 hunyuan(5.00) 相差约 80 倍。"""
        base_in, base_out = 1000, 1000
        flash = self.router.charge("deepseek-v4-flash", base_in, base_out)
        art = self.router.charge("hunyuan-image-v3.0-art", base_in, base_out)
        ratio = art / flash
        # 5.00 / 0.06 ≈ 83.3x，断言落在「约 80 倍」区间
        self.assertGreater(ratio, 80)
        self.assertLess(ratio, 90)
        # 同时给出直观断言：图像模型单次花费远超 lite 系列
        self.assertAlmostEqual(flash, 0.12, places=6)
        self.assertAlmostEqual(art, 10.0, places=6)

    def test_total_credits_accumulates(self):
        """多次 charge 后 total_credits 正确累加。"""
        self.router.charge("default", 1000, 1000)   # 4.0
        self.router.charge("lite", 1000, 1000)       # 0.1
        self.router.charge("deepseek-v4-flash", 1000, 1000)  # 0.12
        self.assertAlmostEqual(self.router.total_credits, 4.0 + 0.1 + 0.12, places=6)


class UsageReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")

    def test_usage_report_grouped_by_model(self):
        self.router.charge("default", 1000, 1000)   # 2k tok, 4.0
        self.router.charge("default", 2000, 1000)   # 3k tok, 6.0
        self.router.charge("lite", 500, 500)        # 1k tok, 0.05
        report = self.router.usage_report()
        self.assertIn("default", report)
        self.assertIn("lite", report)
        self.assertEqual(report["default"]["tokens_in"], 3000)
        self.assertEqual(report["default"]["tokens_out"], 2000)
        self.assertAlmostEqual(report["default"]["credits"], 10.0, places=6)
        self.assertEqual(report["lite"]["tokens_in"], 500)
        self.assertEqual(report["lite"]["tokens_out"], 500)
        self.assertAlmostEqual(report["lite"]["credits"], 0.05, places=6)

    def test_reset_usage_clears(self):
        self.router.charge("default", 1000, 1000)
        self.router.reset_usage()
        self.assertEqual(self.router.total_credits, 0.0)
        self.assertEqual(self.router.usage_report(), {})


class CostComparisonTest(unittest.TestCase):
    """成本对比：13 个纯推理代理各跑一次都用 lite vs 都用 default。"""

    def setUp(self) -> None:
        self.router = ModelRouter(models=dict(MODELS), default_model_id="default")
        # 真实 13 个纯推理代理（白名单为空）
        self.pure_reasoners = [a for a in AGENTS.values() if a.is_pure_reasoner]
        self.assertEqual(len(self.pure_reasoners), 13)
        self.in_tok, self.out_tok = 2000, 1000  # 每次推理的假设 token 量

    def _cost_of(self, agents: list[AgentSpec]) -> float:
        return sum(
            self.router.route(a).estimate_credits(self.in_tok, self.out_tok)
            for a in agents
        )

    def test_real_pure_reasoners_partially_use_lite(self):
        """真实配置里，13 个纯推理代理中 LITE 档的会路由到 lite。"""
        lite_count = sum(
            1 for a in self.pure_reasoners if a.model_tier == ModelTier.LITE
        )
        self.assertEqual(lite_count, 3)  # memorySelector/autoModeClassifier/promptHookEvaluator
        routed_lite = [
            self.router.route(a).model.id
            for a in self.pure_reasoners
            if a.model_tier == ModelTier.LITE
        ]
        self.assertTrue(all(mid == "lite" for mid in routed_lite))

    def test_lite_routing_saves_the_bulk(self):
        # 场景 A：13 个纯推理代理「都用 lite」（tier 全设为 LITE）
        lite_scenario = [
            AgentSpec(name=a.name, description=a.description, tools=(),
                      model_tier=ModelTier.LITE)
            for a in self.pure_reasoners
        ]
        lite_cost = self._cost_of(lite_scenario)
        # 双重保险：lite 场景里每个决策都落在 lite 模型上
        self.assertTrue(
            all(self.router.route(a).model.id == "lite" for a in lite_scenario)
        )

        # 场景 B：13 个纯推理代理「都用 default」（AUTO → default）
        default_scenario = [
            AgentSpec(name=a.name, description=a.description, tools=(),
                      model_tier=ModelTier.AUTO)
            for a in self.pure_reasoners
        ]
        default_cost = self._cost_of(default_scenario)
        self.assertTrue(
            all(self.router.route(a).model.id == "default" for a in default_scenario)
        )

        # 数字说话：lite 总花费应远小于 default
        self.assertLess(lite_cost, default_cost * 0.1)  # 省下 >90%
        # 实际倍率：lite=0.05, default=2.00 → lite 仅为 default 的 2.5%
        self.assertAlmostEqual(lite_cost, default_cost * (0.05 / 2.00), places=6)
        # 直观可读性：省下的钱是大头
        self.assertGreater(default_cost - lite_cost, default_cost * 0.9)


if __name__ == "__main__":
    unittest.main()
