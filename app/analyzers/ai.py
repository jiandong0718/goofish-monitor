"""AI analyzer — OpenAI-compatible chat for seller profiling + 中文 reason."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

ANALYZER_NAME = "ai"

SELLER_PROFILES = ("个人", "疑似商家", "不确定")
VERDICTS = ("推荐", "待观察", "不推荐")

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT = 45.0


def ai_configured() -> bool:
    """True when AI_API_KEY is set (non-empty)."""
    return bool((os.getenv("AI_API_KEY") or "").strip())


def ai_always() -> bool:
    return (os.getenv("AI_ALWAYS") or "").strip().lower() in ("1", "true", "yes", "on")


def should_run_ai(rules_verdict: Optional[str]) -> bool:
    """
    AI runs when (a) rules 推荐 or 待观察, or (b) AI_ALWAYS=true.
    Do not burn tokens on clear hard-fail (不推荐) unless AI_ALWAYS.
    """
    if not ai_configured():
        return False
    if ai_always():
        return True
    return rules_verdict in ("推荐", "待观察")


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _listing_get(listing: Any, key: str, default: Any = None) -> Any:
    if listing is None:
        return default
    if isinstance(listing, dict):
        return listing.get(key, default)
    return getattr(listing, key, default)


def _raw_snippet(listing: Any, limit: int = 800) -> str:
    raw = _listing_get(listing, "raw") or {}
    if not isinstance(raw, dict):
        return str(raw)[:limit]
    parts: list[str] = []
    for k in ("desc", "description", "subtitle", "note", "seller_nick", "seller", "location"):
        if raw.get(k):
            parts.append(f"{k}: {raw[k]}")
    # fallback: compact JSON
    if not parts:
        try:
            return json.dumps(raw, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(raw)[:limit]
    return " | ".join(parts)[:limit]


def _build_prompt(
    listing: Any,
    config: Any,
    rules_result: dict[str, Any],
) -> list[dict[str, str]]:
    hard = dict(_cfg_get(config, "hard_requirements") or {})
    soft = dict(_cfg_get(config, "soft_preferences") or {})
    # surface common ProductConfig columns into the prompt
    for key in (
        "brand",
        "model",
        "size",
        "ram",
        "storage",
        "price_min",
        "price_max",
        "condition_min",
        "battery_health_min",
        "seller_preference",
        "exclude_keywords",
    ):
        val = _cfg_get(config, key)
        if val is not None and val != "" and val != []:
            if key in ("ram", "storage", "condition_min", "battery_health_min") and key not in soft:
                soft[key] = val
            elif key not in hard and key not in (
                "ram",
                "storage",
                "condition_min",
                "battery_health_min",
            ):
                hard[key] = val

    payload = {
        "listing": {
            "title": _listing_get(listing, "title"),
            "price": _listing_get(listing, "price"),
            "seller_type": _listing_get(listing, "seller_type"),
            "condition": _listing_get(listing, "condition"),
            "raw_snippet": _raw_snippet(listing),
        },
        "hard_requirements": hard,
        "soft_preferences": soft,
        "rules": {
            "verdict": rules_result.get("verdict"),
            "tags": rules_result.get("tags") or [],
            "reason": rules_result.get("reason"),
            "passed_hard": rules_result.get("passed_hard"),
        },
    }

    system = (
        "你是闲鱼二手商品评估助手。根据给定 JSON（标题/价格/卖家线索、硬软条件、规则结论），"
        "输出严格 JSON 对象，不要 markdown，不要多余文字。字段：\n"
        '- verdict_adjust: 可选，"推荐"|"待观察"|"不推荐"|null（仅当规则结论需微调时填写）\n'
        "- tags: 字符串数组（短标签，如「疑似商家」「成色存疑」）\n"
        "- reason: 中文一到三句，像评估卡片文案，说明为何推荐/待观察/不推荐\n"
        '- seller_profile: 必须是 "个人"|"疑似商家"|"不确定" 之一\n'
        "结合卖家类型字段、标题话术（大量库存/包邮专营等）判断 seller_profile。"
    )
    user = (
        "请评估以下商品并只返回 JSON：\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")
    # strip ```json fences if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # try first {...} blob
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("model JSON is not an object")
    return data


def _normalize_ai_payload(data: dict[str, Any], rules_result: dict[str, Any]) -> dict[str, Any]:
    verdict_adjust = data.get("verdict_adjust")
    if verdict_adjust in ("", "null", None):
        verdict_adjust = None
    if verdict_adjust is not None and verdict_adjust not in VERDICTS:
        verdict_adjust = None

    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(t) for t in tags if t is not None and str(t).strip()]

    reason = str(data.get("reason") or "").strip()
    if not reason:
        reason = rules_result.get("reason") or "AI 未给出说明"

    seller_profile = str(data.get("seller_profile") or "不确定").strip()
    if seller_profile not in SELLER_PROFILES:
        # light normalize
        low = seller_profile.lower()
        if "商" in seller_profile or "shop" in low or "store" in low:
            seller_profile = "疑似商家"
        elif "个" in seller_profile or "personal" in low:
            seller_profile = "个人"
        else:
            seller_profile = "不确定"

    final_verdict = verdict_adjust or rules_result.get("verdict")
    return {
        "analyzer": ANALYZER_NAME,
        "verdict": final_verdict,
        "verdict_adjust": verdict_adjust,
        "tags": tags,
        "reason": reason,
        "seller_profile": seller_profile,
        "score": rules_result.get("score"),
        "passed_hard": rules_result.get("passed_hard"),
        "soft_score": rules_result.get("soft_score"),
        "details": {
            "seller_profile": seller_profile,
            "verdict_adjust": verdict_adjust,
            "rules_verdict": rules_result.get("verdict"),
            "raw_ai": {
                k: data.get(k)
                for k in ("verdict_adjust", "tags", "reason", "seller_profile")
            },
        },
    }


def _chat_completion(messages: list[dict[str, str]]) -> str:
    api_key = (os.getenv("AI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("AI_API_KEY not set")
    base = (os.getenv("AI_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    model = (os.getenv("AI_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    url = f"{base}/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected chat response shape: {data!r}") from exc


def evaluate_ai(
    listing: Any,
    config: Any = None,
    *,
    rules_result: dict[str, Any] | None = None,
) -> Optional[dict[str, Any]]:
    """
    Call OpenAI-compatible chat API; return normalized evaluation dict.

    If AI_API_KEY is missing, log and return None (caller keeps rules-only).
    On HTTP/parse errors, log and return None (do not fail the pipeline).
    """
    if not ai_configured():
        logger.info("[ai] AI_API_KEY unset — skip AI analyzer (rules-only)")
        return None

    rules_result = rules_result or {}
    try:
        messages = _build_prompt(listing, config, rules_result)
        content = _chat_completion(messages)
        parsed = _extract_json(content)
        result = _normalize_ai_payload(parsed, rules_result)
        logger.info(
            "[ai] listing=%s seller_profile=%s verdict=%s",
            _listing_get(listing, "external_id") or _listing_get(listing, "title"),
            result.get("seller_profile"),
            result.get("verdict"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — never break rules pipeline
        logger.warning("[ai] evaluate failed: %s", exc)
        return None


# Keep async alias for callers that prefer await (wraps sync httpx).
async def evaluate_ai_async(
    listing: Any,
    config: Any = None,
    *,
    rules_result: dict[str, Any] | None = None,
) -> Optional[dict[str, Any]]:
    return evaluate_ai(listing, config, rules_result=rules_result)
