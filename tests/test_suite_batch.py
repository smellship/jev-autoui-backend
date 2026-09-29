"""场景管理：套件 CRUD + 批量串跑（批次进度 / 停止 / 失败继续），内核走桩、零浏览器。

在 workspace 场景下自建样本用例，不依赖 test_api_flow 对 workspace 用例的删改。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Run
from app.services import run_service

API = "/api/v1"
SECRET_ACCOUNT = "ut-scene-account-8642"

CASE_TMPL = """id: sc-0001-01
title: __TITLE__
priority: P2
tags: [串跑]
env: 192.0.2.10

vars:
  账号: ${{secret.账号}}

steps:
  - goal: 打开登录页并用 账号 登录
    checks:
      - element_exists: 查询
  - goal: __LAST__
    checks:
      - text_contains: 提交成功
"""

# 指向「串跑未配齐」环境：__KEY__ 登记了但没写值时，串跑按 var_unresolved 拒
BLANK_ENV_TMPL = """id: sc-0001-01
title: __TITLE__
priority: P2
env: 串跑未配齐

vars:
  __KEY__: ${{secret.__KEY__}}

steps:
  - goal: 用 __KEY__ 登录系统
    checks:
      - element_exists: 查询
"""


def expect(resp, status: int, what: str) -> dict:
    assert resp.status_code == status, f"{what} 期望 {status}，实际 {resp.status_code}：{resp.text[:300]}"
    return resp.json() if resp.content else {}


def wait_batch(client, auth, code: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = expect(client.get(f"{API}/batches/{code}", headers=auth), 200, "批次详情")
        if data["done"]:
            return data
        time.sleep(0.2)
    raise TimeoutError(f"批次 {code} 超时未结束")


@pytest.fixture(scope="module")
def pool(client, auth, workspace) -> dict:
    """串跑样本池：3 条正常用例 + 1 条必然断言失败（goal 里带「失败」触发桩）。"""
    expect(client.put(f"{API}/envs/{workspace['env']['id']}/secrets", headers=auth, json={
        "base_url": "http://192.0.2.10", "secrets": {"账号": SECRET_ACCOUNT},
    }), 200, "补写密钥")

    def mk_case(name: str, *, fail: bool = False, yaml: str | None = None) -> str:
        case = expect(client.post(f"{API}/scenarios/{workspace['scenario']['id']}/cases",
                                  headers=auth, json={"name": name}), 201, "建用例")
        text = yaml if yaml is not None else CASE_TMPL.replace("__TITLE__", name).replace(
            "__LAST__", "点击提交，这一步断言会失败（样本）" if fail else "点击提交，等待提交结果")
        expect(client.put(f"{API}/cases/{case['id']}", headers=auth,
                          json={"yaml_text": text, "version": 1}), 200, "保存用例")
        return case["id"]

    return {"mk_case": mk_case, "a": mk_case("串跑样本A"), "b": mk_case("串跑样本B"),
            "c": mk_case("串跑样本C"), "f": mk_case("串跑样本F", fail=True)}


@pytest.fixture(scope="module")
def suite(client, auth) -> dict:
    created = expect(client.post(f"{API}/suites", headers=auth, json={
        "name": "日常串跑", "description": "场景管理串跑套件"}), 201, "建套件")
    assert re.fullmatch(r"su-\d{4}", created["id"]), created
    assert created["case_count"] == 0 and created["last_batch"] is None, created
    return created


def test_case_pool_lists_and_filters(client, auth, workspace, pool):
    data = expect(client.get(f"{API}/cases?q=串跑样本", headers=auth), 200, "全局用例搜索")
    assert {item["id"] for item in data["items"]} == {pool["a"], pool["b"], pool["c"], pool["f"]}, data
    sample = next(item for item in data["items"] if item["id"] == pool["a"])
    assert sample["scenario_code"] == workspace["scenario"]["id"], sample
    assert sample["scenario_name"] == "手工查验", sample

    by_scenario = expect(client.get(
        f"{API}/cases?scenario_id={workspace['scenario']['id']}&q=串跑样本", headers=auth),
        200, "按场景筛")
    assert {item["id"] for item in by_scenario["items"]} == {pool["a"], pool["b"], pool["c"], pool["f"]}

    by_node = expect(client.get(
        f"{API}/cases?node_id={workspace['node']['id']}&q=串跑样本", headers=auth),
        200, "按父节点筛（含子孙）")
    assert {item["id"] for item in by_node["items"]} == {pool["a"], pool["b"], pool["c"], pool["f"]}, \
        "点父节点应能看到子节点场景里的用例"

    bad = client.get(f"{API}/cases?scenario_id=sc-9999-99", headers=auth)
    assert bad.status_code == 404, bad.text


def test_suite_crud_and_ordered_refs(client, auth, suite, pool):
    sid = suite["id"]
    updated = expect(client.patch(f"{API}/suites/{sid}", headers=auth, json={
        "description": "每日回归", "tags": ["回归", "串跑"]}), 200, "改套件")
    assert updated["description"] == "每日回归" and updated["tags"] == ["回归", "串跑"], updated
    assert updated["name"] == "日常串跑", "没传的字段不该被动"

    saved = expect(client.put(f"{API}/suites/{sid}/cases", headers=auth, json={
        "case_ids": [pool["b"], pool["a"], pool["b"]]}), 200, "整表保存用例")
    assert saved["count"] == 2, saved
    detail = expect(client.get(f"{API}/suites/{sid}", headers=auth), 200, "套件详情")
    assert [item["id"] for item in detail["items"]] == [pool["b"], pool["a"]], "顺序应保持传入顺序（重复去重）"
    assert detail["case_count"] == 2 and detail["missing"] == []

    rejected = client.put(f"{API}/suites/{sid}/cases", headers=auth, json={
        "case_ids": [pool["c"], "sc-9999-99"]})
    assert rejected.status_code == 409 and rejected.json()["code"] == "case_invalid", rejected.text
    assert "sc-9999-99" in rejected.json()["detail"]["cases"]
    detail = expect(client.get(f"{API}/suites/{sid}", headers=auth), 200, "拒绝后的套件")
    assert [item["id"] for item in detail["items"]] == [pool["b"], pool["a"]], "失败的保存不该改到旧顺序"

    copy = expect(client.post(f"{API}/suites/{sid}/copy", headers=auth), 201, "复制套件")
    assert copy["id"] != sid and int(copy["id"].split("-")[1]) > int(sid.split("-")[1]), copy
    copy_detail = expect(client.get(f"{API}/suites/{copy['id']}", headers=auth), 200, "副本详情")
    assert [item["id"] for item in copy_detail["items"]] == [pool["b"], pool["a"]], "副本要带上同样的有序引用"

    listed = expect(client.get(f"{API}/suites?q=日常", headers=auth), 200, "套件列表")
    assert {item["id"] for item in listed["items"]} >= {sid, copy["id"]}
    row = next(item for item in listed["items"] if item["id"] == sid)
    assert row["case_count"] == 2 and row["last_batch"] is None, row

    expect(client.delete(f"{API}/suites/{copy['id']}", headers=auth), 204, "删副本")
    assert client.get(f"{API}/suites/{copy['id']}", headers=auth).status_code == 404
    assert client.get(f"{API}/suites/su-9999", headers=auth).status_code == 404


def test_suite_run_creates_ordered_batch(client, auth, workspace, suite, pool):
    sid = suite["id"]
    expect(client.put(f"{API}/suites/{sid}/cases", headers=auth,
                      json={"case_ids": [pool["a"], pool["b"]]}), 200, "重置套件顺序")
    started = expect(client.post(f"{API}/suites/{sid}/run", headers=auth, json={
        "env_id": workspace["env"]["id"], "mode": "debug"}), 202, "运行套件")
    assert started["label"] == "日常串跑" and started["suite_id"] == sid, started
    assert started["total"] == 2 and started["code"].startswith("b-"), started
    assert [item["case_id"] for item in started["items"]] == [pool["a"], pool["b"]], started

    code = started["code"]
    data = wait_batch(client, auth, code)
    assert data["done"] is True and data["counts"]["passed"] == 2, data
    assert [item["status"] for item in data["items"]] == ["passed", "passed"], data
    assert {item["batch_code"] for item in data["items"]} == {code}
    assert all(item["report_url"].endswith("/report") for item in data["items"])

    ledger = expect(client.get(f"{API}/runs?batch_code={code}", headers=auth), 200, "批次台账")
    assert ledger["total"] == 2, ledger
    listed = expect(client.get(f"{API}/suites?q=日常", headers=auth), 200, "套件列表")
    row = next(item for item in listed["items"] if item["id"] == sid)
    assert row["last_batch"]["code"] == code and row["last_batch"]["counts"]["passed"] == 2, row


def test_batch_failure_continues(client, auth, workspace, pool):
    started = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"], pool["f"], pool["c"]],
        "env_id": workspace["env"]["id"], "mode": "ci"}), 202, "即席串跑（含失败样本）")
    assert started["label"] == "临时串跑" and started["suite_id"] is None, started
    assert [item["case_id"] for item in started["items"]] == [pool["a"], pool["f"], pool["c"]]

    data = wait_batch(client, auth, started["code"])
    assert data["done"] and data["counts"]["passed"] == 2 and data["counts"]["failed"] == 1, data
    assert [item["status"] for item in data["items"]] == ["passed", "failed", "passed"], data
    failed = data["items"][1]
    assert failed["kernel_status"] == "check_failed", failed


def test_batch_stop(client, auth, workspace, pool, monkeypatch):
    monkeypatch.setenv("FAKE_KERNEL_STEP_MS", "600")
    started = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"], pool["b"], pool["c"]],
        "env_id": workspace["env"]["id"], "mode": "debug"}), 202, "即席串跑（待停止）")
    code = started["code"]
    deadline = time.time() + 15
    while time.time() < deadline:
        data = expect(client.get(f"{API}/batches/{code}", headers=auth), 200, "批次详情")
        if any(item["status"] == "running" for item in data["items"]):
            break
        assert data["counts"]["queued"] + data["counts"]["running"] >= 1, data
        time.sleep(0.05)
    else:
        pytest.fail("批次一直没进入 running")

    stopped = expect(client.post(f"{API}/batches/{code}/stop", headers=auth), 200, "停止批次")
    assert stopped["status"] == "aborted" and stopped["aborted"] == 3, stopped
    data = wait_batch(client, auth, code)
    assert data["counts"]["aborted"] == 3, data
    assert all(item["status"] == "aborted" for item in data["items"]), data
    assert "批次停止" in data["items"][0]["detail"], data["items"][0]
    assert data["items"][1]["detail"] == "批次停止：未执行", data["items"][1]
    assert data["items"][2]["detail"] == "批次停止：未执行", data["items"][2]

    again = client.post(f"{API}/batches/{code}/stop", headers=auth)
    assert again.status_code == 409 and again.json()["code"] == "batch_not_active", again.text
    time.sleep(0.8)  # 等 worker 收尾：它只该收摊，不许把 aborted 盖回其它状态
    settled = expect(client.get(f"{API}/batches/{code}", headers=auth), 200, "收尾后的批次")
    assert settled["counts"]["aborted"] == 3, settled


def test_batch_stop_before_worker_claim(client, auth, workspace, pool, monkeypatch):
    """排队中被整批停止：worker 事后领到这些运行，也不许起进程、不许改状态。"""
    monkeypatch.setattr(run_service.queue, "submit", lambda run_id: None)  # 只落台账，不入队
    started = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"], pool["b"]],
        "env_id": workspace["env"]["id"], "mode": "debug"}), 202, "发起排队中的批次")
    code = started["code"]
    stopped = expect(client.post(f"{API}/batches/{code}/stop", headers=auth), 200, "停止排队中的批次")
    assert stopped["aborted"] == 2, stopped

    spawned = []
    monkeypatch.setattr(run_service, "KernelProcess", lambda *args, **kwargs: spawned.append(args))
    with SessionLocal() as db:
        rows = db.scalars(select(Run).where(Run.batch_code == code).order_by(Run.id)).all()
        runs = [(row.id, row.artifact_dir) for row in rows]
    assert len(runs) == 2
    for db_id, _ in runs:
        run_service.execute(db_id)
    assert spawned == [], "已被停止的批次不该再起内核进程"
    data = expect(client.get(f"{API}/batches/{code}", headers=auth), 200, "停止后的批次")
    assert data["done"] and data["counts"]["aborted"] == 2, data
    assert all(item["detail"] == "批次停止：未执行" for item in data["items"]), data
    for _, artifact_dir in runs:
        assert not (Path(artifact_dir) / "plan.json").exists(), "被停止的运行不该留下产物"


def test_batch_validation_and_guards(client, auth, workspace, pool):
    env_id = workspace["env"]["id"]
    bad = client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"], "sc-9999-99"], "env_id": env_id})
    assert bad.status_code == 409 and bad.json()["code"] == "case_invalid", bad.text
    assert "sc-9999-99" in bad.json()["detail"]["cases"]

    dup = expect(client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"], pool["a"]], "env_id": env_id}), 202, "重复用例串跑")
    assert dup["total"] == 1, dup
    assert wait_batch(client, auth, dup["code"])["counts"]["passed"] == 1

    bad_mode = client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"]], "env_id": env_id, "mode": "fast"})
    assert bad_mode.status_code == 422 and bad_mode.json()["code"] == "bad_mode", bad_mode.text
    assert client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [pool["a"]], "env_id": 999999}).status_code == 404
    assert client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [], "env_id": env_id}).status_code == 422
    assert client.get(f"{API}/batches/b-29990101-000000", headers=auth).status_code == 404

    env_blank = expect(client.post(f"{API}/envs", headers=auth, json={
        "name": "串跑未配齐", "label": "登记了键但没写值", "secret_keys": ["验证码", "编号"],
        "base_url": "http://192.0.2.10/"}), 201, "建未配齐环境")
    expect(client.put(f"{API}/envs/{env_blank['id']}/secrets", headers=auth,
                      json={"secrets": {"编号": SECRET_ACCOUNT}}), 200, "只配一个键")

    def blank_yaml(title: str, key: str) -> str:
        return BLANK_ENV_TMPL.replace("__TITLE__", title).replace("__KEY__", key)

    case_g = pool["mk_case"]("串跑样本G", yaml=blank_yaml("串跑样本G", "编号"))
    case_m = pool["mk_case"]("串跑样本M", yaml=blank_yaml("串跑样本M", "验证码"))
    before = expect(client.get(f"{API}/runs?case_id={case_g}", headers=auth), 200, "批次前台账")["total"]
    blocked = client.post(f"{API}/batches", headers=auth, json={
        "case_ids": [case_g, case_m], "env_id": env_blank["id"]})
    assert blocked.status_code == 422 and blocked.json()["code"] == "batch_invalid", blocked.text
    problems = blocked.json()["detail"]["problems"]
    assert [item["case"] for item in problems] == [case_m], problems
    assert problems[0]["code"] == "var_unresolved", problems
    assert problems[0]["detail"]["missing"] == ["secret.验证码"], problems
    after = expect(client.get(f"{API}/runs?case_id={case_g}", headers=auth), 200, "批次后台账")["total"]
    assert after == before == 0, "整批校验失败时不该留下半截台账"
    expect(client.delete(f"{API}/envs/{env_blank['id']}", headers=auth), 204, "删环境")


def test_suite_missing_case_blocks_run(client, auth, workspace, suite, pool):
    sid = suite["id"]
    extra = pool["mk_case"]("串跑样本D")
    expect(client.put(f"{API}/suites/{sid}/cases", headers=auth,
                      json={"case_ids": [pool["a"], extra, pool["b"]]}), 200, "加入待删用例")
    expect(client.delete(f"{API}/cases/{extra}", headers=auth), 204, "软删用例")

    detail = expect(client.get(f"{API}/suites/{sid}", headers=auth), 200, "套件详情")
    assert [item["id"] for item in detail["items"]] == [pool["a"], pool["b"]], "已删用例不该出现在可用清单"
    assert detail["missing"] == [{"id": extra, "name": "串跑样本D"}], detail["missing"]
    blocked = client.post(f"{API}/suites/{sid}/run", headers=auth, json={
        "env_id": workspace["env"]["id"], "mode": "debug"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "suite_has_missing", blocked.text
    assert blocked.json()["detail"]["cases"] == [extra]
    expect(client.put(f"{API}/suites/{sid}/cases", headers=auth,
                      json={"case_ids": [pool["a"], pool["b"]]}), 200, "修好套件")

    empty_suite = expect(client.post(f"{API}/suites", headers=auth,
                                     json={"name": "空套件"}), 201, "建空套件")
    blocked_empty = client.post(f"{API}/suites/{empty_suite['id']}/run", headers=auth,
                                json={"env_id": workspace["env"]["id"]})
    assert blocked_empty.status_code == 409 and blocked_empty.json()["code"] == "suite_empty", \
        blocked_empty.text
    expect(client.delete(f"{API}/suites/{empty_suite['id']}", headers=auth), 204, "删空套件")
