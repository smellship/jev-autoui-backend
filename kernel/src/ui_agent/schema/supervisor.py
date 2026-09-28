"""监督裁决契约：Jev 卡住时，监督模型只能在这四种裁决里选一个，且必须通过校验。

校验的分量在这里，不在模型：元素只能用当次快照的 index，恢复动作只能用白名单操作，
REPLAN 的步骤要过和 planner 一样的断言类型检查。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ui_agent.schema.check import KINDS
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.state import State

RULINGS = ("HINT", "RECOVER", "REPLAN", "ABORT")
# 恢复动作不接受 CLICK_FORCE（那是缺陷路径）、TYPE_TEXT 与 UPLOAD（值/文件不能由监督模型现编）
RECOVER_OPS = ("CLICK", "SELECT", "HOVER", "SCROLL_TO", "PRESS_KEY", "SCROLL_UP", "SCROLL_DOWN", "WAIT")
NEEDS_ELEMENT = ("CLICK", "SELECT", "HOVER", "SCROLL_TO")
MAX_HINT = 600
MAX_STEPS = 12


class Ruling(BaseModel):
    ruling: str
    hint: str = ""
    reason: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    operation: str = ""
    element: int | None = None
    option: str = ""
    option_dom_index: int | None = None
    press_key: str = ""

    @property
    def label(self) -> str:
        if self.ruling == "HINT":
            return f"HINT：{self.hint}"
        if self.ruling == "RECOVER":
            target = self.element if self.element is not None else (self.press_key or "页面级")
            return f"RECOVER：{self.operation} → {target}"
        if self.ruling == "REPLAN":
            return f"REPLAN：{len(self.steps)} 步（{self.reason}）"
        return f"ABORT：{self.reason}"


def parse_ruling(data: dict, state: State) -> Ruling:
    """把监督模型的 JSON 变成可执行的裁决；任何不合规都抛错，绝不"猜着执行"。"""
    if not isinstance(data, dict):
        raise RuntimeError("监督模型没有返回 JSON 对象。")
    kind = str(data.get("ruling", "")).strip().upper()
    if kind not in RULINGS:
        raise RuntimeError(f"监督模型给的裁决不在 {RULINGS} 里：{data.get('ruling')!r}")
    ruling = Ruling(ruling=kind, reason=str(data.get("reason", "")).strip()[:400])
    if kind == "HINT":
        hint = str(data.get("hint", "")).strip()
        if not hint:
            raise RuntimeError("监督模型给了 HINT 但没给 hint 文本。")
        ruling.hint = hint[:MAX_HINT]
        return ruling
    if kind == "ABORT":
        if not ruling.reason:
            raise RuntimeError("监督模型给了 ABORT 但没说原因。")
        return ruling
    if kind == "REPLAN":
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RuntimeError("监督模型给了 REPLAN 但没有给出步骤。")
        try:
            parsed = [PlanStep(**step) for step in steps]
        except Exception as exc:
            raise RuntimeError(f"监督模型给的步骤不合法：{_brief(exc)}") from None
        bad = [spec.kind for step in parsed for spec in step.checks if spec.kind not in KINDS]
        if bad:
            raise RuntimeError(f"监督模型的断言类型不支持：{'、'.join(bad)}；可用：{'、'.join(KINDS)}")
        if len(parsed) > MAX_STEPS:
            raise RuntimeError(f"监督模型给了 {len(parsed)} 步，超过上限 {MAX_STEPS}")
        ruling.steps = parsed
        return ruling
    return _parse_recover(ruling, data.get("action"), state)


def _parse_recover(ruling: Ruling, action, state: State) -> Ruling:
    if not isinstance(action, dict):
        raise RuntimeError("监督模型给了 RECOVER 但没有给出动作。")
    op = str(action.get("operation", "")).strip().upper()
    if op not in RECOVER_OPS:
        raise RuntimeError(f"恢复动作只能在这些操作里选：{'、'.join(RECOVER_OPS)}（收到 {op or '空'}）")
    ruling.operation = op
    if op == "PRESS_KEY":
        ruling.press_key = str(action.get("key", "")).strip()
        if not ruling.press_key or len(ruling.press_key) > 20:
            raise RuntimeError("恢复动作 PRESS_KEY 需要一个不超过 20 字符的按键名。")
        return ruling
    if op not in NEEDS_ELEMENT:
        return ruling
    index = action.get("element")
    element = next((el for el in state.elements if el.index == index), None) if isinstance(index, int) else None
    if element is None:
        raise RuntimeError(f"恢复动作指向的元素 {index!r} 不在当次快照里；未执行任何动作。")
    if op in ("CLICK", "HOVER", "SELECT") and op not in element.operations:
        raise RuntimeError(f"元素 [{element.index}] {element.label} 不支持 {op}（可能已禁用）；未执行任何动作。")
    ruling.element = element.index
    if op == "SELECT":
        option = next((o for o in element.options if o.key == str(action.get("option", ""))), None)
        if option is None or option.disabled:
            raise RuntimeError(f"恢复动作选的选项不在元素 [{element.index}] 的当次选项里；未执行任何动作。")
        ruling.option, ruling.option_dom_index = option.key, option.dom_index
    return ruling


def _brief(exc: Exception, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class SupervisorBudget:
    """每步两次、每次运行六次：取一次裁决扣一格，扣完就回到"直接停机"的语义。

    预算在调用前扣除，所以无论模型答得好不好，升级次数有限 ⇒ 循环必然终止。
    """

    def __init__(self, per_step: int = 2, per_run: int = 6):
        self.per_step = per_step
        self.per_run = per_run
        self.step_used = 0
        self.run_used = 0

    @property
    def exhausted(self) -> bool:
        return self.step_used >= self.per_step or self.run_used >= self.per_run

    @property
    def detail(self) -> str:
        return f"本步 {self.step_used}/{self.per_step}，本次运行 {self.run_used}/{self.per_run}"

    def new_step(self) -> None:
        self.step_used = 0

    def take(self) -> None:
        self.step_used += 1
        self.run_used += 1
