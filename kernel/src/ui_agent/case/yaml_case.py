"""用例 YAML：校验编译（compile）与反向生成（draft）。

校验只信 YAML 本身，不信任何模型；行号来自 yaml.compose 的节点标记，
所以报错指得到「第几行」，而不是只丢一句结论。
引用约定：`${{secret.账号}}` / `${{env.样例目录}}` 只允许写在 vars 槽位，
值本身不进 goal、不进模型；键名是否登记由 --secret-keys 决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ui_agent.schema.check import KINDS, CheckSpec
from ui_agent.schema.plan import PlanStep, TestPlan

MAX_STEPS = 12                  # 与 planner 同一个上限：步骤太多既跑不动也读不懂
MAX_CHECKS_PER_STEP = 3
RESULT_KINDS = ("text_contains", "element_value", "element_absent")  # 能证明业务结果的断言
BAD_NAME_CHARS = '/\\:*?"<>|'
SENSITIVE_KEY_WORDS = ("密码", "口令", "password", "passwd", "secret", "token", "凭据")
REF_RE = re.compile(r"\$\{\{\s*(secret|env)\.([^}]*)\}\}")
# 只认「敏感词 + 像值的尾巴」：尾巴要含 ASCII 字母/数字，免得把「使用账号密码登录」这类说法当成字面量
GOAL_SECRET_RE = re.compile(r"(?:密码|口令|passwd|password)\s*[:：=是为]?\s*([^\s，。；、,;:：()（）【】\[\]\"']{3,})",
                            re.IGNORECASE)


def compile_case(text: str, secret_keys: list[str] | None = None) -> dict:
    """校验用例 YAML 并编译成 TestPlan；errors 非空时 plan 为 None。

    secret_keys=None 表示调用方不知道环境登记了哪些密钥（跳过该项校验）；
    给了列表（哪怕为空）就按「环境的已登记键名」严格比对。
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(line: int, code: str, message: str) -> None:
        errors.append({"line": int(line), "code": code, "message": message})

    def warn(line: int, code: str, message: str) -> None:
        warnings.append({"line": int(line), "code": code, "message": message})

    if not (text or "").strip():
        err(1, "empty_file", "YAML 内容为空")
        return _result(errors, warnings, None)
    try:
        node = yaml.compose(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        err(getattr(mark, "line", 0) + 1, "yaml_error", _flat(exc))
        return _result(errors, warnings, None)
    if node is None:
        err(1, "empty_file", "YAML 内容为空")
        return _result(errors, warnings, None)

    doc = _Doc()
    data = _value(node, (), doc)
    if not isinstance(data, dict):
        err(doc.lines.get((), 1), "bad_root", "YAML 顶层必须是键值对映射（至少要有一个 steps:）")
        return _result(errors, warnings, None)
    for path, line in doc.dups:
        err(line, "duplicate_key", f"重复的键：{path[-1]}（同名键只能出现一次，后写的那份会盖掉前一份）")
    plan = _build(data, doc, secret_keys, err, warn, errors) if not errors else None
    return _result(errors, warnings, plan)


def plan_to_case_yaml(plan: TestPlan) -> str:
    """TestPlan → 用例 YAML 文本。

    只写用例本身（标题 / 变量 / 步骤）：id、优先级、负责人、环境这些表头由平台管，
    入口地址也不写进来——同一份用例要能跑在不同环境下。
    """
    doc: dict[str, Any] = {}
    if plan.name and plan.name != "task":
        doc["title"] = plan.name
    if plan.notes:
        doc["notes"] = plan.notes
    if plan.vars:
        doc["vars"] = dict(plan.vars)
    steps: list[dict[str, Any]] = []
    for step in plan.steps:
        item: dict[str, Any] = {"goal": step.goal}
        if step.checks:
            item["checks"] = [_check_yaml(spec) for spec in step.checks]
        if step.note:
            item["note"] = step.note
        steps.append(item)
    doc["steps"] = steps
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False, width=200)


# --- 校验主体 ---

