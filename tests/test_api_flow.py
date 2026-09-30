"""接口主流程：TestClient 直连 ASGI，内核走桩（零模型、零浏览器、零网络）。

顺序就是主流程顺序：登录 → 树 → 环境与密钥 → 用例校验/保存 → 运行 → 产物/SSE → 台账/版本 → 删除。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog, PageInventory, Run, utcnow
from app.services import run_service, secret_store

API = "/api/v1"
SECRET_ACCOUNT = "ut-account-2468"
SECRET_PASSWORD = "ut-password-1357"

CASE_YAML = """id: sc-0001-01
title: 83票
priority: P1
tags: [冒烟, 回归]
env: 192.0.2.10

vars:
  账号: ${{secret.账号}}
  上传文件: ${{env.样例目录}}/83票.pdf

steps:
  - goal: 打开系统登录页，使用 账号 登录，进入系统主页面
    checks:
      - element_exists: 查询
  - goal: 在查询条件中把行政区域选择为 2102 大连
    checks:
      - element_value: 行政区域|2102 大连
  - goal: 点击查询，等待票据列表加载完成
    checks:
      - text_contains: 票据列表
      - element_absent: 暂无数据
  - goal: 在上传区上传一个 PDF 发票文件，等它进入待提交列表
    checks:
      - text_contains: 83票.pdf
  - goal: 点击提交，等待提交结果
    checks:
      - text_contains: 提交成功
"""

BAD_YAML = """id: sc-0001-01
title: 坏用例
vars:
  密码: ${{secret.密码}}
steps:
  - goal: 直接把引用代入 ${{secret.密码}} 试试
  - goal: 这一步没有断言
"""

MISSING_YAML = """id: sc-0001-01
title: 引用未配置密钥
env: 未配齐密钥
vars:
  验证码: ${{secret.验证码}}
steps:
  - goal: 用 验证码 登录系统
    checks:
      - element_exists: 查询
"""

UNREGISTERED_YAML = """id: sc-0001-01
title: 引用未登记密钥
env: 192.0.2.10
vars:
  口令: ${{secret.没登记的键}}
steps:
  - goal: 用 口令 登录系统
    checks:
      - element_exists: 查询
