"""互斥挡：Case.mutex 编辑接口 + 提交 run 时的挡位/账号键快照（内核走桩、零浏览器）。

本模块自建环境与用例：不依赖主流程测试留下的数据（它们会软删场景里的用例）。
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Run
from app.services import run_service

API = "/api/v1"
ACCOUNT, PASSWORD = "ut-mutex-account-0001", "ut-mutex-pass-0001"

CASE_YAML = """id: sc-0000-01
title: __TITLE__
priority: P2
env: 192.0.2.30

vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}

steps:
  - goal: 打开登录页并用 账号 登录
    checks:
      - element_exists: 查询
"""

BARE_YAML = """id: sc-0000-02
title: __TITLE__
priority: P2
env: 192.0.2.30

steps:
  - goal: 打开首页确认渲染完成
    checks:
      - text_contains: 首页
"""


def expect(resp, status: int, what: str) -> dict:
    assert resp.status_code == status, f"{what} 期望 {status}，实际 {resp.status_code}：{resp.text[:300]}"
    return resp.json() if resp.content else {}


def run_row(code: str) -> Run:
    with SessionLocal() as db:
        row = db.scalar(select(Run).where(Run.code == code))
        assert row is not None, f"run {code} 不在库里"
        return row


@pytest.fixture(scope="module")
def sample(client, auth, workspace) -> dict:
    env = expect(client.post(f"{API}/envs", headers=auth, json={
        "name": "192.0.2.30", "label": "互斥样本环境", "secret_keys": ["账号", "密码"],
    }), 201, "建环境")
    expect(client.put(f"{API}/envs/{env['id']}/secrets", headers=auth, json={
        "base_url": "http://192.0.2.30", "secrets": {"账号": ACCOUNT, "密码": PASSWORD},
    }), 200, "补写密钥")

    def mk_case(name: str, yaml_text: str) -> str:
        case = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases",
                                  headers=auth, json={"name": name}), 201, "建用例")
        expect(client.put(f"{API}/cases/{case['id']}", headers=auth,
                          json={"yaml_text": yaml_text.replace("__TITLE__", name), "version": 1}),
               200, "保存用例")
        return case["id"]

    return {"env_id": env["id"], "account": mk_case("互斥样本用例", CASE_YAML),
            "bare": mk_case("互斥样本裸用例", BARE_YAML)}


def run_case(client, auth, sample, case_id: str) -> str:
    body = expect(client.post(f"{API}/runs", headers=auth, json={
        "case_id": case_id, "env_id": sample["env_id"], "mode": "debug"}), 202, "发起运行")
    return body["id"]


def test_patch_case_mutex_validates_and_saves(client, auth, sample):
    code = sample["account"]
    expect(client.patch(f"{API}/cases/{code}", headers=auth, json={"mutex": "weird"}),
           422, "非法挡位")
    expect(client.patch(f"{API}/cases/{code}", headers=auth, json={}), 422, "空修改体")

    body = expect(client.patch(f"{API}/cases/{code}", headers=auth, json={"mutex": "env"}),
                  200, "改为环境挡")
    assert body["mutex"] == "env", body
    detail = expect(client.get(f"{API}/cases/{code}", headers=auth), 200, "用例详情")
    assert detail["mutex"] == "env", detail


def test_account_mutex_snapshots_hashed_key(client, auth, sample):
    code = sample["account"]
    expect(client.patch(f"{API}/cases/{code}", headers=auth, json={"mutex": "account"}),
           200, "改为账号挡")
    run_code = run_case(client, auth, sample, code)
    row = run_row(run_code)
    assert row.mutex == "account"
    expected = hashlib.sha256(f"{sample['env_id']}|{ACCOUNT}|{PASSWORD}".encode("utf-8")).hexdigest()
    assert row.mutex_key == expected, "键快照应为 sha256(env_id|账号|密码)"
    assert ACCOUNT not in (row.plan_json or "") and PASSWORD not in (row.plan_json or ""), \
        "台账不落明文（vars 保持引用未解析）"


def test_none_mutex_still_snapshots_key(client, auth, sample):
    """挡=none 也存键：后到的互斥用例（挡=账号）能命中在跑的非互斥同账号用例。"""
    expect(client.patch(f"{API}/cases/{sample['account']}", headers=auth, json={"mutex": "none"}),
           200, "改回无挡")
    run_code = run_case(client, auth, sample, sample["account"])
    row = run_row(run_code)
    assert row.mutex == "none" and row.mutex_key, "无挡也要有账号键快照"


def test_account_mutex_without_key_downgrades_to_env(client, auth, sample):
    """账号挡但解析不到账号：键为空、按环境挡参与互斥，运行详情带降级标注。"""
    code = sample["bare"]
    expect(client.patch(f"{API}/cases/{code}", headers=auth, json={"mutex": "account"}),
           200, "改为账号挡")
    run_code = run_case(client, auth, sample, code)
    row = run_row(run_code)
    assert row.mutex == "account" and row.mutex_key is None
    detail = expect(client.get(f"{API}/runs/{run_code}", headers=auth), 200, "运行详情")
    assert detail["mutex"] == "account" and detail["mutex_note"], detail
    assert run_service.effective_mutex("account", None) == "env"
    assert run_service.effective_mutex("account", "k") == "account"
    assert run_service.effective_mutex("none", None) == "none"


def test_batch_runs_snapshot_mutex_too(client, auth, sample):
    expect(client.patch(f"{API}/cases/{sample['account']}", headers=auth, json={"mutex": "env"}),
           200, "改为环境挡")
    body = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [sample["account"], sample["bare"]], "env_id": sample["env_id"],
        "mode": "debug"}), 202, "发起批次")
    by_case = {item["case_id"]: item["id"] for item in body["items"]}
    assert run_row(by_case[sample["account"]]).mutex == "env"
    bare = run_row(by_case[sample["bare"]])
    assert bare.mutex == "account" and bare.mutex_key is None  # 上一测试留下的挡位
