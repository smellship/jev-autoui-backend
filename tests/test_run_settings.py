"""运行并发设置：管理员可改 max/k（范围校验、k>max 夹取、落文件、唤醒调度器）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog
from app.services import run_service, run_settings

API = "/api/v1"


def login(client, account: str, password: str) -> dict:
    resp = client.post(f"{API}/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(autouse=True)
def clean_run_settings():
    path = run_settings.store_path()
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


def test_member_forbidden(client, auth):
    created = client.post(f"{API}/users", headers=auth, json={
        "account": "ut-run-member", "name": "改不了的人", "password": "member-pass-1"})
    assert created.status_code == 201, created.text
    mate = login(client, "ut-run-member", "member-pass-1")
    assert client.get(f"{API}/settings/run", headers=mate).status_code == 403
    assert client.put(f"{API}/settings/run", headers=mate, json={"max": 2}).status_code == 403


def test_defaults_and_update(client, auth):
    body = client.get(f"{API}/settings/run", headers=auth).json()
    assert body["max"] == 1 and body["k"] == 1 and body["hard_max"] == 8, body
    assert "running" in body and "pending" in body, body

    put = client.put(f"{API}/settings/run", headers=auth, json={"max": 3, "k": 2})
    assert put.status_code == 200, put.text
    assert put.json()["max"] == 3 and put.json()["k"] == 2, put.text
    path = run_settings.store_path()
    assert path.parent == get_settings().secrets_dir and path.exists()
    assert run_settings.load() == {"max": 3, "k": 2}


def test_k_over_max_clamped(client, auth):
    put = client.put(f"{API}/settings/run", headers=auth, json={"max": 2, "k": 5})
    assert put.status_code == 200 and put.json()["k"] == 2, "k>max 应自动夹到 max"
    put = client.put(f"{API}/settings/run", headers=auth, json={"max": 1})
    assert put.json()["max"] == 1 and put.json()["k"] == 1, "调小 max 时已存的 k 也跟着夹"


def test_validation(client, auth):
    cases = [({}, "empty_payload"), ({"max": 9}, "bad_max"), ({"max": 0}, "bad_max"),
             ({"max": -1}, "bad_max"), ({"k": 0}, "bad_k")]
    for payload, code in cases:
        resp = client.put(f"{API}/settings/run", headers=auth, json=payload)
        assert resp.status_code == 422 and resp.json()["code"] == code, (payload, resp.text)
    assert not run_settings.store_path().exists(), "非法请求不该落文件"


def test_update_wakes_scheduler_and_audits(client, auth, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(run_service.queue, "wake",
                        lambda: calls.__setitem__("n", calls["n"] + 1))
    put = client.put(f"{API}/settings/run", headers=auth, json={"max": 2, "k": 1})
    assert put.status_code == 200 and calls["n"] == 1, "改完设置要唤醒调度器（动态生效）"
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "settings.run")).all()
    assert rows and rows[-1].detail == {"max": 2, "k": 1}, [r.detail for r in rows]
