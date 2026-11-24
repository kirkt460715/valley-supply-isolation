"""滚动保供计划与通知驱动的重规划。

每条计划都必须满足:
- 只用决策时点已收到的信息(预报版本、封控通知、巡查、既往运输);
  未来日子的路况只以情景分布进入,绝不用"明天才会看到"的实现值。
- 县仓固定出车窗口内往返;路段限重、车辆限量;一次转运出去的托盘
  当天收不回;已发出物资不在新计划里重复出现。
- 多日连在一起的强降雨按联合情景考虑;可承诺数量按风险水平(alpha)
  取分位,未知风险的路段不凭点估计承诺,改列人工核实事项。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import risk
from .inventory import Inventory
from .ledger import (Ledger, abs_hour, closure_intervals, day_of, hour_of,
                     latest_forecasts, road_state_at)
from .state import CaseState

DEFAULT_SEED = 20261124
SCENARIOS = 400

POWER_ITEMS = ("cells", "battery")  # 照明电源类,可按台账替代关系互顶


# ----------------------------------------------------------------  封控区间

def _intervals_hit(intervals: list[tuple[int, int | None]], a: int, b: int) -> int | None:
    """[a,b) 是否与封闭区间相交;相交则返回封闭解除小时(None=未定)。"""
    for start, end in intervals:
        stop = math.inf if end is None else end
        if a < stop and b > start:
            return None if end is None else end
    return -1  # 未相交


# ----------------------------------------------------------------  在途推算

def shipment_progress(route: tuple[str, ...] | list[str], depart_abs: int,
                      intervals: dict[str, list[tuple[int, int | None]]],
                      ledger: Ledger, now: int) -> tuple[str, int | None]:
    """按通知区间推算一车的状态,返回 (status, eta)。

    status: received / in_transit / blocked。eta 为预计落地绝对小时,
    受阻且解除时间未定时为 None。
    """
    t = depart_abs
    for edge in route:
        duration = ledger.roads[edge].duration
        while True:
            hit = _intervals_hit(intervals.get(edge, []), t, t + duration)
            if hit == -1:
                t += duration
                break
            if hit is None:
                return "blocked", None
            t = hit  # 等到解除再走这一段
    return ("received", t) if t <= now else ("in_transit", t)


# ----------------------------------------------------------------  态势快照

@dataclass
class Situation:
    case: str
    at_hour: int
    day: int
    days: list[int]                      # 剩余推演日(含今天)
    rain_by_day: dict[int, dict[str, str | None]]
    rain_basis: dict[int, dict[str, dict]]   # 每天每路采用的预报版本依据
    road_now: dict[str, tuple[str, str]]     # road -> (状态, 依据)
    intervals: dict[str, list[tuple[int, int | None]]]
    inventory: Inventory
    expected_inbound: dict[str, list[tuple[int, str, int, str]]]  # village -> [(day,item,qty,来源)]
    stuck: list[dict]                      # 受阻物资(车与货都不可用)
    vehicle_busy: dict[int, set[str]]      # day -> 已占用车辆
    planned_existing: list[dict]           # 台账中尚未执行的 planned 调度
    warnings: list[str] = field(default_factory=list)


def build_situation(ledger: Ledger, case: str, at_hour: int,
                    state: CaseState | None) -> Situation:
    day = day_of(at_hour)
    days = [d for d in ledger.horizon_days if d >= day]
    if not days:
        days = [day]

    # 可见预报:每天每路取已发布的最新版;未发布的日子记 None(气候率)
    latest = latest_forecasts(ledger.forecasts, case, at_hour)
    rain_by_day: dict[int, dict[str, str | None]] = {}
    rain_basis: dict[int, dict[str, dict]] = {}
    for d in days:
        rain_by_day[d] = {}
        rain_basis[d] = {}
        for road in ledger.roads:
            fc = latest.get((d, road))
            if fc is None:
                rain_by_day[d][road] = None
                rain_basis[d][road] = {"版本": None, "说明": "当日预报尚未发布,用气候率"}
            else:
                rain_by_day[d][road] = fc.rain
                rain_basis[d][road] = {
                    "版本": f"第{fc.day}天{fc.run_hour}时",
                    "发布时间": fc.published_hour, "雨量": fc.rain,
                }

    road_now = {r: road_state_at(ledger, case, r, at_hour) for r in ledger.roads}
    intervals = closure_intervals(ledger.notices, ledger.patrols, case, at_hour)

    # 真实库存:期初 + 已落地 + 已确证到货 - 已消耗;已发出物资不再可拨
    inventory = Inventory(ledger.stocks)
    expected: dict[str, list[tuple[int, str, int, str]]] = {v: [] for v in ledger.villages}
    stuck: list[dict] = []
    vehicle_busy: dict[int, set[str]] = {}
    planned_existing: list[dict] = []
    warnings: list[str] = []

    def book_vehicle(vehicle: str, d: int) -> None:
        vehicle_busy.setdefault(d, set()).add(vehicle)

    # 台账调度登记
    for sh in ledger.shipments:
        if sh.case != case:
            continue
        if sh.status == "received":
            inventory.add_arrival(sh.to, sh.item, sh.quantity, sh.day)
        elif sh.status == "in_transit":
            status, eta = shipment_progress(sh.route, sh.depart_abs, intervals, ledger, at_hour)
            if status == "received" and eta is not None:
                inventory.add_arrival(sh.to, sh.item, sh.quantity, day_of(eta))
            elif status == "in_transit" and eta is not None:
                expected[sh.to].append((day_of(eta), sh.item, sh.quantity, f"在途 {sh.shipment}"))
                book_vehicle(sh.vehicle, sh.day)
            else:
                stuck.append({"id": sh.shipment, "vehicle": sh.vehicle, "item": sh.item,
                              "qty": sh.quantity, "to": sh.to, "route": list(sh.route),
                              "原因": "路线被通知封闭且解除时间未定"})
                book_vehicle(sh.vehicle, sh.day)
                warnings.append(f"{sh.shipment} 在途受阻:{sh.quantity} {sh.item} 与车辆 {sh.vehicle} 暂不可用")
        elif sh.status == "planned":
            planned_existing.append({"id": sh.shipment, "day": sh.day, "hour": sh.hour,
                                     "vehicle": sh.vehicle, "item": sh.item,
                                     "qty": sh.quantity, "to": sh.to, "route": list(sh.route)})

    # 已放行决定(重跑时恢复,已发物资不得重复拨付)
    if state is not None:
        for d in state.decisions:
            if d.status == "cancelled":
                continue
            for drop in d.drops:
                entry = inventory.get("depot", drop["item"])
                if entry is not None:
                    taken = inventory.take("depot", drop["item"], drop["qty"], d.day)
                    if taken < drop["qty"]:
                        warnings.append(f"{d.decision_id} 对应县仓库存不足,仅登记 {taken}")
            if d.status == "received":
                for drop in d.drops:
                    inventory.add_arrival(drop["village"], drop["item"], drop["qty"], d.day)
            elif d.status in ("released", "in_transit"):
                status, eta = shipment_progress(d.route, d.depart_abs, intervals, ledger, at_hour)
                if d.status == "released" and at_hour < d.depart_abs:
                    book_vehicle(d.vehicle, d.day)  # 今晚已排定,占用车辆与物资
                    for drop in d.drops:
                        expected[drop["village"]].append(
                            (d.day, drop["item"], drop["qty"], f"已排定 {d.decision_id}"))
                elif status == "received" and eta is not None:
                    for drop in d.drops:
                        inventory.add_arrival(drop["village"], drop["item"], drop["qty"], day_of(eta))
                elif status == "in_transit":
                    book_vehicle(d.vehicle, d.day)
                    for drop in d.drops:
                        expected[drop["village"]].append(
                            (day_of(eta), drop["item"], drop["qty"], f"在途 {d.decision_id}"))
                else:
                    book_vehicle(d.vehicle, d.day)
            elif d.status == "blocked":
                book_vehicle(d.vehicle, d.day)
                for drop in d.drops:
                    stuck.append({"id": d.decision_id, "vehicle": d.vehicle,
                                  "item": drop["item"], "qty": drop["qty"],
                                  "to": drop["village"], "route": d.route,
                                  "原因": "决定执行后路线被封"})

    # 已确证到货(晚到确认):known_hour 不晚于决策时点才入账
    for rec in ledger.deliveries:
        if rec.case != case or rec.known_hour > at_hour:
            continue
        inventory.add_arrival(rec.village, _delivery_item(ledger, rec.village),
                              rec.quantity, rec.day)

    # 消耗推演:从第 0 天补到昨天,得到当前真实存量
    for d in range(0, day):
        for village in ledger.villages:
            inventory.consume_day(village, d)
        inventory.expire_day(d)

    return Situation(case=case, at_hour=at_hour, day=day, days=days,
                     rain_by_day=rain_by_day, rain_basis=rain_basis,
                     road_now=road_now, intervals=intervals, inventory=inventory,
                     expected_inbound=expected, stuck=stuck,
                     vehicle_busy=vehicle_busy, planned_existing=planned_existing,
                     warnings=warnings)


def _delivery_item(ledger: Ledger, village: str) -> str:
    """历史到货只记村庄与路段;按该村消耗最大的物资入账。"""
    best, best_use = "water", -1
    for s in ledger.stocks:
        if s.site == village and s.use_per_day > best_use:
            best, best_use = s.item, s.use_per_day
    return best


# ----------------------------------------------------------------  需求分位

def _quantile(values: list[float], alpha: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(alpha * len(ordered)) - 1))
    return ordered[idx]


def _first_open_days(scenarios: list[dict[int, dict[str, bool]]],
                     routes: dict[str, list[tuple[str, ...]]],
                     village: str, days: list[int]) -> list[int | None]:
    """每个情景里村庄未来首个可到达日(无可到达日则为 None)。"""
    result: list[int | None] = []
    for s in scenarios:
        found = None
        for d in days:
            if any(all(s[d].get(e, False) for e in path) for path in routes.get(village, [])):
                found = d
                break
        result.append(found)
    return result


# ----------------------------------------------------------------  计划编制

@dataclass
class PlanRequest:
    ledger: Ledger
    case: str
    at_hour: int
    model: risk.RiskModel
    state: CaseState | None = None
    alpha: float = 0.8
    stance: str = "central"
    seed: int = DEFAULT_SEED
    n_scenarios: int = SCENARIOS


def _village_items(ledger: Ledger, village: str) -> list[str]:
    """该村需要保障的物资族:water 单列,电源类合并为 power。"""
    items = []
    for s in ledger.stocks:
        if s.site != village:
            continue
        if s.item in POWER_ITEMS:
            if "power" not in items:
                items.append("power")
        elif s.item not in items:
            items.append(s.item)
    return items


def _pool_usable(inventory: Inventory, village: str, family: str, day: int) -> int:
    if family == "power":
        return sum((inventory.get(village, it) or _null_stock()).on_hand(day)
                   for it in POWER_ITEMS if inventory.get(village, it))
    entry = inventory.get(village, family)
    return entry.on_hand(day) if entry else 0


def _null_stock():
    from .inventory import SiteStock
    return SiteStock(site="", item="", minimum=0, use_per_day=0, life_days=0,
                     substitute="", return_required=False)


def _pool_spec(ledger: Ledger, village: str, family: str) -> tuple[int, int]:
    """物资族的 (最低保障, 日消耗):电源类按该村台账条目合并。"""
    if family == "power":
        minimum = sum(s.minimum for s in ledger.stocks
                      if s.site == village and s.item in POWER_ITEMS)
        use = sum(s.use_per_day for s in ledger.stocks
                  if s.site == village and s.item in POWER_ITEMS)
        return minimum, use
    for s in ledger.stocks:
        if s.site == village and s.item == family:
            return s.minimum, s.use_per_day
    return 0, 0


def known_overrides(sit: Situation) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """按日汇总已确知状态:今天用当前已知;未来日含封闭时段的按已知封闭。"""
    known_closed: dict[int, set[str]] = {}
    known_open: dict[int, set[str]] = {}
    for d in sit.days:
        known_closed[d] = set()
        known_open[d] = set()
    for road, (state, _) in sit.road_now.items():
        if state == "closed":
            known_closed[sit.day].add(road)
        elif state == "open":
            known_open[sit.day].add(road)
    horizon_end = abs_hour(sit.days[-1] + 1, 0)
    for road, ivs in sit.intervals.items():
        for start, end in ivs:
            stop = horizon_end if end is None else end
            for d in sit.days:
                if d == sit.day:
                    continue
                if abs_hour(d, 0) < stop and abs_hour(d + 1, 0) > start:
                    known_closed[d].add(road)
    return known_closed, known_open


def plan(req: PlanRequest) -> dict:
    ledger = req.ledger
    sit = build_situation(ledger, req.case, req.at_hour, req.state)
    day, days = sit.day, sit.days
    future_days = [d for d in days if d > day]

    known_closed, known_open = known_overrides(sit)

    result = risk.simulate(req.model, ledger, future_days or [day + 1],
                           {d: sit.rain_by_day.get(d, {}) for d in (future_days or [day + 1])},
                           known_closed={d: known_closed.get(d, set()) for d in (future_days or [day + 1])},
                           known_open={d: known_open.get(d, set()) for d in (future_days or [day + 1])},
                           n_scenarios=req.n_scenarios, seed=req.seed, stance=req.stance)
    routes = ledger.routes()

    # 今日可通行路线:每段当前已知 open,且往返不撞已知封闭区间
    open_roads = {r for r, (st, _) in sit.road_now.items() if st == "open"}
    unknown_roads_now = {r for r, (st, _) in sit.road_now.items() if st == "unknown"}

    def route_ok(route: tuple[str, ...], depart: int) -> bool:
        if not all(e in open_roads for e in route):
            return False
        t = depart
        for e in route:
            dur = ledger.roads[e].duration
            if _intervals_hit(sit.intervals.get(e, []), t, t + dur) != -1:
                return False
            if _intervals_hit(sit.intervals.get(e, []), t + dur, t + 2 * dur) != -1:
                return False
            t += dur
        return True

    # 各村各物资族:今日应前置的量(分位)
    daily_capacity = sum(w.capacity for w in ledger.windows if w.day == day)
    needs: dict[str, dict[str, int]] = {}
    need_detail: dict[str, dict[str, dict]] = {}
    for village in ledger.villages:
        needs[village] = {}
        need_detail[village] = {}
        first_open = _first_open_days(result.scenarios, routes, village,
                                      future_days or [day + 1])
        for family in _village_items(ledger, village):
            minimum, use = _pool_spec(ledger, village, family)
            if minimum <= 0 and use <= 0:
                continue
            inbound: dict[int, list[tuple[str, int]]] = {}
            for d, item, qty, _src in sit.expected_inbound.get(village, []):
                inbound.setdefault(d, []).append((item, qty))
            # 无新投送时逐日缺口:日末可用可能为负(消耗透支),不能按 0 截断
            sim = sit.inventory.clone()
            gap_by_day: dict[int, int] = {}
            for d in days:
                for item, qty in inbound.get(d, []):
                    sim.add_arrival(village, item, qty, d)
                pre = _pool_usable(sim, village, family, d)
                gap_by_day[d] = max(0, minimum - (pre - use))
                sim.consume_day(village, d)
                sim.expire_day(d)
            per_scenario: list[float] = []
            for first in first_open:
                cover = [d for d in days if first is None or d < first]
                gap = max((gap_by_day[d] for d in cover), default=0)
                if first is not None and first in gap_by_day:
                    # 首个可到达日的补给受当日车队总运力限制,超出部分也要今日前置
                    gap = max(gap, gap_by_day[first] - daily_capacity)
                per_scenario.append(float(gap))
            qty = int(math.ceil(_quantile(per_scenario, req.alpha)))
            needs[village][family] = qty
            need_detail[village][family] = {
                "最低保障": minimum, "日消耗": use,
                "今日可用": _pool_usable(sit.inventory, village, family, day),
                "分位需求": qty,
                "逐日缺口": {str(d): g for d, g in gap_by_day.items()},
                "情景缺口": [int(x) for x in per_scenario[:12]],
            }

    # 今日放行:按最紧迫村优先分配车辆与路线;同车顺路带其他待保物资
    windows_today = [w for w in ledger.windows if w.day == day and w.close_abs > sit.at_hour]
    windows_today.sort(key=lambda w: w.open_abs)
    busy = sit.vehicle_busy.get(day, set())
    decisions: list[dict] = []
    depot_reserved: dict[str, int] = {}

    def depot_free(item: str, reserved: dict[str, int] | None = None) -> int:
        entry = sit.inventory.get("depot", item)
        on_hand = entry.on_hand(day) if entry else 0
        hold = (reserved if reserved is not None else depot_reserved).get(item, 0)
        return max(0, on_hand - hold)

    pending: dict[str, dict[str, int]] = {
        v: {f: q for f, q in needs[v].items() if q > 0} for v in ledger.villages
    }

    def pick_items(family: str, qty: int, reserved: dict[str, int]) -> dict[str, int]:
        """物资族 -> 具体物资并占用库存;电源类优先免回库、保质长的 battery。"""
        out: dict[str, int] = {}
        left = qty
        order = ("battery", "cells") if family == "power" else (family,)
        for item in order:
            give = min(left, depot_free(item, reserved))
            if give:
                out[item] = give
                reserved[item] = reserved.get(item, 0) + give
                left -= give
            if not left:
                break
        return out

    def route_villages(route: tuple[str, ...]) -> list[str]:
        """路径依次经过的村(不含县仓)。"""
        node = "depot"
        seq: list[str] = []
        for edge in route:
            road = ledger.roads[edge]
            node = road.right if road.left == node else road.left
            if node in pending:
                seq.append(node)
        return seq

    for window in windows_today:
        if window.vehicle in busy:
            continue
        depart = max(window.open_abs, sit.at_hour)
        # 候选(村,路线)按紧迫度排序,第一个能装出物资的成交
        candidates = []
        for village in ledger.villages:
            if not any(pending.get(village, {}).values()):
                continue
            urgency_key = min(
                (need_detail[village][f]["今日可用"] - need_detail[village][f]["最低保障"]
                 for f, q in pending[village].items() if q > 0), default=0)
            for route in routes.get(village, []):
                if not route_ok(route, depart):
                    continue
                round_trip = 2 * sum(ledger.roads[e].duration for e in route)
                if depart + round_trip > window.close_abs:
                    continue
                candidates.append((urgency_key, len(route), route))
        candidates.sort()
        committed = False
        for _, _, route in candidates:
            stops = route_villages(route)
            drops: list[dict] = []
            segment_load = [0] * len(route)
            stop_index = {v: i for i, v in enumerate(stops)}
            reserved = dict(depot_reserved)
            taken: list[tuple[str, str, int]] = []
            for v in stops:
                for family, qty in sorted(pending.get(v, {}).items()):
                    if qty <= 0:
                        continue
                    seg_upto = stop_index[v]
                    room = min(
                        window.capacity,
                        min(ledger.roads[route[i]].max_weight - segment_load[i]
                            for i in range(seg_upto + 1)),
                    )
                    if room <= 0:
                        continue
                    items = pick_items(family, min(qty, room), reserved)
                    got = sum(items.values())
                    if got <= 0:
                        continue
                    for item, q in items.items():
                        drops.append({"village": v, "item": item, "qty": q})
                        taken.append((v, family, q))
                    for i in range(seg_upto + 1):
                        segment_load[i] += got
            if not drops:
                continue
            depot_reserved.update(reserved)
            for v, family, q in taken:
                pending[v][family] -= q
                need_detail[v][family]["今日放行"] = (
                    need_detail[v][family].get("今日放行", 0) + q)
            decisions.append({
                "vehicle": window.vehicle, "day": day, "depart_hour": depart,
                "route": list(route), "drops": drops, "backhaul": [],
                "note": f"保障 {'、'.join(sorted({d['village'] for d in drops}))}",
            })
            busy.add(window.vehicle)
            committed = True
            break
        if not committed:
            continue

    for village in ledger.villages:
        for family, qty in needs[village].items():
            left = pending.get(village, {}).get(family, 0)
            need_detail[village][family].setdefault("今日放行", 0)
            if left > 0:
                need_detail[village][family]["今日缺口"] = left

    # 回程带回待回库件(当天转运出去的托盘当天收不回)
    for dec in decisions:
        end_village = dec["drops"][-1]["village"]
        ready = sit.inventory.backhaul_ready(end_village, day)
        if ready:
            window = next(w for w in windows_today if w.vehicle == dec["vehicle"])
            for item, q in ready.items():
                take = min(q, window.capacity)
                if take > 0:
                    dec["backhaul"].append({"village": end_village, "item": item, "qty": take})

    # 展望:含今日放行与机会式后续补给,逐情景核对最低保障
    outlook, uncoverable = _outlook(ledger, sit, result, routes, decisions,
                                    needs, req.alpha, depot_reserved)

    # 未知风险路段:不凭点估计承诺,列出人工核实与潜在可承诺增量
    unknowns = _unknowns(ledger, req.model, sit, routes, open_roads,
                         unknown_roads_now, needs, req.alpha)

    # 台账中已登记未执行的调度:逐条评估按现知条件能否照办
    planned_review = []
    for p in sit.planned_existing:
        p_day = p["day"]
        if p_day < day:
            continue
        depart = abs_hour(p_day, p["hour"])
        verdict, reason = "可按原案执行", ""
        window = next((w for w in ledger.windows
                       if w.day == p_day and w.vehicle == p["vehicle"]), None)
        if window is None:
            verdict, reason = "需调整", "当日无此车窗口"
        else:
            round_trip = 2 * sum(ledger.roads[e].duration for e in p["route"])
            if depart + round_trip > window.close_abs:
                verdict, reason = "需调整", "往返超出出车窗口"
            else:
                t = depart
                for edge in p["route"]:
                    dur = ledger.roads[edge].duration
                    hit = _intervals_hit(sit.intervals.get(edge, []), t, t + dur)
                    if hit != -1:
                        verdict = "需调整"
                        reason = (f"{edge} 在行程中与封控冲突"
                                  + ("(解除时间未定)" if hit is None else f"(第{hit}时后解除)"))
                        break
                    t += dur
            if p["vehicle"] in sit.vehicle_busy.get(p_day, set()):
                verdict, reason = "需调整", f"{p['vehicle']} 当日已有任务"
        planned_review.append({**p, "评估": verdict, "原因": reason})

    plan_doc = {
        "case": req.case,
        "at_hour": req.at_hour,
        "时点": f"第{day}天{hour_of(req.at_hour)}时",
        "stance": req.stance,
        "alpha": req.alpha,
        "seed": req.seed,
        "scenarios": req.n_scenarios,
        "decisions": decisions,
        "needs": need_detail,
        "outlook": outlook,
        "uncoverable": uncoverable,
        "unknowns": unknowns,
        "stuck": sit.stuck,
        "planned_existing": sit.planned_existing,
        "planned_review": planned_review,
        "next_recalc": next_recalc(ledger, req.case, req.at_hour),
        "basis": _basis(ledger, req, sit),
        "warnings": sit.warnings,
    }
    return plan_doc


def _outlook(ledger: Ledger, sit: Situation, result: risk.ScenarioResult,
             routes: dict[str, list[tuple[str, ...]]], decisions: list[dict],
             needs: dict[str, dict[str, int]], alpha: float,
             depot_reserved: dict[str, int]) -> tuple[dict, list[dict]]:
    """逐村逐日核对:今日放行 + 机会式后续补给下最低保障的守住比例。"""
    days = sit.days
    day = sit.day
    future_days = [d for d in days if d > day]
    firm_by_village: dict[str, dict[int, list[tuple[str, int]]]] = {v: {} for v in ledger.villages}
    for dec in decisions:
        for drop in dec["drops"]:
            firm_by_village.setdefault(drop["village"], {}).setdefault(day, []).append(
                (drop["item"], drop["qty"]))
    depot_left: dict[str, int] = {}
    for item in {s.item for s in ledger.stocks if s.site == "depot"}:
        entry = sit.inventory.get("depot", item)
        depot_left[item] = max(0, (entry.on_hand(day) if entry else 0)
                               - depot_reserved.get(item, 0))
    daily_capacity = sum(w.capacity for w in ledger.windows if w.day == day)

    outlook: dict[str, dict] = {}
    uncoverable: list[dict] = []
    for village in ledger.villages:
        outlook[village] = {}
        for family in _village_items(ledger, village):
            minimum, use = _pool_spec(ledger, village, family)
            if minimum <= 0 and use <= 0:
                continue
            held = {d: 0 for d in days}
            short_mean = {d: 0.0 for d in days}
            n = len(result.scenarios)
            for s in result.scenarios:
                sim = sit.inventory.clone()
                for item, qty in (firm_by_village.get(village, {}).get(day, [])):
                    sim.add_arrival(village, item, qty, day)
                depot_pool = dict(depot_left)
                for d in days:
                    for dd, item, qty, _ in sit.expected_inbound.get(village, []):
                        if dd == d:
                            sim.add_arrival(village, item, qty, d)
                    if d > day:
                        open_today = any(all(s[d].get(e, False) for e in path)
                                         for path in routes.get(village, []))
                        if open_today:
                            usable = _pool_usable(sim, village, family, d)
                            gap = minimum + use - usable
                            if gap > 0:
                                bring = min(gap, daily_capacity)
                                bring = _draw_from_pool(depot_pool, family, bring)
                                if bring > 0:
                                    _add_family(sim, village, family, bring, d, ledger)
                    sim.consume_day(village, d)
                    sim.expire_day(d)
                    usable = _pool_usable(sim, village, family, d)
                    if usable >= minimum:
                        held[d] += 1
                    else:
                        short_mean[d] += (minimum - usable)
            row = {}
            for d in days:
                row[d] = {"held": round(held[d] / n, 3),
                          "shortfall_mean": round(short_mean[d] / n, 2)}
                if held[d] / n < alpha:
                    uncoverable.append({
                        "village": village, "family": family, "day": d,
                        "守住比例": round(held[d] / n, 3),
                        "平均缺口": round(short_mean[d] / n, 2),
                        "说明": "在当前资源与风险水平下无法承诺守住",
                    })
            outlook[village][family] = row
    return outlook, uncoverable


def _draw_from_pool(pool: dict[str, int], family: str, qty: int) -> int:
    if family == "power":
        got = 0
        for item in ("battery", "cells"):
            take = min(pool.get(item, 0), qty - got)
            pool[item] = pool.get(item, 0) - take
            got += take
        return got
    take = min(pool.get(family, 0), qty)
    pool[family] = pool.get(family, 0) - take
    return take


def _add_family(sim: Inventory, village: str, family: str, qty: int, day: int,
                ledger: Ledger) -> None:
    if family == "power":
        target = "battery" if sim.get(village, "battery") is not None else "cells"
        sim.add_arrival(village, target, qty, day)
    else:
        sim.add_arrival(village, family, qty, day)


def _unknowns(ledger: Ledger, model: risk.RiskModel, sit: Situation,
              routes: dict[str, list[tuple[str, ...]]], open_roads: set[str],
              unknown_now: set[str], needs: dict[str, dict[str, int]],
              alpha: float) -> list[dict]:
    """风险未知或当前状态不明的路段:说明对可承诺数量与核实工作的影响。"""
    out: list[dict] = []
    model_unknown = set(model.unknown_roads)
    for road in sorted(model_unknown | unknown_now):
        affected = [v for v, opts in routes.items()
                    if any(road in p for p in opts)]
        if not affected:
            continue
        only_routes = [v for v in affected
                       if all(road in p for p in routes[v])]
        alt = [v for v in affected if v not in only_routes]
        reasons = []
        if road in model_unknown:
            src = [k for k, v in model.closure_source.items()
                   if k[0] == road and v == "unknown"]
            levels = "、".join(sorted({lv for _, lv in src})) or "各雨量等级"
            reasons.append(f"历史观察不足({levels})")
        if road in unknown_now:
            reasons.append("当前路况未知(无有效巡查或通知)")
        out.append({
            "road": road,
            "原因": ";".join(reasons),
            "受影响村": sorted(affected),
            "无替代路线的村": sorted(only_routes),
            "有替代路线的村": sorted(alt),
            "处理": "相关承诺按该路不可用保守计算;安排巡测或向乡镇核实后方可上调承诺",
            "承诺影响": "经该路的补给量未计入可承诺数量",
        })
    return out


def next_recalc(ledger: Ledger, case: str, at_hour: int) -> dict:
    """下一次可重算时点:最近的未来信息事件或出车窗口。"""
    events: list[tuple[int, str]] = []
    for fc in ledger.forecasts:
        if fc.case == case and fc.published_hour > at_hour:
            events.append((fc.published_hour, f"第{fc.day}天{fc.run_hour}时预报版本送达"))
    for p in ledger.patrols:
        if p.case == case and p.known_hour > at_hour:
            events.append((p.known_hour, f"{p.road} 巡查结果送达"))
    for n in ledger.notices:
        if n.case == case and n.known_hour > at_hour:
            events.append((n.known_hour, f"通知 {n.notice} 送达"))
        if n.case == case and n.end_hour is not None and n.end_hour > at_hour:
            events.append((n.end_hour, f"通知 {n.notice} 封闭解除"))
    for w in ledger.windows:
        if w.open_abs > at_hour:
            events.append((w.open_abs, f"{w.vehicle} 出车窗口开启"))
    if not events:
        return {"hour": None, "label": "过程内无预定信息事件"}
    hour, label = min(events)
    return {"hour": hour, "label": f"第{day_of(hour)}天{hour_of(hour)}时:{label}"}


def _basis(ledger: Ledger, req: PlanRequest, sit: Situation) -> dict:
    return {
        "训练案例": req.model.train_cases,
        "预报版本": {str(d): {r: b for r, b in sit.rain_basis[d].items()}
                     for d in sit.days},
        "路况依据": {r: {"状态": st, "依据": bs} for r, (st, bs) in sit.road_now.items()},
        "可见通知": [n.notice for n in ledger.notices
                     if n.case == req.case and n.known_hour <= req.at_hour],
        "说明": "仅使用决策时点前已送达的预报版本、通知、巡查与运输记录",
    }
