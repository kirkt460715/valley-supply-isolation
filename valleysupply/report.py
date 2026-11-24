"""值班员视图:把结果整理成可按村、按配送决定追查的文本。"""

from __future__ import annotations

from .ledger import day_of, hour_of


def fmt_hour(h: int | None) -> str:
    if h is None:
        return "未定"
    return f"第{day_of(h)}天{hour_of(h)}时"


def render_risk(doc: dict) -> str:
    lines = [f"== 断路风险展望({doc['时点']},立场 {doc['stance']}) =="]
    for road, row in doc["roads"].items():
        lines.append(f"路段 {road}:今日 {row['今日状态']}({row['依据']})")
        for d, prob in row["预测"].items():
            lines.append(f"  第{d}天断路概率 {prob}")
    lines.append("-- 村庄孤立风险 --")
    for village, days in doc["isolation"]["per_village"].items():
        txt = ", ".join(f"第{d}天 {p:.0%}" for d, p in days.items())
        lines.append(f"{village}:孤立概率 {txt};全程无可达日 "
                     f"{doc['isolation']['no_open_day_prob'][village]:.0%}")
    for pair, days in doc["isolation"]["joint_pairs"].items():
        txt = ", ".join(f"第{d}天 {p:.0%}" for d, p in days.items())
        lines.append(f"同时孤立 {pair}:{txt}")
    if doc["isolation"]["unknown_roads"]:
        lines.append("风险未知路段(需人工核实):"
                     + "、".join(doc["isolation"]["unknown_roads"]))
    lines.append(f"情景数 {doc['isolation']['n_scenarios']},种子 {doc['seed']}")
    return "\n".join(lines)


def render_plan(doc: dict) -> str:
    lines = [f"== 保供计划({doc['时点']},alpha={doc['alpha']},立场 {doc['stance']}) =="]
    if doc.get("重算说明"):
        lines.append(doc["重算说明"])
    for chg in doc.get("status_changes", []):
        lines.append(f"状态变化:{chg['decision']} {chg['从']}→{chg['到']}({chg['原因']})")
    if doc["decisions"]:
        lines.append("-- 今日放行 --")
        for i, dec in enumerate(doc["decisions"], 1):
            drops = ";".join(f"{d['village']} {d['item']}×{d['qty']}" for d in dec["drops"])
            back = (";回程带 " + ";".join(f"{b['item']}×{b['qty']}" for b in dec["backhaul"])
                    if dec["backhaul"] else "")
            lines.append(f"{i}. {dec['vehicle']} {fmt_hour(dec['depart_hour'])}发 "
                         f"经 {'-'.join(dec['route'])}:{drops}{back}({dec['note']})")
    else:
        lines.append("-- 今日无需放行或无可行路线 --")
    if doc.get("planned_review"):
        lines.append("-- 台账已登记未执行的调度评估 --")
        for p in doc["planned_review"]:
            extra = f"({p['原因']})" if p["原因"] else ""
            lines.append(f"{p['id']}:第{p['day']}天{p['hour']}时 {p['vehicle']} "
                         f"经 {'-'.join(p['route'])} {p['item']}×{p['qty']} → {p['to']}:"
                         f"{p['评估']}{extra}")
    lines.append("-- 各村保障核对 --")
    for village, families in doc["needs"].items():
        for family, row in families.items():
            gap = f",今日缺口 {row['今日缺口']}" if row.get("今日缺口") else ""
            lines.append(
                f"{village} {family}:最低 {row['最低保障']},日耗 {row['日消耗']},"
                f"今日可用 {row['今日可用']},分位需求 {row['分位需求']},"
                f"今日放行 {row.get('今日放行', 0)}{gap}")
    lines.append("-- 守住比例(逐日)--")
    for village, families in doc["outlook"].items():
        for family, days in families.items():
            txt = ", ".join(f"第{d}天 {v['held']:.0%}" for d, v in days.items())
            lines.append(f"{village} {family}:{txt}")
    if doc["uncoverable"]:
        lines.append("-- 明确无法覆盖 --")
        for u in doc["uncoverable"]:
            lines.append(f"{u['village']} {u['family']} 第{u['day']}天:"
                         f"守住 {u['守住比例']:.0%},平均缺口 {u['平均缺口']}({u['说明']})")
    if doc["stuck"]:
        lines.append("-- 受阻物资 --")
        for s in doc["stuck"]:
            lines.append(f"{s['id']}:{s['qty']} {s['item']} 发往 {s['to']} 受阻({s['原因']})")
    if doc["unknowns"]:
        lines.append("-- 风险未知与人工核实 --")
        for u in doc["unknowns"]:
            lines.append(f"{u['road']}:{u['原因']};影响 {'、'.join(u['受影响村'])};"
                         f"{u['处理']}")
    for w in doc.get("warnings", []):
        lines.append(f"警告:{w}")
    nxt = doc["next_recalc"]
    lines.append(f"下一次可重算时点:{nxt['label']}")
    return "\n".join(lines)


