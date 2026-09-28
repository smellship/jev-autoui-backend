"""执行层：白名单动作 + 执行前守卫。

模型输出只能到这里为止：索引 → locator 的翻译、坐标、降级都封在本层与 driver 里。
L4/L5（强制/合成）以及 T2（强制输入）必须原样上报，作为可用性缺陷候选。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ui_agent.driver.ladder import run_ladder
from ui_agent.driver.playwright_ import Driver, FileRef, StaleTarget
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element

EXECUTABLE = {"CLICK", "CLICK_FORCE", "TYPE_TEXT", "UPLOAD", "SELECT", "HOVER", "PRESS_KEY",
              "SCROLL_UP", "SCROLL_DOWN", "SCROLL_TO", "WAIT"}


@dataclass
class ActionResult:
    ok: bool
    operation: str
    element: int | None = None
    level: str = ""
    warning: str = ""
    detail: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def trace(self) -> dict:
        data = {"ok": self.ok, "level": self.level, "detail": self.detail}
        if self.warning:
            data["warning"] = self.warning
        if self.attempts:
            data["attempts"] = [list(a) for a in self.attempts]
        return data


class Executor:
    def __init__(self, driver: Driver):
        self.d = driver
        self.click_failures: dict[str, str] = {}

    # --- 唯一入口 ---

    def apply(self, decision: Decision, text: str = "", element: Element | None = None,
              files: list[FileRef] | None = None) -> ActionResult:
        op = decision.operation
        if op not in EXECUTABLE:
            return ActionResult(False, op, element=decision.target_element, detail=f"不接受的操作：{op}")
        try:
            return self._dispatch(op, decision, text, element, files)
        except StaleTarget as exc:
            return ActionResult(False, op, element=decision.target_element, detail=str(exc))
        except Exception as exc:  # 驱动异常只记录，不让循环崩掉
            name = exc.__class__.__name__
            detail = f"{name}: {str(exc)[:200] or '无详情'}"
            return ActionResult(False, op, element=decision.target_element, detail=detail)

    def _dispatch(self, op: str, decision: Decision, text: str, element: Element | None,
                  files: list[FileRef] | None) -> ActionResult:
        if op == "CLICK":
            return self._click(decision, force=False)
        if op == "CLICK_FORCE":
            return self._click(decision, force=True)
        if op == "TYPE_TEXT":
            return self._type(decision, text, element)
        if op == "UPLOAD":
            return self._upload(decision, files)
        if op == "SELECT":
            return self._select(decision)
        if op == "HOVER":
            return self._hover(decision)
        if op == "PRESS_KEY":
            key = decision.press_key or ""
            if not key:
                return ActionResult(False, op, detail="缺少按键；未执行。")
            self.d.press_key(key)
            self.d.settle()
            return ActionResult(True, op, detail=key)
        if op in ("SCROLL_UP", "SCROLL_DOWN"):
            self.d.scroll("up" if op.endswith("UP") else "down")
            return ActionResult(True, op, detail="滚动一屏")
        if op == "SCROLL_TO":
            index = decision.target_element
            if index is None:
                return ActionResult(False, op, detail="缺少目标索引；未执行滚动。")
            pre = self._precheck(index, op)
            if pre is not None:
                return pre
            self.d.scroll_to(index)
            self.d.settle(300)
            return ActionResult(True, op, element=index, detail="滚动到元素可见")
        if op == "WAIT":
            self.d.wait(self.d.s.settle_ms * 2)
            return ActionResult(True, op, detail="等待页面更新")
        return ActionResult(False, op, detail=f"未实现的动作：{op}")

    # --- 守卫与点击记录 ---

    def _precheck(self, index: int, op: str) -> ActionResult | None:
        """执行前守卫：索引失效/不可见就别浪费一次降级阶梯。视口外不算失败——L1 会滚动补上。"""
        guard = self.d.guard(index)
        if guard.get("ok"):
            return None
        return ActionResult(False, op, element=index, detail=f"守卫未通过：{guard.get('reason') or '不可见'}")

    def _record_click(self, index: int, outcome) -> None:
        key = str(index)
        if outcome.ok:
            self.click_failures.pop(key, None)
            return
        reason = outcome.attempts[-1][1] if outcome.attempts else "未知原因"
        self.click_failures[key] = reason[:200]

    def _click(self, decision: Decision, force: bool) -> ActionResult:
        op = decision.operation
        index = decision.target_element
        if index is None:
            return ActionResult(False, op, detail="缺少目标索引；未执行点击。")
        pre = self._precheck(index, op)
        if pre is not None:
            self.click_failures[str(index)] = pre.detail
            return pre
        outcome = self.d.click(index, allow_force=force)
        self._record_click(index, outcome)
        self.d.settle()
        detail = "已点击"
        if not outcome.ok:
            reason = outcome.attempts[-1][1] if outcome.attempts else "未知原因"
            detail = f"点击失败：{reason}"
        return ActionResult(outcome.ok, op, element=index, level=outcome.level,
                            warning=outcome.warning, detail=detail, attempts=outcome.attempts)

    def _type(self, decision: Decision, text: str, element: Element | None) -> ActionResult:
        op = "TYPE_TEXT"
        index = decision.target_element
        if index is None:
            return ActionResult(False, op, detail="缺少目标索引；未执行输入。")
        if not text:
            return ActionResult(False, op, element=index, detail="没有可用的字段值；未执行输入。")
        pre = self._precheck(index, op)
        if pre is not None:
            return pre
        outcome = self.d.type_text(index, text)
        self.d.settle()
        shown = "•" * 6 if (element and element.secret) else text
        reason = outcome.attempts[-1][1] if outcome.attempts else "未知原因"
        detail = f"已输入：{shown}" if outcome.ok else f"输入失败：{reason}"
        return ActionResult(outcome.ok, op, element=index, level=outcome.level,
                            warning=outcome.warning, detail=detail, attempts=outcome.attempts)

    def _upload(self, decision: Decision, files: list[FileRef] | None) -> ActionResult:
        op = "UPLOAD"
        index = decision.target_element
        if index is None:
            return ActionResult(False, op, detail="缺少目标索引；未执行上传。")
        if not files:
            return ActionResult(False, op, element=index, detail="没有可用的文件；未执行上传。")
        # 不做可见性守卫：文件输入常被 display:none 藏着，注入文件不依赖它可见
        mode = self.d.s.ui_agent_upload_mode
        outcome = self.d.upload(index, files, mode)
        self.d.settle()
        shown = "、".join(f.name for f in files)
        detail = f"已上传：{shown}" if outcome.ok else \
            f"上传失败：{outcome.attempts[-1][1] if outcome.attempts else '未知原因'}"
        return ActionResult(outcome.ok, op, element=index, level=outcome.level, detail=detail,
                            attempts=outcome.attempts)

    def _select(self, decision: Decision) -> ActionResult:
        op = "SELECT"
        index, dom_index = decision.target_element, decision.target_option
        if index is None or dom_index is None:
            return ActionResult(False, op, detail="缺少下拉目标；未执行选择。")
        pre = self._precheck(index, op)
        if pre is not None:
            return pre

        def s0() -> None:
            self.d.select(index, dom_index)

        def s1() -> None:
            self.d.click(index)
            self.d.select(index, dom_index)

        outcome = run_ladder([("S0", s0), ("S1", s1)])
        self.d.settle()
        detail = "已选择" if outcome.ok else f"选择失败：{outcome.attempts[-1][1] if outcome.attempts else '未知原因'}"
        return ActionResult(outcome.ok, op, element=index, level=outcome.level, detail=detail,
                            attempts=outcome.attempts)

    def _hover(self, decision: Decision) -> ActionResult:
        op = "HOVER"
        index = decision.target_element
        if index is None:
            return ActionResult(False, op, detail="缺少目标索引；未执行悬停。")
        pre = self._precheck(index, op)
        if pre is not None:
            return pre
        self.d.hover(index)
        self.d.settle(300)
        return ActionResult(True, op, element=index, detail="已悬停")
