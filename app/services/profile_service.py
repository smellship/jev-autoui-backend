"""登录态 profile 管理：列清单 / 清单个 / 清全部。

profile 目录结构：artifacts/chrome-profiles/u{user_id}/lane-{lane}
slot 名 = "u0/lane-0" 这种两级相对路径。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.paths import safe_join


def _profiles_root() -> Path:
    return get_settings().artifacts_dir / "chrome-profiles"


def _dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    return total


def list_slots() -> list[dict]:
    root = _profiles_root()
    if not root.is_dir():
        return []
    items = []
    for user_dir in sorted(root.iterdir()):
        if not user_dir.is_dir():
            continue
        for lane_dir in sorted(user_dir.iterdir()):
            if not lane_dir.is_dir():
                continue
            slot = f"{user_dir.name}/{lane_dir.name}"
            items.append({
                "slot": slot,
                "size": _dir_size(lane_dir),
                "path": str(lane_dir),
            })
    return items


def clear_slot(slot: str) -> dict:
    if "/" not in slot or slot.startswith("/") or slot.endswith("/") or ".." in slot:
        raise ApiError(400, "bad_slot", "槽名格式不对，应为 u{数字}/lane-{数字}", {"slot": slot})
    root = _profiles_root()
    target = safe_join(root, slot)
    if not target.is_dir():
        raise ApiError(404, "slot_not_found", f"没有这个槽：{slot}", {"slot": slot})
    size = _dir_size(target)
    shutil.rmtree(target)
    return {"slot": slot, "cleared": True, "freed": size}


def clear_all() -> dict:
    root = _profiles_root()
    if not root.is_dir():
        return {"cleared": 0, "freed": 0}
    size = _dir_size(root)
    slots = list_slots()
    shutil.rmtree(root)
    return {"cleared": len(slots), "freed": size}
