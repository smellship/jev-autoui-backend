"""模型配置：管理员接口（Key 只写不读；地址/模型名可回显）、secrets 文件落点、内核子进程注入。"""

from __future__ import annotations

from sqlalchemy import select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog
from app.services import kernel_client, model_keys

API = "/api/v1"
TEXT_KEY = "sk-ut-text-2468"
JEV_KEY = "sk-ut-jev-1357"
RELAY_URL = "http://127.0.0.1:8898/v1/systemone"  # 本地 mock/中转站：http 也要放行


def login(client, account: str, password: str) -> dict:
    resp = client.post(f"{API}/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def status_map(client, headers) -> dict:
    resp = client.get(f"{API}/settings/model-keys", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["key"]: item for item in resp.json()["items"]}


def endpoints_map(client, headers) -> dict:
    resp = client.get(f"{API}/settings/model-keys", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["key"]: item for item in resp.json()["endpoints"]}


def _clear_endpoint_envs(monkeypatch) -> None:
    for name in ("TEXT_MODEL_BASE_URL", "TYPESAFE_BASE_URL", "TEXT_MODEL", "TYPESAFE_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_member_forbidden(client, auth):
    created = client.post(f"{API}/users", headers=auth, json={
        "account": "ut-key-member", "name": "读不到的人", "password": "member-pass-1"})
    assert created.status_code == 201, created.text
    mate = login(client, "ut-key-member", "member-pass-1")
    assert client.get(f"{API}/settings/model-keys", headers=mate).status_code == 403
    put = client.put(f"{API}/settings/model-keys", headers=mate,
                     json={"text_model_api_key": TEXT_KEY})
    assert put.status_code == 403 and put.json()["code"] == "forbidden", put.text


def test_admin_key_lifecycle(client, auth, monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    items = status_map(client, auth)
    assert [item["set"] for item in items.values()] == [False, False], items
    assert all(item["source"] is None for item in items.values()), items
    assert all(item["env_name"] for item in items.values()), items

    # 写入一把，另一把 None=不改
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"text_model_api_key": TEXT_KEY})
    assert put.status_code == 200, put.text
    body = put.json()
    assert TEXT_KEY not in put.text, "响应不该回显明文"
    assert body["changed"] == ["text_model_api_key"], body
    assert body["items"][0]["set"] is True and body["items"][0]["source"] == "file", body
    assert body["items"][1]["set"] is False, body

    path = model_keys.store_path()
    assert path.parent == get_settings().secrets_dir, path
    assert TEXT_KEY in path.read_text(encoding="utf-8"), "明文应落在 secrets/ 文件里"

    # 第二把写入；第一把仍保留
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"typesafe_api_key": JEV_KEY})
    assert put.status_code == 200 and put.json()["changed"] == ["typesafe_api_key"], put.text
    items = status_map(client, auth)
    assert {key: item["set"] for key, item in items.items()} == {
        "text_model_api_key": True, "typesafe_api_key": True}, items

    # 空串 / 纯空白 = 清除
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"text_model_api_key": ""})
    assert put.json()["changed"] == ["text_model_api_key"], put.text
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"typesafe_api_key": "   "})
    assert put.json()["changed"] == ["typesafe_api_key"], put.text
    text = path.read_text(encoding="utf-8")
    assert TEXT_KEY not in text and JEV_KEY not in text, "清除后文件里不该留明文"
    items = status_map(client, auth)
    assert not any(item["set"] for item in items.values()), items

    # 清除一个本来就没有的键：不报错，changed 为空
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"text_model_api_key": ""})
    assert put.status_code == 200 and put.json()["changed"] == [], put.text

    # 审计：只记键名与动作，不含明文
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "settings.model_keys")).all()
    assert rows, "缺 settings.model_keys 审计"
    detail = str([row.detail for row in rows])
    assert "text_model_api_key" in detail and "typesafe_api_key" in detail, detail
    assert TEXT_KEY not in detail and JEV_KEY not in detail, "审计里不该出现明文"


def test_child_env_injection(client, auth, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "sk-from-server-env")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-from-server-env")
    model_keys.write({"text_model_api_key": "", "typesafe_api_key": JEV_KEY})
    try:
        env = kernel_client.child_env()
        assert env["TYPESAFE_API_KEY"] == JEV_KEY, "存储值应压过服务器进程 env"
        assert env["TEXT_MODEL_API_KEY"] == "sk-from-server-env", "没存过时应透传进程 env"
        assert env["PYTHONUTF8"] == "1"
    finally:
        model_keys.write({"typesafe_api_key": ""})
    env = kernel_client.child_env()
    assert env["TYPESAFE_API_KEY"] == "sk-from-server-env", "清掉后回到进程 env"


