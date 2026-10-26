"""Rule-based listing evaluation → 推荐 / 待观察 / 不推荐."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# Verdict scheme (documented in README «规则评估»):
# - Any hard-requirement failure → 不推荐
# - All hard pass, but one or more soft preferences miss → 待观察
# - All hard pass and soft prefs satisfied (or none set) → 推荐
VERDICT_RECOMMEND = "推荐"
VERDICT_WATCH = "待观察"
VERDICT_REJECT = "不推荐"

ANALYZER_NAME = "rules"


@dataclass
class RuleResult:
    verdict: str
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    score: Optional[float] = None
    passed_hard: bool = True
    soft_score: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "analyzer": ANALYZER_NAME,
            "verdict": self.verdict,
            "tags": list(self.tags),
            "reason": self.reason,
            "score": self.score,
            "passed_hard": self.passed_hard,
            "soft_score": self.soft_score,
            "details": dict(self.details),
        }


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


def _norm(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def _haystack(listing: Any) -> str:
    title = _listing_get(listing, "title") or ""
    raw = _listing_get(listing, "raw") or {}
    extra = ""
    if isinstance(raw, dict):
        for k in ("desc", "description", "subtitle", "note"):
            if raw.get(k):
                extra += " " + str(raw[k])
    return _norm(f"{title} {extra}")


def _contains(haystack: str, needle: Optional[str]) -> bool:
    n = _norm(needle)
    if not n:
        return True
    if n in haystack:
        return True
    # "M2 Pro" ↔ "M2Pro" / "m2pro"
    n_compact = re.sub(r"\s+", "", n)
    hay_compact = re.sub(r"\s+", "", haystack)
    if n_compact and n_compact in hay_compact:
        return True
    # size helpers: "14寸" ↔ "14"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*寸$", n)
    if m and m.group(1) in haystack:
        return True
    # ram/storage: "16G" / "16GB" / "16g"
    m2 = re.match(r"^(\d+)\s*(g|gb|t|tb)$", n)
    if m2:
        num, unit = m2.group(1), m2.group(2)[0]
        variants = {f"{num}{unit}", f"{num}{unit}b", f"{num} {unit}", f"{num} {unit}b"}
        if any(v in haystack for v in variants):
            return True
    return False


def _price(listing: Any) -> Optional[float]:
    p = _listing_get(listing, "price")
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def evaluate_rules(
    listing: Any,
    config: Any = None,
    hard_requirements: dict[str, Any] | None = None,
    soft_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluate a listing against ProductConfig (or dict) + optional hard/soft overrides.

    Hard fails (any) → verdict 不推荐 with tags.
    Soft misses only → 待观察.
    Otherwise → 推荐.

    Accepts legacy stub signature (listing, hard_requirements=, soft_preferences=)
    or (listing, config).
    """
    # Support both call styles
    if config is None and (hard_requirements is not None or soft_preferences is not None):
        config = {
            "hard_requirements": hard_requirements or {},
            "soft_preferences": soft_preferences or {},
        }
    result = _evaluate(listing, config)
    return result.as_dict()


