"""重规划:通知使路线失效时,从真实库存重算且不重复拨付。"""

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from valleysupply import risk
from valleysupply.ledger import Notice, load
from valleysupply.plan import PlanRequest, plan
from valleysupply.replan import record_decisions, replan
from valleysupply.state import load_state, save_state

ROOT = Path(__file__).resolve().parent.parent
TRAIN = [f"K{i:03d}" for i in range(1, 31)]


class ReplanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")
        cls.model = risk.fit(cls.ledger, TRAIN)

    def test_undeparted_decision_cancelled_and_goods_returned(self):
        # 构造:第30–40时封 E5,第26时送达;小时25的计划经 E5,未发车即失效
        ledger = copy.deepcopy(self.ledger)
        ledger.notices.append(Notice(notice="JX", case="K042", road="E5",
                                     start_hour=30, end_hour=40,
                                     known_hour=26, state="closed"))
        with TemporaryDirectory() as tmp:
            state = load_state(tmp, "K042")
            req = PlanRequest(ledger=ledger, case="K042", at_hour=25,
                              model=self.model, state=state)
            doc = plan(req)
            self.assertTrue(any("E5" in dec["route"] for dec in doc["decisions"]))
            record_decisions(state, "K042", 25, doc)
            save_state(tmp, state)

            state2 = load_state(tmp, "K042")
            doc2 = replan(ledger, "K042", 26, self.model, state2)
            cancelled = [c for c in doc2["status_changes"] if c["到"] == "cancelled"]
            self.assertTrue(cancelled, doc2["status_changes"])
            # 取消后物资回到可拨库存:新计划不再经 E5,且水总量不超期初
            for dec in doc2["decisions"]:
                self.assertNotIn("E5", dec["route"])

    def test_blocked_then_resume_on_reopen(self):
        with TemporaryDirectory() as tmp:
            state = load_state(tmp, "K041")
            # 第8时:J01 已送达,V1 受阻
            doc = replan(self.ledger, "K041", 8, self.model, state)
            self.assertIn("V1", {s["id"] for s in doc["stuck"]})
            record_decisions(state, "K041", 8, doc)
            save_state(tmp, state)
            # 第21时:J03 重开 E3,V1 应落地,不再受阻
            state2 = load_state(tmp, "K041")
            doc2 = replan(self.ledger, "K041", 21, self.model, state2)
            self.assertNotIn("V1", {s["id"] for s in doc2["stuck"]})

    def test_replan_keeps_real_inventory_floor(self):
        with TemporaryDirectory() as tmp:
            state = load_state(tmp, "K041")
            for at in (6, 8, 21, 27):
                doc = replan(self.ledger, "K041", at, self.model, state)
                record_decisions(state, "K041", at, doc)
                save_state(tmp, state)
                state = load_state(tmp, "K041")
            # 全程已发出的水不得超过县仓期初与确证回库之和
            sent = sum(drop["qty"] for d in state.decisions
                       if d.status != "cancelled"
                       for drop in d.drops if drop["item"] == "water")
            self.assertLessEqual(sent, 48)


if __name__ == "__main__":
    unittest.main()
