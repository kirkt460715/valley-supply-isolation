"""适用范围比较:不同路网、货种与时长下,保供工具能承诺什么。

仓管可以横向比较:
- 路网:完整路网 / 去掉某段(如 E5 直达段不可用)后的可达性;
- 货种:水(无替代)与电源类(可互顶、需回库)的不同约束;
- 时长:1/2/3 天连降暴雨下,孤立风险与可承诺保障如何变化。
"""

from __future__ import annotations

from . import risk
from .inventory import Inventory
from .ledger import Ledger, latest_forecasts, abs_hour
from .plan import _pool_spec, _village_items

NETWORKS = {
    "完整路网": frozenset(),
    "无E5直达": frozenset({"E5"}),
    "无E6支线": frozenset({"E6"}),
    "无E5无E6": frozenset({"E5", "E6"}),
}


def compare(ledger: Ledger, case: str, at_hour: int, model: risk.RiskModel,
            horizons: list[int] | None = None, alpha: float = 0.8,
            stance: str = "central", seed: int = 20261124,
            n_scenarios: int = 400) -> dict:
    horizons = horizons or [1, 2, 3]
    day = at_hour // 24
    latest = latest_forecasts(ledger.forecasts, case, at_hour)
    tables: list[dict] = []
    for net_name, blocked in NETWORKS.items():
        routes = ledger.routes(blocked=blocked)
        for span in horizons:
            days = [day + i for i in range(1, span + 1)]
            rain_by_day = {}
            for d in days:
                rain_by_day[d] = {}
                for road in ledger.roads:
                    fc = latest.get((d, road))
                    rain_by_day[d][road] = fc.rain if fc else None
            result = risk.simulate(model, ledger, days, rain_by_day,
                                   n_scenarios=n_scenarios, seed=seed, stance=stance)
            metrics = risk.isolation_metrics(result, routes, model.unknown_roads)
            for village in ledger.villages:
                for family in _village_items(ledger, village):
                    minimum, use = _pool_spec(ledger, village, family)
                    tables.append({
                        "路网": net_name,
                        "时长": span,
                        "村": village,
                        "货种": family,
                        "最低保障": minimum,
                        "日消耗": use,
                        "孤立概率": round(max(metrics["per_village"][village].values()), 3),
                        "全程无可达日概率": round(metrics["no_open_day_prob"][village], 3),
                        "最长连续孤立(天,90分位)": _duration_quantile(
                            metrics["duration_dist"][village], 0.9),
                    })
    return {
        "case": case, "at_hour": at_hour, "stance": stance, "alpha": alpha,
        "说明": "孤立概率为推演期内最高单日值;未知风险路段按不可用保守处理",
        "未知风险路段": model.unknown_roads,
        "比较表": tables,
    }


def _duration_quantile(dist: dict[int, float], q: float) -> int:
    acc = 0.0
    for days, prob in sorted(dist.items()):
        acc += prob
        if acc >= q:
            return days
    return max(dist) if dist else 0
