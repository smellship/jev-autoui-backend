"""回放执行：按步取录像，把语义身份（角色/标签/选项）重定位到当次快照。

匹配 = 变体选择 + 顺序指针 + 上下文守卫：录像按起点分变体（同一 (step, variant)
的行是一条同起点的路径），首次取用时按当前页面选变体（后录优先）；元素按语义重定位——
索引跨运行没有意义，绝不按旧索引点。任何一处对不上就返回未命中原因（词表见
run/trace.py），且本步回放就此终止（handover）：余下决策交模型，中途续放会把转写
搅乱。零模型模式下 runner 按原因停机。

严判（session.strict）额外要求状态签名一致：页面结构一变就按未命中处理——
用户强行用它探查「页面是否已经变了」。
"""

from __future__ import annotations

from ui_agent.replay.cache import CACHE_SOURCE, CachedDecision, normalize_url, state_signature
from ui_agent.replay.session import ReplaySession
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element, State

ELEMENT_OPS = {"CLICK", "CLICK_FORCE", "TYPE_TEXT", "UPLOAD", "SELECT", "HOVER", "SCROLL_TO", "DRAG", "CHECK"}
FREEBAND_OPS = {"PRESS_KEY", "SCROLL_UP", "SCROLL_DOWN", "WAIT", "DONE"}
KNOWN_OPS = ELEMENT_OPS | FREEBAND_OPS


def match_next(session: ReplaySession, state: State) -> tuple[Decision | None, str]:
    """取该步的下一条录像并对上当前快照；返回 (决策, "") 或 (None, 未命中原因)。

    变体化（v3）：首次取用时按当前页面从录像的变体组里选起点（后录优先），选不中
    整步交还（no-variant）；未命中即终结本步回放（handover），余下决策交给模型。
    """
    if session.handed_over():
        return None, "handover"
    groups = session.variant_groups()
    if not groups:
        return _fail(session, "no-entry")
    rows = session.active_rows()
    if rows is None:
        for variant, candidate in reversed(groups):
            if _context_ok(candidate[0], state):
                session.activate(variant)
                rows = candidate
                break
        else:
            return _fail(session, "no-variant")
    used = session.consumed
    if used >= len(rows):
        return _fail(session, "truncated")
    record = rows[used]
    if not _context_ok(record, state):
        return _fail(session, "wrong-context")
    if session.strict and record.signature and record.signature != state_signature(state):
        return _fail(session, "wrong-context")
    if record.operation not in KNOWN_OPS:
        return _fail(session, "invalid-entry")

    if record.operation == "DONE":
        session.advance()
        return Decision(operation="DONE", confidence=1.0, model=CACHE_SOURCE), ""
    if record.operation == "PRESS_KEY":
        if not record.press_key:
            return _fail(session, "invalid-entry")
        session.advance()
        return Decision(operation="PRESS_KEY", press_key=record.press_key, confidence=1.0,
                        model=CACHE_SOURCE), ""
    if record.operation in ("SCROLL_UP", "SCROLL_DOWN", "WAIT"):
        session.advance()
        return Decision(operation=record.operation, confidence=1.0, model=CACHE_SOURCE), ""

    element = _relocate(record, state.elements)
    if element is None:
        return _fail(session, "invalid-entry")
    if record.operation == "SELECT":
        option = _relocate_option(record, element)
        if option is None:
            return _fail(session, "invalid-entry")
        session.advance()
        return Decision(operation="SELECT", target=option.key, target_element=element.index,
                        confidence=1.0, model=CACHE_SOURCE), ""
    if record.operation == "DRAG":
        to_element = _relocate_to(record, state.elements)
        if to_element is None:
            return _fail(session, "invalid-entry")
        session.advance()
        return Decision(operation="DRAG", target=str(element.index), target_element=element.index,
                        target_option=str(to_element.index), confidence=1.0, model=CACHE_SOURCE), ""
    if record.operation == "CHECK":
        session.advance()
        return Decision(operation="CHECK", target=str(element.index), target_element=element.index,
                        press_key="true" if record.checked else "false", confidence=1.0,
                        model=CACHE_SOURCE), ""
    session.advance()
    # 文本模型产出的字段值随录像复用（零模型回归不再问模型）；raw_answers 只是运输工具
    answers = {"text": record.text} if (record.operation == "TYPE_TEXT" and record.text) else {}
    return Decision(operation=record.operation, target=str(element.index),
                    target_element=element.index, confidence=1.0, model=CACHE_SOURCE,
                    raw_answers=answers), ""


def _fail(session: ReplaySession, reason: str) -> tuple[None, str]:
    """未命中即终结本步回放（handover）：记词、本步余下决策全交模型。"""
    session.miss(reason)
    session.hand_over()
    return None, reason


def _context_ok(record: CachedDecision, state: State) -> bool:
    """页面身份守卫：地址按路径比（query 常挂易变串），标题只在两边都有时比。"""
    if normalize_url(record.url, keep_query=False) != normalize_url(state.page.url, keep_query=False):
        return False
    if record.title and state.page.title and record.title != state.page.title:
        return False
    return True


def _relocate(record: CachedDecision, elements: list[Element]) -> Element | None:
    """语义重定位：角色+标签精确命中优先；标签唯一且角色变了也认。"""
    hits = [el for el in elements if el.label == record.target_label]
    for el in hits:
        if el.role == record.target_role:
            return el
    return hits[0] if len(hits) == 1 else None


def _relocate_option(record: CachedDecision, element: Element):
    """选项重定位：标签优先，value 兜底（标签动态的场景）。"""
    for option in element.options:
        if record.target_option_label and option.label == record.target_option_label:
            return option
    for option in element.options:
        if record.target_option_value and option.value == record.target_option_value:
            return option
    return None


def _relocate_to(record: CachedDecision, elements: list[Element]) -> Element | None:
    """拖拽终点重定位：角色+标签精确命中优先；标签唯一且角色变了也认。"""
    hits = [el for el in elements if el.label == record.to_label]
    for el in hits:
        if el.role == record.to_role:
            return el
    return hits[0] if len(hits) == 1 else None
