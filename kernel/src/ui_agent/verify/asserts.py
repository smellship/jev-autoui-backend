"""验收断言：只看 State（事实），不看模型怎么说。DONE 只是线索，断言才是结论。"""

from __future__ import annotations

import json

from ui_agent.schema.check import KINDS, CheckResult, CheckSpec
from ui_agent.schema.state import Element, State

__all__ = ["KINDS", "CheckResult", "CheckSpec", "check", "find_elements", "parse_check", "run_checks"]


def parse_check(text: str) -> CheckSpec:
    """支持 JSON 或 kind=value / element_value=标签|期望值 两种写法。"""
    raw = text.strip()
    if raw.startswith("{"):
        spec = CheckSpec(**json.loads(raw))
    else:
        kind, _, rest = raw.partition("=")
        kind = kind.strip()
        if kind not in KINDS:
            raise ValueError(f"未知断言类型 {kind}；可用：{'、'.join(KINDS)}")
        value, _, expected = rest.partition("|")
        spec = CheckSpec(kind=kind, value=value.strip(), expected=expected.strip())
    if spec.kind not in KINDS:
        raise ValueError(f"未知断言类型 {spec.kind}；可用：{'、'.join(KINDS)}")
    if not spec.value:
        spec.value = spec.expected
    if not spec.expected:
        spec.expected = spec.value
    if not spec.value:
        raise ValueError("断言缺少目标值")
    return spec


def find_elements(state: State, label: str, role: str = "") -> list[Element]:
    needle = label.strip().lower()
    hits = []
    for el in state.elements:
        if role and el.role != role:
            continue
        if needle and needle not in el.label.lower():
            continue
        hits.append(el)
    return hits


def check(state: State, spec: CheckSpec) -> CheckResult:
    ok, evidence = False, ""
    if spec.kind == "text_contains":
        ok = spec.value in state.page.text
        evidence = _snippet(state.page.text, spec.value) if ok else "页面文本中没有该内容"
    elif spec.kind == "url_contains":
        ok = spec.value in state.page.url
        evidence = state.page.url
    elif spec.kind == "title_contains":
        ok = spec.value in state.page.title
        evidence = state.page.title
    elif spec.kind in ("element_exists", "element_absent", "element_value"):
        hits = find_elements(state, spec.value, spec.role)
        if spec.kind == "element_exists":
            ok = bool(hits)
            evidence = f"匹配到 {len(hits)} 个：{hits[0].label}" if hits else "没有匹配元素"
        elif spec.kind == "element_absent":
            ok = not hits
            evidence = "没有匹配元素" if ok else f"仍存在：{hits[0].label}"
        else:
            hit = next((el for el in hits if el.value.strip() == spec.expected.strip()), None)
            ok = hit is not None
            actual = "、".join(el.value for el in hits[:3]) if hits else "没有匹配元素"
            evidence = f"值 = {spec.expected}" if ok else f"实际值：{actual}"
    return CheckResult(kind=spec.kind, expected=spec.expected or spec.value, ok=ok, evidence=evidence)


def run_checks(state: State, specs: list[CheckSpec]) -> list[CheckResult]:
    return [check(state, spec) for spec in specs]


def _snippet(text: str, needle: str, span: int = 40) -> str:
    at = text.find(needle)
    start = max(0, at - span)
    return "…" + text[start:at + len(needle) + span].replace("\n", " ") + "…"
