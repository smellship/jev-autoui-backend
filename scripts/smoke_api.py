"""本地接口冒烟：真起 uvicorn（指向内核桩）+ 真打 HTTP，走完 spec 的主流程。

- 数据全落 .smoke/，可反复跑；不联网、不调模型、不起浏览器；
- 覆盖：登录鉴权 / 用户管理（自助改密 + 管理员围墙）/ 分类树 / 场景 / 用例 / 校验 /
  保存与乐观锁 / TestPlan 预览 / 自然语言草稿 / 环境与密钥（只写不读）/ 探针与页清单 /
  运行（debug·ci·失败·停止）/ SSE 实时事件 / 产物与报告（含路径穿越）/ 版本回滚 /
  复制与软删除 / 明文泄漏扫描。

用法：uv run python scripts/smoke_api.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / ".smoke"
PORT = int(os.environ.get("SMOKE_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"
API = f"{BASE}/api/v1"
ACCOUNT, PASSWORD = "admin", "admin123"
SECRET_ACCOUNT = "smoke-account-9527"
SECRET_PASSWORD = "smoke-password-8531"

CASE_YAML = """id: sc-0001-01
title: 83票
priority: P1
tags: [冒烟, 回归]
owner: 林赟曦
env: 192.0.2.10

vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}
  上传文件: ${{env.样例目录}}/83票.pdf

steps:
  - goal: 打开系统登录页，使用 账号 / 密码 登录，进入系统主页面
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
  - goal: 直接把明文口令代入 ${{secret.密码}} 试试
  - goal: 这一步没有断言
