"""内核桩：本地开发/冒烟用，按 spec §4.2/§6.2 的契约返回，零浏览器、零模型调用。

真实实现在 `ui_agent`（D:\\test_skills\\ui_agent）；本文件只是让平台后端能独立跑通，
JSON 形状必须与下表一致：

| 命令 | 返回 |
|---|---|
| `case compile --in <yaml> --json` | `{"ok","errors":[…],"warnings":[…],"plan":{…}}`（可带 `--secret-keys a,b`） |
| `case draft --desc <text> --url <url> --json` | `{"ok","yaml":"…","warnings":[…],"meta":{…}}` |
| `snapshot --url <url> --json` | `{"ok","summary":{final_url,title,element_count,needs_login,has_password_field}}` |
| `run --plan-file <p> --run-dir <d> --mode debug\|ci --json [--var k=v]` | 落产物 + `{"ok","status","run_dir"}` |
| `report --run-dir <d> --json` | `{"ok","report"}` |
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

KINDS = ("text_contains", "url_contains", "title_contains",
         "element_exists", "element_absent", "element_value")
BAD_NAME_CHARS = set('/\\:*?"<>|')
SLOW_MS = int(os.environ.get("FAKE_KERNEL_STEP_MS", "120"))


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def find_line(text: str, needle: str) -> int:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle and needle in line:
            return index
    return 1


def check_spec(kind: str, value: str) -> dict:
    role = ""
    expected = value
    if kind == "element_value" and "|" in value:
        role, _, expected = value.partition("|")
    return {"kind": kind, "value": value if kind != "element_value" else role,
            "expected": expected, "role": role}


def cmd_compile(args) -> int:
    text = Path(args.path).read_text(encoding="utf-8")
    errors: list[dict] = []
    warnings: list[dict] = []
    plan = {"name": "task", "url": "", "steps": [], "vars": {}, "notes": ""}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark else 1
        errors.append({"line": line, "code": "yaml_error", "message": f"YAML 解析失败：{exc}"})
        emit({"ok": False, "errors": errors, "warnings": warnings, "plan": None})
        return 0
    if not isinstance(data, dict):
        errors.append({"line": 1, "code": "bad_root", "message": "用例顶层必须是字典"})
        emit({"ok": False, "errors": errors, "warnings": warnings, "plan": None})
        return 0

    title = str(data.get("title") or "")
    if any(ch in BAD_NAME_CHARS for ch in title):
        errors.append({"line": find_line(text, "title:"), "code": "bad_name",
                       "message": "名称不能包含 /\\:*?\"<>|"})

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append({"line": find_line(text, "steps:"), "code": "steps_empty",
                       "message": "steps 不能为空：至少要有一个步骤"})
        steps = []

    secret_keys = [key.strip() for key in (args.secret_keys or "").split(",") if key.strip()]
    vars_map = data.get("vars") or {}
    if not isinstance(vars_map, dict):
        errors.append({"line": find_line(text, "vars:"), "code": "bad_vars", "message": "vars 必须是键值对"})
        vars_map = {}
    for key, value in vars_map.items():
        for ref in _refs(str(value)):
            if ref.startswith("secret.") and secret_keys and ref.split(".", 1)[1] not in secret_keys:
                errors.append({"line": find_line(text, str(value)), "code": "unknown_secret",
                               "message": f"vars.{key} 引用了环境未登记的密钥 {ref}"})

    checked_steps = []
    for number, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            errors.append({"line": find_line(text, "- goal:"), "code": "bad_step",
                           "message": f"第 {number} 步必须是字典"})
            continue
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            errors.append({"line": find_line(text, "- goal:"), "code": "goal_empty",
                           "message": f"第 {number} 步缺少 goal"})
        if "${{secret." in goal or "${{env." in goal:
            tip = "${" + "{secret.键名}} / ${" + "{env.键名}}"
            errors.append({"line": find_line(text, goal[:12]), "code": "ref_in_goal",
                           "message": f"第 {number} 步的 goal 里不能出现 {tip} 引用（只允许写在 vars 槽位）"})
        checks = raw.get("checks")
        if not isinstance(checks, list) or not checks:
            errors.append({"line": find_line(text, "- goal:"), "code": "checks_empty",
                           "message": f"第 {number} 步没有断言（没有断言 = 未验收）"})
            checks = []
        specs = []
        for item in checks:
            if not isinstance(item, dict) or len(item) != 1:
                errors.append({"line": find_line(text, str(item)[:12]), "code": "bad_check",
                               "message": f"第 {number} 步的断言写法应为 `kind: 值` 或 `kind: 标签|期望值`"})
                continue
            kind, value = next(iter(item.items()))
            if kind not in KINDS:
                errors.append({"line": find_line(text, str(kind)), "code": "bad_kind",
                               "message": f"断言类型非法：{kind}（可用：{'/'.join(KINDS)}）"})
                continue
            specs.append(check_spec(str(kind), str(value)))
        if len(specs) > 3:
            warnings.append({"line": find_line(text, "- goal:"), "code": "too_many_checks",
                             "message": f"第 {number} 步断言多于 3 条，容易互相耦合"})
        checked_steps.append({"goal": goal, "checks": specs, "url": str(raw.get("url") or ""),
                              "note": str(raw.get("note") or "")})

    if len(checked_steps) > 12:
        warnings.append({"line": find_line(text, "steps:"), "code": "too_many_steps",
                         "message": "步骤多于 12，建议拆成多条用例"})
    if checked_steps and all(spec["kind"] == "element_exists" for spec in checked_steps[-1]["checks"]):
        warnings.append({"line": find_line(text, "- goal:"), "code": "weak_final_check",
                         "message": "最后一步只有 element_exists，建议加一条能证明业务结果的 text_contains"})
    for key in ("密码", "口令", "pwd", "password"):
        if key in text and "${{secret." not in text:
            warnings.append({"line": find_line(text, key), "code": "literal_secret",
                             "message": f"疑似把敏感值写成字面量（出现「{key}」），请改用 ${{{{secret.键名}}}}"})
            break

    plan = {"name": title or "task", "url": str(data.get("url") or ""),
            "steps": checked_steps, "vars": {str(k): str(v) for k, v in vars_map.items()},
            "notes": str(data.get("note") or data.get("notes") or "")}
    emit({"ok": not errors, "errors": errors, "warnings": warnings, "plan": plan})
    return 0


def _refs(text: str) -> list[str]:
    out, index = [], 0
    while True:
        start = text.find("${{", index)
        if start < 0:
            return out
        end = text.find("}}", start)
        if end < 0:
            return out
        out.append(text[start + 3:end].strip())
        index = end + 2


def cmd_draft(args) -> int:
    lines = [line.strip() for line in (args.desc or "").splitlines() if line.strip()]
    title = (lines[0] if lines else "新用例")[:40]
    steps = [
        {"goal": f"打开登录页并用 账号 / 密码 登录（{title}）",
         "checks": [{"element_exists": "查询"}]},
        {"goal": "把查询条件设置成用例要求的值并提交查询",
         "checks": [{"text_contains": "列表"}]},
        {"goal": "完成用例描述里的关键操作并确认结果",
         "checks": [{"text_contains": "成功"}]},
    ]
    yaml_text = (
        f'id: "sc-0000-00"\n'
        f'title: {json.dumps(title, ensure_ascii=False)}\n'
        'priority: "P2"\n'
        "tags: []\n"
        'owner: ""\n'
        'env: ""\n'
        "\nvars:\n"
        '  账号: "${{secret.账号}}"\n'
        '  密码: "${{secret.密码}}"\n'
        "\nsteps:\n"
        + "".join(
            f"  - goal: {json.dumps(step['goal'], ensure_ascii=False)}\n"
            f"    checks:\n"
            + "".join(f"      - {kind}: {json.dumps(value, ensure_ascii=False)}\n"
                      for item in step["checks"] for kind, value in item.items())
            for step in steps)
    )
    emit({"ok": True, "yaml": yaml_text,
          "warnings": [{"line": 0, "code": "draft_review",
                        "message": "模型生成，请检查每步断言与敏感值后再保存"}],
          "meta": {"model": "fake-kernel", "latency_ms": 12}})
    return 0


def cmd_snapshot(args) -> int:
    url = args.url
    needs_login = any(word in url.lower() for word in ("login", "signin", "login.htm"))
    emit({"ok": True, "summary": {
        "final_url": url,
        "title": "用户登录" if needs_login else "首页",
        "element_count": 42 if needs_login else 87,
        "needs_login": needs_login,
        "has_password_field": needs_login,
    }, "at": datetime.now().isoformat(timespec="seconds")})
    return 0


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def cmd_run(args) -> int:
    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    run_dir = Path(args.run_dir)
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    steps = plan.get("steps") or []
    url = plan.get("url") or args.url or "http://example.local/"
    failed = any("失败" in str(step.get("goal") or "") for step in steps)
    started = time.time()
    outcomes = []
    with (run_dir / "trace.jsonl").open("w", encoding="utf-8") as trace:
        for index, step in enumerate(steps, start=1):
            goal = str(step.get("goal") or "")
            step_failed = failed and index == len(steps)
            for operation, target in (("WAIT", "等待页面渲染完成（就绪门）"), ("CLICK", "查询")):
                trace.write(json.dumps({
                    "step": index, "time": datetime.now().isoformat(timespec="seconds"),
                    "url": url, "title": "首页", "operation": operation, "target": target,
                    "target_element": f"{target}(idx={index * 7})", "confidence": 0.93,
                    "model": "fake-kernel", "latency_ms": 210,
                    "action": {"ok": True, "changed": True, "level": "L0"},
                    "page_facts": [], "signal": None, "supervisor": None,
                }, ensure_ascii=False) + "\n")
                trace.flush()
                time.sleep(SLOW_MS / 1000)
            shots = run_dir / "screenshots" / f"{index:02d}-{operation.lower()}.png"
            shots.write_bytes(PNG_1PX)
            (run_dir / "snapshots" / f"{index:03d}.json").write_text(
                json.dumps({"step": index, "elements": 12}, ensure_ascii=False), encoding="utf-8")
            checks = [{"kind": spec.get("kind"), "expected": spec.get("expected"),
                       "ok": not step_failed, "evidence": "桩：命中" if not step_failed else "桩：未命中"}
                      for spec in (step.get("checks") or [])]
            outcomes.append({"index": index, "goal": goal,
                             "status": "check_failed" if step_failed else "ok",
                             "status_label": "失败：断言未通过" if step_failed else "成功：全部断言通过",
                             "detail": "桩：断言未通过" if step_failed else "桩：断言通过",
                             "decisions": 2, "checks": checks, "defects": [], "attempt": 1})
    status = "check_failed" if failed else "ok"
    summary = {
        "kind": "plan", "name": plan.get("name"), "url": url, "status": status,
        "status_label": "失败：断言未通过" if failed else "成功：全部断言通过",
        "detail": "桩运行：最后一步断言未通过" if failed else "桩运行：全部断言通过",
        "mode": args.mode, "started": datetime.now().isoformat(timespec="seconds"),
        "ended": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1), "steps": outcomes,
        "vars": [], "defect_candidates": [], "replan": [],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "actions.json").write_text("[]", encoding="utf-8")
    (run_dir / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{plan.get('name')}</title></head><body>"
        f"<h1>运行报告（内核桩）</h1><p>状态：{summary['status_label']}</p></body></html>",
        encoding="utf-8")
    emit({"ok": status == "ok", "status": status, "run_dir": str(run_dir), "steps": len(outcomes),
          "report": str(run_dir / "report.html")})
    return 0 if status == "ok" else 1


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    report = run_dir / "report.html"
    if not report.exists():
        report.write_text("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                          "<title>运行报告（内核桩）</title></head><body>"
                          "<h1>运行报告（内核桩）</h1></body></html>", encoding="utf-8")
    emit({"ok": True, "report": str(report)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-kernel", description="ui_agent 内核桩（本地开发用）")
    sub = parser.add_subparsers(dest="command", required=True)

    compile_parser = sub.add_parser("case")
    case_sub = compile_parser.add_subparsers(dest="case_command", required=True)
    compile_cmd = case_sub.add_parser("compile")
    compile_cmd.add_argument("--in", dest="path", required=True)
    compile_cmd.add_argument("--json", action="store_true")
    compile_cmd.add_argument("--secret-keys", default="")
    compile_cmd.set_defaults(func=cmd_compile)
    draft_cmd = case_sub.add_parser("draft")
    draft_cmd.add_argument("--desc", required=True)
    draft_cmd.add_argument("--url", default="")
    draft_cmd.add_argument("--json", action="store_true")
    draft_cmd.set_defaults(func=cmd_draft)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--url", required=True)
    snap.add_argument("--json", action="store_true")
    snap.set_defaults(func=cmd_snapshot)

    run = sub.add_parser("run")
    run.add_argument("--plan-file", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument("--mode", choices=["debug", "ci"], default="ci")
    run.add_argument("--url", default="")
    run.add_argument("--var", action="append", default=[])
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    rep = sub.add_parser("report")
    rep.add_argument("--run-dir", required=True)
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
