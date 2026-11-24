"""断路风险模型:区间删失下的经验估计、共模失效与情景模拟。

设计原则:
- 只用训练窗(K001–K030 等指定案例)拟合,当前演练案例的巡查/通知
  只作为"已知状态"覆盖,绝不回灌参数;复盘案例整段留出。
- 巡查只给离散时点状态:两次巡查之间断没断、什么时候断的都不知道;
  末次巡查仍 closed 的,结束时间超出观察期(右删失)。因此日尺度
  结果定义为"当日任一有效巡查见 closed",持续期用相邻巡查对的
  转移经验估计,两者都如实标注为观察下界。
- 送货记录的空缺既不是可达证据也不是断货证据:上游一断下游就不再
  回报。模型只把"有确证到货"当作该路当日曾可通行的旁证,用于复盘
  诊断,不把空缺记录当作 closed 训练标签。
- 同一 shared 防护工程的路段共模失效:用"组冲击 + 各自冲击"的
  两分量结构表达,使多个村同时无法补给的联合风险可以被计算。
- 观察次数不足的路段不给点估计,标记为"未知",由调用方决定
  需要怎样的人工核实。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .ledger import Ledger, Patrol, latest_forecasts

RAIN_LEVELS = ("dry", "wet", "severe")
MIN_OBS = 5  # 单元格最少有效观察天数,不足则回落到并集,再不足则未知
PRIOR_STRENGTH = 4.0  # 平滑先验强度(等效先验天数)


# ----------------------------------------------------------------  Beta 工具

def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """正则不完全 Beta 函数 I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_interval(k: float, n: float, mass: float = 0.90) -> tuple[float, float]:
    """Beta(k, n-k) 后验的中心可信区间(先验已并入 k、n)。"""
    if n <= 0:
        return (0.0, 1.0)
    lo_q = (1.0 - mass) / 2.0
    a, b = k, n - k
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if betai(a, b, mid) < lo_q:
            lo = mid
        else:
            hi = mid
    lower = (lo + hi) / 2.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if betai(a, b, mid) < 1.0 - lo_q:
            lo = mid
        else:
            hi = mid
    return lower, (lo + hi) / 2.0


# ----------------------------------------------------------------  训练数据

@dataclass
class DayOutcome:
    case: str
    road: str
    day: int
    closed: bool | None  # None = 当日巡查均 unknown,无法判定


@dataclass
class RiskModel:
    """拟合结果。所有概率均为日尺度。"""
    train_cases: list[str]
    closure: dict[tuple[str, str], tuple[float, float, float, int]] = field(default_factory=dict)
    # (road, rain) -> (均值, 区间下, 区间上, 有效观察天数);观察不足时回落并集
    closure_source: dict[tuple[str, str], str] = field(default_factory=dict)
    # (road, rain) -> "road" / "pooled" / "unknown"
    persistence: dict[str, tuple[float, int]] = field(default_factory=dict)
    # road -> (今日断则明日仍断的概率, 观察对数)
    group_shock: dict[str, float] = field(default_factory=dict)
    # shared 组 -> 共模冲击概率(同日整组失效的份额)
    groups: dict[str, list[str]] = field(default_factory=dict)
    climatology: dict[str, float] = field(default_factory=dict)  # rain -> 并集断路率
    unknown_roads: list[str] = field(default_factory=list)

    def daily_probability(self, road: str, rain: str | None,
                          stance: str = "central") -> float | None:
        """某日某路断路概率。rain 为 None 时用气候率。未知返回 None。"""
        if rain is None:
            pool = [self.closure.get((road, level)) for level in RAIN_LEVELS]
            pool = [p for p in pool if p is not None]
            if not pool:
                return None
            value = sum(p[0] for p in pool) / len(pool)
            if stance == "cautious":
                value = max(value, max(p[2] for p in pool))
            return value
        entry = self.closure.get((road, rain))
        if entry is None:
            return None
        mean, lo, hi, _ = entry
        return hi if stance == "cautious" else mean


