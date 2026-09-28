"""命令行入口：case（用例 YAML 校验/草稿）/ plan（编排计划）/ run（跑目标或计划）。

snapshot 只看不点，report 重出报告。

凡是带 `--json` 的子命令：人类话术走 stderr，stdout 只留最后一份 JSON，退出码
0=成功、1=用例失败（JSON 仍有效）、其他=内核错误——平台按这个契约回读。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ui_agent.case.yaml_case import compile_case, plan_to_case_yaml
from ui_agent.config import Settings
from ui_agent.decide.jev import to_jev_state
from ui_agent.driver.playwright_ import Driver
from ui_agent.llm.planner import make_plan, plan_from_json, plan_to_json
from ui_agent.observe.snapshot import build_state, page_summary
from ui_agent.report.html import build_report
from ui_agent.run.plan_runner import PlanResult, PlanRunner
from ui_agent.run.runner import Runner, RunResult
from ui_agent.verify.asserts import parse_check

MAX_GOAL_CHARS = 8000


def load_goal(args) -> str:
    if args.goal:
        text = args.goal
    else:
        text = Path(args.goal_file).read_text(encoding="utf-8")
    text = text.strip()
    if not text:
        raise RuntimeError("目标为空")
    return text[:MAX_GOAL_CHARS]


def parse_vars(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise RuntimeError(f"--var 需要 key=value 形式（键名不能为空）：{item}")
        out[key.strip()] = value
    return out


def apply_overrides(settings: Settings, args) -> Settings:
    overrides = {}
    if getattr(args, "mode", None):
        overrides["ui_agent_mode"] = args.mode
    if getattr(args, "max_actions", None):
        overrides["max_actions"] = args.max_actions
    if getattr(args, "conf_min", None):
        overrides["ui_agent_conf_min"] = args.conf_min
    if getattr(args, "no_supervisor", False):
        overrides["ui_agent_supervisor"] = False
    if getattr(args, "upload_mode", None):
        overrides["ui_agent_upload_mode"] = args.upload_mode
    if getattr(args, "dialog_policy", None):
        overrides["ui_agent_dialog_policy"] = args.dialog_policy
    return settings.model_copy(update=overrides) if overrides else settings


def cmd_snapshot(args) -> int:
    settings = apply_overrides(Settings(), args)
    driver = Driver(settings)
    try:
        driver.start()
        driver.goto(args.url)
        driver.wait_first_paint()
        state = build_state(driver.snapshot(), [], {}, settings.max_elements, settings.max_text)
        payload = to_jev_state(state) | {"fingerprint": state.fingerprint, "omitted": state.omitted}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        if args.json:
            _print_json({"ok": True, "summary": page_summary(state), "state": payload})
        elif args.out:
            print(f"已写入 {args.out}：元素 {len(state.elements)} 个，文本 {len(state.page.text)} 字")
        else:
            print(text)
    finally:
        driver.close()
    return 0


def cmd_case_compile(args) -> int:
    try:
        text = Path(args.in_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"读不到用例文件 {args.in_file}：{exc}") from exc
    secret_keys = None if args.secret_keys is None else [
        item.strip() for item in args.secret_keys.split(",") if item.strip()]
    result = compile_case(text, secret_keys)
    if args.json:
        _print_json(result)
    else:
        _print_lint(result)
    return 0 if result["ok"] else 1


def cmd_case_draft(args) -> int:
    settings = apply_overrides(Settings(), args)
    plan, meta = make_plan(settings, args.desc, args.url or "")
    text = plan_to_case_yaml(plan)
    lint = compile_case(text)
    if not lint["ok"]:
        detail = "；".join(f"第 {item['line']} 行 {item['message']}" for item in lint["errors"][:3])
        raise RuntimeError(f"planner 产出的草稿没通过校验：{detail}")
    payload = {"ok": True, "yaml": text, "warnings": lint["warnings"], "meta": meta,
               "plan": plan.model_dump()}
    if args.json:
        _print_json(payload)
    else:
        print(text)
        for item in lint["warnings"]:
            print(f"警告（第 {item['line']} 行）{item['message']}")
    return 0


def _print_lint(result: dict) -> None:
    for item in result["errors"]:
        print(f"第 {item['line']} 行 [{item['code']}] {item['message']}")
    for item in result["warnings"]:
        print(f"警告 第 {item['line']} 行 [{item['code']}] {item['message']}")
    if not result["ok"]:
        print(f"校验未通过：{len(result['errors'])} 个错误、{len(result['warnings'])} 条警告")
        return
    plan = result["plan"] or {}
    tail = f"，{len(result['warnings'])} 条警告" if result["warnings"] else ""
    print(f"校验通过：{len(plan.get('steps') or [])} 步{tail}")


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def cmd_plan(args) -> int:
    settings = apply_overrides(Settings(), args)
    text = Path(args.desc_file).read_text(encoding="utf-8") if args.desc_file else args.desc
    if not (text or "").strip():
        raise RuntimeError("流程描述为空")
    plan, meta = make_plan(settings, text, args.url or "")
    out = Path(args.out) if args.out else settings.runs_dir / "plans" / f"{plan.slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan_to_json(plan), encoding="utf-8")
    print(f"计划「{plan.name}」共 {len(plan.steps)} 步 → {out}")
    for index, step in enumerate(plan.steps, start=1):
        marks = "、".join(spec.kind for spec in step.checks) or "无断言"
        print(f"  {index:02d}. {step.goal}（{marks}）")
    print(f"模型 {meta.get('model')} · {meta.get('latency_ms')}ms")
    return 0


def cmd_run(args) -> int:
    settings = apply_overrides(Settings(), args)
    if args.plan_file:
        return _run_plan(args, settings)
    return _run_goal(args, settings)


def _run_goal(args, settings: Settings) -> int:
    checks = [parse_check(item) for item in args.check]
    runner = Runner(settings, load_goal(args), args.url, checks=checks, task=args.task,
                    vars=parse_vars(args.var), run_dir=_run_dir(args), json_mode=args.json)
    result = runner.run()
    if args.json:
        _print_json(_goal_payload(result))
        return 0 if result.success else 1
    print("")
    print(f"状态：{result.label}")
    if result.detail:
        print(f"说明：{result.detail}")
    if result.status == "replanned":
        print(f"监督模型要求重排为 {len(result.replan or [])} 步；单目标模式不换计划，"
              f"请用 ui-agent plan 生成计划后重跑（步骤已写进 result.json 的 replan）")
    print(f"决策步数：{result.steps}")
    for item in result.checks:
        print(f"  {item.label}")
    _tail(result.run_dir, result.report, result.defect_candidates, result.zoom_note)
    return 0 if result.success else 1


def _run_plan(args, settings: Settings) -> int:
    plan = plan_from_json(Path(args.plan_file).read_text(encoding="utf-8"))
    if args.url:
        plan.url = args.url
    if not plan.url:
        raise RuntimeError("计划里没有 url，请用 --url 指定入口")
    runner = PlanRunner(settings, plan, vars=parse_vars(args.var), run_dir=_run_dir(args),
                        json_mode=args.json)
    result = runner.run()
    if args.json:
        _print_json(_plan_payload(result))
        return 0 if result.success else 1
    print("")
    print(f"状态：{result.label}")
    if result.detail:
        print(f"说明：{result.detail}")
    for step in result.steps:
        mark = "✓" if step.success else "✗"
        print(f"  {step.index:02d}. {mark} {step.label} — {step.detail}")
    _tail(result.run_dir, result.report, result.defect_candidates, result.zoom_note)
    return 0 if result.success else 1


def _run_dir(args) -> Path | None:
    return Path(args.run_dir) if getattr(args, "run_dir", None) else None


def _goal_payload(result: RunResult) -> dict:
    return {
        "ok": result.success,
        "status": result.status,
        "status_label": result.label,
        "detail": result.detail,
        "run_dir": str(result.run_dir),
        "report": str(result.report or ""),
        "steps": result.steps,
        "checks": [item.model_dump() for item in result.checks],
        "replan": [item.model_dump() for item in result.replan or []],
        "defect_candidates": result.defect_candidates,
        "zoom_note": result.zoom_note,
    }


def _plan_payload(result: PlanResult) -> dict:
    return {
        "ok": result.success,
        "status": result.status,
        "status_label": result.label,
        "detail": result.detail,
        "run_dir": str(result.run_dir),
        "report": str(result.report or ""),
        "steps": [{"index": step.index, "attempt": step.attempt, "goal": step.goal,
                   "status": step.status, "status_label": step.label, "detail": step.detail,
                   "decisions": step.decisions} for step in result.steps],
        "checks": [{"step": step.index, **item.model_dump()}
                   for step in result.steps for item in step.checks],
        "defect_candidates": result.defect_candidates,
        "zoom_note": result.zoom_note,
    }


def _tail(run_dir: Path, report: Path | None, defects: list[str], zoom: str) -> None:
    if defects:
        print("可用性缺陷候选（降级路径）：")
        for item in defects:
            print(f"  ⚠ {item}")
    if zoom:
        print(f"注意：{zoom}")
    print(f"产物目录：{run_dir}")
    if report:
        print(f"报告：{report}")


def cmd_report(args) -> int:
    out = build_report(Path(args.run_dir), Path(args.out) if args.out else None)
    if args.json:
        _print_json({"ok": True, "report": str(out)})
    else:
        print(f"报告已生成：{out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ui-agent",
        description="Jev 决策驱动的 UI 自动化：代码与模型都读 DOM，截图只进报告不进模型",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行一个自然语言目标或一份计划文件")
    goal = run.add_mutually_exclusive_group()
    goal.add_argument("--goal", help="自然语言目标（整段文本）")
    goal.add_argument("--goal-file", help="目标文本文件（UTF-8）")
    goal.add_argument("--plan-file", help="plan.json（由 ui-agent plan 生成；此时不需要 --goal）")
    run.add_argument("--url", help="起始地址；目标模式必填，计划模式可覆盖计划里的入口")
    run.add_argument("--check", action="append", default=[],
                     help="验收断言，可重复。写法：kind=值，或 element_value=标签|期望值，或 JSON")
    run.add_argument("--task", default="task", help="任务名（目标模式用于产物目录）")
    run.add_argument("--var", action="append", default=[], metavar="键=值",
                     help="字段值或上传文件路径（键名要出现在控件标签里，多文件用 ; 分隔）；"
                          "优先于文本模型，值不落盘、不进模型。可重复")
    run.add_argument("--mode", choices=["debug", "ci"], help="覆盖 UI_AGENT_MODE")
    run.add_argument("--upload-mode", choices=["input", "drop"],
                     help="上传方式：input=给隐藏 input 注入文件（默认）；drop=页内合成拖拽事件")
    run.add_argument("--dialog-policy", choices=["dismiss", "accept"],
                     help="原生对话框应答方式：dismiss=关掉（默认，不改变页面状态）；"
                          "accept=点确定；出现 confirm/prompt 一律记缺陷候选")
    run.add_argument("--max-actions", type=int, help="覆盖 MAX_ACTIONS")
    run.add_argument("--conf-min", type=float, help="覆盖 UI_AGENT_CONF_MIN")
    run.add_argument("--no-supervisor", action="store_true",
                     help="卡住时不咨询监督模型，直接按 M0 语义停机")
    run.add_argument("--run-dir", help="指定产物目录（默认 .artifacts/runs/<任务>-<时间戳>）")
    run.add_argument("--json", action="store_true",
                     help="stdout 只输出一份 JSON（人类话术走 stderr），供平台回读")
    run.set_defaults(func=cmd_run)

    case = sub.add_parser("case", help="用例 YAML：校验编译（不联网）或自然语言生成草稿")
    case_sub = case.add_subparsers(dest="case_command", required=True)
    compile_parser = case_sub.add_parser("compile", help="校验用例 YAML 并编译成执行计划")
    compile_parser.add_argument("--in", dest="in_file", required=True, metavar="用例.yaml",
                                help="用例 YAML 文件路径")
    compile_parser.add_argument("--secret-keys", help="环境已登记的密钥键名（逗号分隔）；"
                                                      "给了就校验 vars 里的 ${{secret.X}} 引用是否登记过")
    compile_parser.add_argument("--json", action="store_true",
                                help="stdout 只输出一份 JSON（errors/warnings/plan）")
    compile_parser.set_defaults(func=cmd_case_compile)
    draft_parser = case_sub.add_parser("draft", help="自然语言流程 → 用例 YAML 草稿（会调用文本模型）")
    draft_parser.add_argument("--desc", required=True, help="流程描述（整段文本）")
    draft_parser.add_argument("--url", help="起始地址，供编排参考")
    draft_parser.add_argument("--json", action="store_true", help="stdout 只输出一份 JSON（yaml/warnings/meta）")
    draft_parser.set_defaults(func=cmd_case_draft)

    plan = sub.add_parser("plan", help="把一段自然语言流程编排成分步计划（会调用文本模型）")
    desc = plan.add_mutually_exclusive_group(required=True)
    desc.add_argument("--desc", help="流程描述（整段文本）")
    desc.add_argument("--desc-file", help="流程描述文件（UTF-8）")
    plan.add_argument("--url", help="起始地址，写进计划")
    plan.add_argument("--out", help="计划输出路径，默认 .artifacts/runs/plans/<名字>.json")
    plan.set_defaults(func=cmd_plan)

    snap = sub.add_parser("snapshot", help="只快照不动作：离线预览喂给 Jev 的 state")
    snap.add_argument("--url", required=True)
    snap.add_argument("--out", help="写入文件，默认打印到标准输出")
    snap.add_argument("--mode", choices=["debug", "ci"], default="ci")
    snap.add_argument("--json", action="store_true",
                      help="stdout 只输出一份 JSON（summary + state），供环境探针回读")
    snap.set_defaults(func=cmd_snapshot)

    rep = sub.add_parser("report", help="按运行目录重出 HTML 报告")
    rep.add_argument("--run-dir", required=True, help="运行目录（含 result.json 或 summary.json）")
    rep.add_argument("--out", help="报告输出路径，默认写进运行目录的 report.html")
    rep.add_argument("--json", action="store_true", help="stdout 只输出一份 JSON（报告路径）")
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if args.command == "run" and not args.plan_file:
        if not args.goal and not args.goal_file:
            parser.error("需要 --goal、--goal-file 或 --plan-file 之一")
        if not args.url:
            parser.error("目标模式需要 --url")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
