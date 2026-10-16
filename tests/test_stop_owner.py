"""停止权限：仅发起人或管理员可停（普通成员停别人的运行/批次一律 403）。

本模块自建样本用例与成员用户：主流程测试会软删 workspace 的用例，不依赖它。
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Run

API = "/api/v1"

CASE_YAML = """id: sc-0001-01
title: __TITLE__
priority: P2
env: 192.0.2.10

vars:
  账号: ${{secret.账号}}

steps:
  - goal: 打开登录页并用 账号 登录
    checks:
      - element_exists: 查询
  - goal: 提交并等待结果
    checks:
      - text_contains: 结果列表
"""


def expect(resp, status: int, what: str) -> dict:
    assert resp.status_code == status, f"{what} 期望 {status}，实际 {resp.status_code}：{resp.text[:300]}"
    return resp.json() if resp.content else {}


def wait_run(client, auth, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = expect(client.get(f"{API}/runs/{run_id}", headers=auth), 200, "运行详情")
        if data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.1)
    raise TimeoutError(f"运行 {run_id} 超时未结束")


@pytest.fixture(scope="module")
def pool(client, auth, workspace) -> dict:
    env_id = workspace["env"]["id"]
    expect(client.put(f"{API}/envs/{env_id}/secrets", headers=auth, json={
        "base_url": "http://192.0.2.10", "secrets": {"账号": "ut-owner-account-0001"},
    }), 200, "补写密钥")
    case = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases",
                              headers=auth, json={"name": "停属主样本"}), 201, "建用例")
    expect(client.put(f"{API}/cases/{case['id']}", headers=auth,
                      json={"yaml_text": CASE_YAML.replace("__TITLE__", "停属主样本"), "version": 1}),
           200, "保存用例")
    mate = expect(client.post(f"{API}/users", headers=auth, json={
        "account": "ut-stopper", "name": "停止测试员", "password": "stopper-pass-1"}), 201, "建成员")
    login = expect(client.post(f"{API}/auth/login",
                               json={"account": "ut-stopper", "password": "stopper-pass-1"}),
                   200, "成员登录")
    return {"env_id": env_id, "case": case["id"], "mate_id": mate["id"],
            "mate_auth": {"Authorization": f"Bearer {login['token']}"}}


def start_run(client, header: dict, pool: dict) -> dict:
    return expect(client.post(f"{API}/runs", headers=header, json={
        "case_id": pool["case"], "env_id": pool["env_id"], "mode": "debug"}), 202, "发起运行")


def test_member_cannot_stop_others_run(client, auth, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    admin_id = expect(client.get(f"{API}/auth/me", headers=auth), 200, "管理员")["id"]
    run = start_run(client, auth, pool)
    assert run["user_id"] == admin_id, run

    denied = client.post(f"{API}/runs/{run['id']}/stop", headers=pool["mate_auth"])
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden", denied.text
    stopped = expect(client.post(f"{API}/runs/{run['id']}/stop", headers=auth), 200, "管理员停自己的")
    assert stopped["status"] == "aborted", stopped
    wait_run(client, auth, run["id"])


def test_member_can_stop_own_run(client, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    run = start_run(client, pool["mate_auth"], pool)
    assert run["user_id"] == pool["mate_id"], run

    stopped = expect(client.post(f"{API}/runs/{run['id']}/stop", headers=pool["mate_auth"]),
                     200, "成员停自己的")
    assert stopped["status"] == "aborted", stopped
    wait_run(client, pool["mate_auth"], run["id"])


def test_denied_even_when_run_is_terminal(client, auth, pool):
    run = start_run(client, auth, pool)
    finished = wait_run(client, auth, run["id"])
    assert finished["status"] == "passed", finished
    denied = client.post(f"{API}/runs/{run['id']}/stop", headers=pool["mate_auth"])
    assert denied.status_code == 403, denied.text


def test_legacy_run_without_owner_is_admin_only(client, auth, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    run = start_run(client, pool["mate_auth"], pool)
    with SessionLocal() as db:  # 模拟老数据：没有发起人记录
        row = db.scalar(select(Run).where(Run.code == run["id"]))
        row.user_id = None
        db.commit()

    denied = client.post(f"{API}/runs/{run['id']}/stop", headers=pool["mate_auth"])
    assert denied.status_code == 403, denied.text
    stopped = expect(client.post(f"{API}/runs/{run['id']}/stop", headers=auth), 200, "管理员兜底停")
    assert stopped["status"] == "aborted", stopped
    wait_run(client, auth, run["id"])


def test_member_cannot_stop_others_batch(client, auth, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    started = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["case"]], "env_id": pool["env_id"], "mode": "debug"}), 202, "管理员发起批次")
    code = started["code"]

    denied = client.post(f"{API}/batches/{code}/stop", headers=pool["mate_auth"])
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden", denied.text
    stopped = expect(client.post(f"{API}/batches/{code}/stop", headers=auth), 200, "管理员停批次")
    assert stopped["status"] == "aborted" and stopped["aborted"] >= 1, stopped


def test_member_can_stop_own_batch(client, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    started = expect(client.post(f"{API}/batches", headers=pool["mate_auth"], json={
        "case_ids": [pool["case"]], "env_id": pool["env_id"], "mode": "debug"}), 202, "成员发起批次")
    stopped = expect(client.post(f"{API}/batches/{started['code']}/stop", headers=pool["mate_auth"]),
                     200, "成员停自己的批次")
    assert stopped["aborted"] >= 1, stopped
