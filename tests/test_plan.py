"""计划编制:不用未来信息、不重复拨付、窗口限重与封控约束。"""

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from valleysupply import risk
from valleysupply.ledger import Patrol, load
from valleysupply.plan import PlanRequest, build_situation, plan
from valleysupply.replan import record_decisions
from valleysupply.state import load_state, save_state

ROOT = Path(__file__).resolve().parent.parent
TRAIN = [f"K{i:03d}" for i in range(1, 31)]


class PlanBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")
        cls.model = risk.fit(cls.ledger, TRAIN)

    def make_plan(self, case, at, state=None, alpha=0.8):
        req = PlanRequest(ledger=self.ledger, case=case, at_hour=at,
                          model=self.model, state=state, alpha=alpha)
        return plan(req)


class NonAnticipationTests(PlanBase):
    def test_future_records_do_not_change_plan(self):
        base = self.make_plan("K041", 4)
        mutated = copy.deepcopy(self.ledger)
        # 一条第 50 时才送达的巡查和一个第 60 时才发布的预报版本
        mutated.patrols.append(Patrol(case="K041", day=2, hour=5, road="E1",
                                      state="closed", known_hour=50))
        from valleysupply.ledger import Forecast
        mutated.forecasts.append(Forecast(case="K041", day=1, run_hour=3,
                                          road="E1", rain="dry",
                                          published_hour=60))
        model2 = risk.fit(mutated, TRAIN)
        req = PlanRequest(ledger=mutated, case="K041", at_hour=4, model=model2)
        changed = plan(req)
        self.assertEqual(base["decisions"], changed["decisions"])
        self.assertEqual(base["needs"], changed["needs"])
        self.assertEqual(base["outlook"], changed["outlook"])

    def test_unknown_roads_block_dispatch_and_raise_verification(self):
        doc = self.make_plan("K041", 4)  # 巡查第6时才送达,4时无任何路况
        self.assertEqual(doc["decisions"], [])
        roads = {u["road"] for u in doc["unknowns"]}
        self.assertIn("E1", roads)
        self.assertTrue(all("人工核实" in u["处理"] or "核实" in u["处理"]
                            for u in doc["unknowns"]))


class ConstraintTests(PlanBase):
    def test_road_weight_limit_on_e5(self):
        doc = self.make_plan("K041", 6)  # 此时 E5 已知可通
        for dec in doc["decisions"]:
            if dec["route"] == ["E5"]:
                total = sum(d["qty"] for d in dec["drops"])
                self.assertLessEqual(total, 4)  # E5 限重 4

    def test_notice_closure_excludes_route(self):
        doc = self.make_plan("K042", 25)  # J04:E1 第27–38时封闭
        for dec in doc["decisions"]:
            self.assertNotIn("E1", dec["route"])
        v3 = next(p for p in doc["planned_review"] if p["id"] == "V3")
        self.assertEqual(v3["评估"], "需调整")

    def test_window_roundtrip(self):
        doc = self.make_plan("K041", 6)
        for dec in doc["decisions"]:
            window = next(w for w in self.ledger.windows
                          if w.day == dec["day"] and w.vehicle == dec["vehicle"])
            trip = 2 * sum(self.ledger.roads[e].duration for e in dec["route"])
            self.assertLessEqual(dec["depart_hour"] + trip, window.close_abs)


class NoDoublePlanTests(PlanBase):
    def test_saved_decisions_reduce_available_stock(self):
        with TemporaryDirectory() as tmp:
            state = load_state(tmp, "K041")
            doc1 = self.make_plan("K041", 27, state=state)  # 第1天晨,E3 已重开
            self.assertTrue(doc1["decisions"])
            record_decisions(state, "K041", 27, doc1)
            save_state(tmp, state)
            sent = {}
            for dec in doc1["decisions"]:
                for d in dec["drops"]:
                    sent[d["item"]] = sent.get(d["item"], 0) + d["qty"]
            state2 = load_state(tmp, "K041")
            sit = build_situation(self.ledger, "K041", 28, state2)
            for item, qty in sent.items():
                start = next(s.start for s in self.ledger.stocks
                             if s.site == "depot" and s.item == item)
                self.assertEqual(sit.inventory.get("depot", item).on_hand(1),
                                 start - qty, item)

    def test_second_plan_does_not_resend_same_goods(self):
        with TemporaryDirectory() as tmp:
            state = load_state(tmp, "K041")
            doc1 = self.make_plan("K041", 27, state=state)
            record_decisions(state, "K041", 27, doc1)
            save_state(tmp, state)
            state2 = load_state(tmp, "K041")
            doc2 = self.make_plan("K041", 28, state=state2)
            total = sum(d["qty"] for dec in doc1["decisions"] + doc2["decisions"]
                        for d in dec["drops"] if d["item"] == "water")
            self.assertLessEqual(total, 48)  # 县仓水总量不变,承诺不超库存


class InTransitTests(PlanBase):
    def test_blocked_shipment_marked_and_vehicle_busy(self):
        doc = self.make_plan("K041", 8)  # J01 第8时送达,V1 在途经 E3
        stuck_ids = {s["id"] for s in doc["stuck"]}
        self.assertIn("V1", stuck_ids)
        self.assertTrue(any("B1" in w for w in doc["warnings"]))

    def test_reopen_resumes_shipment(self):
        doc = self.make_plan("K041", 21)  # J03:E3 第19时重开
        self.assertNotIn("V1", {s["id"] for s in doc["stuck"]})
        # V1 落地后 T2 水应已入账
        sit = build_situation(self.ledger, "K041", 21, None)
        self.assertGreaterEqual(sit.inventory.get("T2", "water").on_hand(0), 7)


if __name__ == "__main__":
    unittest.main()
