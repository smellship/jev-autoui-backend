"""原始快照 → State（事实层）：元素索引化、文本裁剪、历史动作、点击失败记录。"""

from __future__ import annotations

from ui_agent.schema.state import Element, PageInfo, RecentAction, State


def page_summary(state: State) -> dict:
    """探针用的最小事实集：只报页面上直接看得到的东西，不做业务推断。

    needs_login 取「有密码输入框」这个确定性信号：它是登录页最硬的特征，
    代价是改密页等含密码框的页面也会被判成登录页。
    """
    has_password = any(el.secret for el in state.elements)
    return {
        "final_url": state.page.url,
        "title": state.page.title,
        "element_count": len(state.elements),
        "needs_login": has_password,
        "has_password_field": has_password,
    }


def build_state(
    raw: dict,
    recent_actions: list[RecentAction],
    click_failures: dict[str, str] | None = None,
    max_elements: int = 250,
    max_text: int = 6000,
) -> State:
    page = raw.get("page", {})
    elements = [Element(**item) for item in raw.get("elements", [])[:max_elements]]
    return State(
        page=PageInfo(
            url=page.get("url", ""),
            title=page.get("title", ""),
            text=(page.get("text") or "")[:max_text],
            viewport=page.get("viewport", {}),
            scroll=page.get("scroll", {}),
        ),
        elements=elements,
        recent_actions=recent_actions,
        fingerprint=raw.get("fingerprint", ""),
        omitted=raw.get("omitted", 0),
        click_failures=click_failures or {},
    )
