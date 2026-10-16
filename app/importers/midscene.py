"""midscene 脚本 → 用例 YAML：只做静态可证的转换，其余进报告交给人工。

三条规则（对应「老脚本没有断言也能跑」的落地）：
- 动作与 expect 按出现顺序落成步骤，expect 挂到它前面的那一步；
- 没有断言的步骤用「下一步入口元素」补一条 element_exists（前瞻）——既让每步都有事实可查，
  也满足内核 lint 的「每步必须有断言」；连下一步也借不到时用自身控件兜底，并如实标注；
- 最后一步没有结果型断言（text_contains / element_value / element_absent / element_count /
  element_attribute）时报告点明「只证明控件还在」，不假装它是验收。

值不进 goal：输入的值收进 vars（键取目标描述的核心词）；键像凭据就改写成 ${{secret.键}} 引用，
字面量不写进用例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

RESULT_KINDS = ("text_contains", "element_value", "element_absent",
                "element_count", "element_attribute")
MAX_STEPS = 12                  # 与内核一致，只用于报告提示
MAX_CHECKS_PER_STEP = 3
SENSITIVE_WORDS = ("密码", "口令", "password", "passwd", "token", "secret", "凭据", "密钥")
TAIL_WORDS = tuple(sorted((
    "输入框", "输入栏", "文本框", "搜索框", "下拉框", "下拉菜单", "选择框", "选择器",
    "复选框", "单选框", "单选按钮", "按钮", "按键", "链接", "超链接", "标签页", "页签",
    "选项卡", "卡片", "弹窗", "对话框", "区域", "图标", "控件", "字段", "输入区", "框",
), key=len, reverse=True))

GOTO_RE = re.compile(r"""\bpage\s*\.\s*goto\s*\(\s*(['"`])(.+?)\1""")
INPUT_RE = re.compile(r"""\baiInput\s*\(\s*(['"`])(.+?)\1\s*,\s*(['"`])(.+?)\3""")
TAP_RE = re.compile(r"""\bai(?:Tap|Click)\s*\(\s*(['"`])(.+?)\1""")
HOVER_RE = re.compile(r"""\baiHover\s*\(\s*(['"`])(.+?)\1""")
KEY_RE = re.compile(r"""\baiKeyboardPress\s*\(\s*(['"`])(.+?)\1(?:\s*,\s*(['"`])(.+?)\3)?""")
PAGE_KEY_RE = re.compile(r"""\bpage\s*\.\s*keyboard\s*\.\s*press\s*\(\s*(['"`])(.+?)\1""")
EXPECT_RE = re.compile(r"\bexpect\s*\(")
GETBYTEXT_RE = re.compile(r"""getByText\s*\(\s*(['"`])(.+?)\1""")
VISIBLE_RE = re.compile(r"\bto(?:BeVisible|BeInViewport|BeTruthy|ContainText|HaveText|Exist)\b")
NOT_RE = re.compile(r"\.\s*not\s*\.")
AI_CALL_RE = re.compile(r"\b(ai[A-Z]\w*)\s*\(")
PAGE_CALL_RE = re.compile(r"\bpage\s*\.\s*(\w+)\s*\(")
PAGE_CHAIN_RE = re.compile(r"\bpage\s*\.\s*(\w+)\s*\.\s*(\w+)\s*\(")
TEST_TITLE_RE = re.compile(r"""\b(?:test|it|describe)\s*\(\s*(['"`])(.+?)\1""")

RECOGNIZED_CALLS = {"aiInput", "aiTap", "aiClick", "aiHover", "aiKeyboardPress"}
# 认得出、但不需要转换的调用（名字 → 提示）
IGNORED_CALLS = {
    "aiWaitFor": "等待语句已忽略：内核在每个动作后有落定等待（settle），不需要显式等",
    "aiQuery": "数据提取调用已忽略：平台用例只做动作与断言",
    "aiNumber": "数据提取调用已忽略：平台用例只做动作与断言",
    "aiBoolean": "数据提取调用已忽略：平台用例只做动作与断言",
    "aiString": "数据提取调用已忽略：平台用例只做动作与断言",
    "aiLocate": "定位调用已忽略：平台按目标描述驱动决策模型，不需要显式定位",
}
# 认得出、但必须人工处理的调用（名字 → 提示）
MANUAL_CALLS = {
    "aiAssert": "自然语言断言无法静态转换：请人工补一条确定性断言（text_contains / element_value / element_count）",
    "aiScroll": "滚动动作暂不支持转换：请人工补成目标描述（例：滚动到「列表」）",
    "aiDoubleClick": "双击动作暂不支持转换：请人工补成目标描述",
    "aiRightClick": "右键点击动作暂不支持转换：请人工补成目标描述",
    "aiClearInput": "清空输入暂不支持转换：请人工补成目标描述",
    "aiSelect": "选择动作暂不支持转换：请人工补成目标描述",
    "aiUpload": "上传动作暂不支持转换：上传要在 vars 里给文件路径，请人工补",
    "aiDragAndDrop": "拖拽动作暂不支持转换：请人工补成目标描述",
    "aiAction": "复合动作暂不支持转换：请人工补成目标描述",
}
PAGE_IGNORED_CALLS = {"on", "off", "setViewportSize", "setDefaultTimeout", "addInitScript",
                      "screenshot", "close", "pause", "title", "viewportSize"}
PAGE_MANUAL_CALLS = {"click", "fill", "type", "press", "check", "uncheck", "selectOption",
                     "dblclick", "hover", "locator", "getByText", "getByRole", "getByLabel",
                     "getByPlaceholder", "getByTestId", "waitForSelector", "waitForURL",
                     "waitForLoadState", "reload", "goBack", "goForward", "setInputFiles"}


@dataclass
class _Step:
    kind: str                   # goto / input / tap / hover / keypress / confirm
    line: int
    raw: str = ""
    target: str = ""
    value: str = ""
    url: str = ""
    key: str = ""
    var_key: str = ""
    origin: str = ""            # explicit / lookahead / fallback / none
    checks: list[dict] = field(default_factory=list)

    def goal(self, index: int = 0) -> str:
        if self.kind == "goto":
            return "打开入口页面" if index == 0 else "打开脚本指定的页面"
        if self.kind == "input":
            return f"在「{self.target}」填入用例给定的值"
        if self.kind == "tap":
            return f"点击「{self.target}」"
        if self.kind == "hover":
            return f"悬停到「{self.target}」"
        if self.kind == "keypress":
            extra = f"（焦点在「{self.target}」）" if self.target else ""
            return f"按「{self.key}」键{extra}"
        return "确认页面内容"


@dataclass
class Report:
    steps: int = 0
    checks: int = 0
    explicit: int = 0
    lookahead: int = 0
    fallback: int = 0
    vars: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"steps": self.steps, "checks": self.checks, "explicit": self.explicit,
                "lookahead": self.lookahead, "fallback": self.fallback,
                "vars": list(self.vars), "issues": list(self.issues), "notes": list(self.notes)}


@dataclass
class ImportResult:
    yaml_text: str
    report: Report

    def as_dict(self) -> dict:
        return {"yaml": self.yaml_text, "report": self.report.as_dict()}


def convert(script: str, *, code: str = "", title: str = "", priority: str = "P2",
            owner: str = "", env_name: str = "", tags: list[str] | None = None,
            keep_url: bool = False) -> ImportResult:
    """脚本文本 → 用例 YAML + 报告。产物只依赖这台机器上的静态文本，不联网。"""
    text = script or ""
    steps, notes, issues = parse(text)
    _fill_checks(steps, notes)
    vars_map = _collect_vars(steps, notes)
    _review(steps, notes)
    entry = next((st.url for st in steps if st.kind == "goto" and st.url), "")
    if entry and not keep_url:
        notes.append(f"脚本入口 {entry} 没有写进用例：入口由环境地址决定；"
                     f"若环境根地址不是这个页面，给那一步补 url:（或重跑导入加 --keep-url）")
    if entry and keep_url:
        notes.append(f"已把脚本入口固定在第 1 步的 url: {entry}；平台运行时用环境地址覆盖计划入口，"
                     f"但会被这一步挡住——要跟随环境就删掉它")
    doc: dict[str, Any] = {}
    if code:
        doc["id"] = code
    doc["title"] = (title or "").strip() or _script_title(text) or "导入用例"
    doc["priority"] = priority or "P2"
    doc["tags"] = list(tags or [])
    doc["owner"] = owner
    doc["env"] = env_name
    if vars_map:
        doc["vars"] = vars_map
    doc["steps"] = [_step_doc(st, index, keep_url) for index, st in enumerate(steps)]
    if not steps:
        notes.append("脚本里没有识别出任何动作：生成的用例没有可执行的步骤，需要人工编写")
    yaml_text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                               default_flow_style=False, width=200)
    report = Report(steps=len(steps), checks=sum(len(st.checks) for st in steps),
                    explicit=sum(len(st.checks) for st in steps if st.origin == "explicit"),
                    lookahead=sum(len(st.checks) for st in steps if st.origin == "lookahead"),
                    fallback=sum(len(st.checks) for st in steps if st.origin == "fallback"),
                    vars=sorted(vars_map), issues=issues, notes=notes)
    return ImportResult(yaml_text, report)


def parse(text: str) -> tuple[list[_Step], list[str], list[str]]:
    """按出现顺序扫出动作与断言：认不出的调用进 issues/notes，不静默丢。"""
    notes: list[str] = []
    issues: list[str] = []
    events: list[tuple[int, _Step]] = []
    spans: list[tuple[int, int]] = []

    def line(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def take(match, step: _Step) -> None:
        events.append((match.start(), step))
        spans.append((match.start(), match.end()))

    for m in GOTO_RE.finditer(text):
        take(m, _Step("goto", line(m.start()), m.group(0), url=m.group(2)))
    for m in INPUT_RE.finditer(text):
        take(m, _Step("input", line(m.start()), m.group(0), target=m.group(4), value=m.group(2)))
    for m in TAP_RE.finditer(text):
        take(m, _Step("tap", line(m.start()), m.group(0), target=m.group(2)))
    for m in HOVER_RE.finditer(text):
        take(m, _Step("hover", line(m.start()), m.group(0), target=m.group(2)))
    for m in KEY_RE.finditer(text):
        take(m, _Step("keypress", line(m.start()), m.group(0),
                      key=m.group(2), target=m.group(4) or ""))
    for m in PAGE_KEY_RE.finditer(text):
        take(m, _Step("keypress", line(m.start()), m.group(0), key=m.group(2)))
    for m in EXPECT_RE.finditer(text):
        segment = _segment(text, m.start())
        spans.append((m.start(), m.start() + len(segment)))
        hit = GETBYTEXT_RE.search(segment)
        if hit is None:
            issues.append(f"第 {line(m.start())} 行：expect 里不是 getByText，暂不支持："
                          f"请人工补成文本断言")
            continue
        if not VISIBLE_RE.search(segment):
            issues.append(f"第 {line(m.start())} 行：认不出 expect 的断言形态"
                          f"（{segment.strip()[:40]}）：请人工补断言")
            continue
        kind = "element_absent" if NOT_RE.search(segment) else "text_contains"
        events.append((m.start(), _Step("expect", line(m.start()), segment.strip()[:80],
                                        checks=[{kind: hit.group(2)}])))

    events.sort(key=lambda item: item[0])
    steps: list[_Step] = []
    for _, event in events:
        if event.kind == "expect":
            if not steps:
                steps.append(_Step("confirm", event.line))
            steps[-1].checks.extend(event.checks)
            continue
        steps.append(event)

    for m in AI_CALL_RE.finditer(text):
        if _consumed(m.start(), spans):
            continue
        name = m.group(1)
        if name in IGNORED_CALLS:
            notes.append(f"第 {line(m.start())} 行：{IGNORED_CALLS[name]}（{name}）")
        elif name in MANUAL_CALLS:
            issues.append(f"第 {line(m.start())} 行：{MANUAL_CALLS[name]}（{name}）")
        elif name in RECOGNIZED_CALLS:
            issues.append(f"第 {line(m.start())} 行：{name} 的参数不是字面量字符串，无法静态转换："
                          f"请人工补这一步")
        else:
            issues.append(f"第 {line(m.start())} 行：未识别的调用 {name}(...)，已跳过："
                          f"请人工确认这一步要做什么")
    for m in PAGE_CALL_RE.finditer(text):
        if _consumed(m.start(), spans):
            continue
        name = m.group(1)
        if name in PAGE_IGNORED_CALLS:
            notes.append(f"第 {line(m.start())} 行：page.{name}(...) 已忽略（与用例无关或由内核自动处理）")
        elif name in PAGE_MANUAL_CALLS:
            issues.append(f"第 {line(m.start())} 行：原生调用 page.{name}(...) 已跳过："
                          f"平台靠目标描述驱动，不写选择器/JS；请人工改写成目标描述")
    for m in PAGE_CHAIN_RE.finditer(text):
        if _consumed(m.start(), spans):
            continue
        issues.append(f"第 {line(m.start())} 行：原生调用 page.{m.group(1)}.{m.group(2)}(...) 已跳过："
                      f"请人工改写成目标描述")
    return steps, notes, issues


def target_token(text: str) -> str:
    """目标描述 → 能拿去匹配元素标签的核心词：「查验按钮」→ 查验，「发票号码输入框」→ 发票号码。"""
    raw = _trim(text)
    raw = re.split(r"[，,。、;；:：|/\\]", raw, maxsplit=1)[0].strip()
    changed = True
    while changed and raw:
        changed = False
        for tail in TAIL_WORDS:
            if len(raw) > len(tail) and raw.endswith(tail):
                raw = _trim(raw[: -len(tail)])
                changed = True
                break
    if not raw or len(raw) > 16 or "${" in raw:
        return ""
    return raw


# --- 断言合成 ---

def _fill_checks(steps: list[_Step], notes: list[str]) -> None:
    """前瞻：本步没有断言就借「下一步要用的元素」；借不到再用自身控件兜底。"""
    for index, step in enumerate(steps):
        if step.checks:
            step.origin = "explicit"
            continue
        token = ""
        for nxt in steps[index + 1:]:
            token = target_token(nxt.target)
            if token:
                break
        if token:
            step.checks = [{"element_exists": token}]
            step.origin = "lookahead"
            continue
        own = target_token(step.target)
        if own:
            step.checks = [{"element_exists": own}]
            step.origin = "fallback"
            tail = ("末端的业务结果没有验证到，建议人工补结果断言（text_contains / element_value / "
                    "element_count）" if index == len(steps) - 1 else "建议人工补一条断言")
            notes.append(f"第 {index + 1} 步「{step.goal(index)}」借不到下一步的元素，"
                         f"用自身控件兜底：{tail}")
        else:
            step.origin = "none"
            notes.append(f"第 {index + 1} 步「{step.goal(index)}」找不到可写的断言来源："
                         f"请人工补断言（否则内核会报 checks_empty）")


def _collect_vars(steps: list[_Step], notes: list[str]) -> dict[str, str]:
    """输入的值进 vars（键取目标核心词）：值不进 goal、不进模型；像凭据的改写成 secret 引用。"""
    vars_map: dict[str, str] = {}
    flagged: set[str] = set()
    for index, step in enumerate(steps):
        if step.kind != "input" or not step.value:
            continue
        base = target_token(step.target) or f"输入{index + 1}"
        if _sensitive(base):
            vars_map[base] = "${{secret." + base + "}}"
            step.var_key = base
            if base not in flagged:
                flagged.add(base)
                notes.append(f"「{base}」像凭据：值没有写进用例，引用写成 "
                             f"${{{{secret.{base}}}}}——请到环境密钥里登记这个键")
            continue
        key, suffix = base, 2
        while key in vars_map and vars_map[key] != step.value:
            key, suffix = f"{base}{suffix}", suffix + 1
        vars_map[key] = step.value
        step.var_key = key
    return vars_map


def _review(steps: list[_Step], notes: list[str]) -> None:
    if not steps:
        return
    last = steps[-1]
    kinds = [kind for check in last.checks for kind in check]
    if kinds and not any(kind in RESULT_KINDS for kind in kinds):
        notes.append("最后一步没有能证明业务结果的断言（text_contains / element_value / "
                     "element_absent / element_count）：只证明控件还在，结果对不对没验证")
    if len(steps) > MAX_STEPS:
        notes.append(f"共 {len(steps)} 步，超过内核建议上限 {MAX_STEPS}：拆成多条用例更好定位问题")
    for index, step in enumerate(steps):
        if len(step.checks) > MAX_CHECKS_PER_STEP:
            notes.append(f"第 {index + 1} 步有 {len(step.checks)} 条断言，"
                         f"超过内核建议的 {MAX_CHECKS_PER_STEP} 条")
        for check in step.checks:
            for kind, value in check.items():
                if kind == "text_contains" and ("：" in value or ":" in value):
                    notes.append(f"第 {index + 1} 步断言「{value}」含冒号：页面文本按文本节点换行拼接，"
                                 f"标签与值分属两个节点时整串会匹配不到；"
                                 f"运行失败可改成冒号后那段重试")


# --- 产物 ---

def _step_doc(step: _Step, index: int, keep_url: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"goal": step.goal(index)}
    if step.kind == "goto" and step.url and keep_url:
        item["url"] = step.url
    if step.checks:
        item["checks"] = [_check_doc(check) for check in step.checks]
    return item


def _check_doc(check: dict[str, str]) -> dict[str, str]:
    (kind, value), = check.items()
    if "|" in value:    # 单键写法会把 | 当「标签|期望值」拆开，改用完整写法
        return {"kind": kind, "value": value, "expected": value}
    return {kind: value}


def _script_title(text: str) -> str:
    match = TEST_TITLE_RE.search(text or "")
    return match.group(2).strip() if match else ""


def _trim(text: str) -> str:
    return (text or "").strip().strip("\"'“”‘’「」『』（）()【】[]· \t")


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in SENSITIVE_WORDS)


def _segment(text: str, pos: int, window: int = 300) -> str:
    chunk = text[pos:pos + window]
    cut = chunk.find(";")
    return chunk[:cut] if cut >= 0 else chunk


def _consumed(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)
