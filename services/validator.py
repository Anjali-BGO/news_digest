import httpx
from datetime import datetime, timedelta
from typing import List, Tuple
from urllib.parse import quote

# ── Configuration ──────────────────────────────────────────────────────────────
# Minimum chars to consider content "present" — kept low because Tavily returns
# short snippets (include_raw_content=False).  Genuine paywalls return empty or
# near-empty content; 30 chars catches those without rejecting real snippets.
MIN_CONTENT_LENGTH  = 30

METERED_PAYWALL_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "economist.com", "thetimes.co.uk", "telegraph.co.uk",
    "businessinsider.com", "hbr.org", "barrons.com",
    "marketwatch.com", "theatlantic.com",
}

BLOCKED_DOMAINS = {
    "pinterest.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "reddit.com", "tiktok.com", "youtube.com",
    "slideshare.net", "scribd.com", "quora.com",
}

ALLOWED_LANGUAGES = {"en", "english", ""}

REMOVED_PHRASES = {
    "[removed]", "[deleted]", "article not found",
    "page not found", "404", "content unavailable",
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_domain(url: str) -> str:
    try:
        return url.split("/")[2].replace("www.", "").lower()
    except Exception:
        return ""

def _is_removed(title: str, content: str) -> bool:
    combined = (title + " " + content).lower()
    return any(phrase in combined for phrase in REMOVED_PHRASES)

def _is_paywalled(content: str, title: str) -> bool:
    """
    Returns True only when the article has genuinely no retrievable content.
    Tavily returns short snippets by design — we accept those.
    We reject only when content is completely absent (empty after strip).
    """
    return not content or len(content.strip()) < MIN_CONTENT_LENGTH

def _is_non_english(article: dict) -> bool:
    return article.get("language", "").lower() not in ALLOWED_LANGUAGES

def _is_blocked_domain(url: str) -> bool:
    return _extract_domain(url) in BLOCKED_DOMAINS

def _is_metered_paywall(url: str) -> bool:
    return _extract_domain(url) in METERED_PAYWALL_DOMAINS


# ── Alternative URL finder ─────────────────────────────────────────────────────
async def _find_alternative_url(title: str) -> str | None:
    query = quote(title.strip())
    candidates = [
        f"https://news.google.com/search?q={query}&hl=en",
        f"https://finance.yahoo.com/search?q={query}",
    ]
    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return url
        except Exception:
            continue
    return None


# ── Main validation function ───────────────────────────────────────────────────
async def validate_articles(
    articles: List[dict],
    window:   dict | None = None,
) -> Tuple[List[dict], List[dict]]:
    """
    8-point validation checklist per article.
    Uses window["from"] and window["to"] for date range checks.
    Falls back to last 30 days if window not provided.

    Returns:
        valid   — articles that passed all checks
        val_log — rejected articles with reason (for audit trail)
    """
    valid   = []
    val_log = []

    # resolve date bounds
    try:
        w_from = datetime.strptime(window["from"], "%Y-%m-%d") \
                 if window and window.get("from") else \
                 datetime.now() - timedelta(days=31)
        w_to   = datetime.strptime(window["to"], "%Y-%m-%d") \
                 if window and window.get("to") else \
                 datetime.now() - timedelta(days=1)
    except (ValueError, KeyError):
        w_from = datetime.now() - timedelta(days=31)
        w_to   = datetime.now() - timedelta(days=1)

    for art in articles:
        title   = art.get("title",          "").strip()
        content = art.get("content",        "").strip()
        url     = art.get("url",            "")
        date    = art.get("published_date", "")
        domain  = _extract_domain(url)

        # 1. Missing / removed title
        if not title or _is_removed(title, content):
            val_log.append({**art, "reason": "Missing or removed title"})
            continue

        # 2 & 3. Date range check against window
        try:
            pub = datetime.strptime(date[:10], "%Y-%m-%d")
            if pub > w_to:
                val_log.append({**art, "reason": f"Outside window end: {date}"})
                continue
            if pub < w_from:
                val_log.append({**art, "reason": f"Outside window start: {date}"})
                continue
        except (ValueError, TypeError):
            val_log.append({**art, "reason": f"Invalid date format: {date}"})
            continue

        # 4. Paywalled / empty content
        if _is_paywalled(content, title):
            val_log.append({**art, "reason": "Paywalled or empty content"})
            continue

        # 5. Non-English
        if _is_non_english(art):
            val_log.append({
                **art,
                "reason": f"Non-English: {art.get('language', 'unknown')}"
            })
            continue

        # 6. Blocked domain
        if _is_blocked_domain(url):
            val_log.append({**art, "reason": f"Blocked domain: {domain}"})
            continue

        # 7. Metered paywall — try alternative link
        if _is_metered_paywall(url):
            alt_url = await _find_alternative_url(title)
            if alt_url:
                art = {
                    **art,
                    "original_url": url,
                    "url":          alt_url,
                    "paywall_note": (
                        f"Original source ({domain}) may require a subscription. "
                        f"Alternative link provided."
                    ),
                }
            else:
                val_log.append({
                    **art,
                    "reason": f"Metered paywall ({domain}) — no free alternative found"
                })
                continue

        valid.append(art)

    print(
        f"[Validator] {len(articles)} in → {len(valid)} valid "
        f"| {len(val_log)} rejected"
    )
    return valid, val_log