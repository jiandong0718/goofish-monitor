"""Real push: 企业微信 / generic webhook + optional Telegram."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
VERDICT_RECOMMEND = "推荐"
VERDICT_WATCH = "待观察"


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def webhook_url() -> str:
    return (os.getenv("PUSH_WEBHOOK_URL") or "").strip()


def telegram_token() -> str:
    return (os.getenv("PUSH_TELEGRAM_BOT_TOKEN") or "").strip()


def telegram_chat_id() -> str:
    return (os.getenv("PUSH_TELEGRAM_CHAT_ID") or "").strip()


def include_watch() -> bool:
    return _truthy("PUSH_INCLUDE_WATCH", False)


def webhook_configured() -> bool:
    return bool(webhook_url())


def telegram_configured() -> bool:
    return bool(telegram_token() and telegram_chat_id())


def any_channel_configured() -> bool:
    return webhook_configured() or telegram_configured()


def push_status() -> dict[str, Any]:
    """UI-safe status (no secrets)."""
    wh = webhook_url()
    host = ""
    if wh:
        try:
            host = urlparse(wh).netloc or ""
        except Exception:  # noqa: BLE001
            host = ""
    return {
        "webhook_configured": webhook_configured(),
        "webhook_host": host or None,
        "telegram_configured": telegram_configured(),
        "include_watch": include_watch(),
        "any_configured": any_channel_configured(),
    }


def should_push_verdict(verdict: Optional[str]) -> bool:
    if verdict == VERDICT_RECOMMEND:
        return True
    if verdict == VERDICT_WATCH and include_watch():
        return True
    return False


def format_push_text(
    *,
    title: str,
    price: Any,
    reason: Optional[str],
    url: Optional[str],
    verdict: Optional[str] = None,
) -> str:
    price_s = "未知"
    if price is not None:
        try:
            price_s = f"¥{float(price):.0f}" if float(price) == int(float(price)) else f"¥{float(price):.2f}"
        except (TypeError, ValueError):
            price_s = str(price)
    lines = [
        f"【闲鱼监控 · {verdict or VERDICT_RECOMMEND}】",
        f"标题：{title or '（无标题）'}",
        f"价格：{price_s}",
    ]
    if reason:
        lines.append(f"理由：{reason}")
    if url:
        lines.append(f"链接：{url}")
    return "\n".join(lines)


def _is_wecom_webhook(url: str) -> bool:
    u = url.lower()
    return "qyapi.weixin.qq.com" in u or "weixin.qq.com" in u


def _post_json(url: str, payload: dict[str, Any]) -> None:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()


def send_webhook(
    *,
    text: str,
    title: str,
    price: Any,
    reason: Optional[str],
    url: Optional[str],
    verdict: Optional[str],
    listing_id: Optional[int] = None,
) -> bool:
    """POST to PUSH_WEBHOOK_URL. WeCom robot uses markdown/text; else generic JSON."""
    target = webhook_url()
    if not target:
        return False
    if _is_wecom_webhook(target):
        # 企业微信群机器人（text 最稳妥）
        payload = {
            "msgtype": "text",
            "text": {"content": text[:2048]},
        }
    else:
        payload = {
            "title": title,
            "body": text,
            "price": price,
            "reason": reason,
            "url": url,
            "verdict": verdict,
            "listing_id": listing_id,
            "msgtype": "text",
            "text": {"content": text[:4096]},
        }
    _post_json(target, payload)
    logger.info("[push] webhook ok host=%s", urlparse(target).netloc)
    return True


def send_telegram(*, text: str) -> bool:
    token = telegram_token()
    chat_id = telegram_chat_id()
    if not token or not chat_id:
        return False
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": False,
    }
    _post_json(api, payload)
    logger.info("[push] telegram ok chat_id=%s", chat_id)
    return True


def deliver_push(
    *,
    title: str,
    price: Any = None,
    reason: Optional[str] = None,
    url: Optional[str] = None,
    verdict: Optional[str] = None,
    listing_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Send to all configured channels.
    Returns {ok, channels: [...], errors: [...], text}.
    """
    text = format_push_text(
        title=title or "",
        price=price,
        reason=reason,
        url=url,
        verdict=verdict,
    )
    channels: list[str] = []
    errors: list[str] = []

    if webhook_configured():
        try:
            send_webhook(
                text=text,
                title=title or "",
                price=price,
                reason=reason,
                url=url,
                verdict=verdict,
                listing_id=listing_id,
            )
            channels.append("webhook")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[push] webhook failed: %s", exc)
            errors.append(f"webhook:{exc}")

    if telegram_configured():
        try:
            send_telegram(text=text)
            channels.append("telegram")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[push] telegram failed: %s", exc)
            errors.append(f"telegram:{exc}")

    return {
        "ok": bool(channels),
        "channels": channels,
        "errors": errors,
        "text": text,
    }


async def push_notification(
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """
    Backward-compatible entry: title + body (+ optional payload fields).
    Prefer deliver_push / maybe_push_evaluation for listing pushes.
    """
    payload = payload or {}
    result = deliver_push(
        title=title,
        price=payload.get("price"),
        reason=body or payload.get("reason"),
        url=payload.get("url"),
        verdict=payload.get("verdict"),
        listing_id=payload.get("listing_id"),
    )
    return bool(result.get("ok"))
