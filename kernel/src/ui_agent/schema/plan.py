"""计划契约：一段自然语言流程 → 若干步小目标 + 每步独立断言。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from ui_agent.schema.check import CheckSpec


class PlanStep(BaseModel):
    goal: str
    checks: list[CheckSpec] = Field(default_factory=list)
    url: str = ""
    note: str = ""

    @field_validator("goal")
    @classmethod
    def _goal_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("步骤缺少 goal")
        return value


class TestPlan(BaseModel):
    name: str = "task"
    url: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    @field_validator("steps")
    @classmethod
    def _steps_required(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not value:
            raise ValueError("计划里至少要有一个步骤")
        return value

    @property
    def slug(self) -> str:
        return re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", self.name).strip("_") or "task"

    def step_dir_name(self, index: int, step: PlanStep, attempt: int = 1) -> str:
        head = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", step.goal)[:24].strip("_") or "step"
        retry = f"-r{attempt}" if attempt > 1 else ""  # 重排后重试的同一步单独占一个目录
        return f"{index:02d}-{head}{retry}"
