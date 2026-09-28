"""共享 HTTP 工具：两个模型调用共用同一套重试与错误语义。"""

from __future__ import annotations

import time

import httpx

CLIENT = httpx.Client(http2=True, timeout=60)


def post_json(url: str, key: str, body: dict, retries: int = 3, label: str = "模型") -> dict:
    """传输层错误（DNS、连接被重置、超时）重试；HTTP 错误里只有 429/529/503 值得重试。"""
    for attempt in range(retries):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            if attempt < retries - 1:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(f"{label}连接失败（{exc.__class__.__name__}）；未执行任何动作。") from None
        if response.status_code in {429, 529, 503} and attempt < retries - 1:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"{label}返回 HTTP {response.status_code}；未执行任何动作。")
        return response.json()
    raise RuntimeError(f"{label}不可用")
