"""报告生成的离线单测：计划模式/单目标模式、相对截图链接、转义与缺件报错。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ui_agent.report.html import build_report


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_trace(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def trace_row(step: int, operation: str, **over) -> dict:
    row = {"step": step, "operation": operation, "target": "1", "target_element": 1, "confidence": 0.8,
           "action": {"ok": True, "level": "L0", "detail": "已点击"}}
    row.update(over)
    return row


def plan_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "plan-1"
    write_json(run_dir / "summary.json", {
        "kind": "plan", "name": "登录并查询", "url": "http://x/", "status": "check_failed",
        "status_label": "失败：断言未通过", "detail": "第 2 步未通过（失败：断言未通过）：断言未通过",
        "mode": "ci", "viewport": "1920x1080", "duration_s": 12.3,
        "decision_model": "jev-latest", "text_model": "deepseek-chat", "vars": ["账号"],
        "notes": "先登录再查询", "zoom_note": "viewport 与配置不一致",
        "defect_candidates": ["第1步 决策#2 CLICK: L4 降级：依赖强制/合成事件"],
        "started": "2026-09-23T10:00:00",
        "steps": [
            {"index": 1, "goal": "登录", "dir": "01-登录", "status": "ok",
             "status_label": "成功：全部断言通过", "detail": "全部断言通过", "decisions": 2,
             "checks": [], "defects": []},
            {"index": 2, "goal": "查询", "dir": "02-查询", "status": "check_failed",
             "status_label": "失败：断言未通过", "detail": "断言未通过：text_contains『票据类型』",
             "decisions": 1, "checks": [], "defects": []},
        ],
    })
    step1 = run_dir / "steps" / "01-登录"
    write_json(step1 / "result.json", {"status": "ok", "detail": "全部断言通过", "checks": [
        {"kind": "text_contains", "expected": "登录完成", "ok": True, "evidence": "…登录完成…"}]})
    write_trace(step1 / "trace.jsonl", [
        trace_row(0, "TYPE_TEXT", text_model={"source": "var", "key": "账号", "model": "-"}),
        trace_row(1, "DONE"),
    ])
    write_json(step1 / "actions.json", [{"action": "TYPE_TEXT", "kind": "1", "text": "sysadmin",
                                         "level": "T0", "page_changed": True, "step": 0}])
    (step1 / "screenshots").mkdir(parents=True)
    (step1 / "screenshots" / "00-type_text.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    step2 = run_dir / "steps" / "02-查询"
    write_json(step2 / "result.json", {"status": "check_failed", "detail": "断言未通过", "checks": [
        {"kind": "text_contains", "expected": "票据类型", "ok": False, "evidence": "…未找到…"}]})
    write_trace(step2 / "trace.jsonl", [trace_row(0, "CLICK", signal={"kind": "repeat_no_progress",
                                                                     "detail": "重复且无进展"})])
    return run_dir


def test_plan_report_renders_steps_checks_and_relative_shots(tmp_path):
    run_dir = plan_dir(tmp_path)
    report = build_report(run_dir)
    text = report.read_text(encoding="utf-8")

    assert report == run_dir / "report.html"
    assert "计划「登录并查询」" in text and "第 1 步：登录" in text
    assert "成功：全部断言通过" in text and "失败：断言未通过" in text
    assert "text_contains" in text and "登录完成" in text
    assert 'src="steps/01-登录/screenshots/00-type_text.png"' in text
    assert "文本值来源：var 账号" in text
    assert "↻" in text and "repeat_no_progress" in text


def test_plan_report_shows_defects_vars_and_zoom_note(tmp_path):
    text = build_report(plan_dir(tmp_path)).read_text(encoding="utf-8")
    assert "可用性缺陷候选" in text and "L4 降级" in text
    assert "变量键" in text and "账号" in text
    assert "缩放提示：viewport 与配置不一致" in text
    assert "先登录再查询" in text


def test_single_run_report(tmp_path):
    run_dir = tmp_path / "single-1"
    write_json(run_dir / "result.json", {"status": "blocked", "detail": "决策模型判定无法推进",
                                         "checks": [], "defect_candidates": [], "url": "http://x/"})
    write_trace(run_dir / "trace.jsonl", [trace_row(0, "BLOCKED", action=None)])
    (run_dir / "screenshots").mkdir()
    (run_dir / "screenshots" / "failure.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    text = build_report(run_dir).read_text(encoding="utf-8")
    assert "单目标运行" in text and "阻塞：决策模型认为无法推进" in text
    assert 'src="screenshots/failure.png"' in text and "BLOCKED" in text


def test_report_without_artifacts_raises(tmp_path):
    with pytest.raises(RuntimeError, match="summary.json"):
        build_report(tmp_path / "empty")


def test_report_escapes_page_and_goal_text(tmp_path):
    run_dir = plan_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    summary["steps"][0]["goal"] = "<script>alert(1)</script>"
    summary["name"] = "a<b>&c"
    write_json(run_dir / "summary.json", summary)

    text = build_report(run_dir).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "a&lt;b&gt;&amp;c" in text


def test_report_tolerates_missing_actions_file(tmp_path):
    run_dir = tmp_path / "no-actions"
    write_json(run_dir / "result.json", {"status": "ok", "detail": "全部断言通过"})
    write_trace(run_dir / "trace.jsonl", [trace_row(0, "CLICK")])
    text = build_report(run_dir).read_text(encoding="utf-8")
    assert "CLICK" in text and "已点击" in text


def test_trace_renders_supervisor_rulings_and_unavailable(tmp_path):
    run_dir = tmp_path / "sup-1"
    write_json(run_dir / "result.json", {"status": "aborted", "detail": "监督模型中止：入口 404"})
    write_trace(run_dir / "trace.jsonl", [
        trace_row(0, "BLOCKED", action=None, supervisor={"trigger": "blocked", "ruling": "HINT",
                                                         "label": "HINT：先关弹窗", "meta": {"model": "m"}}),
        trace_row(1, "CLICK", supervisor={"trigger": "budget_actions", "ruling": None,
                                          "label": "监督预算已用完（本步 2/2）",
                                          "error": "监督预算已用完（本步 2/2）"}),
    ])
    text = build_report(run_dir).read_text(encoding="utf-8")
    assert "中止：监督模型判定无法继续" in text
    assert "监督 HINT" in text and "先关弹窗" in text
    assert "监督不可用" in text and "本步 2/2" in text


def test_plan_report_shows_replans_and_retry_attempts(tmp_path):
    run_dir = plan_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    old = summary["steps"][0]
    summary["steps"][0] = {**old, "attempt": 1, "status": "replanned",
                           "status_label": "待重排：监督模型给出了新的步骤序列",
                           "detail": "监督模型重排：新序列 2 步", "decisions": 1}
    summary["steps"].insert(1, {**old, "attempt": 2, "dir": "01-登录-r2", "detail": "全部断言通过"})
    summary["replans"] = [{"index": 1, "attempt": 1,
                           "steps": [{"goal": "重进首页", "checks": []}, {"goal": "再查询", "checks": []}]}]
    write_json(run_dir / "summary.json", summary)

    text = build_report(run_dir).read_text(encoding="utf-8")
    assert "监督重排" in text and "重进首页" in text and "再查询" in text
    assert "（第 2 次）" in text and "待重排：监督模型给出了新的步骤序列" in text
