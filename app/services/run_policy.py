"""运行策略（模型开关四档）：服务器本地文件 `secrets/_run_policy.local.yaml`（现读现用）。

字段就是内核的四个开关：submit 时整份快照进 `run.policy`（台账），执行时原样进内核 argv。
- `decider`：jev=Jev 决策（默认）/ llm=大模型代班（快路径不开思考）/ off=关闭决策（只回放缓存）
- `supervisor`：大模型总闸。on=监督升级 + 字段取值（默认）；off=升级点直接停、TYPE_TEXT 只认 --var
- `cache`：回放缓存。on=命中免模型 + 断言确认后录制（默认）；off=本次运行不读写缓存
- `cache_strict`：严判。命中的记录必须状态签名一致，否则按失配交还

顶栏两模型开关的落点（四档）：
全开 = decider=jev + supervisor=on；只开 Jev = jev + off；只开大模型 = llm + on；全关 = off + off。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.core.config import get_settings

DECIDERS = ("jev", "llm", "off")
FILE_NAME = "_run_policy.local.yaml"


def store_path() -> Path:
    return get_settings().secrets_dir / FILE_NAME


def defaults() -> dict:
    return {"decider": "jev", "supervisor": True, "cache": True, "cache_strict": False}


def _as_bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "on", "yes"):
            return True
        if text in ("false", "0", "off", "no"):
            return False
    return fallback


def normalize(data: dict | None) -> dict:
    """把任意来源（文件 / 台账快照）夹成合法策略：坏值回落默认，缺字段补默认。"""
    data = data if isinstance(data, dict) else {}
    fallback = defaults()
    decider = data.get("decider")
    return {
        "decider": decider if decider in DECIDERS else fallback["decider"],
        "supervisor": _as_bool(data.get("supervisor"), fallback["supervisor"]),
        "cache": _as_bool(data.get("cache"), fallback["cache"]),
        "cache_strict": _as_bool(data.get("cache_strict"), fallback["cache_strict"]),
    }


def load() -> dict:
    path = store_path()
    if not path.exists():
        return defaults()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return defaults()
    return normalize(data)


def save(decider, supervisor, cache, cache_strict) -> dict:
    conf = normalize({"decider": decider, "supervisor": supervisor,
                      "cache": cache, "cache_strict": cache_strict})
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return conf


def kernel_args(policy: dict | None) -> list[str]:
    """策略 → 内核 `run` 子命令开关；老台账没有快照时按默认全开显式透传。"""
    conf = normalize(policy)
    args = ["--decider", conf["decider"],
            "--supervisor", "on" if conf["supervisor"] else "off",
            "--cache", "on" if conf["cache"] else "off"]
    if conf["cache_strict"]:
        args.append("--cache-strict")
    return args