def test_validation(client, auth):
    empty = client.put(f"{API}/settings/model-keys", headers=auth, json={})
    assert empty.status_code == 422 and empty.json()["code"] == "empty_payload", empty.text
    blank = client.put(f"{API}/settings/model-keys", headers=auth,
                       json={"text_model_api_key": "has space"})
    assert blank.status_code == 422 and blank.json()["code"] == "bad_key", blank.text
    too_long = client.put(f"{API}/settings/model-keys", headers=auth,
                          json={"text_model_api_key": "x" * 513})
    assert too_long.status_code == 422, too_long.text
    path = model_keys.store_path()
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    assert "x" * 100 not in before, "非法请求不该写进文件"


def test_endpoint_lifecycle(client, auth, monkeypatch):
    _clear_endpoint_envs(monkeypatch)

    eps = endpoints_map(client, auth)
    assert set(eps) == {"text_model_base_url", "text_model", "typesafe_base_url", "typesafe_model"}
    assert eps["typesafe_base_url"]["default"] == "https://api.typesafe.ai/v1/systemone"
    assert all(e["value"] == "" and e["source"] is None for e in eps.values()), eps

    # 写：http 地址（中转站/mock 场景）+ 模型名，响应可回显
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"typesafe_base_url": RELAY_URL, "text_model": "relay-gpt"})
    assert put.status_code == 200, put.text
    eps = {item["key"]: item for item in put.json()["endpoints"]}
    assert eps["typesafe_base_url"]["value"] == RELAY_URL, eps
    assert eps["typesafe_base_url"]["source"] == "file", eps
    assert eps["text_model"]["value"] == "relay-gpt", eps

    # 进程 env 是次优先级来源：未存文件时回显 env 值
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://env-relay.example/v1")
    eps = endpoints_map(client, auth)
    assert eps["text_model_base_url"]["value"] == "https://env-relay.example/v1", eps
    assert eps["text_model_base_url"]["source"] == "env", eps

    # 平台文件压过 env（与 Key 同规则）
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"text_model_base_url": "http://platform-relay.local/v1"})
    eps = {item["key"]: item for item in put.json()["endpoints"]}
    assert eps["text_model_base_url"]["value"] == "http://platform-relay.local/v1", eps
    assert eps["text_model_base_url"]["source"] == "file", eps

    # 清空 = 清除：env 还在 → 回落 env；env 不在 → 回内核默认（value 空）
    put = client.put(f"{API}/settings/model-keys", headers=auth,
                     json={"text_model_base_url": "", "typesafe_base_url": ""})
    eps = {item["key"]: item for item in put.json()["endpoints"]}
    assert eps["text_model_base_url"]["source"] == "env", eps["text_model_base_url"]
    assert eps["typesafe_base_url"]["value"] == "" and eps["typesafe_base_url"]["source"] is None

    # 校验：坏地址 422 bad_url；模型名含空白 422 bad_key
    bad_url = client.put(f"{API}/settings/model-keys", headers=auth,
                         json={"typesafe_base_url": "ftp://relay.local"})
    assert bad_url.status_code == 422 and bad_url.json()["code"] == "bad_url", bad_url.text
    bad_name = client.put(f"{API}/settings/model-keys", headers=auth,
                          json={"text_model": "has space"})
    assert bad_name.status_code == 422 and bad_name.json()["code"] == "bad_key", bad_name.text

    # 收尾：清掉残留（会话级 tmp secrets 目录是共享的）
    client.put(f"{API}/settings/model-keys", headers=auth, json={"text_model": ""})


def test_child_env_endpoint_injection(client, auth, monkeypatch):
    _clear_endpoint_envs(monkeypatch)
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://env-relay.example/v1/systemone")
    model_keys.write({"typesafe_base_url": RELAY_URL, "text_model": "relay-gpt"})
    try:
        env = kernel_client.child_env()
        assert env["TYPESAFE_BASE_URL"] == RELAY_URL, "存储值应压过服务器进程 env"
        assert env["TEXT_MODEL"] == "relay-gpt", env
        assert "TEXT_MODEL_BASE_URL" not in env, "没存过且进程 env 没设，就不注入"
    finally:
        model_keys.write({"typesafe_base_url": "", "text_model": ""})
    env = kernel_client.child_env()
    assert env["TYPESAFE_BASE_URL"] == "https://env-relay.example/v1/systemone", "清掉后回到进程 env"
