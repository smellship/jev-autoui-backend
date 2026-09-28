"""Jev 决策契约：一次请求的解析结果。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChoiceAnswer(BaseModel):
    choice: str
    probabilities: dict[str, float]
    confidence: float


class Decision(BaseModel):
    operation: str
    target: str | None = None
    target_element: int | None = None
    target_option: int | None = None
    press_key: str | None = None
    confidence: float = 0.0
    operation_probabilities: dict[str, float] = Field(default_factory=dict)
    target_probabilities: dict[str, float] = Field(default_factory=dict)
    model: str = ""
    usage: dict = Field(default_factory=dict)
    latency_ms: int = 0
    raw_answers: dict = Field(default_factory=dict)
