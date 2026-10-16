"""登录态 profile 管理：列清单 / 清单个 / 清全部 / 路径穿越守卫。"""

from __future__ import annotations

import shutil

from app.core.config import get_settings
from app.core.errors import ApiError
from app.services import profile_service


def _profiles_root():
    return get_settings().artifacts_dir / "chrome-profiles"


def _clean_all():
    root = _profiles_root()
    if root.is_dir():
        shutil.rmtree(root)


def _make_slot(slot: str, files: list[str] | None = None):
    root = _profiles_root()
    target = root / slot
    target.mkdir(parents=True, exist_ok=True)
    for name in (files or ["Cookies", "Login Data"]):
        (target / name).write_text("fake-data", encoding="utf-8")
    return target


def test_list_empty():
    _clean_all()
    assert profile_service.list_slots() == []


def test_list_and_clear_slot():
    _clean_all()
    _make_slot("u0/lane-0", ["Cookies", "Login Data"])
    _make_slot("u0/lane-1", ["Cookies"])
    _make_slot("u1/lane-0")

    slots = profile_service.list_slots()
    assert len(slots) == 3
    names = {s["slot"] for s in slots}
    assert names == {"u0/lane-0", "u0/lane-1", "u1/lane-0"}

    result = profile_service.clear_slot("u0/lane-0")
    assert result["cleared"] is True
    assert result["freed"] > 0

    remaining = profile_service.list_slots()
    assert len(remaining) == 2
    assert all(s["slot"] != "u0/lane-0" for s in remaining)


def test_clear_slot_not_found():
    _clean_all()
    try:
        profile_service.clear_slot("u99/lane-99")
    except ApiError as exc:
        assert exc.status == 404
        assert exc.code == "slot_not_found"
    else:
        raise AssertionError("should have raised")


def test_clear_slot_bad_name():
    for bad in ("../etc", "/absolute", "trailing/", ""):
        try:
            profile_service.clear_slot(bad)
        except ApiError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f"should have raised for {bad!r}")


def test_clear_all():
    _clean_all()
    _make_slot("u0/lane-0")
    _make_slot("u0/lane-1")
    result = profile_service.clear_all()
    assert result["cleared"] == 2
    assert profile_service.list_slots() == []


def test_clear_all_empty():
    _clean_all()
    result = profile_service.clear_all()
    assert result["cleared"] == 0


def test_api_list_and_clear(client, auth, workspace):
    _clean_all()
    _make_slot("u0/lane-0", ["Cookies"])

    resp = client.get("/api/v1/profiles", headers=auth)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    resp = client.delete("/api/v1/profiles/u0/lane-0", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cleared"] is True

    resp = client.get("/api/v1/profiles", headers=auth)
    slots = [s["slot"] for s in resp.json()["items"]]
    assert "u0/lane-0" not in slots


def test_api_clear_all(client, auth, workspace):
    _clean_all()
    _make_slot("u0/lane-0")
    _make_slot("u1/lane-0")

    resp = client.delete("/api/v1/profiles", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 2
