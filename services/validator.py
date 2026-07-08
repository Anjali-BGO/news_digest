import re
from datetime import datetime, timedelta
from typing import List, Tuple
from urllib.parse import urlparse
from logger import get_logger

log = get_logger("validator")

# ── Configuration ──────────────────────────────────────────────────────────────
# Reject only when BOTH content < 150 chars AND title < 60 chars.
# Tavily returns short snippets by design — a 60-char title confirms a real article.
MIN_CONTENT_LENGTH = 150
MIN_TITLE_LENGTH   = 60

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

REMOVED_PHRASES = {
    "[removed]", "[deleted]", "article not found",
    "page not found", "404", "content unavailable",
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        u = url if "://" in url else f"https://{url}"
        netloc = urlparse(u).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""

def _is_removed(title: str, content: str) -> bool:
    # Check title only — content may legitimately contain phrases like "page not found"
    # in articles about HTTP errors or website migrations.
    return any(phrase in title.lower() for phrase in REMOVED_PHRASES)

def _has_limited_preview(content: str, title: str) -> bool:
    """
    Returns True when BOTH content AND title are very short — common for
    SerpAPI/NewsData snippets by design, not necessarily a paywall.
    Rejects only when both signals fire together to avoid silently dropping
    valid articles from snippet-only sources.
    """
    content_short = not content or len(content.strip()) < MIN_CONTENT_LENGTH
    title_short   = len(title.strip()) < MIN_TITLE_LENGTH
    return content_short and title_short

def _is_non_english(article: dict) -> bool:
    lang = article.get("language", "").lower().strip()
    if not lang:
        return False   # no language field → assume English (Tavily/SerpAPI default)
    return not (lang.startswith("en") or lang == "english")

def _is_blocked_domain(url: str) -> bool:
    return _extract_domain(url) in BLOCKED_DOMAINS

def _is_metered_paywall(url: str) -> bool:
    return _extract_domain(url) in METERED_PAYWALL_DOMAINS

def _has_capitalised_match(text: str, entity_lower: str) -> bool:
    """
    Case-insensitive search; returns True if any match has its first letter
    capitalised (proper-noun usage rather than verb/common-word usage).
    """
    pattern = re.compile(re.escape(entity_lower), re.IGNORECASE)
    for match in pattern.finditer(text):
        if match.group()[0].isupper():
            return True
    return False


# Compiled once — strips trailing legal/business suffixes so "Barclays Group"
# → "barclays" is searched in addition to "barclays group".
_SUFFIX_RE = re.compile(
    r"\s+(group|ltd|plc|inc|corp|limited|co|llc|llp|holdings|sa|ag|gmbh|nv|bv)\.?$",
    re.IGNORECASE,
)


def _strip_business_suffix(name_lower: str) -> str:
    """Return name with trailing business suffix removed; original if no suffix matched."""
    return _SUFFIX_RE.sub("", name_lower).strip()


def _build_search_terms(ent_lower: str, aliases: list) -> list:
    """
    Combine entity name, auto short-name (suffix-stripped), and any explicit
    aliases into a deduplicated list of lowercase search terms.
    """
    terms = {ent_lower}
    short = _strip_business_suffix(ent_lower)
    if short != ent_lower:
        terms.add(short)
    for a in aliases:
        if a.strip():
            terms.add(a.strip().lower())
    return list(terms)


def _is_relevant(title: str, content: str, url: str,
                 ent_lower: str, ent_domain: str,
                 fetch_source: str = "", aliases: list = None) -> bool:
    """
    Checks whether this article is about the entity.

    URL from entity's own domain → automatic pass.

    Require a capitalised match (proper-noun check) for every search term
    (entity name, auto short-name, and explicit aliases). This prevents
    common-word drift ("apple" → fruit article) and verb false-positives
    ("Tribal Rallies Affirm Readiness" → "Affirm" is a verb, not the company).

    Two-phase check to maximise accuracy:

    Phase 1 — content body (most reliable signal):
      Verb uses of entity-name words stay LOWERCASE in article body text; company
      names are always capitalised. A cap match in the content body is definitive.

    Phase 2 — title fallback (weak signal):
      Short snippets from SerpAPI (<400 chars) may omit the entity name
      from the description even when the article is genuinely about them. In that
      case the title is used as a fallback. For long content (Tavily full articles)
      the title is NOT used as a fallback — if the entity does not appear capitalised
      in the body, the article is about something else.
    """
    if ent_domain and ent_domain in url.lower():
        return True
    terms = _build_search_terms(ent_lower, aliases or [])

    if content.strip():
        # Phase 1: cap match in content body → confirmed proper-noun usage
        if any(_has_capitalised_match(content, term) for term in terms):
            return True
        # Phase 2: cap match only in title
        # Short snippets (<400 chars): allow title as supplementary signal —
        # the snippet may not repeat the entity name even in a relevant article.
        # Long content (≥400 chars, i.e. Tavily full articles): title-only match
        # is not enough — the entity name must appear in the body itself.
        return len(content.strip()) < 400 and any(
            _has_capitalised_match(title, term) for term in terms
        )

    # No content at all: title is the only signal
    return any(_has_capitalised_match(title, term) for term in terms)


# ── Main validation function ───────────────────────────────────────────────────
async def validate_articles(
    articles:       List[dict],
    window:         dict | None = None,
    entity_name:    str = "",
    entity_website: str = "",
    entity_aliases: list = [],
    entity_type:    str = "",
) -> Tuple[List[dict], List[dict]]:
    """
    8-point validation checklist. Designed to reject only what is clearly wrong;
    borderline articles are kept and tagged rather than silently dropped.

    Point 1  — Missing / removed title
    Point 2  — Published date too old (before window start)
    Point 3  — Published date too far in future (> 1 day after window end)
    Point 4  — Limited content (content < 150 chars AND title < 60 chars)
                → soft tag only; article is KEPT with a "Limited preview" note.
                SerpAPI/NewsData always return short snippets — hard-
                rejecting on length alone would silently discard valid articles.
    Point 5  — Non-English article (only fires when language field is explicitly set)
    Point 6  — Blocked domain (social media / forums)
    Point 7  — Entity relevance (entity name not found in article — catches search drift)
    Point 8  — Metered paywall domain → tag with note, keep article

    Date window note: Tavily's `days` filter is approximate. Articles published on
    the same day as the run frequently return with today's date even when the window
    ends yesterday. A 1-day buffer at the upper end prevents mass rejection of
    same-day articles.

    Returns:
        valid   — articles that passed all checks
        val_log — rejected articles with reason (for audit trail)
    """
    valid   = []
    val_log = []

    ent_lower  = entity_name.lower().strip() if entity_name else ""
    ent_domain = _extract_domain(entity_website)

    # Resolve date bounds — fall back to last 30 days if window missing
    try:
        w_from = datetime.strptime(window["from"], "%Y-%m-%d") \
                 if window and window.get("from") else \
                 datetime.now() - timedelta(days=31)
        w_to   = datetime.strptime(window["to"], "%Y-%m-%d") \
                 if window and window.get("to") else \
                 datetime.now() - timedelta(days=1)
    except (ValueError, KeyError):
        w_from = datetime.now() - timedelta(days=30)
        w_to   = datetime.now() - timedelta(days=1)

    # Tavily often dates articles to today even when the window ends yesterday.
    # Allow up to 1 day beyond w_to so that same-day articles aren't mass-rejected.
    w_to_extended = w_to + timedelta(days=1)

    for art in articles:
        title   = art.get("title",          "").strip()
        content = art.get("content",        "").strip()
        url     = art.get("url",            "")
        date    = art.get("published_date", "")
        domain  = _extract_domain(url)

        # 1. Missing / removed title
        if not title or _is_removed(title, content):
            val_log.append({**art, "reason": "Title is empty, null, or marked as [Removed] / [Deleted]"})
            continue

        # 2 & 3. Date range check — skipped for "NA" (date couldn't be determined)
        if date and date != "NA":
            try:
                pub = datetime.strptime(date[:10], "%Y-%m-%d")
                if pub > w_to_extended:
                    val_log.append({**art, "reason": f"Published date {date} is after the window end (future article)"})
                    continue
                if pub < w_from:
                    val_log.append({**art, "reason": f"Published date {date} is before the window start — article is too old for this period"})
                    continue
            except (ValueError, TypeError):
                val_log.append({**art, "reason": f"Could not parse published date '{date}' — article skipped"})
                continue

        # 4. Limited content preview — soft tag only, article is KEPT.
        # SerpAPI returns 80–150 char snippets by design; NewsData descriptions
        # are similarly short. Hard-rejecting on length alone silently drops valid articles.
        # We tag the article so the user can see it in the digest with a notice.
        if _has_limited_preview(content, title):
            art = {
                **art,
                "paywall_note": (
                    art.get("paywall_note", "")
                    or "Limited content preview — only a short snippet was available from this source."
                ),
            }

        # 5. Non-English
        if _is_non_english(art):
            lang = art.get("language", "unknown")
            val_log.append({
                **art,
                "reason": f"Non-English article (language field is '{lang}') — only English articles are included"
            })
            continue

        # 6. Blocked domain
        if _is_blocked_domain(url):
            val_log.append({**art, "reason": f"Source domain blocked: {domain} (social media or forum — not a news source)"})
            continue

        # 7. Entity relevance
        # Runs for company entities (client/prospect). Industry entities are
        # meta-category labels ("Financial Services & Banking") that never
        # appear verbatim in articles — skip the check; the Tavily query already
        # ensures topic relevance. For companies, this catches search drift where
        # Tavily returns unrelated articles (e.g. "Stripe" → LUCA MINING CORP).
        # Domain match (entity_website set) is an automatic pass.
        if ent_lower and entity_type != "industry" and not _is_relevant(
            title, content, url, ent_lower, ent_domain,
            art.get("fetch_source", ""),
            entity_aliases,
        ):
            val_log.append({
                **art,
                "reason": f"Not relevant to '{entity_name}' — entity name not found in title or content (search drift)",
            })
            continue

        # 8. Metered paywall domain — soft tag only, article is KEPT.
        # The paywall_note field is set so readers see a subscription warning in the digest.
        # The article continues through: hyperlink validation → AI summarize → categorize → save.
        # Hard-rejecting Reuters/Bloomberg/FT/WSJ silently removes high-value coverage;
        # tagging is always preferable to silent removal.
        if _is_metered_paywall(url):
            # Combine with any note point 4 already set (e.g. "Limited content
            # preview") instead of letting one soft-tag silently overwrite the
            # other — Reuters/Bloomberg/FT/WSJ articles very commonly trigger both.
            _paywall_msg = f"Article from {domain} may require a subscription — content preview may be limited."
            _existing_note = art.get("paywall_note", "")
            art = {
                **art,
                "paywall_note": (
                    f"{_existing_note} {_paywall_msg}"
                    if _existing_note and _existing_note != _paywall_msg
                    else _paywall_msg
                ),
            }

        valid.append(art)

    log.info(
        f"[Validator] {len(articles)} in -> {len(valid)} valid "
        f"| {len(val_log)} rejected"
        + (f" | relevance active for: {entity_name}" if ent_lower else "")
    )
    return valid, val_log
