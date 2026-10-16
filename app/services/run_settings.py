"""运行并发设置：服务器本地文件 `secrets/_run_settings.local.yaml`（现读现用，即改即生效）。

- `max`：全局同时在跑的 run 数上限（1..8，默认取 UI_WEB_RUN_CONCURRENCY）；
- `k`：单个用户同时在跑的 run 数上限（1..max，默认 1；k>max 自动夹到 max）。
调度器每次派发前现读本文件，管理员改完调 `run_service.queue.wake()` 唤醒重派；
调小不杀在跑 run，只影响新派发。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.core.config import get_settings

MAX_HARD = 8
FILE_NAME = "_run_settings.local.yaml"


def store_path() -> Path:
    return get_settings().secrets_dir / FILE_NAME


def defaults() -> dict:
    return {"max": min(max(1, get_settings().run_concurrency), MAX_HARD), "k": 1}


def sanitize(max_value, k) -> dict:
    """夹到合法区间：max ∈ 1..MAX_HARD；k ∈ 1..max（k>max 自动 clamp）。"""
    fallback = defaults()
    try:
        max_value = int(max_value)
    except (TypeError, ValueError):
        max_value = fallback["max"]
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = fallback["k"]
    max_value = min(max(1, max_value), MAX_HARD)
    return {"max": max_value, "k": min(max(1, k), max_value)}


def load() -> dict:
    path = store_path()
    if not path.exists():
        return defaults()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return defaults()
    if not isinstance(data, dict):
        return defaults()
    return sanitize(data.get("max"), data.get("k"))


def save(max_value, k) -> dict:
    conf = sanitize(max_value, k)
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return conf
