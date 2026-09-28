"""离线测试的公共替身：假驱动 + 假决策引擎 + 脚本化监督模型（不起浏览器、不调模型）。"""

from __future__ import annotations

import json
from pathlib import Path

from ui_agent.config import Settings
from ui_agent.driver.ladder import LadderOutcome
from ui_agent.schema.decision import Decision
from ui_agent.schema.supervisor import Ruling, SupervisorBudget, parse_ruling


class FakeDriver:
    """只实现 Executor 会用到的那几个方法，外加 Runner/PlanRunner 需要的属性。"""

    def __init__(self, snapshots: list[dict]):
        self.s = Settings(ui_agent_mode="ci", max_actions=3, max_decisions=8)
        self._snapshots = list(snapshots)
        self._last = snapshots[-1]
        self._frames: dict[int, object] = {}
        self.zoom_note = ""
        self.closed = False
        self.starts = 0
        self.gotos: list[str] = []
        self.settles = 0
        self.paints = 0
        self.reloads = 0
        self.paint_note = ""
        self.uploads: list[tuple[int, list[str], str]] = []
        self.notes: list[str] = []
        self.dialogs: list[dict] = []

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    def take_dialogs(self) -> list[dict]:
        dialogs, self.dialogs = self.dialogs, []
        return dialogs

    def start(self) -> None:
        self.starts += 1

    def close(self) -> None:
        self.closed = True

    def goto(self, url: str) -> None:
        self.gotos.append(url)

    def wait_first_paint(self) -> dict:
        self.paints += 1
        return self._last

    def wait_rendered(self) -> dict:
        return self._last

    def reload(self) -> None:
        """模拟"刷新后页面变了"：脚本里的下一条就是刷新后的快照。"""
        self.reloads += 1
        if self._snapshots:
            self._last = self._snapshots.pop(0)

    def snapshot(self) -> dict:
        if self._snapshots:
            self._last = self._snapshots.pop(0)
        return self._last

    # Executor 会用到的动作面
    def guard(self, index: int) -> dict:
        return {"ok": True, "reachable": True, "in_viewport": True}

    def click(self, index: int, allow_force: bool = False) -> LadderOutcome:
        return LadderOutcome(ok=True, level="L0")

    def type_text(self, index: int, text: str) -> LadderOutcome:
        return LadderOutcome(ok=True, level="T0")

    def select(self, index: int, dom_index: int) -> None:
        pass

    def upload(self, index: int, files, mode: str = "input") -> LadderOutcome:
        self.uploads.append((index, [f.name for f in files], mode))
        return LadderOutcome(ok=True, level="U1" if mode == "input" else "U2")

    def hover(self, index: int) -> None:
        pass

    def press_key(self, key: str) -> None:
        pass

    def scroll(self, direction: str, amount: int = 560) -> None:
        pass

    def scroll_to(self, index: int) -> None:
        pass

    def wait(self, ms: int) -> None:
        pass

    def settle(self, ms: int | None = None) -> None:
        self.settles += 1

    def screenshot(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")


class FakeEngine:
    def __init__(self, decisions: list[Decision], error: Exception | None = None):
        self._decisions = list(decisions)
        self.error = error
        self.seen_states: list = []
        self.goals: list[str] = []
        self.rules: list[list[str] | None] = []

    def decide(self, state, goal, rules=None) -> Decision:
        self.seen_states.append(state)
        self.goals.append(goal)
        self.rules.append(rules)
        if self.error is not None:
            raise self.error
        return self._decisions.pop(0)


class ScriptedSupervisor:
    """按脚本回答的监督模型替身：裁决照样过 schema 校验，预算规则与真身一致。"""

    def __init__(self, rulings: list[dict | Exception], per_step: int = 2, per_run: int = 6):
        self._rulings = list(rulings)
        self.budget = SupervisorBudget(per_step, per_run)
        self.available = True
        self.asked: list[dict] = []

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state, goal, trigger, detail, remaining=None) -> tuple[Ruling, dict]:
        self.asked.append({"trigger": trigger, "goal": goal, "detail": detail,
                           "remaining": list(remaining or [])})
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        assert self._rulings, f"监督脚本不够用了：第 {len(self.asked)} 次提问（{trigger}）没有脚本"
        item = self._rulings.pop(0)
        if isinstance(item, Exception):
            raise item
        return parse_ruling(item, state), {"model": "scripted", "latency_ms": 0}


def settings_for(tmp_path, **overrides) -> Settings:
    class Local(Settings):
        @property
        def runs_dir(self):
            return tmp_path / "runs"

        @property
        def profile_dir(self):
            return tmp_path / "profile"

    base = {"ui_agent_mode": "ci", "max_actions": 3, "max_decisions": 8}
    return Local(**{**base, **overrides})


def snapshot(fingerprint: str, text: str = "", labels: list[str] | None = None) -> dict:
    labels = ["查询"] if labels is None else labels
    elements = [
        {"index": i + 1, "role": "button", "label": label, "rect": [0, 0, 10, 10],
         "in_viewport": True, "occluded_by": "", "disabled": False, "operations": ["CLICK"]}
        for i, label in enumerate(labels)
    ]
    return {"page": {"url": "http://x/", "title": "T", "text": text, "viewport": {}, "scroll": {}},
            "elements": elements, "fingerprint": fingerprint, "omitted": 0}


def click(index: int = 1, confidence: float = 0.9) -> Decision:
    return Decision(operation="CLICK", target=str(index), target_element=index, confidence=confidence)


def done(confidence: float = 0.9) -> Decision:
    return Decision(operation="DONE", confidence=confidence)


def blocked(confidence: float = 0.9) -> Decision:
    return Decision(operation="BLOCKED", confidence=confidence)


def read_trace(run_dir) -> list[dict]:
    text = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []
