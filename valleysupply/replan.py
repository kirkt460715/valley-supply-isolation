"""重规划:新通知到达后,从真实库存与在途状态重新编排。

规则:
- 已发出(在途、已落地、已确定今晚装车)的物资不在新计划里重复出现;
- 受阻决定保留原状并标记,物资与车辆都暂不可用,不另派一份相同的;
- 未发车的决定若路线已被通知封闭,取消并把物资退回可拨库存;
- 每次重算都在台账里留下线索,值班员可按村或按决定查来龙去脉。
"""

from __future__ import annotations

from .ledger import Ledger, closure_intervals, day_of, hour_of
from .plan import (PlanRequest, plan, shipment_progress, _intervals_hit)
from .state import CaseState, Decision


def refresh_decisions(ledger: Ledger, case: str, at_hour: int,
                      state: CaseState) -> list[dict]:
    """按可见通知刷新已放行决定的状态,返回状态变化记录。"""
    intervals = closure_intervals(ledger.notices, ledger.patrols, case, at_hour)
    changes: list[dict] = []
    for d in state.decisions:
        if d.status in ("received", "cancelled"):
            continue
        old = d.status
        if d.status == "released" and at_hour < d.depart_abs:
            # 未发车:路线若已被封闭则取消,物资退回可拨库存
            t = d.depart_abs
            blocked = False
            for edge in d.route:
                dur = ledger.roads[edge].duration
                if _intervals_hit(intervals.get(edge, []), t, t + dur) != -1:
                    blocked = True
                    break
                t += dur
            if blocked:
                d.status = "cancelled"
                changes.append({"decision": d.decision_id, "从": old, "到": "cancelled",
                                "原因": "发车前路线被通知封闭,物资退回可拨库存"})
            continue
        status, eta = shipment_progress(d.route, d.depart_abs, intervals, ledger, at_hour)
        if d.status == "released" and at_hour >= d.depart_abs:
            d.status = "in_transit"
        if status == "received" and eta is not None:
            d.status = "received"
        elif status == "blocked":
            d.status = "blocked"
        elif status == "in_transit":
            d.status = "in_transit"  # 含受阻后路线重开、继续行程的情形
        if d.status != old:
            changes.append({"decision": d.decision_id, "从": old, "到": d.status,
                            "原因": "按可见通知与巡查推算"})
    return changes


def replan(ledger: Ledger, case: str, at_hour: int, model,
           state: CaseState, alpha: float = 0.8, stance: str = "central",
           seed: int = 20261124) -> dict:
    """以 at_hour 可见的信息重算。返回差异与新计划。"""
    changes = refresh_decisions(ledger, case, at_hour, state)
    req = PlanRequest(ledger=ledger, case=case, at_hour=at_hour, model=model,
                      state=state, alpha=alpha, stance=stance, seed=seed)
    new_plan = plan(req)
    new_plan["status_changes"] = changes
    new_plan["重算说明"] = (
        f"第{day_of(at_hour)}天{hour_of(at_hour)}时按最新可见通知重算;"
        "在途、已落地与已确定装车的物资均未重复拨付"
    )
    return new_plan


def record_decisions(state: CaseState, case: str, at_hour: int,
                     plan_doc: dict, decided_by: str = "plan") -> list[str]:
    """把计划中的今日决定登记进台账,返回新决定编号。"""
    ids: list[str] = []
    for dec in plan_doc.get("decisions", []):
        decision = Decision(
            decision_id=state.next_id(), case=case, decided_at=at_hour,
            day=dec["day"], vehicle=dec["vehicle"],
            depart_hour=dec["depart_hour"], route=list(dec["route"]),
            drops=list(dec["drops"]), backhaul=list(dec.get("backhaul", [])),
            status="released",
            basis={"时点": plan_doc.get("时点"), "stance": plan_doc.get("stance"),
                   "alpha": plan_doc.get("alpha"), "seed": plan_doc.get("seed"),
                   "依据": plan_doc.get("basis", {}).get("说明")},
            note=dec.get("note", ""),
        )
        state.decisions.append(decision)
        ids.append(decision.decision_id)
    state.log.append({"hour": at_hour, "事件": f"{decided_by} 重算",
                      "新决定": ids,
                      "状态变化": plan_doc.get("status_changes", [])})
    return ids
