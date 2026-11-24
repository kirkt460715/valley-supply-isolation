"""调度状态台账:让工具可以重跑而不重复拨付。

每次放行的配送决定都追加到 out/state_<case>.json;下一次运行时
据此还原:哪些车已出、哪些物资已在路上或已落地、哪些决定因新
通知失效。已发出的物资在任何一份新计划里都不得再出现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 决定状态:released(已放行未发车)-> in_transit(在途)-> received(已落地)
# 可能因通知变为 blocked(受阻);未发车前可 cancelled(取消,物资回可拨库存)
DECISION_STATES = ("released", "in_transit", "received", "blocked", "cancelled")


@dataclass
class Decision:
    decision_id: str
    case: str
    decided_at: int            # 作出决定的绝对小时
    day: int                   # 执行日
    vehicle: str
    depart_hour: int           # 绝对小时
    route: list[str]
    drops: list[dict]          # [{village, item, qty}]
    backhaul: list[dict] = field(default_factory=list)  # [{village, item, qty}]
    status: str = "released"
    basis: dict = field(default_factory=dict)   # 依据快照(可见信息清单)
    note: str = ""

    @property
    def depart_abs(self) -> int:
        return self.depart_hour


@dataclass
class CaseState:
    case: str
    decisions: list[Decision] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)  # 重算历史

    def active(self) -> list[Decision]:
        return [d for d in self.decisions if d.status in ("released", "in_transit", "blocked")]

    def landed(self) -> list[Decision]:
        return [d for d in self.decisions if d.status == "received"]

    def next_id(self) -> str:
        return f"D-{self.case}-{len(self.decisions) + 1:03d}"


def state_path(directory: Path | str, case: str) -> Path:
    return Path(directory) / f"state_{case}.json"


def load_state(directory: Path | str, case: str) -> CaseState:
    path = state_path(directory, case)
    if not path.exists():
        return CaseState(case=case)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return CaseState(
        case=raw["case"],
        decisions=[Decision(**d) for d in raw.get("decisions", [])],
        log=raw.get("log", []),
    )


def save_state(directory: Path | str, state: CaseState) -> Path:
    path = state_path(directory, case=state.case)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": state.case,
        "decisions": [asdict(d) for d in state.decisions],
        "log": state.log,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
