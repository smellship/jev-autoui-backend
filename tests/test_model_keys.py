"""模型 API Key：管理员只写不读接口、secrets 文件落点、内核子进程注入。"""

from __future__ import annotations

from sqlalchemy import select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog
from app.services import kernel_client, model_keys

API = "/api/v1"
TEXT_KEY = "sk-ut-text-2468"
JEV_KEY = "sk-ut-jev-1357"


def login(client, account: str, password: str) -> dict:
    resp = client.post(f"{API}/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def status_map(client, headers) -> dict:
    resp = client.get(f"{API}/settings/model-keys", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["key"]: item for item in resp.json()["items"]}


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
