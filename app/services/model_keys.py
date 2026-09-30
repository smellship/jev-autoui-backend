"""模型 API Key：服务器本地文件 `secrets/_model_keys.local.yaml`（gitignore，只写不读）。

内核读的是环境变量 `TEXT_MODEL_API_KEY` / `TYPESAFE_API_KEY`（pydantic-settings，
优先级高于内核 .env）。平台只在两处读明文：① 状态回「已设置」；
② 起内核子进程时经 `kernel_client.child_env()` 注入。任何响应、日志、产物都不含明文（§12）。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.core.config import get_settings

KEYS: tuple[dict, ...] = (
    {
        "key": "text_model_api_key",
        "env_name": "TEXT_MODEL_API_KEY",
        "label": "文本模型 Key",
        "hint": "TYPE_TEXT / planner / supervisor（默认 deepseek-flash）",
    },
    {
        "key": "typesafe_api_key",
        "env_name": "TYPESAFE_API_KEY",
        "label": "决策模型 Key",
        "hint": "Jev 决策模型（默认 jev-latest）",
    },
)
KEY_MAP = {spec["key"]: spec for spec in KEYS}
FILE_NAME = "_model_keys.local.yaml"


def store_path() -> Path:
    return get_settings().secrets_dir / FILE_NAME


def _read() -> dict:
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def status() -> list[dict]:
    """每把 Key 的「已设置」状态；source 区分来自存储文件还是服务器进程环境变量。"""
    stored = _read()
    items = []
    for spec in KEYS:
        in_file = bool(str(stored.get(spec["key"]) or "").strip())
        in_env = bool((os.environ.get(spec["env_name"]) or "").strip())
        items.append({
            "key": spec["key"], "label": spec["label"], "hint": spec["hint"],
            "env_name": spec["env_name"],
            "set": in_file or in_env,
            "source": "file" if in_file else ("env" if in_env else None),
        })
    return items


def write(updates: dict[str, str | None]) -> list[str]:
    """None=不改；空串=清除；其余写入。返回实际变化的键名。"""
    stored = _read()
    changed: list[str] = []
    for key, value in updates.items():
        if key not in KEY_MAP or value is None:
            continue
        value = value.strip()
        if value:
            if stored.get(key) != value:
                stored[key] = value
                changed.append(key)
        elif key in stored:
            del stored[key]
            changed.append(key)
    if changed:
        _save(stored)
    return changed


def env_overrides() -> dict[str, str]:
    """注入内核子进程的两把 Key（存储值优先于进程 env，空值不注入）。"""
    stored = _read()
    overrides: dict[str, str] = {}
    for spec in KEYS:
        value = str(stored.get(spec["key"]) or "").strip()
        if value:
            overrides[spec["env_name"]] = value
    return overrides