def _day_outcomes(patrols: list[Patrol], cases: set[str]) -> list[DayOutcome]:
    """日尺度结果:当日任一有效巡查 closed 记 True;全 unknown 记 None。"""
    by_key: dict[tuple[str, str, int], list[str]] = {}
    for p in patrols:
        if p.case not in cases:
            continue
        by_key.setdefault((p.case, p.road, p.day), []).append(p.state)
    outcomes = []
    for (case, road, day), states in sorted(by_key.items()):
        known = [s for s in states if s in ("open", "closed")]
        if not known:
            outcomes.append(DayOutcome(case, road, day, None))
        else:
            outcomes.append(DayOutcome(case, road, day, "closed" in known))
    return outcomes


def _persistence_pairs(patrols: list[Patrol], cases: set[str]) -> dict[str, list[tuple[bool, bool]]]:
    """相邻两次有效巡查的 (前闭, 后闭) 对,跨天也算;unknown 跳过。"""
    by_road: dict[str, dict[str, list[Patrol]]] = {}
    for p in patrols:
        if p.case not in cases:
            continue
        by_road.setdefault(p.road, {}).setdefault(p.case, []).append(p)
    pairs: dict[str, list[tuple[bool, bool]]] = {}
    for road, per_case in by_road.items():
        road_pairs: list[tuple[bool, bool]] = []
        for case, rows in per_case.items():
            rows = sorted(rows, key=lambda p: p.obs_abs)
            prev = None
            for p in rows:
                if p.state not in ("open", "closed"):
                    continue  # 观察缺失,边界顺延到下一个有效巡查
                cur = p.state == "closed"
                if prev is not None:
                    road_pairs.append((prev, cur))
                prev = cur
        pairs[road] = road_pairs
    return pairs


