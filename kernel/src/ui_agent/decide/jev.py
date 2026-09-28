"""TypeSafe Jev 客户端：state + questions → 类型化决策（operation + target）。

一次请求并行问多个 head，只消费选中 operation 对应的那个 target head（speculative fan-out）。
"""

from __future__ import annotations

import math
import time

from ui_agent.config import Settings
from ui_agent.decide.prompts import NEXT_ACTION, TARGET
from ui_agent.net import post_json
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element, State

OPERATION_LABELS = {
    "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
    "CLICK_FORCE": "Force-click an element whose normal click already failed (occluded or clipped).",
    "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
    "UPLOAD": "Attach file(s) to a file-input control (may be hidden). The harness supplies the file.",
    "SELECT": "Select an observed dropdown value.",
    "HOVER": "Hover an element to reveal a submenu or tooltip.",
    "PRESS_KEY": "Press one keyboard key on the page.",
    "SCROLL_UP": "Scroll up by one screen.",
    "SCROLL_DOWN": "Scroll down by one screen.",
    "SCROLL_TO": "Scroll an observed element into view.",
    "WAIT": "Wait for the page to update.",
    "DONE": "Every requirement is visibly satisfied.",
    "BLOCKED": "No supported operation can progress.",
}

PRESS_KEYS = {
    "Enter": "Submit a ready form or confirm a focused control.",
    "Escape": "Dismiss an open dropdown, popup, or dialog.",
    "Tab": "Move focus to the next control.",
}


def validate_choice(answer: dict, ids) -> dict:
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("决策模型响应未通过校验；未执行任何动作。")
    return answer


def _payload(el: Element) -> dict:
    data = {"element": f"[{el.index}] {el.role} {el.label}", "current_value": el.value}
    if el.accept:
        data["accept"] = el.accept
    if el.multiple:
        data["multiple"] = True
    if el.hidden:
        data["hidden"] = True
    if el.checked is not None:
        data["checked"] = el.checked
    if el.selected is not None:
        data["selected"] = el.selected
    if el.expanded is not None:
        data["expanded"] = el.expanded
    if not el.in_viewport:
        data["in_viewport"] = False
    if el.occluded_by:
        data["occluded_by"] = el.occluded_by
    return data


def build_action_space(elements: list[Element], click_failures: dict[str, str] | None = None) -> dict[str, dict]:
    """每个操作只提供被观测到且兼容的目标；SELECT 的目标是已观测选项 key。"""
    targets: dict[str, dict] = {}
    for el in elements:
        for op in el.operations:
            if op == "SELECT":
                group = targets.setdefault("SELECT", {})
                for opt in el.options:
                    if opt.disabled:
                        continue
                    group[opt.key] = {
                        "element": f"[{el.index}] {el.label}",
                        "option": opt.label,
                        "selected": opt.selected,
                    }
                continue
            targets.setdefault(op, {})[str(el.index)] = _payload(el)
    offscreen = {str(el.index): _payload(el) for el in elements if not el.in_viewport}
    if offscreen:
        targets["SCROLL_TO"] = offscreen
    failed = set((click_failures or {}))
    force_targets = {str(el.index): _payload(el) for el in elements if str(el.index) in failed}
    if force_targets:
        targets["CLICK_FORCE"] = force_targets
    return targets


def available_operations(state: State, targets: dict[str, dict]) -> list[str]:
    """可选操作一律从 targets 派生，保证"给了这个操作"就一定"给了它的目标 head"。"""
    operations = [op for op in ("CLICK", "CLICK_FORCE", "TYPE_TEXT", "UPLOAD", "SELECT", "HOVER", "SCROLL_TO")
                  if targets.get(op)]
    scroll = state.page.scroll or {}
    if scroll.get("y", 0) > 0:
        operations.append("SCROLL_UP")
    if scroll.get("y", 0) < scroll.get("max", 0) - 2:
        operations.append("SCROLL_DOWN")
    operations += ["PRESS_KEY", "WAIT", "DONE", "BLOCKED"]
    return operations


