"""环境密钥：服务器本地文件 `secrets/<env>.local.yaml`（gitignore，只写不读）。

平台只在两处读明文：① 解析用例 vars 里的 `${{secret.X}}` 注入内核 `--var`；
② 探针取 base_url。任何响应、日志、产物都不含明文（§12）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from app.core.config import get_settings

REF_PATTERN = re.compile(r"\$\{\{\s*(secret|env)\.([^}\s]+?)\s*\}\}")


def secret_path(env_name: str) -> Path:
    return get_settings().secrets_dir / f"{env_name}.local.yaml"


def read_env_file(env_name: str) -> dict:
    path = secret_path(env_name)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def save_env_file(env_name: str, data: dict) -> None:
    path = secret_path(env_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def write_env_file(env_name: str, base_url: str | None = None, secrets: dict | None = None) -> None:
    current = read_env_file(env_name)
    if base_url is not None:
        current["base_url"] = base_url
    merged = dict(current.get("secrets") or {})
    merged.update({k: str(v) for k, v in (secrets or {}).items()})
    current["secrets"] = merged
    save_env_file(env_name, current)


def delete_key(env_name: str, key: str) -> bool:
    """从密钥文件里删掉一个键；返回它原本是否在文件里。"""
    current = read_env_file(env_name)
    secrets = dict(current.get("secrets") or {})
    if key not in secrets:
        return False
    del secrets[key]
    current["secrets"] = secrets
    save_env_file(env_name, current)
    return True


def key_status(env_name: str, keys: list[str]) -> dict[str, bool]:
    data = read_env_file(env_name).get("secrets") or {}
    return {key: bool(str(data.get(key) or "").strip()) for key in keys}


def base_url(env_row) -> str:
    """密钥文件里的 base_url 优先（真实地址只放服务器），回落到 env 表。"""
    return str(read_env_file(env_row.name).get("base_url") or env_row.base_url or "").strip()


def resolve_text(text: str, env_row, env_name: str) -> tuple[str, list[str]]:
    """替换一段文本里的 `${{secret.X}}` / `${{env.X}}`；返回 (结果, 缺失引用)。"""
    file_data = read_env_file(env_name)
    secrets = file_data.get("secrets") or {}
    extra = env_row.extra or {}
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        scope, key = match.group(1), match.group(2)
        if scope == "secret":
            value = secrets.get(key)
            if value is None or str(value) == "":
                missing.append(f"secret.{key}")
                return match.group(0)
            return str(value)
        value = extra.get(key)
        if value is None:
            missing.append(f"env.{key}")
            return match.group(0)
        return str(value)

    return REF_PATTERN.sub(replace, text), missing


def resolve_vars(vars_map: dict, env_row, env_name: str) -> tuple[dict[str, str], list[str]]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, value in (vars_map or {}).items():
        text, gaps = resolve_text(str(value), env_row, env_name)
        resolved[str(key)] = text
        missing.extend(gaps)
    return resolved, sorted(set(missing))


def refs_in(text: str) -> list[str]:
    return [f"{m.group(1)}.{m.group(2)}" for m in REF_PATTERN.finditer(text or "")]
