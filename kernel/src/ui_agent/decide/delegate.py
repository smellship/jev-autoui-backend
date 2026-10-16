"""大模型代班 Jev：入参/出参与 Jev 请求同构（state + questions → answers），只换答题人。

两阶段决策：
  Phase 1（快路径）：reasoning=none，最多由 runner 重试 2 次（第 2 次带上一次报错）。
  Phase 2（思考路径）：runner 在 Phase 1 两次都失败后升级到这里，reasoning 开启，
    最多 5 次（每次带上一次报错）。模型可以返回 {"error": "..."} 直接中止本次运行。

落点校验与 Jev 完全共用（assemble_decision），答歪了照样拒。
"""

from __future__ import annotations

from ui_agent.config import Settings
from ui_agent.decide.jev import assemble_decision, build_questions, to_jev_state
from ui_agent.decide.prompts import DELEGATE, THINKING_DELEGATE
from ui_agent.llm.client import chat_json
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import State


class AbortDecision(Exception):
    """思考模式下模型主动返回的错误信息——任务无法继续，应直接中止本次运行。"""


class LlmEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    def decide(self, state: State, goal: str, rules: list[str] | None = None,
               error_context: str | None = None) -> Decision:
        """Phase 1 快路径决策。error_context 为上一次尝试的报错，用于重试时提供给模型参考。"""
        if not self.s.text_model_api_key:
            raise RuntimeError("缺少 TEXT_MODEL_API_KEY；大模型代班需要它。")
        questions, _ = build_questions(state, goal, rules)
        user = {"state": to_jev_state(state), "questions": questions}
        if error_context:
            user["previous_error"] = error_context
        return self._ask_once(state, questions, user, reasoning="none")

    def decide_thinking(self, state: State, goal: str, rules: list[str] | None = None,
                        error_context: str | None = None) -> Decision:
        """Phase 2 思考路径决策。开启 reasoning，模型可返回 {"error": "..."} 中止运行。"""
        if not self.s.text_model_api_key:
            raise RuntimeError("缺少 TEXT_MODEL_API_KEY；大模型代班需要它。")
        questions, _ = build_questions(state, goal, rules)
        user = {"state": to_jev_state(state), "questions": questions}
        if error_context:
            user["previous_error"] = error_context
        return self._ask_once_thinking(state, questions, user)

    def _ask_once(self, state: State, questions: dict, user: dict, *, reasoning: str) -> Decision:
        data, meta = chat_json(self.s, DELEGATE, user, max_tokens=4096, reasoning=reasoning)
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("代班模型未返回 answers 对象；未执行任何动作。")
        return assemble_decision(state, questions, answers,
                                 model=str(meta.get("model", "")), usage=meta.get("usage", {}),
                                 latency_ms=int(meta.get("latency_ms", 0)))

    def _ask_once_thinking(self, state: State, questions: dict, user: dict) -> Decision:
        # reasoning="enabled"：reasoning_options 对非 none 口径不下发关思考字段，
        # 让网关用自己的默认（deepseek-flash 默认思考开启）；None 会回落到配置值 "none"。
        data, meta = chat_json(self.s, THINKING_DELEGATE, user, max_tokens=8192, reasoning="enabled")
        error_msg = data.get("error")
        if isinstance(error_msg, str) and error_msg.strip():
            raise AbortDecision(error_msg.strip())
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("思考模型未返回 answers 对象；未执行任何动作。")
        return assemble_decision(state, questions, answers,
                                 model=str(meta.get("model", "")), usage=meta.get("usage", {}),
                                 latency_ms=int(meta.get("latency_ms", 0)))
