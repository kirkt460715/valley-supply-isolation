"""库存推演:批次保质、替代、回库与在途的真实库存口径。

口径约定(与演练台账一致):
- 未落地的在途物资不计入目标屯点存量;已装车未回收的物资不再可拨付。
- 县仓库存 = 期初 + 已回库 - 已发出(在途、已落地、已确定今晚装车)。
- 保质期按批次管理:批次在到货第 life_days 天起不可用;消耗先到期先出。
- 替代品(如 battery 可代 cells)在自身库存不足时按 1:1 顶用。
- 需回库的物资(充电电源)在屯点消耗后成为待回库件;转运出去的
  托盘当天收不回来,次日起才能由回程车带回,回库次日可再发出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import Stock


@dataclass
class Batch:
    qty: int
    expiry_day: int  # 该批次可使用的最后一天(含)
    available_day: int = 0  # 可使用的第一天(回库充电的批次次日才可用)


@dataclass
class SiteStock:
    site: str
    item: str
    minimum: int
    use_per_day: int
    life_days: int
    substitute: str
    return_required: bool
    batches: list[Batch] = field(default_factory=list)
    empties: int = 0          # 待回库件(仅 return_required 的物资)
    empties_earliest_day: int = 0  # 当日转运出去的托盘当天不能收回

    def on_hand(self, day: int) -> int:
        return sum(b.qty for b in self.batches
                   if b.available_day <= day <= b.expiry_day)

    def usable(self, day: int, pool: "Inventory") -> int:
        own = self.on_hand(day)
        if self.substitute:
            other = pool.get(self.site, self.substitute)
            if other is not None:
                own += other.on_hand(day)
        return own


class Inventory:
    """某一决策时点的真实库存快照,可向未来推演。"""

    def __init__(self, stocks: list[Stock], start_day: int = 0):
        self.sites: dict[tuple[str, str], SiteStock] = {}
        self.item_spec: dict[str, Stock] = {}  # 物资的全局规格(保质、替代、回库)
        for s in stocks:
            self.item_spec.setdefault(s.item, s)
            entry = SiteStock(
                site=s.site, item=s.item, minimum=s.minimum,
                use_per_day=s.use_per_day, life_days=s.life_days,
                substitute=s.substitute, return_required=s.return_required,
            )
            if s.start:
                entry.batches.append(Batch(qty=s.start, expiry_day=start_day + s.life_days - 1))
            self.sites[(s.site, s.item)] = entry

    def get(self, site: str, item: str) -> SiteStock | None:
        return self.sites.get((site, item))

    def ensure(self, site: str, item: str) -> SiteStock | None:
        """取站点物资条目;站点此前没有该物资时按全局规格补建(最低量 0)。"""
        entry = self.sites.get((site, item))
        if entry is None:
            spec = self.item_spec.get(item)
            if spec is None:
                return None
            entry = SiteStock(site=site, item=item, minimum=0, use_per_day=0,
                              life_days=spec.life_days, substitute=spec.substitute,
                              return_required=spec.return_required)
            self.sites[(site, item)] = entry
        return entry

    def sites_at(self, site: str) -> list[SiteStock]:
        return [s for (site_name, _), s in self.sites.items() if site_name == site]

    def add_arrival(self, site: str, item: str, qty: int, day: int) -> None:
        """确证到货,按该物资保质期起算新批次;站点无此物资时补建条目。"""
        entry = self.ensure(site, item)
        if entry is None or qty <= 0:
            return
        entry.batches.append(Batch(qty=qty, expiry_day=day + entry.life_days - 1))
        entry.batches.sort(key=lambda b: b.expiry_day)

    def take(self, site: str, item: str, qty: int, day: int) -> int:
        """从某站点取出可用库存(先到期先出),返回实际取到的量。"""
        entry = self.get(site, item)
        if entry is None or qty <= 0:
            return 0
        remaining = qty
        for batch in sorted(entry.batches, key=lambda b: b.expiry_day):
            if batch.expiry_day < day or batch.available_day > day:
                continue
            got = min(batch.qty, remaining)
            batch.qty -= got
            remaining -= got
            if remaining == 0:
                break
        return qty - remaining

    def consume_day(self, site: str, day: int) -> dict[str, int]:
        """屯点一天的消耗;自身不足时用替代品,并登记待回库件。

        返回 {item: 缺口}——即当天动用库存仍无法满足的消耗量。
        """
        shortfall: dict[str, int] = {}
        for entry in self.sites_at(site):
            need = entry.use_per_day
            if need <= 0:
                continue
            got = self.take(site, entry.item, need, day)
            if got < need and entry.substitute:
                got += self.take(site, entry.substitute, need - got, day)
            if got < need:
                shortfall[entry.item] = need - got
            if entry.return_required and got:
                entry.empties += got
                entry.empties_earliest_day = day + 1  # 当天运出的当天收不回
        return shortfall

    def expire_day(self, day: int) -> dict[tuple[str, str], int]:
        """移除到期批次,返回 {(site, item): 报废量}。"""
        expired: dict[tuple[str, str], int] = {}
        for key, entry in self.sites.items():
            keep = [b for b in entry.batches if b.expiry_day >= day]
            lost = sum(b.qty for b in entry.batches) - sum(b.qty for b in keep)
            if lost:
                expired[key] = lost
            entry.batches = [b for b in keep if b.qty > 0]
        return expired

    def backhaul_ready(self, site: str, day: int) -> dict[str, int]:
        """当日可由回程车带回的待回库件(转运出去的托盘当天收不回)。"""
        ready: dict[str, int] = {}
        for entry in self.sites_at(site):
            if entry.return_required and entry.empties > 0 and entry.empties_earliest_day <= day:
                ready[entry.item] = entry.empties
        return ready

    def receive_empties(self, item: str, qty: int, day: int) -> None:
        """回库件到达县仓,充电次日起可再发出。"""
        entry = self.get("depot", item)
        if entry is None or qty <= 0:
            return
        entry.batches.append(Batch(qty=qty, available_day=day + 1,
                                   expiry_day=day + entry.life_days))
        entry.batches.sort(key=lambda b: b.expiry_day)

    def clone(self) -> "Inventory":
        import copy
        return copy.deepcopy(self)
