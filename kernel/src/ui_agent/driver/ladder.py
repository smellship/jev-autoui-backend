"""点击/输入降级阶梯：L0–L3 是正常路径；L4（force）与 L5（合成事件）必须标记为可用性缺陷候选。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

FORCE_LEVELS = {"L4", "L5", "T2", "F1"}


@dataclass
class LadderOutcome:
    ok: bool
    level: str = ""
    warning: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list)


def run_ladder(steps: list[tuple[str, Callable[[], None]]]) -> LadderOutcome:
    attempts: list[tuple[str, str]] = []
    for level, fn in steps:
        try:
            fn()
        except Exception as exc:
            attempts.append((level, str(exc)[:200] or exc.__class__.__name__))
            continue
        warning = ""
        if level in FORCE_LEVELS:
            warning = f"{level} 降级：依赖强制/合成事件，真实用户可能做不到（可用性缺陷候选）"
        return LadderOutcome(ok=True, level=level, warning=warning, attempts=attempts)
    return LadderOutcome(ok=False, attempts=attempts)
