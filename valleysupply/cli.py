"""命令行:县仓可重跑的断路风险与滚动保供工具。

用法示例:
    python3 -m valleysupply risk --case K041 --at 27
    python3 -m valleysupply plan --case K041 --at 4 --save
    python3 -m valleysupply replan --case K041 --at 8 --save
    python3 -m valleysupply review --score-as-of 72
    python3 -m valleysupply compare --case K041 --at 4
    python3 -m valleysupply trace --case K041 --village T2
    python3 -m valleysupply trace --case K041 --decision D-K041-001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import compare as compare_mod
from . import report, review as review_mod, risk
from .ledger import load
from .plan import PlanRequest, plan
from .replan import record_decisions, replan
from .state import load_state, save_state

ROOT = Path(__file__).resolve().parent.parent


def _split_cases(ledger, train_count: int, holdout_count: int):
    cases = ledger.cases
    train = cases[:train_count]
    holdout = cases[train_count:train_count + holdout_count]
    current = cases[train_count + holdout_count:]
    return train, holdout, current


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", type=Path, default=ROOT / "data", help="台账目录")
    parser.add_argument("--out", type=Path, default=ROOT / "out", help="状态与结果目录")
    parser.add_argument("--train-count", type=int, default=30, help="训练案例数")
    parser.add_argument("--holdout-count", type=int, default=10, help="留出复盘案例数")
    parser.add_argument("--seed", type=int, default=20261124, help="情景模拟种子")
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valleysupply", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_risk = sub.add_parser("risk", help="断路风险展望")
    _common(p_risk)
    p_risk.add_argument("--case", required=True)
    p_risk.add_argument("--at", type=int, required=True, help="决策时点(绝对小时)")
    p_risk.add_argument("--stance", choices=["central", "cautious"], default="central")

    p_plan = sub.add_parser("plan", help="编制今日保供计划")
    _common(p_plan)
    p_plan.add_argument("--case", required=True)
    p_plan.add_argument("--at", type=int, required=True)
    p_plan.add_argument("--alpha", type=float, default=0.8, help="承诺风险水平")
    p_plan.add_argument("--stance", choices=["central", "cautious"], default="central")
    p_plan.add_argument("--save", action="store_true", help="把今日决定登记入台账")

    p_replan = sub.add_parser("replan", help="新通知后重算")
    _common(p_replan)
    p_replan.add_argument("--case", required=True)
    p_replan.add_argument("--at", type=int, required=True)
    p_replan.add_argument("--alpha", type=float, default=0.8)
    p_replan.add_argument("--stance", choices=["central", "cautious"], default="central")
    p_replan.add_argument("--save", action="store_true")

    p_review = sub.add_parser("review", help="留出案例独立复盘")
    _common(p_review)
    p_review.add_argument("--score-as-of", type=int, default=None,
                          help="只给此时点前送达的确认打分(晚到确认列为待确认)")

    p_compare = sub.add_parser("compare", help="路网×货种×时长适用范围比较")
    _common(p_compare)
    p_compare.add_argument("--case", required=True)
    p_compare.add_argument("--at", type=int, required=True)
    p_compare.add_argument("--alpha", type=float, default=0.8)
    p_compare.add_argument("--stance", choices=["central", "cautious"], default="central")

    p_trace = sub.add_parser("trace", help="按村或按配送决定追查")
    _common(p_trace)
    p_trace.add_argument("--case", required=True)
    group = p_trace.add_mutually_exclusive_group(required=True)
    group.add_argument("--village")
    group.add_argument("--decision")

    args = parser.parse_args(argv)
    ledger = load(args.data)
    train, holdout, _current = _split_cases(ledger, args.train_count, args.holdout_count)

    if args.command == "review":
        store = args.out / "review_predictions.json"
        doc = review_mod.run_review(ledger, train, holdout, store,
                                    score_as_of=args.score_as_of, seed=args.seed)
        _emit(doc, args, report.render_review)
        return 0

    model = risk.fit(ledger, train)

    if args.command == "risk":
        from .plan import build_situation, known_overrides
        from .risk import isolation_metrics, simulate
        sit = build_situation(ledger, args.case, args.at, None)
        days = list(sit.days)
        known_closed, known_open = known_overrides(sit)
        result = simulate(model, ledger, days,
                          {d: sit.rain_by_day.get(d, {}) for d in days},
                          known_closed=known_closed, known_open=known_open,
                          seed=args.seed, stance=args.stance)
        metrics = isolation_metrics(result, ledger.routes(), model.unknown_roads)
        doc = {
            "时点": f"第{sit.day}天{args.at % 24}时", "stance": args.stance,
            "seed": args.seed,
            "roads": {r: {"今日状态": st, "依据": bs,
                          "预测": {d: _fmt_prob(model, r, sit.rain_by_day.get(d, {}).get(r),
                                               args.stance)
                                   for d in days}}
                      for r, (st, bs) in sit.road_now.items()},
            "isolation": metrics,
        }
        _emit(doc, args, report.render_risk)
        return 0

    if args.command == "plan":
        state = load_state(args.out, args.case)
        req = PlanRequest(ledger=ledger, case=args.case, at_hour=args.at, model=model,
                          state=state, alpha=args.alpha, stance=args.stance, seed=args.seed)
        doc = plan(req)
        if args.save:
            ids = record_decisions(state, args.case, args.at, doc, decided_by="plan")
            save_state(args.out, state)
            doc["登记决定"] = ids
        _emit(doc, args, report.render_plan)
        return 0

    if args.command == "replan":
        state = load_state(args.out, args.case)
        doc = replan(ledger, args.case, args.at, model, state,
                     alpha=args.alpha, stance=args.stance, seed=args.seed)
        if args.save:
            ids = record_decisions(state, args.case, args.at, doc, decided_by="replan")
            save_state(args.out, state)
            doc["登记决定"] = ids
        _emit(doc, args, report.render_plan)
        return 0

    if args.command == "compare":
        doc = compare_mod.compare(ledger, args.case, args.at, model,
                                  alpha=args.alpha, stance=args.stance, seed=args.seed)
        _emit(doc, args, report.render_compare)
        return 0

    if args.command == "trace":
        state = load_state(args.out, args.case)
        if args.village:
            print(report.render_trace_village(args.village, state, []))
        else:
            print(report.render_trace_decision(args.decision, state))
        return 0
    return 1


def _fmt_prob(model, road, rain, stance) -> str:
    p = model.daily_probability(road, rain, stance)
    if p is None:
        return "未知(观察不足,需人工核实)"
    src = model.closure_source.get((road, rain), "climatology") if rain else "climatology"
    tag = {"road": "", "pooled": "(并集估计)", "unknown": "",
           "climatology": "(气候率)"}.get(src, "")
    return f"{p:.0%}{tag}"


def _emit(doc: dict, args, renderer) -> None:
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
    else:
        print(renderer(doc))


if __name__ == "__main__":
    sys.exit(main())
