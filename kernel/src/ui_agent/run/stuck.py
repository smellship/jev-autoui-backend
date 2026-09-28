"""卡住判定：只做"能确定"的两种——重复无进展、置信度持续过低。

detector 只给信号；runner 拿着信号去问监督模型（HINT | RECOVER | REPLAN | ABORT），
监督模型不可用时才退回 M0 的停机语义。
"""

from __future__ import annotations

from dataclasses import dataclass

from ui_agent.schema.decision import Decision
from ui_agent.schema.state import RecentAction


@dataclass
class StuckSignal:
    kind: str
    detail: str


class StuckDetector:
    def __init__(self, conf_min: float = 0.55, repeat_limit: int = 3, low_conf_limit: int = 3):
        self.conf_min = conf_min
        self.repeat_limit = repeat_limit
        self.low_conf_limit = low_conf_limit
        self._last_key: tuple[str, str] | None = None
        self._attempts = 0
        self._low_conf = 0

    @staticmethod
    def key(decision: Decision) -> tuple[str, str]:
        return (decision.operation, str(decision.target or decision.press_key or ""))

    def reset(self) -> None:
        """监督模型介入后调用：干预前的重复与低置信不再作为证据。"""
        self._last_key = None
        self._attempts = 0
        self._low_conf = 0

    def observe(self, decision: Decision, recent: list[RecentAction]) -> StuckSignal | None:
        key = self.key(decision)
        self._attempts = self._attempts + 1 if key == self._last_key else 1
        self._last_key = key

        # "没有进展"看最近一次真正的动作：页面事实行（新标签/对话框）不算尝试
        last = next((a for a in reversed(recent) if a.action != "PAGE"), None)
        no_progress = last is not None and last.page_changed is False
        if self._attempts >= self.repeat_limit and no_progress:
            return StuckSignal(
                "repeat_no_progress",
                f"{key[0]} → {key[1] or '—'} 连续 {self._attempts} 次决策相同，且期间页面没有变化",
            )

        if decision.confidence < self.conf_min:
            self._low_conf += 1
            if self._low_conf >= self.low_conf_limit:
                return StuckSignal(
                    "low_confidence",
                    f"连续 {self._low_conf} 次决策置信度低于 {self.conf_min}（本次 {decision.confidence:.2f}）",
                )
        else:
            self._low_conf = 0
        return None
