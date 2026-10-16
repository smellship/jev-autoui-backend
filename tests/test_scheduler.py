"""调度器：派发条件（max/k/互斥双向）、动态设置、路号与 profile 目录（内核起桩前的纯线程测试）。

execute 被替换成可控假实现（gate 门闩），不落库、不起内核。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.services import run_service, run_settings


def wait_until(predicate, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def meta(user_id: int | None = 1, env_id: int | None = 1, mutex: str = "none",
         key: str | None = None) -> dict:
    return {"run_id": 0, "user_id": user_id, "env_id": env_id, "mutex": mutex, "mutex_key": key}


def ns_run(run_id: int, **kw) -> SimpleNamespace:
    row = {"user_id": 1, "env_id": 1, "mutex": "none", "mutex_key": None}
    row.update(kw)
    return SimpleNamespace(id=run_id, **row)


@pytest.fixture(autouse=True)
def clean_run_settings():
    """本模块单独操控设置文件；用完删掉，避免影响其他模块（默认 max=1/k=1）。"""
    path = run_settings.store_path()
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


class FakeExec:
    """可控 execute：登记开始/结束、统计峰值并发；设了 gate 的 run 阻塞到 release。"""

    def __init__(self):
        self.started: list[tuple[int, str]] = []
        self.finished: list[int] = []
        self.gates: dict[int, threading.Event] = {}
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, run_id: int, profile_dir=None) -> None:
        with self._lock:
            self.started.append((run_id, str(profile_dir or "")))
            self.active += 1
            self.peak = max(self.peak, self.active)
        gate = self.gates.get(run_id)
        if gate is not None:
            gate.wait(5)
        with self._lock:
            self.finished.append(run_id)
            self.active -= 1

    def ids(self) -> list[int]:
        with self._lock:
            return [run_id for run_id, _ in self.started]

    def release(self, run_id: int) -> None:
        self.gates.setdefault(run_id, threading.Event()).set()


@pytest.fixture
def fake_exec() -> FakeExec:
    return FakeExec()


def test_mutex_conflict_matrix():
    env7, env8 = dict(env_id=7), dict(env_id=8)
    # ① R=环境挡，C 无挡：同 env 冲突、异 env 不冲突
    assert run_service.mutex_conflict(meta(**env7), meta(mutex="env", **env7))
    assert not run_service.mutex_conflict(meta(**env8), meta(mutex="env", **env7))
    # ③ C=环境挡（不看 R 挡）
    assert run_service.mutex_conflict(meta(mutex="env", **env7), meta(**env7))
    # 无挡对无挡：永不冲突（键相等也不冲突，键只是给互斥方对照的）
    assert not run_service.mutex_conflict(meta(key="K"), meta(key="K"))
    # ② R=账号挡 且键相等（C 哪怕无挡）
    assert run_service.mutex_conflict(meta(key="K"), meta(mutex="account", key="K"))
    # ④ C=账号挡 且键相等（R 哪怕无挡）
    assert run_service.mutex_conflict(meta(mutex="account", key="K"), meta(key="K"))
    assert not run_service.mutex_conflict(meta(mutex="account", key="K1"), meta(key="K2"))
    # 账号挡无键 → 降级环境挡
    assert run_service.mutex_conflict(meta(mutex="account", key=None, **env7), meta(**env7))
    assert not run_service.mutex_conflict(meta(mutex="account", key=None, **env7), meta(**env8))


def test_eligible_respects_max_k_and_mutex():
    loose = {"max": 3, "k": 2}
    cand = meta(user_id=1)
    assert run_service.eligible(cand, [meta(user_id=2), meta(user_id=3)], loose)
    assert not run_service.eligible(cand, [meta(user_id=1), meta(user_id=1)], loose)  # k=2 已满
    assert not run_service.eligible(cand, [meta(user_id=2)] * 3, loose)  # 全局 max 已满
    env7 = dict(env_id=7)
    env8 = dict(env_id=8)
    assert not run_service.eligible(meta(mutex="env", **env7), [meta(**env7)], loose)
    assert run_service.eligible(meta(**env7), [meta(mutex="account", key="K", **env8)], loose)


def test_queue_serial_by_default(fake_exec):
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        for run_id in (1, 2, 3):
            queue.submit(ns_run(run_id, user_id=(1 if run_id < 3 else 2)))
        assert wait_until(lambda: len(fake_exec.finished) == 3)
    finally:
        queue.stop()
    assert fake_exec.ids() == [1, 2, 3], "默认 max=1：严格 FIFO 串行"
    assert fake_exec.peak == 1


def test_same_user_lanes_get_distinct_profiles(fake_exec):
    run_settings.save(2, 2)
    fake_exec.gates[1] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1))
        queue.submit(ns_run(2))
        assert wait_until(lambda: len(fake_exec.ids()) == 2), "k=2 时同一人两路并行"
        assert fake_exec.peak == 2
        dirs = {path for _, path in fake_exec.started}
        assert len(dirs) == 2 and all(f"u1{chr(47)}lane-" in path.replace("\\", "/") for path in dirs), dirs
        assert fake_exec.started[0][1] != fake_exec.started[1][1], "并发两路必须用不同 profile 目录"
    finally:
        fake_exec.release(1)
        fake_exec.release(2)
        queue.stop()


def test_k_one_serializes_same_user_even_with_max_two(fake_exec):
    run_settings.save(2, 1)
    fake_exec.gates[1] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, user_id=1))
        queue.submit(ns_run(2, user_id=1))
        queue.submit(ns_run(3, user_id=2))
        assert wait_until(lambda: set(fake_exec.ids()) == {1, 3}), "异人可并行，同人第二路要等"
        assert 2 not in fake_exec.ids()
        fake_exec.release(1)
        assert wait_until(lambda: 2 in fake_exec.ids())
    finally:
        for run_id in (1, 2, 3):
            fake_exec.release(run_id)
        queue.stop()


def test_mutex_env_blocks_later_run_same_env(fake_exec):
    run_settings.save(2, 2)
    for run_id in (1, 2, 3):
        fake_exec.gates[run_id] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, env_id=7, mutex="env"))
        queue.submit(ns_run(2, env_id=7))  # 后到者无挡：前面同 env 的互斥运行让它排队
        queue.submit(ns_run(3, env_id=8))
        assert wait_until(lambda: set(fake_exec.ids()) == {1, 3})
        assert 2 not in fake_exec.ids(), "同环境互斥命中，2 号必须排队"
        fake_exec.release(1)
        assert wait_until(lambda: 2 in fake_exec.ids())
    finally:
        for run_id in (1, 2, 3):
            fake_exec.release(run_id)
        queue.stop()


def test_mutex_account_key_hits_both_directions(fake_exec):
    run_settings.save(3, 3)
    for run_id in (1, 2, 4):
        fake_exec.gates[run_id] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, mutex="none", mutex_key="K1"))       # 在跑：无挡但有账号键
        queue.submit(ns_run(2, mutex="account", mutex_key="K1"))   # 互斥方后来：键命中 → 等
        queue.submit(ns_run(3, mutex="account", mutex_key="K2"))   # 键不同 → 可并行
        assert wait_until(lambda: set(fake_exec.ids()) == {1, 3})
        assert 2 not in fake_exec.ids()
        fake_exec.release(1)
        assert wait_until(lambda: 2 in fake_exec.ids())
    finally:
        for run_id in (1, 2, 3, 4):
            fake_exec.release(run_id)
        queue.stop()


def test_account_without_key_downgrades_to_env_scope(fake_exec):
    run_settings.save(2, 2)
    for run_id in (1, 2):
        fake_exec.gates[run_id] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, env_id=7, mutex="account", mutex_key=None))  # 降级环境挡
        queue.submit(ns_run(2, env_id=7))
        queue.submit(ns_run(3, env_id=8))
        assert wait_until(lambda: set(fake_exec.ids()) == {1, 3})
        assert 2 not in fake_exec.ids()
    finally:
        for run_id in (1, 2, 3):
            fake_exec.release(run_id)
        queue.stop()


def test_dynamic_max_raise_wakes_waiting(fake_exec):
    run_settings.save(1, 1)
    fake_exec.gates[1] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, user_id=1))
        queue.submit(ns_run(2, user_id=2))
        assert wait_until(lambda: 1 in fake_exec.ids())
        assert 2 not in fake_exec.ids(), "max=1：第二路要等"
        run_settings.save(2, 1)
        queue.wake()
        assert wait_until(lambda: 2 in fake_exec.ids()), "调大 max 后唤醒重派"
    finally:
        fake_exec.release(1)
        fake_exec.release(2)
        queue.stop()


def test_discard_removes_waiting_run(fake_exec):
    fake_exec.gates[1] = threading.Event()
    queue = run_service.RunQueue(pool_size=4, executor=fake_exec)
    queue.start()
    try:
        queue.submit(ns_run(1, user_id=1))
        queue.submit(ns_run(2, user_id=2))
        assert wait_until(lambda: 1 in fake_exec.ids())
        queue.discard(2)  # 用户在排队期间停止
        fake_exec.release(1)
        assert wait_until(lambda: queue.pending() == 0 and queue.active() == 0)
    finally:
        queue.stop()
    assert 2 not in fake_exec.ids()


def test_profile_dir_shape():
    path = run_service.profile_dir_for(7, 2)
    parts = path.parts[-3:]
    assert parts == ("chrome-profiles", "u7", "lane-2"), path
    assert run_service.profile_dir_for(None, 0).parts[-2:] == ("u0", "lane-0")
