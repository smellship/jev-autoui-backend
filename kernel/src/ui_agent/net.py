"""共享 HTTP 工具：两个模型调用共用同一套重试与错误语义。"""

from __future__ import annotations

import time

import httpx

CLIENT = httpx.Client(http2=True, timeout=60)


def post_json(url: str, key: str, body: dict, retries: int = 3, label: str = "模型",
              auth_mode: str = "bearer") -> dict:
    """传输层错误（DNS、连接被重置、超时）重试；HTTP 错误里只有 429/529/503 值得重试。"""
    if auth_mode == "appcode":
        auth_header = f"APPCODE {key}"
    else:
        auth_header = f"Bearer {key}"
    for attempt in range(retries):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": auth_header})
        except httpx.HTTPError as exc:
            if attempt < retries - 1:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(f"{label}连接失败（{exc.__class__.__name__}）；未执行任何动作。") from None
        if response.status_code in {429, 529, 503} and attempt < retries - 1:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            # 带上服务端响应体摘要：400/422 的原文是唯一能把"为什么被拒"查清楚的东西
            detail = " ".join((response.text or "").split())[:300]
            suffix = f" 服务端响应：{detail}" if detail else ""
            raise RuntimeError(f"{label}返回 HTTP {response.status_code}；未执行任何动作。{suffix}")
        return response.json()
    raise RuntimeError(f"{label}不可用")
