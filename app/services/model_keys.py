"""模型配置：服务器本地文件 `secrets/_model_keys.local.yaml`（gitignore）。

- 两把 Key（`TEXT_MODEL_API_KEY` / `TYPESAFE_API_KEY`）：只写不读，平台只回「已设置」；
- 端点四项（`TEXT_MODEL_BASE_URL` / `TEXT_MODEL` / `TYPESAFE_BASE_URL` / `TYPESAFE_MODEL`）：
  地址与模型名不是秘密，接口可回显（file > 进程 env > 内核默认）。

内核 pydantic-settings 直接读这些环境变量（优先级高于内核 .env）；
起内核子进程时经 `kernel_client.child_env()` 注入，响应/日志/产物都不含 Key 明文（§12）。
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

# 端点配置（非秘密、可回显）：中转站/替代网关就填在这两项的地址上。
# default 仅作界面占位提示，实际缺省值以内核 config.py 为准。
ENDPOINTS: tuple[dict, ...] = (
    {
        "key": "text_model_base_url",
        "env_name": "TEXT_MODEL_BASE_URL",
        "group": "text",
        "field": "base_url",
        "label": "文本模型 API 地址",
        "default": "https://api.deepseek.com/v1",
    },
    {
        "key": "text_model",
        "env_name": "TEXT_MODEL",
        "group": "text",
        "field": "model",
        "label": "文本模型名",
        "default": "deepseek-flash",
    },
    {
        "key": "typesafe_base_url",
        "env_name": "TYPESAFE_BASE_URL",
        "group": "typesafe",
        "field": "base_url",
        "label": "决策模型 API 地址",
        "default": "https://api.typesafe.ai/v1/systemone",
    },
    {
        "key": "typesafe_model",
        "env_name": "TYPESAFE_MODEL",
        "group": "typesafe",
        "field": "model",
        "label": "决策模型名",
        "default": "jev-latest",
    },
)
ENDPOINT_MAP = {spec["key"]: spec for spec in ENDPOINTS}
ALL_KEYS = frozenset(KEY_MAP) | frozenset(ENDPOINT_MAP)
URL_KEYS = frozenset(spec["key"] for spec in ENDPOINTS if spec["field"] == "base_url")
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


def endpoints() -> list[dict]:
    """端点四项的当前生效值：file > 进程 env > 内核默认（缺省值为空 + default 提示）。"""
    stored = _read()
    items = []
    for spec in ENDPOINTS:
        in_file = str(stored.get(spec["key"]) or "").strip()
        in_env = str(os.environ.get(spec["env_name"]) or "").strip()
        value, source = ((in_file, "file") if in_file
                         else (in_env, "env") if in_env else ("", None))
        items.append({
            "key": spec["key"], "env_name": spec["env_name"],
            "group": spec["group"], "field": spec["field"],
            "label": spec["label"], "value": value, "source": source,
            "default": spec["default"],
        })
    return items


def write(updates: dict[str, str | None]) -> list[str]:
    """None=不改；空串=清除；其余写入。返回实际变化的键名。"""
    stored = _read()
    changed: list[str] = []
    for key, value in updates.items():
        if key not in ALL_KEYS or value is None:
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
    """注入内核子进程的 Key 与端点（存储值优先于进程 env，空值不注入）。"""
    stored = _read()
    overrides: dict[str, str] = {}
    for spec in (*KEYS, *ENDPOINTS):
        value = str(stored.get(spec["key"]) or "").strip()
        if value:
            overrides[spec["env_name"]] = value
    return overrides
