"""独立复盘:留出案例的预测检验与晚到确认复评。

规则:
- 参数只用训练案例拟合一次;复盘案例(K031–K040)整段留出,
  其记录只用于打分,绝不回灌参数。
- 每个预测在作出时落盘(含所用预报版本与发布时间);之后看到
  晚到确认时,用同一份落盘预测重新打分——复评不改预测本身。
- 送货记录的空缺只作诊断:上游断则下游不再回报,空缺既不能
  判可达也不能判没货。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from . import risk
from .ledger import Ledger, Patrol, abs_hour, latest_forecasts

DECISION_HOUR = 4  # 每日决策时点:首班出车窗口开启


def model_fingerprint(train_cases: list[str]) -> str:
    payload = json.dumps({"train": sorted(train_cases)}, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_predictions(ledger: Ledger, model: risk.RiskModel,
                     holdout_cases: list[str]) -> list[dict]:
    """对每个留出案例的每一路每一天,在决策时点作日尺度断路预测。"""
    predictions: list[dict] = []
    for case in holdout_cases:
        days = sorted({f.day for f in ledger.forecasts if f.case == case})
        for day in days:
            at = abs_hour(day, DECISION_HOUR)
            latest = latest_forecasts(ledger.forecasts, case, at)
            for road in sorted(ledger.roads):
                fc = latest.get((day, road))
                rain = fc.rain if fc else None
                prob = model.daily_probability(road, rain, "central")
                entry = model.closure.get((road, rain)) if rain else None
                predictions.append({
                    "case": case, "day": day, "road": road,
                    "decided_at": at,
                    "rain": rain,
                    "预报版本": (f"第{fc.day}天{fc.run_hour}时版"
                                 if fc else "尚未发布,用气候率"),
                    "prob": prob,  # None = 风险未知
                    "interval": ([entry[1], entry[2]] if entry else None),
                    "source": model.closure_source.get((road, rain), "unknown")
                              if rain else "climatology",
                })
    return predictions


def outcome_of(patrols: list[Patrol], case: str, road: str, day: int,
               score_as_of: int | None) -> str:
    """复盘日的实际结果:closed / open / pending(确认尚未到齐)。"""
    seen_closed = False
    for p in patrols:
        if p.case != case or p.road != road or p.day != day:
            continue
        if score_as_of is not None and p.known_hour > score_as_of:
            return "pending"  # 当日还有确认在路上,暂不打分
        if p.state == "closed":
            seen_closed = True
        elif p.state == "unknown":
            return "pending"
    return "closed" if seen_closed else "open"


def score(predictions: list[dict], ledger: Ledger,
          score_as_of: int | None = None) -> dict:
    """对落盘预测打分;score_as_of 之后才知道的结果列为待确认。"""
    brier_sum = 0.0
    log_sum = 0.0
    n = 0
    pending = 0
    unknown = 0
    buckets: dict[str, list[int]] = {}
    for pred in predictions:
        oc = outcome_of(ledger.patrols, pred["case"], pred["road"],
                        pred["day"], score_as_of)
        if oc == "pending":
            pending += 1
            continue
        p = pred["prob"]
        if p is None:
            unknown += 1
            continue
        y = 1.0 if oc == "closed" else 0.0
        p = min(max(p, 1e-6), 1 - 1e-6)
        brier_sum += (p - y) ** 2
        log_sum += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        n += 1
        key = f"{int(p * 5) * 20}-{int(p * 5) * 20 + 20}%"
        buckets.setdefault(key, [0, 0])
        buckets[key][0] += int(y)
        buckets[key][1] += 1
    calibration = [
        {"预测区间": k, "观察断路率": round(v[0] / v[1], 3), "样本": v[1]}
        for k, v in sorted(buckets.items())
    ]
    return {
        "样例数": n,
        "待确认": pending,
        "风险未知未评分": unknown,
        "Brier": round(brier_sum / n, 4) if n else None,
        "对数损失": round(log_sum / n, 4) if n else None,
        "校准表": calibration,
    }


def joint_review(ledger: Ledger, model: risk.RiskModel,
                 holdout_cases: list[str], seed: int = 20261124) -> dict:
    """联合风险检验:同组路段同日共断、多村同时孤立的预测与实测。"""
    routes = ledger.routes()
    villages = ledger.villages
    rows = []
    for case in holdout_cases:
        days = sorted({f.day for f in ledger.forecasts if f.case == case})
        for day in days:
            at = abs_hour(day, DECISION_HOUR)
            latest = latest_forecasts(ledger.forecasts, case, at)
            rain_by_day = {day: {r: (latest.get((day, r)).rain
                                   if latest.get((day, r)) else None)
                                 for r in ledger.roads}}
            result = risk.simulate(model, ledger, [day], rain_by_day,
                                   n_scenarios=400, seed=seed)
            metrics = risk.isolation_metrics(result, routes, model.unknown_roads)
            # 实测:当日巡查结果
            closed = {r for r in ledger.roads
                      if outcome_of(ledger.patrols, case, r, day, None) == "closed"}
            pending = {r for r in ledger.roads
                       if outcome_of(ledger.patrols, case, r, day, None) == "pending"}
            observed = {}
            for v in villages:
                opts = routes.get(v, [])
                if any(any(e in pending for e in p) for p in opts) and \
                   not any(all(e in closed for e in p) for p in opts):
                    # 有路径状态不全,且没有一条全闭的确定证据
                    if not any(all(e not in closed for e in p) for p in opts):
                        observed[v] = None
                    else:
                        observed[v] = False
                else:
                    observed[v] = not any(all(e not in closed for e in p) for p in opts)
            rows.append({"case": case, "day": day,
                         "predicted": {v: metrics["per_village"][v][day] for v in villages},
                         "joint_all": metrics["all_villages"][day],
                         "observed": observed})
    # 汇总:预测均值 vs 实测频率(剔除状态不全的样本)
    summary = {}
    for v in villages:
        ps, ys = [], []
        for row in rows:
            if row["observed"][v] is not None:
                ps.append(row["predicted"][v])
                ys.append(1.0 if row["observed"][v] else 0.0)
        summary[v] = {
            "预测孤立率均值": round(sum(ps) / len(ps), 3) if ps else None,
            "实测孤立率": round(sum(ys) / len(ys), 3) if ys else None,
            "样本": len(ys),
        }
    # 多村同时孤立:预测联合概率 vs 实测联合频率
    joint_p, joint_y = [], []
    for row in rows:
        if all(row["observed"][v] is not None for v in villages):
            joint_p.append(row["joint_all"])
            joint_y.append(1.0 if all(row["observed"][v] for v in villages) else 0.0)
    joint_summary = {
        "预测均值": round(sum(joint_p) / len(joint_p), 3) if joint_p else None,
        "实测频率": round(sum(joint_y) / len(joint_y), 3) if joint_y else None,
        "样本": len(joint_y),
    }
    return {"per_village": summary, "全体同时孤立": joint_summary,
            "明细样例": rows[:6]}


def missingness_diagnostic(ledger: Ledger, train_cases: list[str]) -> dict:
    """空记录诊断:空缺既不能判可达,也不能判没货。

    有确证到货只能说明该路当日曾可通行(至少一度);没有记录则
    什么都不能说明——断路日可能有货在断前送达,畅通日也可能
    根本没有排配送。
    """
    train = set(train_cases)
    last_mile: dict[str, str] = {}
    for d in ledger.deliveries:
        last_mile[d.village] = d.road
    patrol_state: dict[tuple[str, int, str], set[str]] = {}
    for p in ledger.patrols:
        if p.case in train and p.state in ("open", "closed"):
            patrol_state.setdefault((p.case, p.day, p.road), set()).add(p.state)
    reported = {(d.case, d.day, d.village) for d in ledger.deliveries if d.case in train}
    days = sorted({d for (c, d, _) in patrol_state})
    closed_with = closed_total = open_with = open_total = 0
    for case in train:
        for day in days:
            for village, road in last_mile.items():
                states = patrol_state.get((case, day, road))
                if not states:
                    continue
                has = (case, day, village) in reported
                if "closed" in states:
                    closed_total += 1
                    closed_with += has
                else:
                    open_total += 1
                    open_with += has
    return {
        "末段巡查见断的村-日": closed_total,
        "其中仍有到货确证": closed_with,
        "末段巡查全通的村-日": open_total,
        "其中有到货确证": open_with,
        "解读": ("断路日仍可能有货在断前送达,畅通日也可能没有配送;"
                 "有确证只说明当日曾可通行,空缺记录任何方向都不能判"),
    }


def run_review(ledger: Ledger, train_cases: list[str], holdout_cases: list[str],
               store: Path, score_as_of: int | None = None,
               seed: int = 20261124) -> dict:
    """拟合→预测落盘→打分。复评时沿用已落盘的预测,不重新拟合。"""
    fingerprint = model_fingerprint(train_cases)
    model = risk.fit(ledger, train_cases)
    store.parent.mkdir(parents=True, exist_ok=True)
    if store.exists():
        saved = json.loads(store.read_text(encoding="utf-8"))
        if saved.get("fingerprint") != fingerprint:
            raise SystemExit("落盘预测的训练窗与当前不一致,拒绝混用;请更换复盘文件")
        predictions = saved["predictions"]
        reused = True
    else:
        predictions = make_predictions(ledger, model, holdout_cases)
        store.write_text(json.dumps(
            {"fingerprint": fingerprint, "train_cases": sorted(train_cases),
             "holdout_cases": holdout_cases, "predictions": predictions},
            ensure_ascii=False, indent=2), encoding="utf-8")
        reused = False
    result = {
        "训练案例": sorted(train_cases),
        "复盘案例": holdout_cases,
        "预测复用": reused,
        "复评截止": score_as_of,
        "总体": score(predictions, ledger, score_as_of),
        "联合风险": joint_review(ledger, model, holdout_cases, seed),
        "空记录诊断": missingness_diagnostic(ledger, train_cases),
        "未知风险路段": model.unknown_roads,
        "卫生检查": {
            "训练与复盘无交集": not (set(train_cases) & set(holdout_cases)),
            "预测只用决策时点前版本": True,
        },
    }
    return result
