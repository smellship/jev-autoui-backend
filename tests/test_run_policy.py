"""运行策略（模型开关四档）：存储/mapping、管理员接口、提交快照进台账、执行透传给内核 argv。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog, Run
from app.services import run_policy, run_service

API = "/api/v1"

CASE_YAML = """\
id: ut-policy-01
title: 策略透传用例
env: ut-policy
vars:
  账号: ${{secret.账号}}
steps:
  - goal: 打开页面并登录
    checks:
      - element_exists: 查询
"""


def login(client, account: str, password: str) -> dict:
    resp = client.post(f"{API}/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(autouse=True)
def clean_run_policy():
    path = run_policy.store_path()
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def ws(client, auth) -> dict:
    node = client.post(f"{API}/nodes", headers=auth, json={"name": "策略节点"}).json()
    env = client.post(f"{API}/envs", headers=auth, json={
        "name": "ut-policy-env", "label": "策略测试环境", "secret_keys": ["账号"]}).json()
    client.put(f"{API}/envs/{env['id']}/secrets", headers=auth,
               json={"base_url": "http://192.0.2.77", "secrets": {"账号": "ut-policy-account"}})
    scenario = client.post(f"{API}/scenarios", headers=auth, json={
        "node_id": node["id"], "name": "策略场景", "priority": "P1"}).json()
    case = client.post(f"{API}/scenarios/{scenario['id']}/cases", headers=auth,
                       json={"name": "策略用例"}).json()
    saved = client.put(f"{API}/cases/{case['id']}", headers=auth,
                       json={"yaml_text": CASE_YAML, "version": 1})
    assert saved.status_code == 200, saved.text
    return {"node": node, "env": env, "scenario": scenario, "case": case}


class FakeProc:
    """内核子进程替身：argv 由工厂闭包记录，这里只把收尾口径答全。"""

    killed = False
    pid = 4242

    def wait(self, timeout: int) -> int:
        return 0

    def result(self) -> dict:
        return {"ok": True, "status": "ok"}

    def close(self) -> None:
        pass


def spawn_once(monkeypatch) -> list[list[str]]:
    """接管 KernelProcess：记 argv，返回空壳进程；配合 queue.submit 空转只跑台账+一次执行。"""
    monkeypatch.setattr(run_service.queue, "submit", lambda run_id: None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(run_service, "KernelProcess",
                        lambda args, log_path, profile_dir=None: spawned.append(args) or FakeProc())
    return spawned


def run_row(db, code: str) -> Run:
    row = db.scalar(select(Run).where(Run.code == code))
    assert row is not None, code
    return row


# --- 服务层：四档 mapping 与坏数据回落 ---

def test_kernel_args_four_tiers():
    base = ["--decider", "jev", "--supervisor", "on", "--cache", "on"]
    assert run_policy.kernel_args({"decider": "jev", "supervisor": True, "cache": True}) == base
    assert run_policy.kernel_args({"decider": "jev", "supervisor": False, "cache": True}) == [
        "--decider", "jev", "--supervisor", "off", "--cache", "on"]
    assert run_policy.kernel_args({"decider": "llm", "supervisor": True, "cache": True}) == [
        "--decider", "llm", "--supervisor", "on", "--cache", "on"]
    assert run_policy.kernel_args({"decider": "off", "supervisor": False, "cache": True,
                                   "cache_strict": True}) == [
        "--decider", "off", "--supervisor", "off", "--cache", "on", "--cache-strict"]


def test_kernel_args_falls_back_on_missing_or_bad_snapshot():
    base = ["--decider", "jev", "--supervisor", "on", "--cache", "on"]
    assert run_policy.kernel_args(None) == base, "老台账没有快照 → 按默认全开"
    assert run_policy.kernel_args({}) == base
    assert run_policy.kernel_args({"decider": "gpt", "supervisor": "嗯"}) == base, "坏值回落默认"


def test_normalize_coerces_string_bools():
    conf = run_policy.normalize({"decider": "llm", "supervisor": "false", "cache": "off",
                                 "cache_strict": "yes"})
    assert conf == {"decider": "llm", "supervisor": False, "cache": False, "cache_strict": True}


# --- 管理员接口 ---

def test_member_forbidden(client, auth):
    created = client.post(f"{API}/users", headers=auth, json={
        "account": "ut-policy-member", "name": "只读的人", "password": "member-pass-1"})
    assert created.status_code == 201, created.text
    mate = login(client, "ut-policy-member", "member-pass-1")
    assert client.get(f"{API}/settings/policy", headers=mate).status_code == 403
    assert client.put(f"{API}/settings/policy", headers=mate,
                      json={"cache": False}).status_code == 403


def test_defaults_partial_update_and_audit(client, auth):
    body = client.get(f"{API}/settings/policy", headers=auth).json()
    assert body["policy"] == {"decider": "jev", "supervisor": True, "cache": True, "cache_strict": False}

    put = client.put(f"{API}/settings/policy", headers=auth,
                     json={"decider": "off", "supervisor": False})
    assert put.status_code == 200, put.text
    assert put.json()["policy"] == {"decider": "off", "supervisor": False, "cache": True,
                                    "cache_strict": False}
    path = run_policy.store_path()
    assert path.parent == get_settings().secrets_dir and path.exists()

    put2 = client.put(f"{API}/settings/policy", headers=auth, json={"cache_strict": True})
    assert put2.json()["policy"] == {"decider": "off", "supervisor": False, "cache": True,
                                     "cache_strict": True}, "None=不改，只动点了的那个开关"
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "settings.policy")).all()
    assert rows and rows[-1].detail == {"decider": "off", "supervisor": False, "cache": True,
                                        "cache_strict": True}


def test_validation(client, auth):
    for payload, code in (({}, "empty_payload"), ({"decider": "gpt"}, "bad_decider")):
        resp = client.put(f"{API}/settings/policy", headers=auth, json=payload)
        assert resp.status_code == 422 and resp.json()["code"] == code, (payload, resp.text)
    assert not run_policy.store_path().exists(), "非法请求不该落文件"


# --- 提交快照 → 台账 → argv ---

def test_run_snapshots_policy_and_passes_flags(client, auth, ws, monkeypatch):
    put = client.put(f"{API}/settings/policy", headers=auth,
                     json={"decider": "off", "supervisor": False, "cache": True, "cache_strict": True})
    assert put.status_code == 200, put.text
    spawned = spawn_once(monkeypatch)

    created = client.post(f"{API}/runs", headers=auth, json={
        "case_id": ws["case"]["id"], "env_id": ws["env"]["id"], "mode": "debug"})
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    detail = client.get(f"{API}/runs/{run_id}", headers=auth).json()
    assert detail["policy"] == {"decider": "off", "supervisor": False, "cache": True,
                                "cache_strict": True}, "台账要能看见提交时的策略快照"

    with SessionLocal() as db:
        db_id = run_row(db, run_id).id
    run_service.execute(db_id)
    argv = spawned[0]
    assert argv[argv.index("--decider") + 1] == "off"
    assert argv[argv.index("--supervisor") + 1] == "off"
    assert argv[argv.index("--cache") + 1] == "on"
    assert "--cache-strict" in argv


def test_legacy_run_without_snapshot_gets_default_flags(client, auth, ws, monkeypatch):
    spawned = spawn_once(monkeypatch)
    created = client.post(f"{API}/runs", headers=auth, json={
        "case_id": ws["case"]["id"], "env_id": ws["env"]["id"], "mode": "debug"})
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    with SessionLocal() as db:
        row = run_row(db, run_id)
        row.policy = None  # 模拟补列之前的老台账
        db_id = row.id
        db.commit()
    run_service.execute(db_id)
    argv = spawned[0]
    assert argv[argv.index("--decider") + 1] == "jev"
    assert argv[argv.index("--supervisor") + 1] == "on"
    assert argv[argv.index("--cache") + 1] == "on"
    assert "--cache-strict" not in argv


def test_batch_runs_share_snapshot(client, auth, ws, monkeypatch):
    put = client.put(f"{API}/settings/policy", headers=auth,
                     json={"decider": "llm", "supervisor": True})
    assert put.status_code == 200, put.text
    spawn_once(monkeypatch)
    started = client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [ws["case"]["id"]], "env_id": ws["env"]["id"], "mode": "ci"})
    assert started.status_code == 202, started.text
    with SessionLocal() as db:
        rows = db.scalars(select(Run).where(Run.batch_code == started.json()["code"])).all()
    assert len(rows) == 1
    assert rows[0].policy == {"decider": "llm", "supervisor": True, "cache": True,
                              "cache_strict": False}