def _build(data: dict, doc: _Doc, secret_keys: list[str] | None, err, warn, errors: list[dict]) -> dict | None:
    title = _text(data.get("title")) or _text(data.get("name"))
    for key in ("id", "title", "name"):
        value = _text(data.get(key))
        if value and any(ch in value for ch in BAD_NAME_CHARS):
            err(doc.lines.get((key,), 1), "bad_name", f'{key} 不能包含这些字符：/ \\ : * ? " < > |')
    if _text(data.get("url")):
        warn(doc.lines.get(("url",), 1), "hardcoded_url",
             "用例里写了 url：运行时会覆盖环境地址；想让同一份用例跑在不同环境，入口应留给环境决定")

    vars_map = _vars(data.get("vars"), doc, secret_keys, err, warn)
    built = _steps(data.get("steps"), doc, err, warn)
    if errors:
        return None
    try:
        return TestPlan(name=title or "task", url=_text(data.get("url")), vars=vars_map,
                        notes=_text(data.get("notes")), steps=built).model_dump()
    except ValidationError as exc:
        err(doc.lines.get((), 1), "plan_invalid", f"编译出的计划不合法：{_flat(exc)}")
        return None


def _vars(raw: Any, doc: _Doc, secret_keys: list[str] | None, err, warn) -> dict[str, str]:
    out: dict[str, str] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        err(doc.lines.get(("vars",), 1), "bad_vars", "vars 必须是「键: 值」映射")
        return out
    registered = None if secret_keys is None else {str(item).strip() for item in secret_keys if str(item).strip()}
    for key, value in raw.items():
        line = doc.lines.get(("vars", str(key)), doc.lines.get(("vars",), 1))
        if value is not None and not isinstance(value, (str, int, float)):
            err(line, "bad_var", f"变量 {key} 的值必须是标量（字符串或数字）")
            continue
        text_value = _plain(value)
        for ref_kind, ref_name in REF_RE.findall(text_value):
            name = ref_name.strip()
            if ref_kind == "secret" and registered is not None and name not in registered:
                known = "、".join(sorted(registered)) or "（无）"
                err(line, "unknown_secret",
                    f"变量 {key} 引用了环境未登记的密钥：{name}（环境已登记：{known}）")
        if _literal_secret(str(key), text_value):
            warn(line, "literal_secret", "变量 " + str(key) + " 疑似明文写了敏感值：改用 ${{secret."
                 + str(key) + "}} 引用（值不进用例、不进模型）")
        out[str(key)] = text_value
    return out


def _steps(raw: Any, doc: _Doc, err, warn) -> list[PlanStep]:
    if not isinstance(raw, list) or not raw:
        err(doc.lines.get(("steps",), 1), "steps_empty", "steps 为空：至少要有一个步骤，否则没有可执行的东西")
        return []
    if len(raw) > MAX_STEPS:
        warn(doc.lines.get(("steps",), 1), "too_many_steps",
             f"步骤 {len(raw)} 个，超过建议上限 {MAX_STEPS}：拆成多个用例更好定位问题")
    built: list[PlanStep] = []
    for index, raw_step in enumerate(raw):
        line = doc.lines.get(("steps", index), 1)
        if not isinstance(raw_step, dict):
            err(line, "bad_step", f"第 {index + 1} 步必须是映射（至少要有 goal: 与 checks:）")
            continue
        goal = _text(raw_step.get("goal"))
        if not goal:
            err(doc.lines.get(("steps", index, "goal"), line), "goal_empty", f"第 {index + 1} 步缺少 goal")
        elif "${{secret." in goal or "${{env." in goal:
            err(doc.lines.get(("steps", index, "goal"), line), "ref_in_goal",
                f"第 {index + 1} 步 goal 里不能出现 " + "${{secret.…}} / ${{env.…}}"
                + " 引用：引用只允许写在 vars 槽位")
        elif _literal_in_goal(goal):
            warn(line, "literal_secret", f"第 {index + 1} 步疑似写了敏感值字面量：值应放进 vars 再用引用")
        checks = _checks(raw_step.get("checks"), index, line, doc, err, warn)
        if checks and index == len(raw) - 1 and not any(item.kind in RESULT_KINDS for item in checks):
            warn(line, "weak_final_check",
                 "最后一步没有能证明业务结果的断言（建议 text_contains / element_value）："
                 "只证明控件还在，结果对不对没验证")
        built.append(PlanStep(goal=goal or "（缺少 goal）", checks=checks,
                              url=_text(raw_step.get("url")), note=_text(raw_step.get("note"))))
    return built