def fit(ledger: Ledger, train_cases: list[str]) -> RiskModel:
    """只用指定训练案例拟合。任何 known_hour 晚到的记录不影响本函数,
    因为训练案例属于已完整观察的历史窗;复盘与当前案例不得传入。"""
    train_set = set(train_cases)
    patrols = [p for p in ledger.patrols if p.case in train_set]
    forecasts = [f for f in ledger.forecasts if f.case in train_set]
    outcomes = _day_outcomes(ledger.patrols, train_set)
    # (case, day, road) -> 当日最终版雨量(训练窗已完整可见)
    rain_of: dict[tuple[str, int, str], tuple[str, int]] = {}
    for fc in forecasts:
        key = (fc.case, fc.day, fc.road)
        prev = rain_of.get(key)
        if prev is None or fc.published_hour > prev[1]:
            rain_of[key] = (fc.rain, fc.published_hour)
    # 单元格计数
    cell: dict[tuple[str, str], list[int]] = {}  # (road, rain) -> [closed, total]
    pool: dict[str, list[int]] = {}  # rain -> [closed, total]
    for oc in outcomes:
        if oc.closed is None:
            continue
        rain = rain_of.get((oc.case, oc.day, oc.road))
        level = rain[0] if rain else None
        if level is None:
            continue
        c = cell.setdefault((oc.road, level), [0, 0])
        c[1] += 1
        c[0] += int(oc.closed)
        pl = pool.setdefault(level, [0, 0])
        pl[1] += 1
        pl[0] += int(oc.closed)

    model = RiskModel(train_cases=sorted(train_set))
    groups: dict[str, list[str]] = {}
    for road in ledger.roads.values():
        groups.setdefault(road.shared, []).append(road.road)
    model.groups = {g: sorted(rs) for g, rs in groups.items()}

    for level in RAIN_LEVELS:
        c, n = pool.get(level, [0, 0])
        model.climatology[level] = (c + 1.0) / (n + 2.0) if n else 0.5

    roads = sorted(ledger.roads)
    for road in roads:
        for level in RAIN_LEVELS:
            c, n = cell.get((road, level), [0, 0])
            pc, pn = pool.get(level, [0, 0])
            if n >= MIN_OBS:
                prior = model.climatology[level]
                k = c + PRIOR_STRENGTH * prior
                nn = n + PRIOR_STRENGTH
                lo, hi = beta_interval(k, nn)
                model.closure[(road, level)] = (k / nn, lo, hi, n)
                model.closure_source[(road, level)] = "road"
            elif pn >= MIN_OBS:
                k = pc + 1.0
                nn = pn + 2.0
                lo, hi = beta_interval(k, nn)
                model.closure[(road, level)] = (k / nn, lo, hi, pn)
                model.closure_source[(road, level)] = "pooled"
            else:
                model.closure_source[(road, level)] = "unknown"
    model.unknown_roads = sorted(
        {road for (road, level), src in model.closure_source.items() if src == "unknown"}
    )

    # 持续期:今日断、明日仍断的经验概率(相邻有效巡查对)
    pairs = _persistence_pairs(ledger.patrols, train_set)
    all_pairs = [pr for rs in pairs.values() for pr in rs]
    pool_cc = sum(1 for a, b in all_pairs if a and b)
    pool_c = sum(1 for a, _ in all_pairs if a)
    pool_rate = (pool_cc + 1.0) / (pool_c + 2.0) if pool_c else 0.5
    for road in roads:
        rs = pairs.get(road, [])
        cc = sum(1 for a, b in rs if a and b)
        c = sum(1 for a, _ in rs if a)
        if c >= MIN_OBS:
            model.persistence[road] = ((cc + PRIOR_STRENGTH * pool_rate) / (c + PRIOR_STRENGTH), c)
        elif pool_c:
            model.persistence[road] = (pool_rate, pool_c)
        else:
            model.persistence[road] = (0.5, 0)

    # 共模冲击:同日整组皆闭的份额超出独立假设的部分
    by_group_day: dict[tuple[str, str, int], list[bool]] = {}
    for oc in outcomes:
        if oc.closed is None:
            continue
        for g, rs in model.groups.items():
            if oc.road in rs and len(rs) > 1:
                by_group_day.setdefault((g, oc.case, oc.day), []).append(oc.closed)
    for g, rs in model.groups.items():
        if len(rs) < 2:
            model.group_shock[g] = 0.0
            continue
        joint = [v for (gg, _, _), v in by_group_day.items() if gg == g and len(v) == len(rs)]
        if len(joint) < MIN_OBS:
            model.group_shock[g] = 0.0
            continue
        both = sum(1 for v in joint if all(v)) / len(joint)
        marginals = []
        for road in rs:
            rates = [model.closure.get((road, lv)) for lv in RAIN_LEVELS]
            rates = [r[0] for r in rates if r]
            marginals.append(sum(rates) / len(rates) if rates else both)
        m1, m2 = marginals[0], marginals[1]
        denom = (1.0 - m1) * (1.0 - m2)
        q = (both - m1 * m2) / denom if denom > 1e-9 else 0.0
        model.group_shock[g] = min(1.0, max(0.0, q))
    return model


# ----------------------------------------------------------------  情景模拟

@dataclass
class ScenarioResult:
    days: list[int]
    scenarios: list[dict[int, dict[str, bool]]]  # day -> road -> open?
    rain_used: dict[int, dict[str, str | None]]  # day -> road -> 采用的雨量( None=气候率)
    seed: int
    stance: str


