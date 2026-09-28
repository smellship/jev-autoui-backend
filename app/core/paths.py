"""产物路径白名单：所有对外暴露的文件访问都必须落在给定根目录内（§12 路径穿越）。"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import ApiError


def safe_join(root: Path, rel: str) -> Path:
    """把相对路径映射到 root 下；绝对路径、`..` 越界一律 400。"""
    root = Path(root).resolve()
    candidate = (root / str(rel or "")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ApiError(400, "path_denied", "路径不在白名单目录内", {"path": rel})
    return candidate