def _checks(raw: Any, index: int, line: int, doc: _Doc, err, warn) -> list[CheckSpec]:
    if not isinstance(raw, list) or not raw:
        err(doc.lines.get(("steps", index, "checks"), line), "checks_empty",
            f"第 {index + 1} 步没有断言：没有断言 = 未验收")
        return []
    if len(raw) > MAX_CHECKS_PER_STEP:
        warn(line, "too_many_checks",
             f"第 {index + 1} 步有 {len(raw)} 条断言，超过建议的 {MAX_CHECKS_PER_STEP} 条")
    out: list[CheckSpec] = []
    for position, item in enumerate(raw):
        spec = _check_spec(item, doc.lines.get(("steps", index, "checks", position), line), err)
        if spec is not None:
            out.append(spec)
    return out


def _check_spec(raw: Any, line: int, err) -> CheckSpec | None:
    if isinstance(raw, dict) and raw:
        if "kind" in raw:
            spec = CheckSpec(kind=_text(raw.get("kind")), value=_text(raw.get("value")),
                             expected=_text(raw.get("expected")), role=_text(raw.get("role")))
        elif len(raw) == 1:
            kind, value = next(iter(raw.items()))
            target, _, expected = _plain(value).partition("|")
            spec = CheckSpec(kind=_text(kind), value=target.strip(), expected=expected.strip())
        else:
            err(line, "bad_check", "断言要么是「类型: 值」单键写法，要么是含 kind/value/expected 的完整写法")
            return None
    else:
        err(line, "bad_check", "断言要写成「类型: 值」，例如 text_contains: 票据列表")
        return None
    if spec.kind not in KINDS:
        err(line, "bad_kind", f"未知断言类型 {spec.kind or '（空）'}；可用：{'、'.join(KINDS)}")
        return None
    if not spec.value:
        spec.value = spec.expected
    if not spec.expected:
        spec.expected = spec.value
    if not spec.value:
        err(line, "empty_check", f"断言 {spec.kind} 没有给值")
        return None
    return spec


def _check_yaml(spec: CheckSpec) -> dict[str, str]:
    value, expected = spec.value or spec.expected, spec.expected or spec.value
    return {spec.kind: expected if value == expected else f"{value}|{expected}"}


def _literal_secret(key: str, value: str) -> bool:
    if not value or "${{" in value:
        return False
    lowered = key.lower()
    return any(word in lowered for word in SENSITIVE_KEY_WORDS)


def _literal_in_goal(goal: str) -> bool:
    if "${{" in goal:
        return False
    match = GOAL_SECRET_RE.search(goal)
    if match is None:
        return False
    tail = match.group(1)
    return not tail.startswith("（") and any(ch.isascii() and ch.isalnum() for ch in tail)


# --- 带行号的 YAML 读取 ---

@dataclass
class _Doc:
    lines: dict[tuple, int] = field(default_factory=dict)
    dups: list[tuple[tuple, int]] = field(default_factory=list)


def _value(node: Node, path: tuple, doc: _Doc) -> Any:
    doc.lines[path] = node.start_mark.line + 1
    if isinstance(node, MappingNode):
        out: dict[str, Any] = {}
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = _plain(_scalar(key_node))
            if key in seen:
                doc.dups.append(((*path, key), key_node.start_mark.line + 1))
            seen.add(key)
            out[key] = _value(value_node, (*path, key), doc)
        return out
    if isinstance(node, SequenceNode):
        return [_value(item, (*path, index), doc) for index, item in enumerate(node.value)]
    return _scalar(node)


def _scalar(node: Node) -> Any:
    if not isinstance(node, ScalarNode):
        return None
    tag = node.tag.rsplit(":", 1)[-1]
    value = node.value
    if tag == "null":
        return None
    if tag == "bool":
        return value.lower() in ("true", "yes", "on", "y")
    if tag == "int":
        try:
            return int(value)
        except ValueError:
            return value
    if tag == "float":
        try:
            return float(value)
        except ValueError:
            return value
    return value


# --- 小工具 ---

def _result(errors: list[dict], warnings: list[dict], plan: dict | None) -> dict:
    errors.sort(key=lambda item: (item["line"], item["code"]))
    warnings.sort(key=lambda item: (item["line"], item["code"]))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "plan": None if errors else plan}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _plain(value: Any) -> str:
    return "" if value is None else str(value)


def _flat(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"
