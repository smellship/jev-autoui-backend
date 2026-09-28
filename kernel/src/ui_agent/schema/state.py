"""state 契约：喂给 Jev 的唯一输入（事实），元素只能用 index 引用。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Option(BaseModel):
    key: str
    dom_index: int
    label: str = ""
    value: str = ""
    selected: bool = False
    disabled: bool = False


class Element(BaseModel):
    index: int
    role: str
    label: str
    value: str = ""
    secret: bool = False
    frame: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    in_viewport: bool = False
    occluded_by: str = ""
    disabled: bool = False
    checked: str | None = None
    selected: str | None = None
    expanded: str | None = None
    operations: list[str] = Field(default_factory=list)
    options: list[Option] = Field(default_factory=list)
    accept: str = ""            # 文件输入的 accept 声明（原样，供执行前校验）
    multiple: bool = False      # 文件输入是否允许多选
    hidden: bool = False        # 存在但不可见（文件输入常被隐藏，UPLOAD 仍可用）
    shadow: bool = False        # 位于 open shadow root 内（只影响模型对"点不点得到"的判断）


class PageInfo(BaseModel):
    url: str = ""
    title: str = ""
    text: str = ""
    viewport: dict = Field(default_factory=dict)
    scroll: dict = Field(default_factory=dict)


class RecentAction(BaseModel):
    action: str
    kind: str = ""
    text: str = ""
    level: str = ""
    page_changed: bool | None = None
    step: int | None = None  # 仅用于对账 trace 与截图，不进模型输入


class State(BaseModel):
    page: PageInfo
    elements: list[Element] = Field(default_factory=list)
    recent_actions: list[RecentAction] = Field(default_factory=list)
    fingerprint: str = ""
    omitted: int = 0
    click_failures: dict[str, str] = Field(default_factory=dict)
