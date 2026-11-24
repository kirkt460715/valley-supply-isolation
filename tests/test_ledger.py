"""信息可见性与路况判定的关口测试。"""

import unittest
from pathlib import Path

from valleysupply.ledger import (Ledger, Notice, Patrol, abs_hour, load,
                                 latest_forecasts, road_state_at, visible)

ROOT = Path(__file__).resolve().parent.parent


class VisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")

    def test_visible_filters_late_records(self):
        # K042 第0天巡查第28时才送达;第25时不可见
        patrols = [p for p in self.ledger.patrols if p.case == "K042"]
        early = visible(patrols, 25, "known_hour")
        self.assertTrue(all(p.known_hour <= 25 for p in early))
        self.assertLess(len(early), len(patrols))

    def test_latest_forecast_respects_publish_time(self):
        # 第1天3时版第27时发布:第26时只能看到第0天版本
        latest = latest_forecasts(self.ledger.forecasts, "K041", 26)
        self.assertFalse(any(day == 1 for (day, _) in latest))
        latest = latest_forecasts(self.ledger.forecasts, "K041", 27)
        self.assertTrue(any(day == 1 for (day, _) in latest))

    def test_notice_beats_patrol(self):
        # K041:J01 第8时送达,封 E3;巡查第6时说 E3 开——通知优先
        state, basis = road_state_at(self.ledger, "K041", "E3", 8)
        self.assertEqual(state, "closed")
        self.assertIn("J01", basis)

    def test_open_notice_reopens(self):
        state, _ = road_state_at(self.ledger, "K041", "E3", 22)
        self.assertEqual(state, "open")  # J03 第19时开通,第21时送达

    def test_unknown_when_no_information(self):
        state, basis = road_state_at(self.ledger, "K041", "E1", 4)
        self.assertEqual(state, "unknown")

    def test_routes_exclude_blocked(self):
        full = self.ledger.routes()
        self.assertTrue(any("E5" in p for p in full["T3"]))
        cut = self.ledger.routes(blocked=frozenset({"E5", "E6"}))
        self.assertTrue(all("E5" not in p and "E6" not in p for p in cut["T3"]))
        self.assertTrue(all(set(p) == {"E1", "E2"} for p in cut["T1"]))


if __name__ == "__main__":
    unittest.main()