def to_jev_state(state: State) -> dict:
    """模型看到的元素事实：无 rect、无 dom_index、无内部索引以外的任何定位信息。"""
    elements = []
    for el in state.elements:
        data = {"index": el.index, "role": el.role, "label": el.label, "operations": el.operations}
        if el.value:
            data["value"] = el.value
        if el.secret:
            data["secret"] = True
        if el.accept:
            data["accept"] = el.accept
        if el.multiple:
            data["multiple"] = True
        if el.hidden:
            data["hidden"] = True
        if el.frame:
            data["frame"] = el.frame
        if el.shadow:
            data["shadow"] = True
        if el.disabled:
            data["disabled"] = True
        for key in ("checked", "selected", "expanded"):
            value = getattr(el, key)
            if value is not None:
                data[key] = value
        if not el.in_viewport:
            data["in_viewport"] = False
        if el.occluded_by:
            data["occluded_by"] = el.occluded_by
        if el.options:
            data["options"] = [o.model_dump(exclude={"dom_index"}) for o in el.options]
        elements.append(data)
    return {
        "page": state.page.model_dump(),
        "elements": elements,
        "recent_actions": [a.model_dump(exclude_none=True, exclude={"step"})
                           for a in state.recent_actions[-10:]],
    }


def build_questions(state: State, goal: str, rules: list[str] | None = None) -> tuple[dict, dict]:
    rules = rules or [NEXT_ACTION]
    targets = build_action_space(state.elements, state.click_failures)
    operations = {op: OPERATION_LABELS[op] for op in available_operations(state, targets)}
    questions: dict[str, dict] = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": {"goal": goal, "rules": rules},
        }
    }
    for op in ("CLICK", "CLICK_FORCE", "TYPE_TEXT", "UPLOAD", "SELECT", "HOVER", "SCROLL_TO"):
        if not targets.get(op):
            continue
        questions[op.lower() + "_target"] = {
            "type": "choice",
            "criteria": targets[op],
            "instructions": {"goal": goal, "operation": op, "rules": [*rules, TARGET]},
        }
    if "PRESS_KEY" in operations:
        questions["press_key_target"] = {
            "type": "choice",
            "criteria": PRESS_KEYS,
            "instructions": {"goal": goal, "operation": "PRESS_KEY", "rules": [*rules, TARGET]},
        }
    return questions, targets


class JevEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    def decide(self, state: State, goal: str, rules: list[str] | None = None) -> Decision:
        if not self.s.typesafe_api_key:
            raise RuntimeError("缺少 TYPESAFE_API_KEY；请在 .env 中补上后再实跑。")
        questions, targets = build_questions(state, goal, rules)
        body = {
            "model": self.s.typesafe_model,
            "state": to_jev_state(state),
            "questions": questions,
        }
        started = time.perf_counter()
        result = post_json(self.s.typesafe_base_url, self.s.typesafe_api_key, body, label="决策模型")
        answers = result.get("answers", {})
        operation_answer = validate_choice(answers.get("operation", {}), set(questions["operation"]["criteria"]))
        operation = operation_answer["choice"]
        decision = Decision(
            operation=operation,
            confidence=float(operation_answer["confidence"]),
            operation_probabilities=operation_answer["probabilities"],
            model=result.get("model", ""),
            usage=result.get("usage", {}),
            raw_answers=answers,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        head = operation.lower() + "_target"
        if head in questions:
            answer = validate_choice(answers.get(head, {}), set(questions[head]["criteria"]))
            decision.target = answer["choice"]
            decision.target_probabilities = answer["probabilities"]
            if operation == "SELECT":
                element_index, _, option_number = answer["choice"].partition(":")
                decision.target_element = int(element_index)
                element = next((el for el in state.elements if el.index == decision.target_element), None)
                option = next((o for o in (element.options if element else []) if o.key == answer["choice"]), None)
                if option is None:
                    raise ValueError("SELECT 目标选项不在当次快照中；未执行任何动作。")
                decision.target_option = option.dom_index
                _ = option_number
            elif operation == "PRESS_KEY":
                decision.press_key = answer["choice"]
            else:
                decision.target_element = int(answer["choice"])
        elif operation not in ("WAIT", "DONE", "BLOCKED", "SCROLL_UP", "SCROLL_DOWN"):
            raise ValueError(f"操作 {operation} 缺少目标 head；未执行任何动作。")
        return decision