def simulate(model: RiskModel, ledger: Ledger, days: list[int],
             rain_by_day: dict[int, dict[str, str | None]],
             known_closed: dict[int, set[str]] | None = None,
             known_open: dict[int, set[str]] | None = None,
             n_scenarios: int = 400, seed: int = 20261124,
             stance: str = "central") -> ScenarioResult:
    """联合情景:组冲击同步整组失效,持续期让断路跨日延续。

    known_closed / known_open:决策时点已确知的状态(通知、巡查),
    直接覆盖抽样,保证"已知条件"不被模型改写。
    """
    known_closed = known_closed or {}
    known_open = known_open or {}
    rng = random.Random(seed)
    roads = sorted(ledger.roads)
    group_of = {r: ledger.roads[r].shared for r in roads}
    scenarios: list[dict[int, dict[str, bool]]] = []
    for _ in range(n_scenarios):
        state: dict[int, dict[str, bool]] = {}
        prev_closed: set[str] = set()
        for day in days:
            today: dict[str, bool] = {}
            shock_draw = {g: rng.random() for g in model.groups}
            for road in roads:
                if road in known_closed.get(day, set()):
                    today[road] = False
                    continue
                if road in known_open.get(day, set()):
                    today[road] = True
                    continue
                if road in prev_closed:
                    persist, _ = model.persistence.get(road, (0.5, 0))
                    if rng.random() < persist:
                        today[road] = False
                        continue
                rain = rain_by_day.get(day, {}).get(road)
                p = model.daily_probability(road, rain, stance)
                if p is None:
                    p = 1.0  # 未知按不可用处理(保守),并在结果中标注
                g = group_of[road]
                q = model.group_shock.get(g, 0.0)
                q = min(q, p)
                own = (p - q) / (1.0 - q) if q < 1.0 else 0.0
                closed = shock_draw[g] < q or rng.random() < own
                today[road] = not closed
            state[day] = today
            prev_closed = {r for r, ok in today.items() if not ok}
        scenarios.append(state)
    return ScenarioResult(days=list(days), scenarios=scenarios,
                          rain_used=rain_by_day, seed=seed, stance=stance)


# ----------------------------------------------------------------  孤立风险

def isolation_metrics(result: ScenarioResult,
                      routes: dict[str, list[tuple[str, ...]]],
                      unknown_roads: list[str]) -> dict:
    """各村每日孤立概率、联合孤立概率与孤立持续天数分布。"""
    villages = sorted(routes)
    days = result.days
    n = len(result.scenarios)
    per_village: dict[str, dict[int, float]] = {v: {} for v in villages}
    joint_pairs: dict[tuple[str, str], dict[int, float]] = {}
    duration: dict[str, dict[int, int]] = {v: {} for v in villages}  # 连续孤立天数 -> 次数
    no_open_day: dict[str, int] = {v: 0 for v in villages}

    def isolated(state_day: dict[str, bool], village: str) -> bool:
        opts = routes.get(village, [])
        if not opts:
            return True
        return not any(all(state_day.get(e, False) for e in path) for path in opts)

    for v in villages:
        for d in days:
            per_village[v][d] = sum(1 for s in result.scenarios if isolated(s[d], v)) / n
        for s in result.scenarios:
            run = 0
            best = 0
            for d in days:
                if isolated(s[d], v):
                    run += 1
                    best = max(best, run)
                else:
                    run = 0
            duration[v][best] = duration[v].get(best, 0) + 1
            if all(isolated(s[d], v) for d in days):
                no_open_day[v] += 1
    for i, a in enumerate(villages):
        for b in villages[i + 1:]:
            joint_pairs[(a, b)] = {
                d: sum(1 for s in result.scenarios
                       if isolated(s[d], a) and isolated(s[d], b)) / n
                for d in days
            }
    all_joint = {
        d: sum(1 for s in result.scenarios
               if all(isolated(s[d], v) for v in villages)) / n
        for d in days
    }
    return {
        "per_village": per_village,
        "joint_pairs": {f"{a}&{b}": v for (a, b), v in joint_pairs.items()},
        "all_villages": all_joint,
        "duration_dist": {v: {k: c / n for k, c in sorted(duration[v].items())}
                          for v in villages},
        "no_open_day_prob": {v: no_open_day[v] / n for v in villages},
        "unknown_roads": list(unknown_roads),
        "n_scenarios": n,
    }
