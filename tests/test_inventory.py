"""库存推演:保质、替代、回库与真实库存口径。"""

import unittest
from pathlib import Path

from valleysupply.inventory import Inventory
from valleysupply.ledger import load

ROOT = Path(__file__).resolve().parent.parent


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(ROOT / "data")

    def make(self) -> Inventory:
        return Inventory(self.ledger.stocks)

    def test_expiry_removes_old_batches(self):
        inv = self.make()
        # depot cells 保质 2 天:第 2 天起不可用
        self.assertEqual(inv.get("depot", "cells").on_hand(0), 22)
        self.assertEqual(inv.get("depot", "cells").on_hand(2), 0)
        expired = inv.expire_day(2)
        self.assertEqual(expired.get(("depot", "cells")), 22)

    def test_substitute_covers_consumption(self):
        inv = self.make()
        inv.add_arrival("T2", "battery", 5, 0)
        # T2 cells 日耗 2,自身 3 用尽后用 battery 顶
        inv.consume_day("T2", 0)
        inv.consume_day("T2", 1)
        short = inv.consume_day("T2", 2)
        self.assertNotIn("cells", short)
        self.assertEqual(inv.get("T2", "battery").on_hand(2), 2)

    def test_return_required_empties_not_same_day(self):
        inv = self.make()
        inv.consume_day("T2", 0)  # cells 消耗 2 → 待回库
        self.assertEqual(inv.get("T2", "cells").empties, 2)
        # 当天转运出去的托盘当天收不回
        self.assertEqual(inv.backhaul_ready("T2", 0), {})
        self.assertEqual(inv.backhaul_ready("T2", 1).get("cells"), 2)

    def test_receive_empties_available_next_day(self):
        inv = self.make()
        inv.take("depot", "cells", 22, 0)
        inv.receive_empties("cells", 4, 1)
        entry = inv.get("depot", "cells")
        self.assertEqual(entry.on_hand(1), 0)   # 充电中
        self.assertEqual(entry.on_hand(2), 4)   # 次日起可再发

    def test_arrival_creates_missing_site_item(self):
        inv = self.make()
        # T3 台账无 cells 条目,确证到货必须入账而不是消失
        inv.add_arrival("T3", "cells", 5, 0)
        self.assertIsNotNone(inv.get("T3", "cells"))
        self.assertEqual(inv.get("T3", "cells").on_hand(0), 5)

    def test_take_is_fifo_by_expiry(self):
        inv = self.make()
        inv.add_arrival("depot", "water", 10, 2)  # 晚到期批次
        got = inv.take("depot", "water", 48, 0)
        self.assertEqual(got, 48)
        # 先到期的期初批次已取完,剩余为晚到期批次
        self.assertEqual(inv.get("depot", "water").on_hand(3), 10)


if __name__ == "__main__":
    unittest.main()
