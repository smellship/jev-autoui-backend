"""并发前置修复：next_seq 原子自增 + 运行编号撞车重试（内核走桩、零浏览器）。

本模块自建样本用例：主流程测试会软删 workspace 的用例，不依赖它。
"""

from __future__ import annotations

import threading

import pytest

from app.db import SessionLocal, next_seq
from app.services import run_service

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
"""


def expect(resp, status: int, what: str) -> dict:
    assert resp.status_code == status, f"{what} 期望 {status}，实际 {resp.status_code}：{resp.text[:300]}"
    return resp.json() if resp.content else {}


@pytest.fixture(scope="module")
def sample(client, auth, workspace) -> dict:
    env_id = workspace["env"]["id"]
    expect(client.put(f"{API}/envs/{env_id}/secrets", headers=auth, json={
        "base_url": "http://192.0.2.10", "secrets": {"账号": "ut-conc-account-0001"},
    }), 200, "补写密钥")

    def mk_case(name: str) -> str:
        case = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases",
                                  headers=auth, json={"name": name}), 201, "建用例")
        expect(client.put(f"{API}/cases/{case['id']}", headers=auth,
                          json={"yaml_text": CASE_YAML.replace("__TITLE__", name), "version": 1}),
               200, "保存用例")
        return case["id"]

    return {"env_id": env_id, "case": mk_case("并发样本用例"),
            "case2": mk_case("并发样本用例二")}


def test_next_seq_is_atomic_under_threads(client):
    """多线程并发取号：不重不漏（回归读-改-写竞态）。"""
    threads_n, per_thread = 6, 15
    values: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(per_thread):
                with SessionLocal() as db:
                    value = next_seq(db, "test:seq-atomic")
                    db.commit()
                with lock:
                    values.append(value)
        except BaseException as exc:  # 线程里的异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert sorted(values) == list(range(1, threads_n * per_thread + 1)), "并发取号应不重不漏"


def test_create_run_retries_when_code_conflicts(client, auth, workspace, sample, monkeypatch):
    """模拟并发输家：候选 code 已被别人提交 → 撞唯一约束后自动换后缀重试，而不是 500。"""
    payload = {"case_id": sample["case"], "env_id": sample["env_id"], "mode": "debug"}
    first = expect(client.post(f"{API}/runs", headers=auth, json=payload), 202, "第一个运行")
    taken = first["id"]

    original = run_service._unique_code
    calls = {"n": 0}

    def fake_unique(db, case):
        calls["n"] += 1
        return taken if calls["n"] == 1 else original(db, case)

    monkeypatch.setattr(run_service, "_unique_code", fake_unique)
    second = expect(client.post(f"{API}/runs", headers=auth, json=payload), 202, "撞车后重试的运行")
    assert second["id"] != taken, second
    assert calls["n"] >= 2, "应发生至少一次重试"
    assert expect(client.get(f"{API}/runs/{taken}", headers=auth), 200, "第一个运行还在")["id"] == taken


def test_create_batch_retries_when_code_conflicts(client, auth, workspace, sample, monkeypatch):
    """批量建的编号撞车同样重试：整批回滚后换后缀重来。"""
    payload = {"case_ids": [sample["case"]], "env_id": sample["env_id"], "mode": "debug"}
    first = expect(client.post(f"{API}/batches", headers=auth, json=payload), 202, "第一个批次")
    taken = first["items"][0]["id"]

    original = run_service._unique_code
    calls = {"n": 0}

    def fake_unique(db, case):
        calls["n"] += 1
        return taken if calls["n"] == 1 else original(db, case)

    monkeypatch.setattr(run_service, "_unique_code", fake_unique)
    second = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [sample["case2"], sample["case"]], "env_id": sample["env_id"],
        "mode": "debug"}), 202, "撞车重试的批次")
    codes = [item["id"] for item in second["items"]]
    assert taken not in codes and len(set(codes)) == 2, codes
    assert calls["n"] >= 2, "应发生至少一次重试"
