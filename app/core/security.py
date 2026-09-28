"""口令哈希（PBKDF2-SHA256）与 JWT 签发/校验。"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

import jwt

from app.core.config import Settings

ALGORITHM = "HS256"
ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return hmac.compare_digest(digest.hex(), hash_hex)


MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 64


def password_problem(password: str) -> str | None:
    """密码策略；返回中文问题说明，None = 通过。"""
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > MAX_PASSWORD_LEN:
        return f"密码最多 {MAX_PASSWORD_LEN} 位"
    if password.strip() != password:
        return "密码首尾不能有空白"
    return None


def create_token(settings: Settings, user_id: int, account: str) -> tuple[str, int]:
    ttl = settings.jwt_ttl_min * 60
    now = int(time.time())
    token = jwt.encode({"sub": str(user_id), "account": account, "iat": now, "exp": now + ttl},
                       settings.jwt_secret, algorithm=ALGORITHM)
    if isinstance(token, bytes):  # PyJWT 1.x 兼容；正常是 str
        token = token.decode("utf-8")
    return token, ttl


def decode_token(settings: Settings, token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