def _evaluate(listing: Any, config: Any) -> RuleResult:
    hard_extra = dict(_cfg_get(config, "hard_requirements") or {})
    soft_extra = dict(_cfg_get(config, "soft_preferences") or {})

    hay = _haystack(listing)
    price = _price(listing)
    title = _listing_get(listing, "title") or ""
    seller_type = _norm(_listing_get(listing, "seller_type"))
    condition = _listing_get(listing, "condition")

    hard_tags: list[str] = []
    hard_notes: list[str] = []
    soft_tags: list[str] = []
    soft_notes: list[str] = []
    soft_hits = 0
    soft_total = 0

    # ── Hard: budget ────────────────────────────────────────────
    price_max = _cfg_get(config, "price_max")
    if price_max is None and "price_max" in hard_extra:
        price_max = hard_extra.get("price_max")
    if price_max is not None and price is not None and price > float(price_max):
        hard_tags.append("超预算")
        hard_notes.append(f"标价 ¥{price:g} 超过预算上限 ¥{float(price_max):g}")

    price_min = _cfg_get(config, "price_min")
    if price_min is None and "price_min" in hard_extra:
        price_min = hard_extra.get("price_min")
    if price_min is not None and price is not None and price < float(price_min):
        hard_tags.append("低于预算")
        hard_notes.append(f"标价 ¥{price:g} 低于预算下限 ¥{float(price_min):g}")

    # ── Hard: model / size / brand (from columns or hard_requirements) ──
    model = _cfg_get(config, "model") or hard_extra.get("model")
    if model and not _contains(hay, str(model)):
        hard_tags.append("型号不符")
        hard_notes.append(f"标题未匹配型号「{model}」")

    size = _cfg_get(config, "size") or hard_extra.get("size")
    if size and not _contains(hay, str(size)):
        hard_tags.append("尺寸不符")
        hard_notes.append(f"标题未匹配尺寸「{size}」")

    brand = _cfg_get(config, "brand") or hard_extra.get("brand")
    if brand and not _contains(hay, str(brand)):
        hard_tags.append("品牌不符")
        hard_notes.append(f"标题未匹配品牌「{brand}」")

    # ── Hard: exclude keywords ──────────────────────────────────
    excludes = list(_cfg_get(config, "exclude_keywords") or [])
    if hard_extra.get("exclude_keywords"):
        excludes = list(hard_extra["exclude_keywords"])
    hit_excludes = [kw for kw in excludes if kw and _norm(kw) in hay]
    if hit_excludes:
        hard_tags.append("命中排除词")
        hard_notes.append("命中排除词：" + "、".join(hit_excludes))

    # ── Hard: seller preference personal_only ───────────────────
    seller_pref = _cfg_get(config, "seller_preference") or hard_extra.get(
        "seller_preference", "any"
    )
    if seller_pref == "personal_only" and seller_type:
        # known non-personal → hard fail; unknown left alone
        if seller_type not in ("personal", "个人", "personal_only"):
            hard_tags.append("疑似非个人")
            hard_notes.append(f"卖家类型为「{seller_type}」，偏好仅个人闲置")

    # Extra hard keys from hard_requirements (string must appear in title)
    for key in ("chip", "cpu", "color"):
        val = hard_extra.get(key)
        if val and not _contains(hay, str(val)):
            tag = f"{key}不符"
            hard_tags.append(tag)
            hard_notes.append(f"硬条件未匹配 {key}「{val}」")

    # ── Soft: ram / storage / condition / battery ───────────────
    ram = soft_extra.get("ram") or _cfg_get(config, "ram")
    if ram:
        soft_total += 1
        if _contains(hay, str(ram)):
            soft_hits += 1
        else:
            soft_tags.append("内存不符")
            soft_notes.append(f"未看到内存「{ram}」")

    storage = soft_extra.get("storage") or _cfg_get(config, "storage")
    if storage:
        soft_total += 1
        if _contains(hay, str(storage)):
            soft_hits += 1
        else:
            soft_tags.append("存储不符")
            soft_notes.append(f"未看到存储「{storage}」")

    condition_min = soft_extra.get("condition_min") or _cfg_get(config, "condition_min")
    if condition_min:
        soft_total += 1
        # rough: if listing condition text contains the min token, count as hit
        cond_text = _norm(condition) or hay
        if _norm(str(condition_min)) in cond_text or _contains(hay, str(condition_min)):
            soft_hits += 1
        else:
            soft_tags.append("成色偏低")
            soft_notes.append(f"成色未达偏好「{condition_min}」（当前：{condition or '未知'}）")

    battery_min = soft_extra.get("battery_health_min")
    if battery_min is None:
        battery_min = _cfg_get(config, "battery_health_min")
    if battery_min is not None:
        soft_total += 1
        # try extract battery % from title/raw
        m = re.search(r"(?:电池|battery)[^\d]{0,6}(\d{2,3})\s*%?", hay)
        if m and int(m.group(1)) >= int(battery_min):
            soft_hits += 1
        else:
            soft_tags.append("电池健康未知" if not m else "电池健康偏低")
            soft_notes.append(
                f"电池健康偏好 ≥{battery_min}%"
                + (f"（检测到 {m.group(1)}%）" if m else "（标题未标注）")
            )

    # Extra soft string prefs
    for key, val in soft_extra.items():
        if key in ("ram", "storage", "condition_min", "battery_health_min", "seller_preference"):
            continue
        if isinstance(val, (str, int, float)) and str(val).strip():
            soft_total += 1
            if _contains(hay, str(val)):
                soft_hits += 1
            else:
                soft_tags.append(f"{key}不符")
                soft_notes.append(f"软偏好未匹配 {key}「{val}」")

    soft_score = 1.0 if soft_total == 0 else round(soft_hits / soft_total, 3)
    passed_hard = len(hard_tags) == 0

    if not passed_hard:
        verdict = VERDICT_REJECT
        tags = hard_tags
        reason = "；".join(hard_notes) if hard_notes else "未通过硬性条件"
        score = 0.0
    elif soft_tags:
        verdict = VERDICT_WATCH
        tags = soft_tags
        reason = "硬条件通过，但：" + "；".join(soft_notes)
        score = round(0.5 + 0.4 * soft_score, 3)
    else:
        verdict = VERDICT_RECOMMEND
        tags = ["硬条件通过"]
        if soft_total:
            tags.append("软偏好匹配")
        bits = []
        if price is not None:
            bits.append(f"价格 ¥{price:g}")
        if model:
            bits.append(f"型号匹配「{model}」")
        if size:
            bits.append(f"尺寸匹配「{size}」")
        reason = ("，".join(bits) + "，符合监控条件") if bits else f"「{title[:40]}」符合监控条件"
        score = round(0.85 + 0.15 * soft_score, 3)

    return RuleResult(
        verdict=verdict,
        tags=tags,
        reason=reason,
        score=score,
        passed_hard=passed_hard,
        soft_score=soft_score,
        details={
            "hard_tags": hard_tags,
            "soft_tags": soft_tags,
            "soft_hits": soft_hits,
            "soft_total": soft_total,
            "price": price,
            "title": title[:120],
        },
    )
