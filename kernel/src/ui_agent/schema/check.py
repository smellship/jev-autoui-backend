"""验收断言的契约：断言描述与结果，供 plan 与 verify 共用。

断言在 DONE 处轮询验收（见 verify.asserts.run_checks_polling）：单条可用 timeout_ms
加宽自己的窗口；结果里的 attempts 记录它是在第几轮采样才成立的（1 = 首评即过）。
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator

KINDS = ("text_contains", "url_contains", "title_contains",
         "element_exists", "element_absent", "element_value",
         "element_count", "element_attribute")


class CheckSpec(BaseModel):
    kind: str
    value: str = ""
    expected: str = ""
    role: str = ""
    attr: str = ""            # element_attribute：属性名（href / src / aria-label …）
    timeout_ms: int = 0       # 轮询窗口；0 = 用运行级默认（UI_AGENT_ASSERT_TIMEOUT_MS）

    @model_validator(mode="after")
    def _attr_required(self) -> "CheckSpec":
        if self.kind == "element_attribute" and not self.attr.strip():
            raise ValueError("element_attribute 断言必须给 attr（属性名）")
        return self


class CheckResult(BaseModel):
    kind: str
    expected: str
    ok: bool
    evidence: str = ""
    attempts: int = 1         # 第几轮采样成立（从未成立 = 总轮数）

    @property
    def label(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"[{mark}] {self.kind}『{self.expected}』{'' if self.ok else ' — ' + (self.evidence or '无证据')}"
