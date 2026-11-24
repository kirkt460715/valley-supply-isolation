"""独立复盘:留出检验、晚到确认复评、不得回灌训练。"""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from valleysupply import review
from valleysupply.ledger import load

ROOT = Path(__file__).resolve().parent.parent
TRAIN = [f"K{i:03d}" for i in range(1, 31)]
HOLDOUT = [f"K{i:03d}" for i in range(31, 41)]


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")

    def test_predictions_persist_and_reuse(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "pred.json"
            first = review.run_review(self.ledger, TRAIN, HOLDOUT, store)
            self.assertFalse(first["预测复用"])
            second = review.run_review(self.ledger, TRAIN, HOLDOUT, store)
            self.assertTrue(second["预测复用"])
            self.assertEqual(first["总体"]["Brier"], second["总体"]["Brier"])

    def test_late_outcomes_never_rewrite_predictions(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "pred.json"
            review.run_review(self.ledger, TRAIN, HOLDOUT, store)
            before = json.loads(store.read_text(encoding="utf-8"))
            # 晚到确认到达(复盘案例结果变化)→ 复评打分变,落盘预测不变
            mutated = copy.deepcopy(self.ledger)
            for p in mutated.patrols:
                if p.case in HOLDOUT:
                    object.__setattr__(p, "state", "open")
            again = review.run_review(mutated, TRAIN, HOLDOUT, store)
            after = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(before["predictions"], after["predictions"])
            self.assertTrue(again["预测复用"])

    def test_fingerprint_mismatch_rejected(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "pred.json"
            review.run_review(self.ledger, TRAIN, HOLDOUT, store)
            with self.assertRaises(SystemExit):
                review.run_review(self.ledger, TRAIN[:20], HOLDOUT, store)

    def test_score_as_of_marks_pending(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "pred.json"
            doc = review.run_review(self.ledger, TRAIN, HOLDOUT, store,
                                    score_as_of=10)
            self.assertGreater(doc["总体"]["待确认"], 0)
            self.assertLess(doc["总体"]["样例数"], 180)

    def test_train_holdout_disjoint(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "pred.json"
            doc = review.run_review(self.ledger, TRAIN, HOLDOUT, store)
            self.assertTrue(doc["卫生检查"]["训练与复盘无交集"])

    def test_missingness_diagnostic_runs(self):
        diag = review.missingness_diagnostic(self.ledger, TRAIN)
        self.assertIn("空缺记录", diag["解读"])


if __name__ == "__main__":
    unittest.main()
