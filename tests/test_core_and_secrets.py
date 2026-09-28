"""口令哈希 / JWT / 路径白名单 / 密钥文件与引用解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.paths import safe_join
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.services import secret_store


def test_password_roundtrip():
    salt, digest = hash_password("口令123")
    assert verify_password("口令123", salt, digest)
    assert not verify_password("口令124", salt, digest)
    assert not verify_password("口令123", "zz", digest)


def test_token_roundtrip_and_tamper():
    settings = get_settings()
    token, ttl = create_token(settings, 7, "admin")
    assert ttl > 0
    payload = decode_token(settings, token)
    assert payload and payload["sub"] == "7"
    assert decode_token(settings, token + "x") is None


def test_safe_join_blocks_escape(tmp_path: Path):
    root = tmp_path / "runs" / "r-1"
    root.mkdir(parents=True)
    assert safe_join(root, "screenshots/a.png") == (root / "screenshots/a.png").resolve()
    for bad in ("../secret.txt", "..\\..\\app/main.py", "/etc/passwd"):
        with pytest.raises(ApiError) as exc:
            safe_join(root, bad)
        assert exc.value.status == 400


def test_secret_file_roundtrip_and_status():
    secret_store.write_env_file("unit-env", base_url="http://192.0.2.10", secrets={"账号": "u1"})
    data = secret_store.read_env_file("unit-env")
    assert data["base_url"] == "http://192.0.2.10" and data["secrets"]["账号"] == "u1"
    assert secret_store.key_status("unit-env", ["账号", "密码"]) == {"账号": True, "密码": False}


class _Env:
    def __init__(self, extra=None, base_url=""):
        self.name = "unit-env"
        self.extra = extra or {}
        self.base_url = base_url


def test_resolve_text_reports_missing_refs():
    text, missing = secret_store.resolve_text("u=${{secret.账号}} p=${{secret.密码}}", _Env(), "unit-env")
    assert text.startswith("u=u1") and missing == ["secret.密码"]


def test_resolve_env_scope_and_precedence():
    env = _Env(extra={"样例目录": "D:/samples"}, base_url="http://db-value")
    text, missing = secret_store.resolve_text("${{env.样例目录}}/83票.pdf", env, "unit-env")
    assert text == "D:/samples/83票.pdf" and not missing
    # 密钥文件里的 base_url 优先于 env 表
    assert secret_store.base_url(env) == "http://192.0.2.10"


def test_resolve_vars_map():
    env = _Env(extra={"样例目录": "D:/samples"})
    resolved, missing = secret_store.resolve_vars(
        {"账号": "${{secret.账号}}", "上传文件": "${{env.样例目录}}/a.pdf", "坏": "${{secret.没有}}"}, env, "unit-env")
    assert resolved["上传文件"] == "D:/samples/a.pdf"
    assert missing == ["secret.没有"]