"""

FAIL_YAML = CASE_YAML.replace("title: 83票", "title: 83票-失败样本").replace(
    "  - goal: 点击提交，等待提交结果", "  - goal: 点击提交，这一步断言会失败")


class Smoke:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=API, timeout=httpx.Timeout(10.0, read=30.0))
        self.results: list[tuple[bool, str, str]] = []
        self.token = ""
        self.ids: dict[str, object] = {}

    # --- 基础设施 ---
    def header(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        kwargs.setdefault("headers", {}).update(self.header())
        return self.client.request(method, path, **kwargs)

    def step(self, name: str, func) -> None:
        try:
            evidence = func() or ""
            self.results.append((True, name, str(evidence)))
        except AssertionError as exc:
            self.results.append((False, name, f"断言失败：{exc}"))
        except Exception as exc:  # 冒烟脚本要给出结论，不能中途炸掉
            self.results.append((False, name, f"{exc.__class__.__name__}: {exc}"))

    def report(self) -> int:
        width = max(len(name) for _, name, _ in self.results)
        print("\n" + "=" * 100)
        for ok, name, evidence in self.results:
            print(f"{'✓' if ok else '✗'} {name.ljust(width)}  {evidence}")
        failed = [name for ok, name, _ in self.results if not ok]
        print("=" * 100)
        print(f"共 {len(self.results)} 项，通过 {len(self.results) - len(failed)}，失败 {len(failed)}")
        return 1 if failed else 0


def expect(response: httpx.Response, status: int, what: str) -> dict:
    assert response.status_code == status, f"{what} 期望 {status}，实际 {response.status_code}：{response.text[:300]}"
    return response.json() if response.content else {}


def wait_run(smoke: Smoke, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = expect(smoke.call("GET", f"/runs/{run_id}"), 200, "运行详情")
        if data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.4)
    raise TimeoutError(f"运行 {run_id} 超时未结束")


def read_sse(smoke: Smoke, run_id: str, timeout: float = 20.0) -> list[str]:
    events: list[str] = []
    with smoke.client.stream("GET", f"/runs/{run_id}/events", headers=smoke.header(),
                             timeout=httpx.Timeout(5.0, read=timeout)) as response:
        assert response.status_code == 200, f"SSE 状态码 {response.status_code}"
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):].strip())
            if "done" in events:
                break
    return events


# --- 各步骤 ---
def step_health(smoke: Smoke) -> str:
    data = expect(smoke.call("GET", "/health"), 200, "健康检查")
    assert data["ok"] is True, f"健康检查不通过：{data}"
    return f"db={data['db']} 队列并发={data['queue']['concurrency']} 内核={data['kernel']}"


def step_auth(smoke: Smoke) -> str:
    expect(smoke.call("GET", "/nodes"), 401, "未带 token 访问")
    expect(smoke.call("POST", "/auth/login", json={"account": ACCOUNT, "password": "wrong"}), 401, "错误口令")
    data = expect(smoke.call("POST", "/auth/login", json={"account": ACCOUNT, "password": PASSWORD}), 200, "登录")
    smoke.token = data["token"]
    me = expect(smoke.call("GET", "/auth/me"), 200, "当前用户")
    assert me["account"] == ACCOUNT, me
    return f"token 已签发（{data['expires_in']}s），401 与错误口令都被拒绝"


def step_users(smoke: Smoke) -> str:
    """用户域：管理员建号 → 本人改昵称/改密 → 权限围墙 → 停用 → 管理员重置。"""
    admin_id = expect(smoke.call("GET", "/auth/me"), 200, "管理员")["id"]
    expect(smoke.call("POST", "/users", json={"account": "smoke-mate", "name": "", "password": "123"}),
           422, "弱口令")
    mate = expect(smoke.call("POST", "/users", json={
        "account": "smoke-mate", "name": "冒烟同事", "password": "mate-pass-1",
    }), 201, "建用户")
    assert mate["is_admin"] is False and mate["active"] is True, mate
    expect(smoke.call("POST", "/users", json={
        "account": "smoke-mate", "name": "", "password": "mate-pass-1"}), 409, "重名账号")

    token = expect(smoke.call("POST", "/auth/login", json={
        "account": "smoke-mate", "password": "mate-pass-1"}), 200, "同事登录")["token"]
    mate_header = {"Authorization": f"Bearer {token}"}
    expect(smoke.client.get("/users", headers=mate_header), 403, "非管理员看用户列表")
    expect(smoke.client.post("/users", headers=mate_header,
                             json={"account": "smoke-x", "name": "", "password": "xxxxxx"}),
           403, "非管理员建用户")
    expect(smoke.client.patch("/auth/me", headers=mate_header, json={"name": "小同事"}),
           200, "本人改昵称")
    expect(smoke.client.put("/auth/password", headers=mate_header,
                            json={"old_password": "nope", "new_password": "mate-pass-2"}),
           401, "原密码不对")
    expect(smoke.client.put("/auth/password", headers=mate_header,
                            json={"old_password": "mate-pass-1", "new_password": "mate-pass-2"}),
           200, "本人改密码")
    expect(smoke.call("POST", "/auth/login", json={"account": "smoke-mate", "password": "mate-pass-1"}),
           401, "旧密码登录")
    expect(smoke.call("POST", "/auth/login", json={"account": "smoke-mate", "password": "mate-pass-2"}),
           200, "新密码登录")

    expect(smoke.call("PATCH", f"/users/{admin_id}", json={"active": False}), 409, "停用自己")
    expect(smoke.call("PATCH", f"/users/{mate['id']}", json={"active": False}), 200, "停用同事")
    expect(smoke.call("POST", "/auth/login", json={"account": "smoke-mate", "password": "mate-pass-2"}),
           401, "停用后登录")
    expect(smoke.call("POST", f"/users/{mate['id']}/password", json={"password": "mate-pass-3"}),
           200, "管理员重置密码")
    expect(smoke.call("PATCH", f"/users/{mate['id']}", json={"active": True}), 200, "启用同事")
    expect(smoke.call("POST", "/auth/login", json={"account": "smoke-mate", "password": "mate-pass-3"}),
           200, "重置后登录")
    users = expect(smoke.call("GET", "/users"), 200, "用户列表")
    assert [item["account"] for item in users["items"]] == [ACCOUNT, "smoke-mate"], users
    return "建号 201 / 弱口令 422 / 重名 409 → 非管理员 403 → 改昵称·改密 → 停用 401 → 重置后可登录"


def step_tree(smoke: Smoke) -> str:
    root = expect(smoke.call("POST", "/nodes", json={"name": "百望云", "description": "一级节点"}), 201, "建一级节点")
    child = expect(smoke.call("POST", "/nodes", json={"parent_id": root["id"], "name": "查验"}), 201, "建二级节点")
    tree = expect(smoke.call("GET", "/nodes"), 200, "节点树")
    assert len(tree["items"]) == 1 and tree["items"][0]["children"][0]["name"] == "查验", tree
    smoke.ids["node"] = child["id"]
    return f"百望云(#{root['id']}) → 查验(#{child['id']})"


def step_scenario(smoke: Smoke) -> str:
    env = expect(smoke.call("POST", "/envs", json={
        "name": "192.0.2.10", "label": "内网测试环境", "auth": "login_form",
        "secret_keys": ["账号", "密码"], "extra": {"样例目录": "D:/samples"},
    }), 201, "建环境")
    smoke.ids["env"] = env["id"]
    scenario = expect(smoke.call("POST", "/scenarios", json={
        "node_id": smoke.ids["node"], "name": "手工查验", "priority": "P1", "tags": ["冒烟"],
    }), 201, "建场景")
    smoke.ids["scenario"] = scenario["id"]
    listed = expect(smoke.call("GET", f"/scenarios?node_id={smoke.ids['node']}"), 200, "场景列表")
    assert listed["total"] == 1 and listed["items"][0]["id"] == scenario["id"], listed
    return f"环境 {env['name']}（地址未配置）· 场景 {scenario['id']} P1"


def step_case(smoke: Smoke) -> str:
    case = expect(smoke.call("POST", f"/scenarios/{smoke.ids['scenario']}/cases",
                             json={"name": "83票"}), 201, "建用例")
    smoke.ids["case"] = case["id"]
    assert case["version"] == 1 and case["yaml_text"].startswith("id:"), case
    detail = expect(smoke.call("GET", f"/cases/{case['id']}"), 200, "用例详情")
    assert detail["scenario_code"] == smoke.ids["scenario"], detail
    return f"{case['id']} v1（骨架 YAML，{len(case['yaml_text'])} 字符）"


def step_validate_bad(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/validate",
                             json={"yaml_text": BAD_YAML}), 200, "坏用例校验")
    assert data["ok"] is False and len(data["errors"]) >= 3, data
    lines = [err.get("line") for err in data["errors"]]
    codes = {err.get("code") for err in data["errors"]}
    assert "ref_in_goal" in codes and "checks_empty" in codes, codes
    return f"{len(data['errors'])} 个错误（行号 {lines}）：{'/'.join(sorted(codes))}"


def step_save(smoke: Smoke) -> str:
    data = expect(smoke.call("PUT", f"/cases/{smoke.ids['case']}",
                             json={"yaml_text": CASE_YAML, "version": 1, "comment": "首版"}), 200, "保存用例")
    assert data["version"] == 2, data
    plan = data["plan"] or {}
    assert len(plan.get("steps") or []) == 5, plan
    expect(smoke.call("PUT", f"/cases/{smoke.ids['case']}",
                      json={"yaml_text": CASE_YAML, "version": 1}), 409, "过期版本保存")
    return f"v2 保存成功；plan.steps={len(plan['steps'])}；过期版本返回 409"


def step_preview_and_draft(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/validate"), 200, "编译预览")
    assert data["ok"] and len(data["plan"]["steps"]) == 5, data
    draft = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/drafts",
                              json={"desc": "登录后按发票号查询并上传 PDF"}), 200, "自然语言草稿")
    assert draft["yaml_text"].strip() and "${{secret." in draft["yaml_text"], draft
    detail = expect(smoke.call("GET", f"/cases/{smoke.ids['case']}"), 200, "草稿后的用例")
    assert detail["version"] == 2, "草稿不应落库"
    return f"预览 {len(data['plan']['steps'])} 步；草稿 {len(draft['yaml_text'])} 字符且不落库"


def step_run_blocked(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", "/runs", json={
        "case_id": smoke.ids["case"], "env_id": smoke.ids["env"], "mode": "debug"}), 409, "地址未配置时运行")
    assert data["code"] == "env_unconfigured", data
    return "环境没地址 → 409 env_unconfigured"


def step_secrets(smoke: Smoke) -> str:
    data = expect(smoke.call("PUT", f"/envs/{smoke.ids['env']}/secrets", json={
        "base_url": "http://192.0.2.10",
        "secrets": {"账号": SECRET_ACCOUNT, "密码": SECRET_PASSWORD},
    }), 200, "写密钥")
    text = json.dumps(data, ensure_ascii=False)
    assert SECRET_ACCOUNT not in text and SECRET_PASSWORD not in text, "响应里出现了密钥明文"
    assert data["configured"] is True, data
    keys = {item["key"]: item["set"] for item in data["secret_keys"]}
    assert keys == {"账号": True, "密码": True}, keys
    envs = expect(smoke.call("GET", "/envs"), 200, "环境列表")
    assert envs["items"][0]["configured"] is True, envs
    return "密钥已写入 secrets/，响应只回键名与 已设置✓"


def step_probe(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/envs/{smoke.ids['env']}/probe",
                             json={"url": "http://192.0.2.10/login"}), 200, "探针")
    assert data["needs_login"] is True and data["has_password_field"] is True, data
    pages = expect(smoke.call("GET", f"/envs/{smoke.ids['env']}/pages"), 200, "页清单")
    assert pages["total"] >= 1 and pages["items"][0]["needs_login"] is True, pages
    return f"标题「{data['title']}」元素 {data['element_count']} 个 · 页清单 {pages['total']} 条"


def step_run_debug(smoke: Smoke) -> str:
    run = expect(smoke.call("POST", "/runs", json={
        "case_id": smoke.ids["case"], "env_id": smoke.ids["env"], "mode": "debug"}), 202, "发起运行")
    assert run["status"] == "queued" and run["queue_position"] == 1, run
    smoke.ids["run"] = run["id"]
    data = wait_run(smoke, run["id"])
    assert data["status"] == "passed", data
    kinds = {item["kind"] for item in data["artifacts"]}
    assert {"screenshot", "trace", "report", "summary"} <= kinds, sorted(kinds)
    assert data["report_exists"] and len(data["checks"]) == 6, data["checks"]
    return (f"{run['id']} → passed，{data['steps']} 步 / {len(data['checks'])} 条断言 / "
            f"{len(data['artifacts'])} 个产物 / {data['duration_ms']}ms")


def step_sse(smoke: Smoke) -> str:
    run = expect(smoke.call("POST", "/runs", json={
        "case_id": smoke.ids["case"], "env_id": smoke.ids["env"], "mode": "debug"}), 202, "发起运行(SSE)")
    events = read_sse(smoke, run["id"])
    assert "status" in events and "done" in events, events
    assert events.count("trace") >= 2, f"trace 事件太少：{events}"
    assert events.index("trace") < events.index("done"), events
    wait_run(smoke, run["id"])
    smoke.ids["run_sse"] = run["id"]
    counts = {name: events.count(name) for name in ("status", "trace", "done")}
    return (f"{len(events)} 个事件：status×{counts['status']} / "
            f"trace×{counts['trace']} / done×{counts['done']}")


def step_artifacts(smoke: Smoke) -> str:
    run_id = smoke.ids["run"]
    report = smoke.call("GET", f"/runs/{run_id}/report")
    assert report.status_code == 200 and "<html" in report.text.lower(), report.status_code
    shot = smoke.call("GET", f"/runs/{run_id}/artifacts/screenshots/01-click.png")
    assert shot.status_code == 200 and shot.content[:4] == b"\x89PNG", shot.status_code
    trav = smoke.call("GET", f"/runs/{run_id}/artifacts/../../app/main.py")
    assert trav.status_code in (400, 404), f"路径穿越没被拦住：{trav.status_code}"
    rebuild = expect(smoke.call("POST", f"/runs/{run_id}/report"), 200, "重出报告")
    assert rebuild["exists"] is True, rebuild
    return "报告 200 · 截图 200 · ../../ 被拒（%d）· 重出报告 ok" % trav.status_code


def step_run_failed(smoke: Smoke) -> str:
    copy = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/copy",
                             json={"name": "83票-失败样本"}), 201, "复制用例")
    smoke.ids["case_fail"] = copy["id"]
    expect(smoke.call("PUT", f"/cases/{copy['id']}",
                      json={"yaml_text": FAIL_YAML, "version": 1}), 200, "保存失败样本")
    run = expect(smoke.call("POST", "/runs", json={
        "case_id": copy["id"], "env_id": smoke.ids["env"], "mode": "ci"}), 202, "发起 ci 运行")
    data = wait_run(smoke, run["id"])
    assert data["status"] == "failed", data
    failed_checks = [item for item in data["checks"] if not item["ok"]]
    assert data["kernel_status"] == "check_failed" and failed_checks, data
    return f"{run['id']}（ci）→ failed，kernel_status=check_failed，失败断言 {len(failed_checks)} 条"


def step_stop(smoke: Smoke) -> str:
    run = expect(smoke.call("POST", "/runs", json={
        "case_id": smoke.ids["case"], "env_id": smoke.ids["env"], "mode": "debug"}), 202, "发起运行(待停止)")
    deadline = time.time() + 15  # 等它真进了 running 再停：否则桩可能先跑完，停止会打到终态上
    while time.time() < deadline:
        detail = expect(smoke.call("GET", f"/runs/{run['id']}"), 200, "运行详情")
        if detail["status"] == "running":
            break
        assert detail["status"] == "queued", detail
        time.sleep(0.05)
    else:
        raise AssertionError("运行一直没进入 running")
    stopped = expect(smoke.call("POST", f"/runs/{run['id']}/stop"), 200, "停止运行")
    assert stopped["status"] == "aborted", stopped
    data = wait_run(smoke, run["id"])
    assert data["status"] == "aborted", data
    again = smoke.call("POST", f"/runs/{run['id']}/stop")
    assert again.status_code == 409, f"终态再次停止应 409，实际 {again.status_code}"
    time.sleep(0.8)  # 等 worker 收尾：它只该补耗时，不能把 aborted 盖回 passed
    settled = expect(smoke.call("GET", f"/runs/{run['id']}"), 200, "收尾后的运行")
    assert settled["status"] == "aborted" and "用户停止" in settled["detail"], settled
    return "停止 → aborted（产物 partial），再次停止 409，worker 收尾不覆盖"


def step_runs_list(smoke: Smoke) -> str:
    data = expect(smoke.call("GET", f"/runs?case_id={smoke.ids['case']}"), 200, "按用例筛运行")
    assert data["total"] >= 3, data
    passed = expect(smoke.call("GET", "/runs?status=passed&mode=debug"), 200, "按状态筛")
    assert passed["total"] >= 1, passed
    bad_time = smoke.call("GET", "/runs?date_from=昨天")
    assert bad_time.status_code == 422, bad_time.status_code
    return f"台账 {data['total']} 条；status=passed&mode=debug {passed['total']} 条；坏时间 422"


def step_versions(smoke: Smoke) -> str:
    versions = expect(smoke.call("GET", f"/cases/{smoke.ids['case']}/versions"), 200, "版本列表")
    assert versions["total"] >= 2, versions
    first = expect(smoke.call("GET", f"/cases/{smoke.ids['case']}/versions/1"), 200, "取 v1")
    rolled = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/rollback",
                               json={"version": 1}), 200, "回滚到 v1")
    assert rolled["version"] > versions["current_version"], rolled
    detail = expect(smoke.call("GET", f"/cases/{smoke.ids['case']}"), 200, "回滚后的用例")
    assert detail["yaml_text"] == first["yaml_text"], "回滚后正文与 v1 不一致"
    return f"共 {versions['total']} 版；回滚=前滚到 v{rolled['version']}（历史不丢）"


def step_delete(smoke: Smoke) -> str:
    expect(smoke.call("DELETE", f"/cases/{smoke.ids['case']}"), 204, "软删除用例")
    expect(smoke.call("GET", f"/cases/{smoke.ids['case']}"), 404, "删除后详情")
    runs = expect(smoke.call("GET", f"/runs?case_id={smoke.ids['case']}"), 200, "删除后的运行台账")
    assert runs["total"] >= 3, "台账不该跟着用例删掉"
    trash = list((SMOKE / "artifacts" / "trash").glob("case-*.yaml"))
    assert trash, "软删除应把 YAML 落到 trash/"
    return f"用例 404，台账仍留 {runs['total']} 条运行，trash 里 {len(trash)} 份 YAML"


def step_audit_and_leak(smoke: Smoke) -> str:
    db_path = SMOKE / "smoke.db"
    with sqlite3.connect(db_path) as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_log").fetchall()]
    for expected in ("auth.login", "node.create", "scenario.create", "case.save", "case.delete",
                     "env.secrets", "env.probe", "run.create", "run.stop",
                     "user.create", "user.profile", "user.password", "user.update",
                     "user.reset_password"):
        assert expected in actions, f"审计缺 {expected}：{sorted(set(actions))}"
    leaked = []
    for path in SMOKE.rglob("*"):
        if not path.is_file() or (SMOKE / "secrets") in path.parents:
            continue  # secrets/ 是明文唯一的落点（gitignore、只写不读），其余任何文件都不该有
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for secret in (SECRET_ACCOUNT.encode(), SECRET_PASSWORD.encode()):
            if secret in blob:
                leaked.append(path.name)
    assert not leaked, f"这些文件里出现了密钥明文：{sorted(set(leaked))}"
    return (f"审计 {len(actions)} 条（{len(set(actions))} 种动作）；"
            f"secrets/ 之外（db / 产物 / 日志）均无密钥明文")


def start_server() -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "UI_WEB_DB_URL": f"sqlite:///{(SMOKE / 'smoke.db').as_posix()}",
        "UI_WEB_ARTIFACTS_DIR": str(SMOKE / "artifacts"),
        "UI_WEB_SECRETS_DIR": str(SMOKE / "secrets"),
        "UI_WEB_KERNEL_CMD": f'"{sys.executable}" "{ROOT / "scripts" / "fake_kernel.py"}"',
        "UI_WEB_RETENTION_ENABLED": "false",
        "UI_WEB_JWT_SECRET": "smoke-only-secret",
        "FAKE_KERNEL_STEP_MS": "150",
    })
    env.pop("UI_WEB_ADMIN_PASSWORD", None)
    log = (SMOKE / "server.log").open("wb")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", str(PORT), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_health(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{API}/health", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.4)
    raise SystemExit("服务没起来，见 .smoke/server.log")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，✓/✗ 会编码失败
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    shutil.rmtree(SMOKE, ignore_errors=True)
    (SMOKE / "artifacts" / "runs").mkdir(parents=True)
    (SMOKE / "secrets").mkdir(parents=True)
    proc = start_server()
    smoke = Smoke()
    try:
        wait_health()
        print(f"服务已起：{BASE}（数据在 {SMOKE}）\n")
        smoke.step("健康检查 / 队列状态", lambda: step_health(smoke))
        smoke.step("登录与鉴权（401 / 错误口令 / me）", lambda: step_auth(smoke))
        smoke.step("用户管理（自助改密 + 管理员围墙）", lambda: step_users(smoke))
        smoke.step("分类树：百望云 → 查验", lambda: step_tree(smoke))
        smoke.step("环境 + 场景（手工查验 P1）", lambda: step_scenario(smoke))
        smoke.step("新建用例（骨架 + v1）", lambda: step_case(smoke))
        smoke.step("校验坏用例（错误码 + 行号）", lambda: step_validate_bad(smoke))
        smoke.step("保存 5 步用例（乐观锁 409）", lambda: step_save(smoke))
        smoke.step("TestPlan 预览 + 自然语言草稿", lambda: step_preview_and_draft(smoke))
        smoke.step("地址未配置时拒绝运行", lambda: step_run_blocked(smoke))
        smoke.step("写密钥（只写不读）+ 环境可用", lambda: step_secrets(smoke))
        smoke.step("探针 + 页清单", lambda: step_probe(smoke))
        smoke.step("运行(debug) 跑完 5 步", lambda: step_run_debug(smoke))
        smoke.step("SSE 实时事件", lambda: step_sse(smoke))
        smoke.step("产物 / 报告 / 路径穿越", lambda: step_artifacts(smoke))
        smoke.step("复制用例 + ci 失败运行", lambda: step_run_failed(smoke))
        smoke.step("停止运行（aborted）", lambda: step_stop(smoke))
        smoke.step("运行台账筛选", lambda: step_runs_list(smoke))
        smoke.step("版本列表 / 回滚", lambda: step_versions(smoke))
        smoke.step("软删除用例（台账保留）", lambda: step_delete(smoke))
        smoke.step("审计落库 + 全目录密钥明文扫描", lambda: step_audit_and_leak(smoke))
        return smoke.report()
    finally:
        stop_server(proc)
        smoke.client.close()
        print(f"\n服务日志：{SMOKE / 'server.log'}")


if __name__ == "__main__":
    raise SystemExit(main())
