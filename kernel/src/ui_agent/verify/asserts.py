"""验收断言：只看 State（事实）与元素属性（只读 DOM），不看模型怎么说。

DONE 只是线索，断言才是结论；DONE 后的验收按轮询进行——页面常有理化延迟，
断言自动重试到全过或超时（见 run_checks_polling），单条可用 timeout_ms 加宽窗口。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ui_agent.schema.check import KINDS, CheckResult, CheckSpec
from ui_agent.schema.state import Element, State

__all__ = ["KINDS", "ASSERT_POLL_INTERVAL_MS", "CheckResult", "CheckSpec", "VerifyOutcome",
           "check", "find_elements", "parse_check", "run_checks", "run_checks_polling"]

ASSERT_POLL_INTERVAL_MS = 250  # DONE 验收的轮询间隔：重读整页快照，比探针贵，别追 100ms


def parse_check(text: str) -> CheckSpec:
    """支持 JSON 或 kind=value / element_value=标签|期望值 两种写法。

    element_count 写「标签|个数」或只写「个数」；element_attribute 需要 attr，只能走 JSON：
    {"kind": "element_attribute", "value": 标签, "attr": "href", "expected": "..."}；
    单条加宽轮询窗口用 JSON 里的 "timeout_ms"。
    """
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
    if spec.kind == "element_count":
        if not spec.expected:
            if spec.value.isdigit():
                spec.value, spec.expected = "", spec.value
            else:
                spec.expected = spec.value
        if not spec.expected.isdigit():
            raise ValueError(f"element_count 的期望值必须是整数个数：{spec.expected or '（空）'}")
    else:
        if not spec.value:
            spec.value = spec.expected
        if not spec.expected:
            spec.expected = spec.value
    if not spec.value and spec.kind != "element_count":
        raise ValueError("断言缺少目标值")
    if not spec.expected:
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


def check(state: State, spec: CheckSpec, dom=None) -> CheckResult:
    """跑一条断言。dom 是只读属性读取口（driver 提供）：get_attribute(index, name) -> str。"""
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
    elif spec.kind in ("element_exists", "element_absent", "element_value", "element_count"):
        hits = find_elements(state, spec.value, spec.role)
        if spec.kind == "element_exists":
            ok = bool(hits)
            evidence = f"匹配到 {len(hits)} 个：{hits[0].label}" if hits else "没有匹配元素"
        elif spec.kind == "element_absent":
            ok = not hits
            evidence = "没有匹配元素" if ok else f"仍存在：{hits[0].label}"
        elif spec.kind == "element_count":
            ok, evidence = _count_evidence(spec, hits, state)
        else:
            hit = next((el for el in hits if el.value.strip() == spec.expected.strip()), None)
            ok = hit is not None
            actual = "、".join(el.value for el in hits[:3]) if hits else "没有匹配元素"
            evidence = f"值 = {spec.expected}" if ok else f"实际值：{actual}"
    elif spec.kind == "element_attribute":
        hits = find_elements(state, spec.value, spec.role)
        if dom is None:
            evidence = "该断言需要实时读取 DOM，当前校验环境不支持"
        elif not hits:
            evidence = "没有匹配元素"
        else:
            actuals = [(el, dom.get_attribute(el.index, spec.attr)) for el in hits]
            hit = next((el for el, value in actuals if value.strip() == spec.expected.strip()), None)
            ok = hit is not None
            shown = "、".join(value or "（无）" for _, value in actuals[:3])
            evidence = f"{spec.attr} = {spec.expected}" if ok else f"实际 {spec.attr}：{shown}"
    return CheckResult(kind=spec.kind, expected=spec.expected or spec.value, ok=ok, evidence=evidence)


def run_checks(state: State, specs: list[CheckSpec], dom=None) -> list[CheckResult]:
    """单次验收（不轮询）；需要重试的场景走 run_checks_polling。"""
    return [check(state, spec, dom) for spec in specs]


@dataclass
class VerifyOutcome:
    checks: list[CheckResult] = field(default_factory=list)
    attempts: int = 1        # 批量轮询次数（1 = 首评即过或未重试）
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.checks)


def run_checks_polling(specs: list[CheckSpec], state: State, state_fn: Callable[[], State],
                       timeout_ms: int = 0, interval_ms: int = ASSERT_POLL_INTERVAL_MS,
                       clock: Callable[[], float] = time.monotonic,
                       sleep: Callable[[float], None] = time.sleep,
                       dom=None) -> VerifyOutcome:
    """DONE 验收：全过即止；否则按 interval 轮询重读 DOM，直到全过或超时。

    首评用当次循环的快照（零额外开销），后续轮次调 state_fn 重读；窗口取运行级默认与
    各条 timeout_ms 的最宽值（窗口 0 = 只评一次）。已通过的断言不再复评，attempts
    停在它成立的那轮。
    """
    if not specs:
        return VerifyOutcome([])
    started = clock()
    window = max([timeout_ms, *(item.timeout_ms for item in specs)]) / 1000
    results: list[CheckResult | None] = [None] * len(specs)
    attempts = 0
    current = state
    while True:
        attempts += 1
        for index, spec in enumerate(specs):
            if results[index] is not None and results[index].ok:
                continue
            result = check(current, spec, dom)
            result.attempts = attempts
            results[index] = result
        if all(item is not None and item.ok for item in results):
            break
        if window <= 0 or clock() - started >= window:
            break
        sleep(interval_ms / 1000)
        current = state_fn()
    outcome = VerifyOutcome([item for item in results if item is not None],
                            attempts, round((clock() - started) * 1000))
    return outcome


def _count_evidence(spec: CheckSpec, hits: list[Element], state: State) -> tuple[bool, str]:
    try:
        want = int(spec.expected.strip())
    except ValueError:
        return False, f"期望值不是整数：{spec.expected}"
    tail = "（快照截断，实际可能更多）" if state.omitted else ""
    if len(hits) == want:
        return True, f"匹配到 {len(hits)} 个{tail}"
    return False, f"实际匹配 {len(hits)} 个，期望 {want} 个{tail}"


def _snippet(text: str, needle: str, span: int = 40) -> str:
    at = text.find(needle)
    start = max(0, at - span)
    return "…" + text[start:at + len(needle) + span].replace("\n", " ") + "…"
