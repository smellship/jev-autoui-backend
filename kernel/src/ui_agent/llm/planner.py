"""planner 缝：一段自然语言流程 → TestPlan（每步一个小 goal + 独立断言）。

计划是"提议"，不是"证据"：每步跑完仍由 verify 断言判定，模型自述不算数。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from ui_agent.config import Settings
from ui_agent.decide.prompts import PLANNER
from ui_agent.llm.client import chat_json
from ui_agent.schema.check import KINDS
from ui_agent.schema.plan import TestPlan

MAX_STEPS = 12
MAX_DESCRIPTION = 6000


def plan_context(description: str, url: str = "", hints: dict | None = None) -> dict:
    data = {"process_description": description.strip()[:MAX_DESCRIPTION]}
    if url:
        data["start_url"] = url
    if hints:
        data["harness_hints"] = hints
    return data


def make_plan(settings: Settings, description: str, url: str = "",
              hints: dict | None = None) -> tuple[TestPlan, dict]:
    data, meta = chat_json(settings, PLANNER, plan_context(description, url, hints), max_tokens=2048)
    try:
        plan = TestPlan(**data)
    except ValidationError as exc:
        raise RuntimeError(f"planner 返回的计划不合法：{_brief(exc)}") from None
    if len(plan.steps) > MAX_STEPS:
        raise RuntimeError(f"planner 给出 {len(plan.steps)} 个步骤，超过上限 {MAX_STEPS}")
    bad = [f"第 {i + 1} 步 {spec.kind or '(空)'}" for i, step in enumerate(plan.steps)
           for spec in step.checks if spec.kind not in KINDS]
    if bad:
        raise RuntimeError(f"planner 用了不支持的断言：{'、'.join(bad)}；可用：{'、'.join(KINDS)}")
    if url and not plan.url:
        plan.url = url
    return plan, meta


def plan_to_json(plan: TestPlan) -> str:
    return json.dumps(plan.model_dump(), ensure_ascii=False, indent=2)


def plan_from_json(text: str) -> TestPlan:
    try:
        return TestPlan(**json.loads(text))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise RuntimeError(f"计划文件不合法：{_brief(exc)}") from None


def _brief(exc: Exception, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
