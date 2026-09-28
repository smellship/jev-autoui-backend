"""TYPE_TEXT 取值的唯一来源：显式声明的测试数据（vars）优先，其次小模型；都没有就拒填。"""

from __future__ import annotations

from ui_agent.config import Settings
from ui_agent.decide.prompts import TEXT_VALUE
from ui_agent.llm.client import chat_json
from ui_agent.schema.state import Element, State


def field_context(goal: str, element: Element, state: State) -> dict:
    return {
        "goal": goal,
        "field": {"label": element.label, "role": element.role, "current_value": element.value},
        "page": {"url": state.page.url, "title": state.page.title, "text": state.page.text},
        "recent_actions": [a.model_dump(exclude_none=True) for a in state.recent_actions[-6:]],
    }


def match_var(vars: dict[str, str] | None, label: str) -> tuple[str, str] | None:
    """控件标签包含某个键就用它（取最长的键，避免"账号"抢"确认账号"）。"""
    if not vars:
        return None
    needle = label.strip().lower()
    hits = [(key, value) for key, value in vars.items() if key.strip() and key.strip().lower() in needle]
    if not hits:
        return None
    key, value = max(hits, key=lambda item: len(item[0]))
    return key, value


def resolve_text(settings: Settings, goal: str, element: Element, state: State,
                 vars: dict[str, str] | None = None) -> tuple[str, dict]:
    """vars 是人给的测试数据，不进任何模型请求；没有匹配才回落到文本模型。"""
    hit = match_var(vars, element.label)
    if hit is not None:
        key, value = hit
        return value, {"source": "var", "key": key, "model": "-", "latency_ms": 0}

    data, meta = chat_json(settings, TEXT_VALUE, field_context(goal, element, state))
    value = data.get("text")
    if set(data) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise RuntimeError("文本模型未给出可用的字段值（可能缺少依据）；未执行任何输入。")
    return value, {"source": "text_model", **meta}