"""

FAIL_YAML = CASE_YAML.replace("title: 83票", "title: 83票-失败样本").replace(
    "  - goal: 点击提交，等待提交结果", "  - goal: 点击提交，这一步断言会失败")


def expect(resp, status: int, what: str) -> dict:
    assert resp.status_code == status, f"{what} 期望 {status}，实际 {resp.status_code}：{resp.text[:300]}"
    return resp.json() if resp.content else {}


def login_status(client, account: str, password: str) -> int:
    return client.post(f"{API}/auth/login", json={"account": account, "password": password}).status_code


def login_token(client, account: str, password: str) -> dict:
    resp = client.post(f"{API}/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, f"{account} 登录失败：{resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def wait_run(client, auth, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = expect(client.get(f"{API}/runs/{run_id}", headers=auth), 200, "运行详情")
        if data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.2)
    raise TimeoutError(f"运行 {run_id} 超时未结束")


def read_sse(client, auth, run_id: str, timeout: float = 20.0) -> list[str]:
    events: list[str] = []
    with client.stream("GET", f"{API}/runs/{run_id}/events", headers=auth,
                       timeout=httpx.Timeout(10.0, read=timeout)) as response:
        assert response.status_code == 200, f"SSE 状态码 {response.status_code}"
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):].strip())
            if "done" in events:
                break
    return events


# --- 主流程 ---

def test_health_and_auth(client, auth):
    data = expect(client.get(f"{API}/health"), 200, "健康检查")
    assert data["ok"] is True and data["db"] == "ok", data
    assert client.get(f"{API}/nodes").status_code == 401
    bad = client.post(f"{API}/auth/login", json={"account": "admin", "password": "nope"})
    assert bad.status_code == 401 and bad.json()["code"] == "bad_credentials", bad.text
    me = expect(client.get(f"{API}/auth/me", headers=auth), 200, "当前用户")
    assert me["account"] == "admin"


def test_node_tree_and_delete_guard(client, auth, workspace):
    tree = expect(client.get(f"{API}/nodes", headers=auth), 200, "节点树")
    root = next(item for item in tree["items"] if item["id"] == workspace["node"]["id"])
    assert [child["name"] for child in root["children"]] == ["查验"]
    blocked = client.delete(f"{API}/nodes/{workspace['node']['id']}", headers=auth)
    assert blocked.status_code == 409 and blocked.json()["code"] == "node_not_empty", blocked.text


def test_env_secrets_are_write_only(client, auth, workspace):
    raw = client.put(f"{API}/envs/{workspace['env']['id']}/secrets", headers=auth, json={
        "base_url": "http://192.0.2.10",
        "secrets": {"账号": SECRET_ACCOUNT, "密码": SECRET_PASSWORD},
    })
    assert raw.status_code == 200, raw.text
    resp = raw.json()
    assert SECRET_ACCOUNT not in raw.text and SECRET_PASSWORD not in raw.text, "响应里出现了密钥明文"
    assert resp["configured"] is True, resp
    keys = {item["key"]: item["set"] for item in resp["secret_keys"]}
    assert keys == {"账号": True, "密码": True}, keys
    envs = expect(client.get(f"{API}/envs", headers=auth), 200, "环境列表")
    assert envs["items"][0]["configured"] is True


def test_env_secret_delete(client, auth, workspace):
    env_id = workspace["env"]["id"]
    env_name = workspace["env"]["name"]
    expect(client.put(f"{API}/envs/{env_id}/secrets", headers=auth,
                      json={"secrets": {"临时键": SECRET_ACCOUNT}}), 200, "写临时密钥")
    file_data = secret_store.read_env_file(env_name)
    assert "临时键" in (file_data.get("secrets") or {}), "临时键应已在文件里"

    resp = expect(client.delete(f"{API}/envs/{env_id}/secrets/临时键", headers=auth), 200, "删密钥")
    assert "临时键" not in [item["key"] for item in resp["secret_keys"]], resp
    file_data = secret_store.read_env_file(env_name)
    assert "临时键" not in (file_data.get("secrets") or {}), "文件里还残留"
    assert "账号" in (file_data.get("secrets") or {}), "不该动其它键"

    missing = client.delete(f"{API}/envs/{env_id}/secrets/没这个键", headers=auth)
    assert missing.status_code == 404 and missing.json()["code"] == "secret_not_found", missing.text


def test_case_validate_save_and_conflict(client, auth, workspace):
    case_id = workspace["case"]["id"]
    bad = expect(client.post(f"{API}/cases/{case_id}/validate", headers=auth,
                             json={"yaml_text": BAD_YAML}), 200, "坏用例校验")
    assert bad["ok"] is False and len(bad["errors"]) >= 3, bad
    codes = {err["code"] for err in bad["errors"]}
    assert {"ref_in_goal", "checks_empty"} <= codes, codes
    assert all(err["line"] >= 1 for err in bad["errors"]), bad["errors"]

    rejected = client.put(f"{API}/cases/{case_id}", headers=auth,
                          json={"yaml_text": BAD_YAML, "version": 1})
    assert rejected.status_code == 422 and rejected.json()["code"] == "lint_failed", rejected.text

    saved = expect(client.put(f"{API}/cases/{case_id}", headers=auth,
                              json={"yaml_text": CASE_YAML, "version": 1, "comment": "首版"}),
                   200, "保存用例")
    assert saved["version"] == 2 and len(saved["plan"]["steps"]) == 5, saved

    conflict = client.put(f"{API}/cases/{case_id}", headers=auth,
                          json={"yaml_text": CASE_YAML, "version": 1})
    assert conflict.status_code == 409 and conflict.json()["code"] == "version_conflict", conflict.text

    detail = expect(client.get(f"{API}/cases/{case_id}", headers=auth), 200, "用例详情")
    assert detail["version"] == 2 and detail["yaml_text"] == CASE_YAML
    assert detail["name"] == "83票" and detail["priority"] == "P1"


def test_plan_preview_and_draft(client, auth, workspace):
    case_id = workspace["case"]["id"]
    preview = expect(client.post(f"{API}/cases/{case_id}/validate", headers=auth), 200, "编译预览")
    assert preview["ok"] is True and len(preview["plan"]["steps"]) == 5, preview
    check = preview["plan"]["steps"][1]["checks"][0]
    assert check == {"kind": "element_value", "value": "行政区域", "expected": "2102 大连",
                     "role": "行政区域"}, check

    draft = expect(client.post(f"{API}/cases/{case_id}/drafts", headers=auth,
                               json={"desc": "登录后按发票号查询并上传 PDF"}), 200, "自然语言草稿")
    assert "${{secret." in draft["yaml_text"] and draft["meta"]["model"], draft

    detail = expect(client.get(f"{API}/cases/{case_id}", headers=auth), 200, "草稿后的用例")
    assert detail["version"] == 2, "草稿不应落库"


def test_env_unconfigured_run_is_blocked(client, auth, workspace):
    other = expect(client.post(f"{API}/envs", headers=auth,
                               json={"name": "未配置环境", "label": "占位"}), 201, "建未配置环境")
    blocked = client.post(f"{API}/runs", headers=auth,
                          json={"case_id": workspace["case"]["id"], "env_id": other["id"],
                                "mode": "debug"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "env_unconfigured", blocked.text
    assert client.delete(f"{API}/envs/{other['id']}", headers=auth).status_code == 204


def test_probe_and_page_inventory(client, auth, workspace):
    data = expect(client.post(f"{API}/envs/{workspace['env']['id']}/probe", headers=auth,
                              json={"url": "http://192.0.2.10/login"}), 200, "探针")
    assert data["needs_login"] is True and data["has_password_field"] is True, data
    pages = expect(client.get(f"{API}/envs/{workspace['env']['id']}/pages", headers=auth),
                   200, "页清单")
    assert pages["total"] >= 1 and pages["items"][0]["url"] == "http://192.0.2.10/login", pages


def test_env_delete_clears_page_inventory(client, auth, workspace):
    """删环境连带清页清单（探针缓存）：带缓存的删除在 MySQL 会被外键挡住（旧逻辑 500），SQLite 下也不许留孤儿行。"""
    env = expect(client.post(f"{API}/envs", headers=auth,
                             json={"name": "待删环境", "label": "探针缓存清理"}), 201, "建环境")
    expect(client.post(f"{API}/envs/{env['id']}/probe", headers=auth,
                       json={"url": "http://192.0.2.10/login"}), 200, "探针")
    pages = expect(client.get(f"{API}/envs/{env['id']}/pages", headers=auth), 200, "页清单")
    assert pages["total"] >= 1, pages
    expect(client.delete(f"{API}/envs/{env['id']}", headers=auth), 204, "删环境")
    gone = client.get(f"{API}/envs/{env['id']}/pages", headers=auth)
    assert gone.status_code == 404, gone.text
    with SessionLocal() as db:
        left = db.scalar(select(func.count()).select_from(PageInventory)
                         .where(PageInventory.env_id == env["id"]))
    assert left == 0, f"环境已删，但页清单还留 {left} 条"


def test_run_missing_secret_is_rejected(client, auth, workspace):
    """登记的键没写值：能保存，但发起运行按 var_unresolved 拒掉（不给内核空值）。"""
    env = expect(client.post("/api/v1/envs", headers=auth, json={
        "name": "未配齐密钥", "label": "只在环境里登记了键名", "secret_keys": ["验证码"],
        "base_url": "http://192.0.2.10/",
    }), 201, "建第二个环境")
    created = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases", headers=auth,
                                 json={"name": "引用未配置密钥"}), 201, "建用例")
    expect(client.put(f"{API}/cases/{created['id']}", headers=auth,
                      json={"yaml_text": MISSING_YAML, "version": 1}), 200, "保存")
    blocked = client.post(f"{API}/runs", headers=auth,
                          json={"case_id": created["id"], "env_id": env["id"], "mode": "ci"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "var_unresolved", blocked.text
    bad_mode = client.post(f"{API}/runs", headers=auth,
                           json={"case_id": created["id"], "env_id": env["id"], "mode": "fast"})
    assert bad_mode.status_code == 422 and bad_mode.json()["code"] == "bad_mode", bad_mode.text


def test_unregistered_secret_key_is_a_lint_error(client, auth, workspace):
    """引用环境没登记的键：按 spec 是错误，保存就被 422 拒掉（内置内核 case compile 报 unknown_secret）。"""
    created = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases", headers=auth,
                                 json={"name": "引用未登记密钥"}), 201, "建用例")
    rejected = client.put(f"{API}/cases/{created['id']}", headers=auth,
                          json={"yaml_text": UNREGISTERED_YAML, "version": 1})
    assert rejected.status_code == 422 and rejected.json()["code"] == "lint_failed", rejected.text
    codes = {err["code"] for err in rejected.json()["detail"]["errors"]}
    assert "unknown_secret" in codes, rejected.text


def test_run_debug_passes_and_artifacts(client, auth, workspace):
    run = expect(client.post(f"{API}/runs", headers=auth,
                             json={"case_id": workspace["case"]["id"],
                                   "env_id": workspace["env"]["id"], "mode": "debug"}),
                 202, "发起运行")
    assert run["status"] == "queued" and run["queue_position"] >= 1, run
    workspace["run"] = run["id"]
    data = wait_run(client, auth, run["id"])
    assert data["status"] == "passed", data
    assert data["steps"] == 5 and len(data["checks"]) == 6, data
    assert all(item["ok"] for item in data["checks"]), data["checks"]
    kinds = {item["kind"] for item in data["artifacts"]}
    assert {"screenshot", "trace", "report", "summary"} <= kinds, sorted(kinds)
    assert data["report_exists"] is True

    report = client.get(f"{API}/runs/{run['id']}/report", headers=auth)
    assert report.status_code == 200 and "<html" in report.text.lower(), report.status_code
    shot = client.get(f"{API}/runs/{run['id']}/artifacts/screenshots/01-click.png", headers=auth)
    assert shot.status_code == 200 and shot.content[:4] == b"\x89PNG", shot.status_code
    trav = client.get(f"{API}/runs/{run['id']}/artifacts/%2e%2e%2f%2e%2e%2fapp%2fmain.py",
                      headers=auth)
    assert trav.status_code in (400, 404), f"路径穿越没被拦住：{trav.status_code}"
    rebuilt = expect(client.post(f"{API}/runs/{run['id']}/report", headers=auth), 200, "重出报告")
    assert rebuilt["exists"] is True, rebuilt


def test_sse_streams_status_trace_done(client, auth, workspace):
    events = read_sse(client, auth, workspace["run"])
    assert "status" in events and "done" in events, events
    assert events.count("trace") >= 2, events
    assert events.index("trace") < events.index("done"), events


def test_run_ci_failure(client, auth, workspace):
    copy = expect(client.post(f"{API}/cases/{workspace['case']['id']}/copy", headers=auth,
                              json={"name": "83票-失败样本"}), 201, "复制用例")
    expect(client.put(f"{API}/cases/{copy['id']}", headers=auth,
                      json={"yaml_text": FAIL_YAML, "version": 1}), 200, "保存失败样本")
    run = expect(client.post(f"{API}/runs", headers=auth,
                             json={"case_id": copy["id"], "env_id": workspace["env"]["id"],
                                   "mode": "ci"}), 202, "发起 ci 运行")
    data = wait_run(client, auth, run["id"])
    assert data["status"] == "failed" and data["kernel_status"] == "check_failed", data
    assert [item for item in data["checks"] if not item["ok"]], data["checks"]
    assert data["steps_detail"][-1]["status"] == "check_failed", data["steps_detail"]


def test_stop_run(client, auth, workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    run = expect(client.post(f"{API}/runs", headers=auth,
                             json={"case_id": workspace["case"]["id"],
                                   "env_id": workspace["env"]["id"], "mode": "debug"}),
                 202, "发起运行(待停止)")
    deadline = time.time() + 15
    while time.time() < deadline:
        detail = expect(client.get(f"{API}/runs/{run['id']}", headers=auth), 200, "运行详情")
        if detail["status"] == "running":
            break
        assert detail["status"] == "queued", detail
        time.sleep(0.05)
    else:
        pytest.fail("运行一直没进入 running")
    stopped = expect(client.post(f"{API}/runs/{run['id']}/stop", headers=auth), 200, "停止运行")
    assert stopped["status"] == "aborted", stopped
    data = wait_run(client, auth, run["id"])
    assert data["status"] == "aborted", data
    again = client.post(f"{API}/runs/{run['id']}/stop", headers=auth)
    assert again.status_code == 409 and again.json()["code"] == "run_not_active", again.text
    time.sleep(0.8)  # 等 worker 收尾：它只该补耗时，不能把 aborted 盖回 failed
    settled = expect(client.get(f"{API}/runs/{run['id']}", headers=auth), 200, "收尾后的运行")
    assert settled["status"] == "aborted" and "用户停止" in settled["detail"], settled


def test_stop_wins_over_late_worker_pickup(client, auth, workspace, monkeypatch):
    """排队中被停止的运行：worker 事后领到这条，也不许起进程、不许改状态。"""
    monkeypatch.setattr(run_service.queue, "submit", lambda run_id: None)  # 只落台账，不入队
    run = expect(client.post(f"{API}/runs", headers=auth,
                             json={"case_id": workspace["case"]["id"],
                                   "env_id": workspace["env"]["id"], "mode": "debug"}),
                 202, "发起运行(排队中)")
    stopped = expect(client.post(f"{API}/runs/{run['id']}/stop", headers=auth), 200, "停止排队中的运行")
    assert stopped["status"] == "aborted", stopped

    spawned = []
    monkeypatch.setattr(run_service, "KernelProcess", lambda *args, **kwargs: spawned.append(args))
    with SessionLocal() as db:
        row = db.scalar(select(Run).where(Run.code == run["id"]))
        db_id, artifact_dir = row.id, row.artifact_dir
    run_service.execute(db_id)
    assert spawned == [], "已被停止的运行不该再起内核进程"
    detail = expect(client.get(f"{API}/runs/{run['id']}", headers=auth), 200, "停止后的运行")
    assert detail["status"] == "aborted" and "用户停止" in detail["detail"], detail
    assert not (Path(artifact_dir) / "plan.json").exists(), "被停止的运行不该留下产物"


def test_stop_landing_after_worker_read_keeps_aborted(client, auth, workspace, monkeypatch):
    """竞态点：worker 读到 queued 之后、认领 running 之前，用户停止落库。

    用 resolve_vars 当缝——它在「读」之后、「认领」之前被调用；在缝里把 aborted 写进库，
    模拟真实撞车（冒烟里出过：停止返回 aborted，最终却成了 passed）。
    """
    monkeypatch.setattr(run_service.queue, "submit", lambda run_id: None)
    run = expect(client.post(f"{API}/runs", headers=auth,
                             json={"case_id": workspace["case"]["id"],
                                   "env_id": workspace["env"]["id"], "mode": "debug"}),
                 202, "发起运行(排队中)")
    original = secret_store.resolve_vars

    def racer(vars_map, env_row, env_name):
        with SessionLocal() as db:
            row = db.scalar(select(Run).where(Run.code == run["id"]))
            row.status, row.detail, row.finished_at = "aborted", "用户停止：赛中落库", utcnow()
            db.commit()
        return original(vars_map, env_row, env_name)

    monkeypatch.setattr(secret_store, "resolve_vars", racer)
    spawned = []
    monkeypatch.setattr(run_service, "KernelProcess", lambda *args, **kwargs: spawned.append(args))
    with SessionLocal() as db:
        db_id = db.scalar(select(Run.id).where(Run.code == run["id"]))
    run_service.execute(db_id)
    assert spawned == [], "认领失败后不该起内核进程"
    detail = expect(client.get(f"{API}/runs/{run['id']}", headers=auth), 200, "赛中停止后的运行")
    assert detail["status"] == "aborted", detail
    assert detail["detail"] == "用户停止：赛中落库", "worker 不该把用户的停止覆盖掉"


def test_runs_ledger_filters(client, auth, workspace):
    listed = expect(client.get(f"{API}/runs?case_id={workspace['case']['id']}", headers=auth),
                    200, "按用例筛")
    assert listed["total"] >= 2, listed
    passed = expect(client.get(f"{API}/runs?status=passed&mode=debug", headers=auth), 200, "按状态筛")
    assert passed["total"] >= 1, passed
    bad = client.get(f"{API}/runs?date_from=昨天", headers=auth)
    assert bad.status_code == 422 and bad.json()["code"] == "bad_time", bad.text


def test_versions_and_rollback(client, auth, workspace):
    case_id = workspace["case"]["id"]
    versions = expect(client.get(f"{API}/cases/{case_id}/versions", headers=auth), 200, "版本列表")
    assert versions["total"] >= 2 and versions["current_version"] >= 2, versions
    first = expect(client.get(f"{API}/cases/{case_id}/versions/1", headers=auth), 200, "取 v1")
    rolled = expect(client.post(f"{API}/cases/{case_id}/rollback", headers=auth,
                               json={"version": 1}), 200, "回滚到 v1")
    assert rolled["version"] > versions["current_version"], rolled
    detail = expect(client.get(f"{API}/cases/{case_id}", headers=auth), 200, "回滚后的用例")
    assert detail["yaml_text"] == first["yaml_text"], "回滚后正文与 v1 不一致"


def test_soft_delete_keeps_ledger(client, auth, workspace):
    case_id = workspace["case"]["id"]
    expect(client.delete(f"{API}/cases/{case_id}", headers=auth), 204, "软删除用例")
    assert client.get(f"{API}/cases/{case_id}", headers=auth).status_code == 404
    runs = expect(client.get(f"{API}/runs?case_id={case_id}", headers=auth), 200, "删除后的台账")
    assert runs["total"] >= 2, "台账不该跟着用例删掉"
    trash = list((get_settings().artifacts_dir / "trash").glob(f"case-{case_id}-*.yaml"))
    assert trash, "软删除应把 YAML 落到 trash/"


def test_user_manage_and_password(client, auth, workspace):
    me = expect(client.get(f"{API}/auth/me", headers=auth), 200, "管理员")
    admin_id = me["id"]
    assert me["is_admin"] is True, me

    dup = client.post(f"{API}/users", headers=auth,
                      json={"account": "admin", "name": "", "password": "whatever-1"})
    assert dup.status_code == 409 and dup.json()["code"] == "user_exists", dup.text
    bad_acc = client.post(f"{API}/users", headers=auth,
                          json={"account": "有 空格", "name": "", "password": "whatever-1"})
    assert bad_acc.status_code == 422 and bad_acc.json()["code"] == "bad_account", bad_acc.text
    weak = client.post(f"{API}/users", headers=auth,
                       json={"account": "ut-weak", "name": "", "password": "123"})
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password", weak.text

    mate = expect(client.post(f"{API}/users", headers=auth, json={
        "account": "ut-mate", "name": "临时同事", "password": "mate-pass-1"}), 201, "建用户")
    assert mate["name"] == "临时同事" and mate["active"] is True and mate["is_admin"] is False, mate
    assert "pwd" not in str(mate), "响应里不该出现口令字段"

    mate_auth = login_token(client, "ut-mate", "mate-pass-1")
    forbidden = client.get(f"{API}/users", headers=mate_auth)
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden", forbidden.text
    blocked = client.post(f"{API}/users", headers=mate_auth,
                          json={"account": "ut-x", "name": "", "password": "xxxxxx"})
    assert blocked.status_code == 403, blocked.text

    nick = expect(client.patch(f"{API}/auth/me", headers=mate_auth, json={"name": "小同事"}),
                  200, "改昵称")
    assert nick["name"] == "小同事" and nick["account"] == "ut-mate", nick
    empty = client.patch(f"{API}/auth/me", headers=mate_auth, json={"name": "   "})
    assert empty.status_code == 422 and empty.json()["code"] == "bad_name", empty.text

    wrong_old = client.put(f"{API}/auth/password", headers=mate_auth,
                           json={"old_password": "nope", "new_password": "mate-pass-2"})
    assert wrong_old.status_code == 401 and wrong_old.json()["code"] == "bad_password", wrong_old.text
    weak_new = client.put(f"{API}/auth/password", headers=mate_auth,
                          json={"old_password": "mate-pass-1", "new_password": "123"})
    assert weak_new.status_code == 422 and weak_new.json()["code"] == "weak_password", weak_new.text
    expect(client.put(f"{API}/auth/password", headers=mate_auth,
                      json={"old_password": "mate-pass-1", "new_password": "mate-pass-2"}),
           200, "改密码")
    assert login_status(client, "ut-mate", "mate-pass-1") == 401, "旧密码不该还能用"
    assert login_status(client, "ut-mate", "mate-pass-2") == 200, "新密码应能登录"

    self_guard = client.patch(f"{API}/users/{admin_id}", headers=auth, json={"active": False})
    assert self_guard.status_code == 409 and self_guard.json()["code"] == "self_guard", self_guard.text

    off = expect(client.patch(f"{API}/users/{mate['id']}", headers=auth, json={"active": False}),
                 200, "停用")
    assert off["active"] is False, off
    assert login_status(client, "ut-mate", "mate-pass-2") == 401, "停用后不该能登录"
    stale = client.get(f"{API}/auth/me", headers=mate_auth)
    assert stale.status_code == 401 and stale.json()["code"] == "account_disabled", stale.text

    expect(client.post(f"{API}/users/{mate['id']}/password", headers=auth,
                       json={"password": "mate-pass-3"}), 200, "重置密码")
    expect(client.patch(f"{API}/users/{mate['id']}", headers=auth, json={"active": True}),
           200, "启用")
    assert login_status(client, "ut-mate", "mate-pass-3") == 200, "重置后应能用新密码登录"
    assert login_status(client, "ut-mate", "mate-pass-2") == 401, "重置后旧密码应失效"

    promoted = expect(client.patch(f"{API}/users/{mate['id']}", headers=auth,
                                   json={"is_admin": True}), 200, "设为管理员")
    assert promoted["is_admin"] is True, promoted
    expect(client.get(f"{API}/users", headers=login_token(client, "ut-mate", "mate-pass-3")),
           200, "新管理员看用户列表")
    expect(client.patch(f"{API}/users/{mate['id']}", headers=auth, json={"is_admin": False}),
           200, "收回管理员")

    listing = expect(client.get(f"{API}/users", headers=auth), 200, "用户列表")
    assert [item["account"] for item in listing["items"]] == ["admin", "ut-mate"], listing


def test_audit_log_written(client, auth, workspace):
    with SessionLocal() as db:
        actions = {row.action for row in db.scalars(select(AuditLog)).all()}
    for expected in ("auth.login", "node.create", "scenario.create", "case.save", "case.delete",
                     "env.secrets", "env.probe", "run.create", "run.stop",
                     "user.create", "user.profile", "user.password", "user.update",
                     "user.reset_password"):
        assert expected in actions, f"审计缺 {expected}：{sorted(actions)}"


def test_creator_and_last_actor_columns(client, auth, workspace):
    """创建人 / 最后修改或运行：新建记创建者；编辑和运行都盖「最后操作人」。"""
    expect(client.post(f"{API}/users", headers=auth, json={
        "account": "ut-actor", "name": "留痕同事", "password": "actor-pass-1"}), 201, "建用户")
    mate_auth = login_token(client, "ut-actor", "actor-pass-1")

    scenario = expect(client.post(f"{API}/scenarios", headers=mate_auth, json={
        "node_id": workspace["child"]["id"], "name": "留痕场景", "priority": "P2"}), 201, "成员建场景")
    assert scenario["created_by"] == "留痕同事" and scenario["updated_by"] == "留痕同事", scenario
    case = expect(client.post(f"{API}/scenarios/{scenario['id']}/cases", headers=mate_auth,
                              json={"name": "留痕用例"}), 201, "成员建用例")
    assert case["created_by"] == "留痕同事" and case["updated_by"] == "留痕同事", case

    patched = expect(client.patch(f"{API}/scenarios/{scenario['id']}", headers=auth,
                                  json={"priority": "P1"}), 200, "管理员改场景")
    assert patched["created_by"] == "留痕同事" and patched["updated_by"] == "管理员", patched

    expect(client.put(f"{API}/cases/{case['id']}", headers=auth,
                      json={"yaml_text": CASE_YAML, "version": 1}), 200, "管理员保存用例")
    detail = expect(client.get(f"{API}/cases/{case['id']}", headers=auth), 200, "用例详情")
    assert detail["created_by"] == "留痕同事" and detail["updated_by"] == "管理员", detail

    run = expect(client.post(f"{API}/runs", headers=mate_auth, json={
        "case_id": case["id"], "env_id": workspace["env"]["id"], "mode": "debug"}),
                 202, "成员发起运行")
    after = expect(client.get(f"{API}/cases/{case['id']}", headers=auth), 200, "运行后的用例")
    assert after["created_by"] == "留痕同事" and after["updated_by"] == "留痕同事", \
        "运行也算一次改动：最后操作人应回到发起运行的人"
    wait_run(client, auth, run["id"])
