"""Headed login into the persistent Chromium profile for Goofish.

Usage (from project root, with display / X11 / VNC):

    source .venv/bin/activate
    python -m app.scripts.login_goofish

Opens https://www.goofish.com/ with GOOFISH_USER_DATA_DIR (default
data/browser_profile/). Log in manually in the window. Script waits until
a personal / nick marker appears, or until timeout, then exits and keeps
the profile on disk for headless scheduled scrapes.

Cloud / headless servers: run this once on a machine with a display
(or SSH -X / VNC), then copy data/browser_profile/ to the server, or
mount the same directory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root on sys.path when run as module
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from app.collectors.goofish_profile import (  # noqa: E402
    GOOFISH_HOME,
    LOGIN_MARKERS,
    PERSONAL_HINTS,
    acquire_profile_lock,
    ensure_user_data_dir,
    user_data_dir,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("login_goofish")


async def _wait_logged_in(page, timeout_s: float) -> bool:
    """Poll until personal markers appear and login CTA fades, or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        url = page.url or ""
        body = ""
        try:
            body = await page.locator("body").inner_text()
        except Exception:  # noqa: BLE001
            pass
        snip = body[:1200]
        has_personal = any(h in snip for h in PERSONAL_HINTS)
        has_login = any(m in snip for m in LOGIN_MARKERS)
        # nick-ish selectors
        nick = None
        for sel in (
            "[class*='nick']",
            "[class*='userName']",
            "[class*='user-name']",
            "a[href*='personal']",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    t = (await loc.inner_text()).strip()
                    if t and t not in LOGIN_MARKERS and len(t) < 40:
                        nick = t
                        break
            except Exception:  # noqa: BLE001
                continue

        if nick or (has_personal and not has_login):
            logger.info("检测到已登录%s", f"（{nick}）" if nick else "")
            return True
        if "login.taobao" not in url and "login.goofish" not in url and not has_login:
            # ambiguous but no login CTA — accept
            if has_personal:
                logger.info("检测到个人入口，视为已登录")
                return True
        await asyncio.sleep(2)
    return False


async def run_login(*, timeout_s: float, start_url: str) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("未安装 playwright：pip install playwright && playwright install chromium")
        return 2

    udd = ensure_user_data_dir()
    # Force headed for interactive login
    os.environ["GOOFISH_HEADLESS"] = "false"
    logger.info("打开有头浏览器，配置目录：%s", udd)
    logger.info("请在弹出窗口中登录闲鱼；成功后脚本会自动检测并退出（超时 %.0fs）", timeout_s)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(udd),
            headless=False,
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(start_url, wait_until="domcontentloaded")
            ok = await _wait_logged_in(page, timeout_s)
            if ok:
                # Brief pause so cookies flush to disk
                await asyncio.sleep(1.5)
                logger.info("登录成功，会话已写入 %s", udd)
                logger.info(
                    "接下来：export COLLECTOR_BACKEND=playwright && "
                    "unset GOOFISH_FIXTURE_PATH && 启动 uvicorn；调度器将用此配置真扫"
                )
                return 0
            logger.error(
                "超时未检测到登录。可稍后重试，或确认本机有图形界面（云主机需 VNC/有头一次）。"
            )
            return 1
        finally:
            await context.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="闲鱼持久化配置登录（有头浏览器）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("GOOFISH_LOGIN_TIMEOUT_S", "300")),
        help="等待登录秒数（默认 300）",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("GOOFISH_LOGIN_URL", GOOFISH_HOME),
        help="起始 URL",
    )
    args = parser.parse_args(argv)
    logger.info("GOOFISH_USER_DATA_DIR=%s", user_data_dir())
    return asyncio.run(run_login(timeout_s=args.timeout, start_url=args.url))


if __name__ == "__main__":
    raise SystemExit(main())
