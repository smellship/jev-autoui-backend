"""测试夹具：临时库 + 内核桩（零模型、零浏览器、零网络）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="uiweb-test-"))
FAKE_KERNEL = ROOT / "scripts" / "fake_kernel.py"

# 默认：一次性临时 SQLite。要跑外部库（如本地 MySQL）就设 UI_WEB_TEST_DB_URL：
#   mysql+pymysql://root:pwd@127.0.0.1:3306/ui_web_test?charset=utf8mb4
# 外部库每次开跑前会被清空重建（drop_all → 启动时 create_all），保证空库开跑。
EXTERNAL_DB_URL = os.environ.get("UI_WEB_TEST_DB_URL", "")
os.environ["UI_WEB_DB_URL"] = EXTERNAL_DB_URL or f"sqlite:///{(TMP / 'test.db').as_posix()}"
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

from app import models  # noqa: E402,F401  （注册全部表，供外部库清库用）
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

if EXTERNAL_DB_URL:
    Base.metadata.drop_all(engine)  # 外部库：清空重来，不受上一次运行的数据影响
    print(f"[conftest] 外部测试库已清空：{EXTERNAL_DB_URL.split('@')[-1]}")

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