def render_review(doc: dict) -> str:
    lines = ["== 独立复盘 ==",
             f"训练 {doc['训练案例'][0]}–{doc['训练案例'][-1]};"
             f"复盘 {'、'.join(doc['复盘案例'][:3])} 等 {len(doc['复盘案例'])} 例;"
             f"预测{'复用落盘' if doc['预测复用'] else '本次落盘'}",
             f"总体:Brier {doc['总体']['Brier']},对数损失 {doc['总体']['对数损失']},"
             f"样例 {doc['总体']['样例数']},待确认 {doc['总体']['待确认']},"
             f"未知未评 {doc['总体']['风险未知未评分']}"]
    for row in doc["总体"]["校准表"]:
        lines.append(f"  校准 {row['预测区间']}:实测 {row['观察断路率']}(n={row['样本']})")
    lines.append("-- 联合风险 --")
    for v, row in doc["联合风险"]["per_village"].items():
        lines.append(f"{v}:预测孤立 {row['预测孤立率均值']},实测 {row['实测孤立率']}"
                     f"(n={row['样本']})")
    j = doc["联合风险"]["全体同时孤立"]
    lines.append(f"全体同时孤立:预测 {j['预测均值']},实测 {j['实测频率']}(n={j['样本']})")
    d = doc["空记录诊断"]
    lines.append(f"空记录诊断:末段见断村-日 {d['末段巡查见断的村-日']} 个,其中仍有确证 "
                 f"{d['其中仍有到货确证']};全通村-日 {d['末段巡查全通的村-日']} 个,其中有确证 "
                 f"{d['其中有到货确证']}。{d['解读']}")
    if doc["未知风险路段"]:
        lines.append("未知风险路段:" + "、".join(doc["未知风险路段"]))
    h = doc["卫生检查"]
    lines.append(f"卫生检查:训练与复盘无交集={h['训练与复盘无交集']};"
                 f"只用决策时点前版本={h['预测只用决策时点前版本']}")
    return "\n".join(lines)


def render_compare(doc: dict) -> str:
    lines = [f"== 适用范围比较({doc['case']} 第{doc['at_hour'] // 24}天,立场 {doc['stance']}) =="]
    header = f"{'路网':<8}{'时长':<4}{'村':<5}{'货种':<7}{'孤立概率':<8}{'全无可达日':<10}{'最长孤立90分位':<12}"
    lines.append(header)
    for row in doc["比较表"]:
        lines.append(f"{row['路网']:<8}{row['时长']:<4}{row['村']:<5}{row['货种']:<7}"
                     f"{row['孤立概率']:<8}{row['全程无可达日概率']:<10}"
                     f"{row['最长连续孤立(天,90分位)']:<12}")
    if doc["未知风险路段"]:
        lines.append("未知风险路段(比较结果对这些路段按不可用保守处理):"
                     + "、".join(doc["未知风险路段"]))
    return "\n".join(lines)


def render_trace_village(village: str, state, plans: list[dict]) -> str:
    lines = [f"== 村台账 {village} =="]
    for d in state.decisions:
        for drop in d.drops:
            if drop["village"] == village:
                lines.append(f"{d.decision_id}:第{d.day}天 {d.vehicle} 经 {'-'.join(d.route)} "
                             f"{drop['item']}×{drop['qty']} 状态 {d.status}({d.note})")
    if not any(drop["village"] == village for d in state.decisions for drop in d.drops):
        lines.append("暂无已登记配送决定")
    for entry in state.log:
        lines.append(f"第{day_of(entry['hour'])}天{hour_of(entry['hour'])}时 {entry['事件']}:"
                     f"新决定 {'、'.join(entry['新决定']) or '无'}")
    return "\n".join(lines)


def render_trace_decision(decision_id: str, state) -> str:
    for d in state.decisions:
        if d.decision_id == decision_id:
            lines = [f"== 配送决定 {decision_id} ==",
                     f"作出于 {fmt_hour(d.decided_at)},第{d.day}天执行,"
                     f"{d.vehicle} {fmt_hour(d.depart_hour)}发,经 {'-'.join(d.route)}",
                     "装载:" + ";".join(f"{x['village']} {x['item']}×{x['qty']}" for x in d.drops)]
            if d.backhaul:
                lines.append("回程:" + ";".join(f"{x['item']}×{x['qty']}" for x in d.backhaul))
            lines.append(f"状态 {d.status};{d.note}")
            for k, v in d.basis.items():
                lines.append(f"依据 {k}:{v}")
            return "\n".join(lines)
    return f"未找到决定 {decision_id}"
