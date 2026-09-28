"""OpenAI 兼容文本模型客户端：planner / textfill / supervisor 三个缝共用；决策一律由 Jev 做。"""

from __future__ import annotations

import json
import time

from ui_agent.config import Settings
from ui_agent.net import post_json


def reasoning_options(base_url: str, mode: str) -> dict:
    """不同网关关闭思考模式的字段名不一致，统一在这里映射。"""
    if mode != "none":
        return {}
    if "deepseek" in base_url and "dashscope" not in base_url:
        return {"thinking": {"type": "disabled"}}
    return {"reasoning": {"enabled": False}}


def chat_json(settings: Settings, system: str, user: dict, max_tokens: int = 1024) -> tuple[dict, dict]:
    """返回 (JSON 对象, 调用元信息)；任何一步不合规都直接抛错，绝不落到"猜一个值"。"""
    if not settings.text_model_api_key:
        raise RuntimeError("缺少 TEXT_MODEL_API_KEY；请在 .env 里补上后再调用文本模型。")
    base = settings.text_model_base_url.rstrip("/")
    url = base + "/chat/completions"
    reasoning = reasoning_options(base, settings.text_model_reasoning)
    body = {
        "model": settings.text_model,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **reasoning,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }
    started = time.perf_counter()
    note = ""
    try:
        result = post_json(url, settings.text_model_api_key, body, label="文本模型")
    except RuntimeError as exc:
        # 各家网关"关思考"的字段名不统一；被拒就摘掉该字段重试一次（400 无计费副作用）
        if not reasoning or "HTTP 400" not in str(exc):
            raise
        for field in reasoning:
            body.pop(field, None)
        result = post_json(url, settings.text_model_api_key, body, label="文本模型")
        note = "网关不接受思考开关字段，已去掉该字段重试"
    meta = {
        "model": settings.text_model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
        "reasoning": note or settings.text_model_reasoning,
    }
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("文本模型响应缺少内容；未执行任何动作。") from None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("文本模型未返回 JSON；未执行任何动作。") from None
    if not isinstance(data, dict):
        raise RuntimeError("文本模型未返回 JSON 对象；未执行任何动作。")
    return data, meta
