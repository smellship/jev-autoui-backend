"""决策引擎工厂：按 UI_AGENT_DECIDER 在 Jev / 大模型代班 / 关闭之间选，两个 runner 共用。"""

from __future__ import annotations

from typing import Protocol

from ui_agent.config import Settings
from ui_agent.decide.delegate import LlmEngine
from ui_agent.decide.jev import JevEngine
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import State

ENGINE_KINDS = ("jev", "llm", "off")


class Decider(Protocol):
    def decide(self, state: State, goal: str, rules: list[str] | None = None,
               error_context: str | None = None) -> Decision: ...


class OffEngine:
    """决策关闭：没有可回放的缓存记录时，一次决策都不做——到点直接停。"""

    def __init__(self, settings: Settings):
        self.s = settings

    def decide(self, state: State, goal: str, rules: list[str] | None = None,
               error_context: str | None = None) -> Decision:
        raise RuntimeError("决策模型已关闭（UI_AGENT_DECIDER=off）：没有可回放的缓存记录；已停止。")


def make_engine(settings: Settings) -> Decider:
    if settings.ui_agent_decider == "llm":
        return LlmEngine(settings)
    if settings.ui_agent_decider == "off":
        return OffEngine(settings)
    return JevEngine(settings)
