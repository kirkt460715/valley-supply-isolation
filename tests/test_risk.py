"""风险模型:删失处理、共模失效、未知风险与训练窗隔离。"""

import unittest
from pathlib import Path

from valleysupply import risk
from valleysupply.ledger import load

ROOT = Path(__file__).resolve().parent.parent
TRAIN = [f"K{i:03d}" for i in range(1, 31)]
HOLDOUT = [f"K{i:03d}" for i in range(31, 41)]


class RiskFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")
        cls.model = risk.fit(cls.ledger, TRAIN)

    def test_train_window_isolated(self):
        # 改动留出案例的巡查不应改变模型参数
        import copy
        mutated = copy.deepcopy(self.ledger)
        for p in mutated.patrols:
            if p.case in HOLDOUT:
                object.__setattr__(p, "state", "closed")
        again = risk.fit(mutated, TRAIN)
        self.assertEqual(self.model.closure, again.closure)
        self.assertEqual(self.model.persistence, again.persistence)

    def test_closure_probabilities_match_empirical_ranking(self):
        # 台账中 E5 从未见断(0/90),E3 近半天数见断:估计应保持这一排序
        e5 = self.model.closure.get(("E5", "severe"))
        e3 = self.model.closure.get(("E3", "severe"))
        self.assertIsNotNone(e5)
        self.assertIsNotNone(e3)
        self.assertLess(e5[0], e3[0])
        self.assertLess(e5[0], 0.1)

    def test_interval_within_unit(self):
        for entry in self.model.closure.values():
            mean, lo, hi, n = entry
            self.assertTrue(0.0 <= lo <= mean <= hi <= 1.0)
            self.assertGreater(n, 0)

    def test_group_shock_nonnegative(self):
        for g, q in self.model.group_shock.items():
            self.assertGreaterEqual(q, 0.0)
        # J3(E3、E4 共防护)应识别出正的共模份额
        self.assertGreater(self.model.group_shock.get("J3", 0.0), 0.0)

    def test_persistence_from_censored_pairs(self):
        # 持续概率在 [0,1],且观察对数被如实记录
        for road, (rho, n) in self.model.persistence.items():
            self.assertTrue(0.0 <= rho <= 1.0)
            self.assertGreaterEqual(n, 0)


class UnknownRoadTests(unittest.TestCase):
    def test_insufficient_history_marks_unknown(self):
        ledger = load(ROOT / "data")
        # 没有任何训练案例:所有路段所有雨量等级都观察不足
        model = risk.fit(ledger, [])
        self.assertTrue(model.unknown_roads)
        p = model.daily_probability(model.unknown_roads[0], "severe")
        self.assertIsNone(p)

    def test_unknown_road_treated_closed_in_scenarios(self):
        ledger = load(ROOT / "data")
        model = risk.fit(ledger, [])
        road = model.unknown_roads[0]
        result = risk.simulate(model, ledger, [1], {1: {road: "severe"}},
                               n_scenarios=50, seed=7)
        for scenario in result.scenarios:
            self.assertFalse(scenario[1][road])  # 未知按不可用保守处理


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")
        cls.model = risk.fit(cls.ledger, TRAIN)

    def test_known_overrides_respected(self):
        result = risk.simulate(self.model, self.ledger, [0, 1],
                               {0: {}, 1: {}},
                               known_closed={0: {"E1"}}, known_open={1: {"E1"}},
                               n_scenarios=20, seed=3)
        for s in result.scenarios:
            self.assertFalse(s[0]["E1"])
            self.assertTrue(s[1]["E1"])

    def test_group_joint_exceeds_independence(self):
        # J3 组内同断的联合频率应高于独立假设的下界
        result = risk.simulate(self.model, self.ledger, [1],
                               {1: {"E3": "severe", "E4": "severe"}},
                               n_scenarios=2000, seed=11)
        both = sum(1 for s in result.scenarios
                   if not s[1]["E3"] and not s[1]["E4"]) / 2000
        p3 = sum(1 for s in result.scenarios if not s[1]["E3"]) / 2000
        p4 = sum(1 for s in result.scenarios if not s[1]["E4"]) / 2000
        self.assertGreater(both, p3 * p4)

    def test_isolation_metrics_keys(self):
        result = risk.simulate(self.model, self.ledger, [1, 2],
                               {1: {}, 2: {}}, n_scenarios=50, seed=5)
        metrics = risk.isolation_metrics(result, self.ledger.routes(), [])
        for v in ("T1", "T2", "T3"):
            self.assertIn(v, metrics["per_village"])
            self.assertIn(1, metrics["per_village"][v])
        self.assertIn("T1&T2", metrics["joint_pairs"])


if __name__ == "__main__":
    unittest.main()
