"""Playwright persistent browser profile helpers for Goofish session.

Product path: Chromium user_data_dir under data/browser_profile/
(Cookie export is blocked in some environments; persistent profile keeps login.)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.db import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_USER_DATA_DIR = PROJECT_ROOT / "data" / "browser_profile"
GOOFISH_HOME = "https://www.goofish.com/"
LOGIN_MARKERS = ("登录", "請登录", "请登录", "马上登录", "立即登录")
PERSONAL_HINTS = ("个人中心", "我的闲鱼", "退出", "退出登录", "卖家中心")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def user_data_dir() -> Path:
    """Resolve GOOFISH_USER_DATA_DIR (default: data/browser_profile)."""
    raw = os.getenv("GOOFISH_USER_DATA_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p)
    return DEFAULT_USER_DATA_DIR


def ensure_user_data_dir() -> Path:
    path = user_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_mtime() -> Optional[datetime]:
    """Newest mtime under the profile dir (None if empty / missing)."""
    root = user_data_dir()
    if not root.is_dir():
        return None
    newest: Optional[float] = None
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name == ".gitkeep":
                    continue
                fp = Path(dirpath) / name
                try:
                    m = fp.stat().st_mtime
                except OSError:
                    continue
                if newest is None or m > newest:
                    newest = m
    except OSError:
        return None
    if newest is None:
        return None
    return datetime.fromtimestamp(newest)


def profile_has_content() -> bool:
    """True if profile dir has files beyond .gitkeep (session likely stored)."""
    root = user_data_dir()
    if not root.is_dir():
        return False
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name == ".gitkeep":
                continue
            return True
    return False


def last_collect_success_at() -> Optional[datetime]:
    """Last successful collect_done from run_logs (best-effort)."""
    try:
        from app.db import SessionLocal
        from app.models.tables import RunLog

        db = SessionLocal()
        try:
            row = (
                db.query(RunLog)
                .filter(RunLog.event == "collect_done", RunLog.level == "info")
                .order_by(RunLog.id.desc())
                .first()
            )
            return row.created_at if row else None
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[profile] last_collect_success_at failed: %s", exc)
        return None




import fcntl
from contextlib import contextmanager

_PROFILE_LOCK_PATH = None


def profile_lock_path() -> Path:
    return ensure_user_data_dir().parent / "browser_profile.lock"


@contextmanager
def acquire_profile_lock(timeout_s: float = 90.0, *, poll_s: float = 0.4):
    """Exclusive lock so only one Playwright persistent context uses the profile.

    Prefer ``await acquire_profile_lock_async`` from async request handlers so
    the uvicorn event loop is not blocked by ``time.sleep``.
    """
    path = profile_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    import time as _time
    start = _time.time()
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if _time.time() - start >= timeout_s:
                fh.close()
                raise TimeoutError(f"profile_locked: {path} after {timeout_s}s")
            _time.sleep(poll_s)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


async def acquire_profile_lock_async(timeout_s: float = 90.0, *, poll_s: float = 0.4):
    """Async lock helper: returns an entered context-manager-like release callback."""
    import asyncio
    import time as _time

    path = profile_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    start = _time.time()
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if _time.time() - start >= timeout_s:
                fh.close()
                raise TimeoutError(f"profile_locked: {path} after {timeout_s}s")
            await asyncio.sleep(poll_s)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()

    def release() -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    return release


async def probe_session(*, timeout_ms: int = 20000) -> dict[str, Any]:
    """
    Short Playwright check against goofish.com using persistent profile.
    Returns {logged_in, nickname?, url, method, error?}.
    Skips live probe when GOOFISH_FIXTURE_PATH set or GOOFISH_SKIP_PROBE=true.
    """
    if os.getenv("GOOFISH_FIXTURE_PATH", "").strip():
        return {
            "logged_in": None,
            "method": "skipped_fixture",
            "message": "fixture mode — live session probe skipped",
        }
    if _env_bool("GOOFISH_SKIP_PROBE", False):
        return {
            "logged_in": None,
            "method": "skipped_env",
            "message": "GOOFISH_SKIP_PROBE=true",
        }

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "logged_in": None,
            "method": "unavailable",
            "error": "playwright_missing",
            "message": "playwright not installed",
        }

    udd = ensure_user_data_dir()
    headless = _env_bool("GOOFISH_HEADLESS", True)
    result: dict[str, Any] = {
        "logged_in": False,
        "method": "playwright_probe",
        "user_data_dir": str(udd),
        "headless": headless,
    }

    try:
        with acquire_profile_lock(timeout_s=45.0):
          async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(udd),
                headless=headless,
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(timeout_ms)
                await page.goto(GOOFISH_HOME, wait_until="domcontentloaded")
                url = page.url or ""
                result["url"] = url
                body = ""
                try:
                    body = await page.locator("body").inner_text()
                except Exception:  # noqa: BLE001
                    pass
                body_snip = body[:800] if body else ""

                if "login.taobao" in url or "login.goofish" in url or "/login" in url:
                    result["logged_in"] = False
                    result["message"] = "redirected to login"
                    return result

                # Heuristic: personal nick / menu vs 登录 CTA
                has_login_cta = any(m in body_snip for m in LOGIN_MARKERS)
                has_personal = any(h in body_snip for h in PERSONAL_HINTS)

                nickname = None
                for sel in (
                    "[class*='nick']",
                    "[class*='userName']",
                    "[class*='user-name']",
                    "a[href*='personal']",
                    "a[href*='person']",
                ):
                    try:
                        loc = page.locator(sel).first
                        if await loc.count():
                            text = (await loc.inner_text()).strip()
                            if text and text not in LOGIN_MARKERS and len(text) < 40:
                                nickname = text
                                break
                    except Exception:  # noqa: BLE001
                        continue

                if nickname or has_personal:
                    result["logged_in"] = True
                    result["nickname"] = nickname
                    result["message"] = "session looks logged in"
                elif has_login_cta and not has_personal:
                    result["logged_in"] = False
                    result["message"] = "login CTA visible — not logged in"
                else:
                    # Profile exists but ambiguous — treat as maybe
                    result["logged_in"] = profile_has_content()
                    result["message"] = (
                        "ambiguous page markers; inferred from profile files"
                        if profile_has_content()
                        else "no clear session markers"
                    )
                return result
            finally:
                await context.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[profile] probe failed: %s", exc)
        result["logged_in"] = None
        result["error"] = "probe_failed"
        result["message"] = str(exc)[:300]
        return result


def session_status_summary(*, probe: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Combine file mtime / last success / optional probe into UI-friendly status."""
    mtime = profile_mtime()
    last_ok = last_collect_success_at()
    has_files = profile_has_content()
    udd = user_data_dir()

    logged_in: Optional[bool] = None
    nickname = None
    message = "尚未登录：请运行登录脚本写入持久化浏览器配置"
    state = "unknown"

    if probe:
        logged_in = probe.get("logged_in")
        nickname = probe.get("nickname")
        if logged_in is True:
            state = "logged_in"
            message = f"已登录{(' · ' + nickname) if nickname else ''}"
        elif logged_in is False:
            state = "login_required"
            message = probe.get("message") or "需要登录"
        else:
            state = "unknown"
            message = probe.get("message") or message
    elif has_files:
        state = "profile_present"
        logged_in = None
        message = "已有浏览器配置目录（未实时探测）；建议 dry-run 验证"
    else:
        state = "empty"
        logged_in = False
        message = "浏览器配置为空 — 请先本地运行登录脚本"

    def _fmt(dt: Optional[datetime]) -> Optional[str]:
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

    return {
        "state": state,
        "logged_in": logged_in,
        "nickname": nickname,
        "message": message.strip(),
        "user_data_dir": str(udd),
        "profile_has_content": has_files,
        "profile_mtime": _fmt(mtime),
        "last_collect_success_at": _fmt(last_ok),
        "collector_backend": os.getenv("COLLECTOR_BACKEND", "stub"),
        "headless": _env_bool("GOOFISH_HEADLESS", True),
        "fixture_path": os.getenv("GOOFISH_FIXTURE_PATH", "").strip() or None,
        "probe": probe,
    }
