"""OpenAI 兼容文本模型客户端：planner / textfill / supervisor 三个缝共用；决策一律由 Jev 做。"""

from __future__ import annotations

import json
import re
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


def _close_truncated(text: str) -> str | None:
    """把被截断的 JSON 补全：字符串没收尾补引号、括号没配对补闭合符。

    只修"尾部被切掉"这一种形态；截断点落在数字中间（7 可能是 79 被切）无法判别，拒绝修。
    本来完整、或括号交叉等非截断形态，返回 None。
    """
    stack: list[str] = []
    in_string = escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != ("{" if ch == "}" else "["):
                return None
            stack.pop()
    if not in_string and not stack:
        return None
    if not in_string and re.search(r"\d[\d.eE+-]*$", text):
        return None
    if in_string:
        text += '"'
    return text + "".join("}" if op == "{" else "]" for op in reversed(stack))


def _try_repair_json(content: str) -> dict | list | None:
    """尝试修复常见的 JSON 格式错误（多余括号、截断等）。失败返回 None。"""
    if not content:
        return None
    text = content.strip()
    for _ in range(10):
        try:
            result = json.loads(text)
            if isinstance(result, (dict, list)):
                return result
        except json.JSONDecodeError:
            pass
        if text.endswith("}"):
            text = text[:-1].rstrip()
        elif text.endswith("]"):
            text = text[:-1].rstrip()
        else:
            break
    closed = _close_truncated(text)
    if closed is not None:
        try:
            result = json.loads(closed)
            if isinstance(result, (dict, list)):
                return result
        except json.JSONDecodeError:
            pass
    return None


def chat_json(settings: Settings, system: str, user: dict, max_tokens: int = 1024,
              reasoning: str | None = None) -> tuple[dict, dict]:
    """返回 (JSON 对象, 调用元信息)；任何一步不合规都直接抛错，绝不落到"猜一个值"。

    reasoning 覆盖 settings.text_model_reasoning（代班快路径固定 none、思考路径固定 enabled）；
    None = 用配置值。
    """
    if not settings.text_model_api_key:
        raise RuntimeError("缺少 TEXT_MODEL_API_KEY；请在 .env 里补上后再调用文本模型。")
    base = settings.text_model_base_url.rstrip("/")
    url = base + "/chat/completions"
    mode = settings.text_model_reasoning if reasoning is None else reasoning
    reasoning_fields = reasoning_options(base, mode)
    body = {
        "model": settings.text_model,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **reasoning_fields,
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
        if not reasoning_fields or "HTTP 400" not in str(exc):
            raise
        for field in reasoning_fields:
            body.pop(field, None)
        result = post_json(url, settings.text_model_api_key, body, label="文本模型")
        note = "网关不接受思考开关字段，已去掉该字段重试"
    meta = {
        "model": settings.text_model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
        "reasoning": note or mode,
    }
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("文本模型响应缺少内容；未执行任何动作。") from None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        repaired = _try_repair_json(content)
        if repaired is not None:
            data = repaired
        else:
            preview = (content or "")[:2000]
            length = len(content or "")
            raise RuntimeError(
                f"文本模型未返回 JSON（长度 {length}，解析错误：{exc}）；未执行任何动作。实际返回：{preview}"
            ) from None
    if not isinstance(data, dict):
        preview = (content or "")[:2000]
        length = len(content or "")
        raise RuntimeError(
            f"文本模型未返回 JSON 对象（长度 {length}，类型 {type(data).__name__}）；"
            f"未执行任何动作。实际返回：{preview}"
        )
    return data, meta
