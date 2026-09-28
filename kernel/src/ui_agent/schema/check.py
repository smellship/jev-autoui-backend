"""验收断言的契约：断言描述与结果，供 plan 与 verify 共用。"""

from __future__ import annotations

from pydantic import BaseModel

KINDS = ("text_contains", "url_contains", "title_contains",
         "element_exists", "element_absent", "element_value")


class CheckSpec(BaseModel):
    kind: str
    value: str = ""
    expected: str = ""
    role: str = ""


class CheckResult(BaseModel):
    kind: str
    expected: str
    ok: bool
    evidence: str = ""

    @property
    def label(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"[{mark}] {self.kind}『{self.expected}』{'' if self.ok else ' — ' + (self.evidence or '无证据')}"
