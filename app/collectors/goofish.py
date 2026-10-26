"""Goofish (闲鱼) Playwright collector — integrable for 联调.

Strategy:
  - Persistent Chromium profile (GOOFISH_USER_DATA_DIR, default data/browser_profile/)
    is the product login path (Cookie export may be blocked)
  - Navigate to https://www.goofish.com/search?q={keyword}
  - Prefer intercepting XHR/fetch JSON for mtop.taobao.idlemtopsearch.pc.search
  - For newest (新发布): click UI control "新发布"/"最新" when present
    (mtop search uses dynamic client-side sign — do NOT forge with httpx alone)
  - Optional operator cookies from GOOFISH_COOKIES / GOOFISH_COOKIES_FILE (legacy)
  - Fixture short-circuit via GOOFISH_FIXTURE_PATH (CI / smoke, no live network)
  - CAPTCHA / login_required → structured CollectorError, no bypass / no crash loop
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from app.collectors.base import BaseCollector, ListingItem, SearchQuery
from app.collectors.goofish_profile import acquire_profile_lock_async, ensure_user_data_dir

logger = logging.getLogger(__name__)

SEARCH_BASE = "https://www.goofish.com/search"
ITEM_URL_TMPL = "https://www.goofish.com/item?id={id}"
MTOP_SEARCH_HINT = "idlemtopsearch"
DEFAULT_TIMEOUT_MS = int(os.getenv("GOOFISH_TIMEOUT_MS", "30000"))


class CollectorError(Exception):
    """Structured collector failure (login wall / blocked / empty / timeout)."""

    def __init__(self, code: str, message: str, *, details: Optional[dict] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code}] {message}")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load_cookie_header() -> Optional[str]:
    raw = os.getenv("GOOFISH_COOKIES", "").strip()
    if raw:
        return raw
    path = os.getenv("GOOFISH_COOKIES_FILE", "").strip()
    if path:
        p = Path(path)
        if not p.is_file():
            raise CollectorError("cookies_file_missing", f"GOOFISH_COOKIES_FILE not found: {path}")
        return p.read_text(encoding="utf-8").strip()
    return None


def _parse_cookie_header(header: str) -> list[dict[str, Any]]:
    """Convert raw Cookie header into Playwright add_cookies payloads."""
    cookies: list[dict[str, Any]] = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".goofish.com",
                "path": "/",
            }
        )
    return cookies


def _content_hash(title: str, price: Optional[float], url: Optional[str]) -> str:
    blob = f"{title}|{price}|{url or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    # strip currency symbols / Chinese yuan markers
    text = text.replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_published_at(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch ms or s
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.utcfromtimestamp(ts).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _item_from_card_dict(card: dict[str, Any]) -> Optional[ListingItem]:
    """Best-effort map of common idle search card shapes → ListingItem."""
    # Nested wrappers seen in mtop payloads
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    item = data.get("item") if isinstance(data.get("item"), dict) else data

    external_id = (
        item.get("itemId")
        or item.get("id")
        or item.get("item_id")
        or data.get("itemId")
        or data.get("id")
    )
    if external_id is None:
        return None
    external_id = str(external_id)

    title = (
        item.get("title")
        or item.get("subject")
        or item.get("itemTitle")
        or data.get("title")
        or ""
    )
    if isinstance(title, dict):
        title = title.get("text") or title.get("name") or ""
    title = str(title).strip() or f"item-{external_id}"

    price_raw = (
        item.get("price")
        or item.get("priceWap")
        or item.get("soldPrice")
        or item.get("showPrice")
        or data.get("price")
    )
    if isinstance(price_raw, dict):
        price_raw = price_raw.get("price") or price_raw.get("value") or price_raw.get("text")
    price = _parse_price(price_raw)

    url = item.get("itemUrl") or item.get("url") or data.get("url")
    if not url:
        url = ITEM_URL_TMPL.format(id=external_id)
    elif isinstance(url, str) and url.startswith("//"):
        url = "https:" + url
    elif isinstance(url, str) and url.startswith("/"):
        url = "https://www.goofish.com" + url

    published = (
        item.get("publishTime")
        or item.get("publishedAt")
        or item.get("gmtCreate")
        or item.get("createTime")
        or data.get("publishTime")
    )
    seller = (
        item.get("userNickName")
        or item.get("userNick")
        or item.get("sellerNick")
        or data.get("userNickName")
    )

    return ListingItem(
        external_id=external_id,
        title=title,
        price=price,
        url=str(url) if url else None,
        seller_type=str(seller) if seller else None,
        published_at=_parse_published_at(published),
        raw={"source": "xhr", "snippet": {k: item.get(k) for k in list(item)[:20]}},
    )


def _walk_for_items(node: Any, out: list[dict], depth: int = 0) -> None:
    """Recursively find list-like item card dicts in mtop JSON."""
    if depth > 12:
        return
    if isinstance(node, dict):
        # Heuristic: looks like an item card
        keys = set(node.keys())
        if {"itemId", "title"} <= keys or {"id", "title", "price"} <= keys:
            out.append(node)
        for v in node.values():
            _walk_for_items(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_for_items(v, out, depth + 1)


def _filter_items(items: list[ListingItem], query: SearchQuery) -> list[ListingItem]:
    result: list[ListingItem] = []
    excludes = [e.lower() for e in (query.exclude_keywords or []) if e]
    for it in items:
        if query.price_min is not None and it.price is not None and it.price < query.price_min:
            continue
        if query.price_max is not None and it.price is not None and it.price > query.price_max:
            continue
        title_l = (it.title or "").lower()
        if excludes and any(ex in title_l for ex in excludes):
            continue
        result.append(it)
    return result


def _load_fixture_items(path: str, query: SearchQuery) -> list[ListingItem]:
    p = Path(path)
    if not p.is_file():
        raise CollectorError("fixture_missing", f"GOOFISH_FIXTURE_PATH not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise CollectorError("fixture_invalid", "fixture JSON must be a list of listings")
    items: list[ListingItem] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ext = str(row.get("external_id") or row.get("id") or "")
        if not ext:
            continue
        items.append(
            ListingItem(
                external_id=ext,
                title=str(row.get("title") or ext),
                price=_parse_price(row.get("price")),
                url=row.get("url") or ITEM_URL_TMPL.format(id=ext),
                seller_type=row.get("seller_type"),
                condition=row.get("condition"),
                published_at=_parse_published_at(row.get("published_at")),
                raw=row.get("raw") or {"source": "fixture", **{k: row[k] for k in row if k != "raw"}},
            )
        )
    return _filter_items(items, query)


class GoofishCollector(BaseCollector):
    """Playwright-based 闲鱼 list search collector."""

    name = "goofish_playwright"

    def __init__(self) -> None:
        self.headless = _env_bool("GOOFISH_HEADLESS", True)
        self.timeout_ms = DEFAULT_TIMEOUT_MS
        self.fixture_path = os.getenv("GOOFISH_FIXTURE_PATH", "").strip() or None
        self.user_data_dir = str(ensure_user_data_dir())

    async def search(self, query: SearchQuery) -> list[ListingItem]:
        if self.fixture_path:
            logger.info(
                "[goofish] fixture mode path=%s keyword=%r",
                self.fixture_path,
                query.keyword,
            )
            return _load_fixture_items(self.fixture_path, query)
        return await self._search_live(query)

    async def fetch_detail(self, external_id: str) -> Optional[ListingItem]:
        """Lighter detail fetch: open item page and try title/price; else TODO stub."""
        if self.fixture_path:
            items = _load_fixture_items(
                self.fixture_path,
                SearchQuery(keyword="", page_size=1000),
            )
            for it in items:
                if it.external_id == external_id:
                    return it
            return None

        url = ITEM_URL_TMPL.format(id=external_id)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise CollectorError(
                "playwright_missing",
                "playwright not installed; pip install playwright && playwright install chromium",
            ) from exc

        cookie_header = _load_cookie_header()
        try:
            release_lock = await acquire_profile_lock_async(timeout_s=90.0)
        except TimeoutError as exc:
            raise CollectorError(
                "profile_locked",
                f"browser profile in use by another collect/login/probe: {exc}",
            ) from exc
        try:
            async with async_playwright() as p:
                context = await self._open_persistent_context(p)
                try:
                    if cookie_header:
                        await context.add_cookies(_parse_cookie_header(cookie_header))
                    page = context.pages[0] if context.pages else await context.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    await page.goto(url, wait_until="domcontentloaded")
                    await self._raise_if_blocked(page)

                    title = None
                    for sel in ("h1", "[class*='title']", "title"):
                        loc = page.locator(sel).first
                        if await loc.count():
                            t = (await loc.inner_text()).strip()
                            if t and t.lower() != "goofish":
                                title = t
                                break
                    price = None
                    for sel in ("[class*='price']", r"text=/￥\s*\d+/"):
                        loc = page.locator(sel).first
                        if await loc.count():
                            price = _parse_price(await loc.inner_text())
                            if price is not None:
                                break

                    return ListingItem(
                        external_id=external_id,
                        title=title or f"item-{external_id}",
                        price=price,
                        url=url,
                        raw={"source": "detail_dom", "todo": "richer detail fields"},
                    )
                finally:
                    await context.close()
        finally:
            release_lock()

    async def _open_persistent_context(self, playwright):
        """Launch Chromium with persistent user_data_dir (login session)."""
        udd = self.user_data_dir or str(ensure_user_data_dir())
        logger.info(
            "[goofish] persistent context user_data_dir=%s headless=%s",
            udd,
            self.headless,
        )
        return await playwright.chromium.launch_persistent_context(
            udd,
            headless=self.headless,
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )

    async def _search_live(self, query: SearchQuery) -> list[ListingItem]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise CollectorError(
                "playwright_missing",
                "playwright not installed; pip install playwright && playwright install chromium",
            ) from exc

        cookie_header = _load_cookie_header()
        search_url = f"{SEARCH_BASE}?q={quote(query.keyword)}"
        captured: list[Any] = []

        try:
            release_lock = await acquire_profile_lock_async(timeout_s=90.0)
        except TimeoutError as exc:
            raise CollectorError(
                "profile_locked",
                f"browser profile in use by another collect/login/probe: {exc}",
            ) from exc
        try:
            async with async_playwright() as p:
                context = await self._open_persistent_context(p)
                try:
                    if cookie_header:
                        await context.add_cookies(_parse_cookie_header(cookie_header))
                    page = context.pages[0] if context.pages else await context.new_page()
                    page.set_default_timeout(self.timeout_ms)

                    def _on_response(response) -> None:
                        try:
                            url = response.url or ""
                            ctype = (response.headers or {}).get("content-type", "")
                            if MTOP_SEARCH_HINT not in url and "mtop.taobao.idle" not in url:
                                # still accept generic search JSON
                                if "search" not in url.lower() or "json" not in ctype and "javascript" not in ctype:
                                    return
                            captured.append(response)
                        except Exception:  # noqa: BLE001
                            return

                    page.on("response", _on_response)

                    logger.info(
                        "[goofish] goto %s headless=%s profile=%s",
                        search_url,
                        self.headless,
                        self.user_data_dir,
                    )
                    await page.goto(search_url, wait_until="domcontentloaded")
                    await self._raise_if_blocked(page)

                    # Prefer UI newest sort for 捡漏
                    await self._try_click_newest(page)
                    try:
                        await page.wait_for_timeout(2500)
                    except Exception:  # noqa: BLE001
                        pass
                    # Wait out「加载中」/ search XHR
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=min(self.timeout_ms, 45000)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        pass
                    for _ in range(15):
                        try:
                            body = await page.locator("body").inner_text()
                            if "加载中" not in body[:800] and captured:
                                break
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            await page.wait_for_timeout(800)
                        except Exception:  # noqa: BLE001
                            break

                    items = await self._items_from_responses(captured)
                    if not items:
                        items = await self._items_from_dom(page)

                    if not items:
                        body_text = ""
                        try:
                            body_text = await page.locator("body").inner_text()
                        except Exception as body_exc:  # noqa: BLE001
                            if "has been closed" in str(body_exc).lower():
                                raise CollectorError(
                                    "browser_closed",
                                    f"browser/page closed during collect: {body_exc}",
                                ) from body_exc
                        # Avoid false positive: logged-in pages still contain the word「登录」
                        personal_hints = ("订单", "个人主页", "personal", "退出", "已卖出", "我的闲鱼")
                        has_personal = any(h in body_text for h in personal_hints)
                        login_only = (
                            any(
                                k in body_text
                                for k in ("请登录", "請登录", "马上登录", "立即登录")
                            )
                            or ("登录" in body_text and not has_personal)
                        )
                        if login_only and not has_personal:
                            raise CollectorError(
                                "login_required",
                                "search page requires login; run: python -m app.scripts.login_goofish",
                                details={
                                    "url": search_url,
                                    "user_data_dir": self.user_data_dir,
                                    "hint": "headed login into persistent profile, then restart scheduler",
                                },
                            )
                        raise CollectorError(
                            "empty_results",
                            "no search results parsed (XHR + DOM empty)",
                            details={"url": search_url, "body_snip": body_text[:200]},
                        )

                    filtered = _filter_items(items, query)
                    seen: set[str] = set()
                    unique: list[ListingItem] = []
                    for it in filtered:
                        if it.external_id in seen:
                            continue
                        seen.add(it.external_id)
                        unique.append(it)
                    return unique[: query.page_size]
                finally:
                    await context.close()
        finally:
            release_lock()

    async def _raise_if_blocked(self, page) -> None:
        url = page.url or ""
        title = ""
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            pass
        lower = (title + " " + url).lower()
        # Obvious captcha / deny pages — do not attempt bypass
        markers = ("captcha", "punish", "deny", "验证码", "滑块", "安全验证")
        body_snip = ""
        try:
            body_snip = (await page.locator("body").inner_text())[:500]
        except Exception:  # noqa: BLE001
            pass
        if any(m in lower or m in body_snip for m in markers):
            raise CollectorError(
                "blocked",
                "page looks like captcha / anti-bot wall; pausing (no bypass)",
                details={"url": url, "title": title},
            )
        if "login.taobao" in url or "login.goofish" in url or "/login" in url:
            raise CollectorError(
                "login_required",
                "redirected to login; run: python -m app.scripts.login_goofish "
                f"(profile={self.user_data_dir})",
                details={"url": url, "user_data_dir": self.user_data_dir},
            )

    async def _try_click_newest(self, page) -> None:
        """Click 新发布 / 最新 UI control when present (sort often create/desc)."""
        for label in ("新发布", "最新"):
            try:
                loc = page.get_by_text(label, exact=False).first
                if await loc.count():
                    await loc.click(timeout=3000)
                    logger.info("[goofish] clicked sort UI: %s", label)
                    return
            except Exception:  # noqa: BLE001
                continue
        logger.info("[goofish] newest sort UI not found; keeping default page order")

    async def _items_from_responses(self, responses: list) -> list[ListingItem]:
        items: list[ListingItem] = []
        for resp in responses:
            try:
                if resp.status != 200:
                    continue
                data = await resp.json()
            except Exception:  # noqa: BLE001
                continue
            cards: list[dict] = []
            _walk_for_items(data, cards)
            for card in cards:
                mapped = _item_from_card_dict(card)
                if mapped:
                    items.append(mapped)
        return items

    async def _items_from_dom(self, page) -> list[ListingItem]:
        """Fallback: parse result cards from DOM anchors to /item?id=."""
        items: list[ListingItem] = []
        try:
            anchors = page.locator("a[href*='item'], a[href*='/item?']")
            n = await anchors.count()
            for i in range(min(n, 60)):
                a = anchors.nth(i)
                href = await a.get_attribute("href") or ""
                m = re.search(r"[?&]id=([^&]+)", href)
                if not m:
                    m = re.search(r"/item/(\w+)", href)
                if not m:
                    continue
                ext = m.group(1)
                text = (await a.inner_text()).strip()
                title = text.split("\n")[0].strip() if text else f"item-{ext}"
                price = _parse_price(text)
                url = href if href.startswith("http") else (
                    "https:" + href if href.startswith("//") else "https://www.goofish.com" + href
                )
                items.append(
                    ListingItem(
                        external_id=ext,
                        title=title[:512],
                        price=price,
                        url=url,
                        raw={"source": "dom", "snippet": text[:300]},
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[goofish] DOM parse failed: %s", exc)
            if "has been closed" in str(exc).lower() or "target page" in str(exc).lower():
                raise CollectorError(
                    "browser_closed",
                    f"browser/page closed during DOM parse: {exc}",
                ) from exc
        return items


# re-export helper for upsert content hash
content_hash_for_item = _content_hash
