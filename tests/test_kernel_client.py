"""内核调用层：argv 组装、JSON 回读、错误分支（走真实内核桩，不起浏览器）。"""

from __future__ import annotations

import pytest

from app.services import kernel_client
from app.services.kernel_client import KernelError


def test_argv_uses_kernel_cmd_and_drops_quotes():
    argv = kernel_client.build_argv(["snapshot", "--url", "http://x/"])
    assert argv[-3:] == ["snapshot", "--url", "http://x/"]
    assert argv[0].endswith("python.exe") or "python" in argv[0]


def test_parse_json_full_body():
    assert kernel_client.parse_json('{"ok": true}', ["x"]) == {"ok": True}


def test_parse_json_takes_last_json_line():
    text = "跑起来了\n一些中文日志\n{\"ok\": false, \"errors\": []}\n"
    assert kernel_client.parse_json(text, ["x"])["ok"] is False


def test_parse_json_rejects_garbage():
    with pytest.raises(KernelError) as exc:
        kernel_client.parse_json("完全不是 JSON", ["x"])
    assert exc.value.code == "kernel_bad_json"


def test_parse_json_rejects_empty():
    with pytest.raises(KernelError) as exc:
        kernel_client.parse_json("   ", ["x"])
    assert exc.value.code == "kernel_empty_output"


def test_call_json_raises_on_bad_exit_code():
    with pytest.raises(KernelError) as exc:
        kernel_client.call_json(["nope"])
    assert exc.value.code == "kernel_failed"


def test_snapshot_summary_shape():
    data = kernel_client.call_json(["snapshot", "--url", "http://192.0.2.10/login", "--json"])
    assert data["ok"] is True
    assert data["summary"]["needs_login"] is True
    assert data["summary"]["has_password_field"] is True


def test_run_process_writes_artifacts(tmp_path):
    plan = {"name": "冒烟", "url": "http://192.0.2.10/", "steps": [
        {"goal": "打开登录页", "checks": [{"kind": "element_exists", "value": "查询", "expected": "查询"}]},
    ], "vars": {}}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    run_dir = tmp_path / "run"
    proc = kernel_client.KernelProcess(
        ["run", "--plan-file", str(plan_path), "--run-dir", str(run_dir), "--mode", "debug", "--json"],
        run_dir / "kernel.log")
    try:
        assert proc.wait(60) == 0
        data = proc.result()
    finally:
        proc.close()
    assert data["status"] == "ok" and data["steps"] == 1
    assert (run_dir / "trace.jsonl").exists() and (run_dir / "summary.json").exists()
