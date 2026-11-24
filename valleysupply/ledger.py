"""演练台账的读取、时间换算与信息可见性。

时间约定:过程起始当日为第 0 天,绝对小时 = day * 24 + hour。
所有"可见性"判断都遵循同一条规则:一条记录只有在它的
known_hour / published_hour 不晚于决策时点时才允许进入计算,
否则视为尚未收到。这是全工具防止"用到明天的路况安排今天的车"
的唯一关口。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

HOURS_PER_DAY = 24


def abs_hour(day: int, hour: int) -> int:
    return day * HOURS_PER_DAY + hour


def day_of(hour: int) -> int:
    return hour // HOURS_PER_DAY


def hour_of(hour: int) -> int:
    return hour % HOURS_PER_DAY


@dataclass(frozen=True)
class Road:
    road: str
    left: str
    right: str
    shared: str
    duration: int
    max_weight: int


@dataclass(frozen=True)
class Window:
    day: int
    vehicle: str
    open_hour: int
    close_hour: int
    capacity: int

    @property
    def open_abs(self) -> int:
        return abs_hour(self.day, self.open_hour)

    @property
    def close_abs(self) -> int:
        return abs_hour(self.day, self.close_hour)


@dataclass(frozen=True)
class Stock:
    site: str
    item: str
    start: int
    minimum: int
    use_per_day: int
    life_days: int
    substitute: str  # 空串表示无替代品
    return_required: bool


@dataclass(frozen=True)
class Shipment:
    shipment: str
    case: str
    day: int
    hour: int
    vehicle: str
    item: str
    quantity: int
    to: str
    status: str  # planned / in_transit / received
    route: tuple[str, ...]

    @property
    def depart_abs(self) -> int:
        return abs_hour(self.day, self.hour)


@dataclass(frozen=True)
class Notice:
    notice: str
    case: str
    road: str
    start_hour: int
    end_hour: int | None  # None 表示结束时间未定(右删失)
    known_hour: int
    state: str  # closed / open


@dataclass(frozen=True)
class Forecast:
    case: str
    day: int
    run_hour: int
    road: str
    rain: str  # severe / wet / dry
    published_hour: int


@dataclass(frozen=True)
class Patrol:
    case: str
    day: int
    hour: int
    road: str
    state: str  # open / closed / unknown
    known_hour: int

    @property
    def obs_abs(self) -> int:
        return abs_hour(self.day, self.hour)


@dataclass(frozen=True)
class Delivery:
    case: str
    day: int
    village: str
    road: str
    quantity: int
    known_hour: int


@dataclass
class Ledger:
    roads: dict[str, Road]
    windows: list[Window]
    stocks: list[Stock]
    shipments: list[Shipment]
    notices: list[Notice]
    forecasts: list[Forecast]
    patrols: list[Patrol]
    deliveries: list[Delivery]

    @property
    def cases(self) -> list[str]:
        seen = {f.case for f in self.forecasts}
        return sorted(seen)

    @property
    def horizon_days(self) -> list[int]:
        return sorted({w.day for w in self.windows})

    @property
    def villages(self) -> list[str]:
        return sorted({s.site for s in self.stocks if s.site != "depot"})

    def routes(self, blocked: frozenset[str] = frozenset()) -> dict[str, list[tuple[str, ...]]]:
        """枚举县仓到各村的简单路径(不超过 4 段),可排除不可用路段。"""
        graph: dict[str, list[tuple[str, str]]] = {}
        for road in self.roads.values():
            if road.road in blocked:
                continue
            graph.setdefault(road.left, []).append((road.right, road.road))
            graph.setdefault(road.right, []).append((road.left, road.road))
        found: dict[str, list[tuple[str, ...]]] = {v: [] for v in self.villages}

        def walk(node: str, target: str, path: tuple[str, ...], seen: frozenset[str]) -> None:
            if node == target:
                found[target].append(path)
                return
            if len(path) >= 4:
                return
            for nxt, edge in graph.get(node, []):
                if nxt in seen:
                    continue
                walk(nxt, target, path + (edge,), seen | {nxt})

        for village in self.villages:
            walk("depot", village, (), frozenset({"depot"}))
        for village in found:
            found[village].sort(key=lambda p: (len(p), sum(self.roads[e].duration for e in p)))
        return found


def _read(location: Path, name: str) -> list[dict[str, str]]:
    with (location / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def load(location: Path | str) -> Ledger:
    location = Path(location)
    roads = {}
    for row in _read(location, "roads"):
        roads[row["road"]] = Road(
            road=row["road"], left=row["left"], right=row["right"],
            shared=row["shared"], duration=int(row["duration"]),
            max_weight=int(row["max_weight"]),
        )
    windows = [
        Window(day=int(r["day"]), vehicle=r["vehicle"],
               open_hour=int(r["open_hour"]), close_hour=int(r["close_hour"]),
               capacity=int(r["capacity"]))
        for r in _read(location, "fleet")
    ]
    stocks = [
        Stock(site=r["site"], item=r["item"], start=int(r["start"]),
              minimum=int(r["minimum"]), use_per_day=int(r["use_per_day"]),
              life_days=int(r["life_days"]), substitute=r["substitute"],
              return_required=r["return_required"].strip().lower() == "true")
        for r in _read(location, "supplies")
    ]
    shipments = [
        Shipment(shipment=r["shipment"], case=r["case"], day=int(r["day"]),
                 hour=int(r["hour"]), vehicle=r["vehicle"], item=r["item"],
                 quantity=int(r["quantity"]), to=r["to"], status=r["status"],
                 route=tuple(part for part in r["road"].split("|") if part))
        for r in _read(location, "shipments")
    ]
    notices = [
        Notice(notice=r["notice"], case=r["case"], road=r["road"],
               start_hour=int(r["start_hour"]),
               end_hour=int(r["end_hour"]) if r["end_hour"] else None,
               known_hour=int(r["known_hour"]), state=r["state"])
        for r in _read(location, "notices")
    ]
    forecasts = [
        Forecast(case=r["case"], day=int(r["day"]), run_hour=int(r["run_hour"]),
                 road=r["road"], rain=r["rain"],
                 published_hour=int(r["published_hour"]))
        for r in _read(location, "forecasts")
    ]
    patrols = [
        Patrol(case=r["case"], day=int(r["day"]), hour=int(r["hour"]),
               road=r["road"], state=r["state"], known_hour=int(r["known_hour"]))
        for r in _read(location, "patrols")
    ]
    deliveries = [
        Delivery(case=r["case"], day=int(r["day"]), village=r["village"],
                 road=r["road"], quantity=int(r["quantity"]),
                 known_hour=int(r["known_hour"]))
        for r in _read(location, "delivery_history")
    ]
    return Ledger(roads=roads, windows=windows, stocks=stocks,
                  shipments=shipments, notices=notices, forecasts=forecasts,
                  patrols=patrols, deliveries=deliveries)


def visible(records, at_hour: int, field_name: str):
    """只保留在 at_hour 之前(含)已经送达县仓的记录。"""
    return [r for r in records if getattr(r, field_name) <= at_hour]


def latest_forecasts(forecasts: list[Forecast], case: str, at_hour: int) -> dict[tuple[int, str], Forecast]:
    """决策时点可见的最新预报版本,键为 (day, road)。

    同一日同一路可能有多个版本(如 3 时与 14 时),只保留已发布的最新版;
    尚未发布任何版本的日子不出现在结果里,调用方必须按"预报未到"处理。
    """
    latest: dict[tuple[int, str], Forecast] = {}
    for fc in forecasts:
        if fc.case != case or fc.published_hour > at_hour:
            continue
        key = (fc.day, fc.road)
        if key not in latest or fc.published_hour > latest[key].published_hour:
            latest[key] = fc
    return latest


def state_evidence(notices: list[Notice], patrols: list[Patrol],
                   case: str, at_hour: int) -> dict[str, list[tuple[int, str, str]]]:
    """决策时点可见的全部路况证据,按路汇总为 (时刻, 状态, 来源) 时间线。

    通知以业务发生时( start/end )计,巡查以观察时刻计;两者都只在
    送达(known_hour)后才算证据。同一时刻通知优先于巡查。
    """
    evidence: dict[str, list[tuple[int, str, str]]] = {}
    for n in notices:
        if n.case != case or n.known_hour > at_hour:
            continue
        road = evidence.setdefault(n.road, [])
        if n.state == "closed":
            road.append((n.start_hour, "closed", f"通知 {n.notice}"))
            if n.end_hour is not None:
                road.append((n.end_hour, "open", f"通知 {n.notice} 解除"))
        elif n.state == "open":
            road.append((n.start_hour, "open", f"通知 {n.notice}"))
    for p in patrols:
        if p.case != case or p.known_hour > at_hour:
            continue
        if p.state in ("open", "closed"):
            evidence.setdefault(p.road, []).append(
                (p.obs_abs, p.state, f"巡查 第{p.day}天{p.hour}时"))
    for road in evidence:
        # 同时刻通知优先:巡查(kind 0)先排,通知(kind 1)后排生效
        evidence[road].sort(key=lambda e: (e[0], 0 if e[2].startswith("巡查") else 1))
    return evidence


def closure_intervals(notices: list[Notice], patrols: list[Patrol],
                      case: str, at_hour: int) -> dict[str, list[tuple[int, int | None]]]:
    """由全部可见证据推出各路的封闭时段;解除时间未定时为 None。"""
    intervals: dict[str, list[tuple[int, int | None]]] = {}
    for road, events in state_evidence(notices, patrols, case, at_hour).items():
        spans: list[tuple[int, int | None]] = []
        current = None
        for t, state, _src in events:
            if state == "closed" and current is None:
                current = t
            elif state == "open" and current is not None:
                spans.append((current, t))
                current = None
        if current is not None:
            spans.append((current, None))
        intervals[road] = spans
    return intervals


def road_state_at(ledger: Ledger, case: str, road: str, at_hour: int) -> tuple[str, str]:
    """某条路在决策时点的已知状态,返回 (状态, 依据)。

    取不晚于决策时点的最后一条证据;没有任何证据时为 unknown。
    """
    events = state_evidence(ledger.notices, ledger.patrols, case, at_hour).get(road, [])
    state, basis = "unknown", "无任何已知信息"
    for t, st, src in events:
        if t <= at_hour:
            state, basis = st, f"{src}(第 {t} 时起)"
        else:
            break
    return state, basis
