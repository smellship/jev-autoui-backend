"""升级点的统一出口：把卡住信号、阻塞、预算、断言未过翻译成一条可执行的裁决。

调用方不关心模型答没答：不可用（没 key、预算用尽、响应不合规）一律降级为 unavailable，
由调用点按 M0 的停机语义收尾——监督模型的问题不该把一次运行拖崩。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ui_agent.llm.supervisor import Supervisor
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import State
from ui_agent.schema.supervisor import Ruling

KINDS = ("hint", "recover", "replan", "abort")
UNAVAILABLE_DETAILS = {
    "off": "未启用监督模型（--no-supervisor）",
    "no_key": "缺少 TEXT_MODEL_API_KEY；监督模型不可用",
}


@dataclass
class Verdict:
    trigger: str
    kind: str
    ruling: Ruling | None = None
    detail: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.ruling.label if self.ruling is not None else self.detail

    @property
    def is_unavailable(self) -> bool:
        return self.kind == "unavailable"

    def trace(self) -> dict:
        data: dict = {"trigger": self.trigger, "ruling": None, "label": self.label}
        if self.ruling is not None:
            data["ruling"] = self.ruling.ruling
            data["meta"] = self.meta
        else:
            data["error"] = self.detail
        return data


def escalate(supervisor: Supervisor | None, state: State, goal: str, trigger: str, detail: str,
             remaining: list[str] | None = None) -> Verdict:
    if supervisor is None:
        return Verdict(trigger, "unavailable", detail=UNAVAILABLE_DETAILS["off"])
    if not supervisor.available:
        return Verdict(trigger, "unavailable", detail=UNAVAILABLE_DETAILS["no_key"])
    if supervisor.budget.exhausted:
        return Verdict(trigger, "unavailable", detail=f"监督预算已用完（{supervisor.budget.detail}）")
    try:
        ruling, meta = supervisor.ask(state, goal, trigger, detail, remaining)
    except RuntimeError as exc:
        return Verdict(trigger, "unavailable", detail=f"{exc.__class__.__name__}: {exc}")
    return Verdict(trigger, ruling.ruling.lower(), ruling=ruling, meta=meta)


def recover_decision(ruling: Ruling) -> Decision:
    """把过检的 RECOVER 翻译成一次普通决策，后续走和 Jev 决策完全相同的执行路径。"""
    decision = Decision(operation=ruling.operation, confidence=1.0, model="supervisor")
    if ruling.element is not None:
        decision.target_element = ruling.element
        decision.target = str(ruling.element)
    if ruling.option:
        decision.target = ruling.option
        decision.target_option = ruling.option_dom_index
    if ruling.press_key:
        decision.press_key = ruling.press_key
    return decision
