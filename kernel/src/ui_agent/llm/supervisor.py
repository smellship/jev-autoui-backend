"""监督模型缝：Jev 卡住时才调用，一次调用换一条裁决，且裁决必须过 schema 校验。

它和 Jev 看同一份 state（无坐标、无选择器、无 dom_index），区别只在问题与输出：
Jev 选操作 + 目标，监督模型给 HINT/RECOVER/REPLAN/ABORT，落点由代码校验而不是由它保证。
"""

from __future__ import annotations

from ui_agent.config import Settings
from ui_agent.decide.jev import to_jev_state
from ui_agent.decide.prompts import SUPERVISOR
from ui_agent.llm.client import chat_json
from ui_agent.schema.state import State
from ui_agent.schema.supervisor import Ruling, SupervisorBudget, parse_ruling


def supervision_context(state: State, goal: str, trigger: str, detail: str,
                        remaining: list[str] | None = None) -> dict:
    return {
        "stuck": {"trigger": trigger, "detail": detail},
        "current_goal": goal,
        "remaining_goals": list(remaining or []),
        "state": to_jev_state(state),
    }


class Supervisor:
    def __init__(self, settings: Settings, budget: SupervisorBudget | None = None):
        self.s = settings
        self.budget = budget or SupervisorBudget(settings.supervisor_per_step, settings.supervisor_per_run)

    @property
    def available(self) -> bool:
        return bool(self.s.text_model_api_key)

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state: State, goal: str, trigger: str, detail: str,
            remaining: list[str] | None = None) -> tuple[Ruling, dict]:
        """返回 (通过校验的裁决, 调用元信息)；没有 key、预算用尽、响应不合规都抛 RuntimeError。"""
        if not self.available:
            raise RuntimeError("缺少 TEXT_MODEL_API_KEY；监督模型不可用。")
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        data, meta = chat_json(self.s, SUPERVISOR, supervision_context(state, goal, trigger, detail, remaining),
                               max_tokens=1200)
        return parse_ruling(data, state), meta
