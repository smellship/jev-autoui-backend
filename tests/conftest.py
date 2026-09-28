"""测试夹具：临时库 + 内核桩（零模型、零浏览器、零网络）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="uiweb-test-"))
FAKE_KERNEL = ROOT / "scripts" / "fake_kernel.py"

os.environ["UI_WEB_DB_URL"] = f"sqlite:///{(TMP / 'test.db').as_posix()}"
os.environ["UI_WEB_ARTIFACTS_DIR"] = str(TMP / "artifacts")
os.environ["UI_WEB_SECRETS_DIR"] = str(TMP / "secrets")
os.environ["UI_WEB_KERNEL_CMD"] = f'"{sys.executable}" "{FAKE_KERNEL}"'
os.environ["UI_WEB_RETENTION_ENABLED"] = "false"
os.environ["UI_WEB_JWT_SECRET"] = "test-secret"
os.environ["FAKE_KERNEL_STEP_MS"] = "10"
(TMP / "artifacts").mkdir(parents=True, exist_ok=True)
(TMP / "secrets").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

CASE_YAML = """id: sc-0000-01
title: 冒烟用例
priority: P1
env: 192.0.2.10

vars:
  账号: ${{secret.账号}}

steps:
  - goal: 打开登录页并用 账号 登录
    checks:
      - element_exists: 查询
  - goal: 提交查询并等待结果
    checks:
      - text_contains: 结果列表
"""


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth(client) -> dict:
    resp = client.post("/api/v1/auth/login", json={"account": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(scope="session")
def workspace(client, auth) -> dict:
    node = client.post("/api/v1/nodes", json={"name": "百望云"}, headers=auth).json()
    child = client.post("/api/v1/nodes", json={"parent_id": node["id"], "name": "查验"},
                        headers=auth).json()
    env = client.post("/api/v1/envs", json={
        "name": "192.0.2.10", "label": "内网测试环境", "secret_keys": ["账号"],
        "extra": {"样例目录": "D:/samples"},
    }, headers=auth).json()
    scenario = client.post("/api/v1/scenarios", json={
        "node_id": child["id"], "name": "手工查验", "priority": "P1",
    }, headers=auth).json()
    case = client.post(f"/api/v1/scenarios/{scenario['id']}/cases", json={"name": "冒烟用例"},
                       headers=auth).json()
    return {"node": node, "child": child, "env": env, "scenario": scenario, "case": case}
