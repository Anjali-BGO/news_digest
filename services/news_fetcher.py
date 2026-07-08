import os
import re
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

IST = timezone(timedelta(hours=5, minutes=30))
from urllib.parse import urlparse
from dotenv import load_dotenv
from tavily import TavilyClient
from models import TOPIC_CATEGORIES
from logger import get_logger

load_dotenv()

log = get_logger("news_fetcher")

TAVILY_API_KEY        = os.getenv("TAVILY_API_KEY")
TAVILY_API_KEYS       = [k for k in [os.getenv("TAVILY_API_KEY"), os.getenv("TAVILY_API_KEY_2")] if k]
SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY")
NEWSDATA_API_KEY      = os.getenv("NEWSDATA_API_KEY")
NEWSAI_API_KEY        = os.getenv("NEWSAI_API_KEY")

TAVILY_MAX_RESULTS    = int(os.getenv("TAVILY_MAX_RESULTS",   4))
SERPAPI_MAX_RESULTS   = int(os.getenv("SERPAPI_MAX_RESULTS",  4))
NEWSDATA_MAX_RESULTS  = int(os.getenv("NEWSDATA_MAX_RESULTS", 4))
NEWSAI_MAX_RESULTS    = int(os.getenv("NEWSAI_MAX_RESULTS",   4))

# Industry entities need higher fetch volumes (no entity-name filter means more
# articles per call are needed to cover the sector breadth).
TAVILY_INDUSTRY_MAX_RESULTS   = int(os.getenv("TAVILY_INDUSTRY_MAX_RESULTS",   20))
SERPAPI_INDUSTRY_MAX_RESULTS  = int(os.getenv("SERPAPI_INDUSTRY_MAX_RESULTS",  20))
NEWSDATA_INDUSTRY_MAX_RESULTS = int(os.getenv("NEWSDATA_INDUSTRY_MAX_RESULTS", 20))
NEWSAI_INDUSTRY_MAX_RESULTS   = int(os.getenv("NEWSAI_INDUSTRY_MAX_RESULTS",   20))

MAX_ARTICLES_PER_ENTITY = int(os.getenv("MAX_ARTICLES_PER_ENTITY", 0))  # 0 = no cap

# Tavily news scores range 0.02–0.23; set low so the validator decides relevance,
# not a score threshold that silently drops valid low-scored articles.
TAVILY_MIN_SCORE = float(os.getenv("TAVILY_MIN_SCORE", 0.05))

# Default sources when none are selected
DEFAULT_SOURCES = ["tavily"]

# ── Per-run source exhaustion tracker ─────────────────────────────────────────
# Populated when an API returns HTTP 429 (credits/quota exhausted).
# Reset once at the start of each pipeline run — never between topics or entities,
# so an exhausted source stays disabled for the entire run.
_exhausted: set = set()

def reset_exhausted_sources() -> None:
    """Clear exhaustion flags — call once at the start of every pipeline run."""
    _exhausted.clear()

def get_exhausted_sources() -> set:
    """Return which sources ran out of credits during the current pipeline run."""
    return set(_exhausted)

# Domains excluded from all Tavily queries at the API level — reduces noise
# before results even reach our validator's blocked-domain check.
TAVILY_EXCLUDE_DOMAINS = [
    "reddit.com", "quora.com", "pinterest.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "slideshare.net", "scribd.com",
]


# ── Window resolver ────────────────────────────────────────────────────────────
def resolve_window(
    period_days: str = "30",
    date_from:   str = "",
    date_to:     str = "",
) -> dict:
    fmt       = "%Y-%m-%d"
    dfmt      = "%d %b %Y"
    today     = datetime.now()
    yesterday = today - timedelta(days=1)

    if date_from and date_to:
        try:
            dt_from = datetime.strptime(date_from, fmt)
            dt_to   = datetime.strptime(date_to,   fmt)
            if dt_to >= today:
                dt_to = yesterday
            if dt_from > dt_to:
                dt_from, dt_to = dt_to, dt_from
            return {
                "from":  dt_from.strftime(fmt),
                "to":    dt_to.strftime(fmt),
                "label": f"{dt_from.strftime(dfmt)} – {dt_to.strftime(dfmt)}",
                "days":  (dt_to - dt_from).days + 1,
            }
        except ValueError:
            pass

    try:
        days = int(period_days)
    except ValueError:
        days = 30
    days    = max(1, min(days, 365))
    dt_to   = yesterday
    dt_from = dt_to - timedelta(days=days - 1)
    return {
        "from":  dt_from.strftime(fmt),
        "to":    dt_to.strftime(fmt),
        "label": f"{dt_from.strftime(dfmt)} – {dt_to.strftime(dfmt)}",
        "days":  days,
    }


def get_current_window() -> dict:
    return resolve_window("30")


# ── Domain extractor ───────────────────────────────────────────────────────────
def _extract_domain(website: str) -> str:
    """
    Extracts bare domain from a website URL.
    "https://www.apple.com/about" → "apple.com"
    """
    if not website:
        return ""
    try:
        url = website if "://" in website else f"https://{website}"
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


# ── Date normaliser ────────────────────────────────────────────────────────────
def _normalise_date(raw: str, fallback: str = "") -> str:
    """
    Parses ISO 8601 dates with or without timezone offsets and converts to
    YYYY-MM-DD in IST.

    Returns `fallback` (default "") when `raw` is empty or cannot be parsed.
    Callers that pass fallback="" use the empty string to detect failure via
    `_normalise_date(...) or None` — returning today's date here would make
    that pattern always truthy and break the fallback chain.
    """
    if not raw:
        return fallback
    s = raw.strip()
    s_iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s_iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(IST).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return fallback


# Common HTML/meta patterns that carry publication dates, tried in priority order.
_DATE_PATTERNS = [
    r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)["\']',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time["\']',
    r'name=["\']pubdate["\'][^>]*content=["\']([^"\']+)["\']',
    r'content=["\']([^"\']+)["\'][^>]*name=["\']pubdate["\']',
    r'name=["\']date["\'][^>]*content=["\']([^"\']+)["\']',
    r'content=["\']([^"\']+)["\'][^>]*name=["\']date["\']',
    r'<time[^>]+datetime=["\']([^"\']+)["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'"dateModified"\s*:\s*"([^"]+)"',
]

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*;q=0.8",
}


async def _scrape_published_date(url: str) -> Optional[str]:
    """
    Fetches the article page and extracts a published/modified date from
    common HTML meta tags and JSON-LD structured data.
    Returns a YYYY-MM-DD string (IST-normalised) or None on failure.
    Timeout is kept short (5 s) so scraping never becomes a bottleneck.
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=5, follow_redirects=True, max_redirects=3, headers=_SCRAPE_HEADERS
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            html = resp.text
        for pattern in _DATE_PATTERNS:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                result = _normalise_date(m.group(1), "")
                if result:
                    return result
        return None
    except Exception:
        return None


def _parse_serpapi_date(raw: str, fallback: str) -> str:
    """
    SerpAPI Google News returns dates as relative strings ("2 hours ago", "3 days ago")
    or human-readable absolutes ("Jun 15, 2026"). Standard ISO parsing fails on these.
    Falls back to `fallback` (window start) rather than window end, so that relative-
    dated articles don't all land on yesterday and falsely appear in-window regardless
    of actual age.
    """
    if not raw:
        return fallback
    # Try standard ISO formats first
    normalised = _normalise_date(raw, "")
    if normalised:
        return normalised
    # "Jun 15, 2026" / "June 15, 2026"
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Relative: "2 hours ago", "3 days ago", "1 week ago"
    lower = raw.strip().lower()
    today = datetime.now()
    match = re.search(r"\d+", lower)
    n = int(match.group()) if match else 1
    if any(w in lower for w in ("hour", "minute", "second", "just now")):
        return today.strftime("%Y-%m-%d")
    if "day" in lower:
        return (today - timedelta(days=n)).strftime("%Y-%m-%d")
    if "week" in lower:
        return (today - timedelta(weeks=n)).strftime("%Y-%m-%d")
    if "month" in lower:
        return (today - timedelta(days=30 * n)).strftime("%Y-%m-%d")
    return fallback


# ── Tavily ─────────────────────────────────────────────────────────────────────
def _next_tavily_key() -> Optional[tuple]:
    """Return (api_key, key_index) of the first non-exhausted Tavily key, or None."""
    for i, key in enumerate(TAVILY_API_KEYS):
        if f"tavily_{i}" not in _exhausted:
            return key, i
    return None


def _mark_tavily_key_exhausted(key_idx: int) -> None:
    """Mark a specific key exhausted; if all keys are exhausted, disable the source."""
    _exhausted.add(f"tavily_{key_idx}")
    if all(f"tavily_{i}" in _exhausted for i in range(len(TAVILY_API_KEYS))):
        _exhausted.add("tavily")
        log.warning("All Tavily keys exhausted — source disabled for remainder of this run")
    else:
        log.warning(
            f"Tavily key {key_idx + 1}/{len(TAVILY_API_KEYS)} exhausted"
            f" — switching to key {key_idx + 2}"
        )


async def _tavily_query(query: str, window: dict, max_results: int = 0) -> List[dict]:
    if not TAVILY_API_KEYS or "tavily" in _exhausted:
        return []
    n = max_results or TAVILY_MAX_RESULTS
    # Cycle through available keys — if one is exhausted mid-run, the next is tried.
    for key_idx, api_key in enumerate(TAVILY_API_KEYS):
        if f"tavily_{key_idx}" in _exhausted:
            continue
        try:
            def _sync_search(ak=api_key):
                client = TavilyClient(ak)
                kwargs = dict(
                    query=query,
                    topic="news",
                    search_depth="basic",
                    max_results=n,
                    start_date=window["from"],
                    end_date=window["to"],
                    exclude_domains=TAVILY_EXCLUDE_DOMAINS,
                )
                return client.search(**kwargs)
            response = await asyncio.to_thread(_sync_search)
            return response.get("results", [])
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in ("429", "quota", "credit", "exceeded", "limit", "usage")):
                _mark_tavily_key_exhausted(key_idx)
                continue  # try next key
            log.error(f"Tavily error for '{query}': {e}")
            return []
    return []


def _to_google_date(yyyymmdd: str) -> str:
    """Converts YYYY-MM-DD to M/D/YYYY — the format Google's cdr tbs filter requires."""
    dt = datetime.strptime(yyyymmdd, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}/{dt.year}"


# ── SerpAPI (Google News tab) ──────────────────────────────────────────────────
async def _serpapi_query(query: str, window: dict, max_results: int = 0) -> List[dict]:
    """
    Fetches news via SerpAPI using Google Web Search + tbm=nws (News tab).
    Returns published_at (ISO datetime) for reliable date parsing.
    Requires SERPAPI_API_KEY in .env.
    """
    if not SERPAPI_API_KEY or "serpapi" in _exhausted:
        return []
    n = max_results or SERPAPI_MAX_RESULTS
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":        "google",
                    "tbm":           "nws",
                    "q":             query,
                    "api_key":       SERPAPI_API_KEY,
                    "num":           n,
                    "google_domain": "google.com",
                    "hl":            "en",
                    "gl":            "us",
                    "tbs":           f"cdr:1,cd_min:{_to_google_date(window['from'])},cd_max:{_to_google_date(window['to'])}",
                }
            )
            resp.raise_for_status()
            return resp.json().get("news_results", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            _exhausted.add("serpapi")
            log.warning("SerpAPI credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"SerpAPI error for '{query}': {e}")
        return []
    except Exception as e:
        log.error(f"SerpAPI error for '{query}': {e}")
        return []


# ── NewsData.io ────────────────────────────────────────────────────────────────
async def _newsdata_query(query: str, window: dict, max_results: int = 0) -> List[dict]:
    """
    Fetches news via NewsData.io.
    Requires NEWSDATA_API_KEY in .env.
    """
    if not NEWSDATA_API_KEY or "newsdata_io" in _exhausted:
        return []
    n = max_results or NEWSDATA_MAX_RESULTS
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://newsdata.io/api/1/news",
                params={
                    "q":               query,
                    "apikey":          NEWSDATA_API_KEY,
                    "language":        "en",
                    "from_date":       window["from"],
                    "to_date":         window["to"],
                    "size":            n,
                    "image":           0,
                    "removeduplicate": 1,
                }
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            _exhausted.add("newsdata_io")
            log.warning("NewsData.io credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"NewsData error for '{query}': {e}")
        return []
    except Exception as e:
        log.error(f"NewsData error for '{query}': {e}")
        return []


# ── NewsAPI.ai (EventRegistry) ────────────────────────────────────────────────
async def _newsai_query(
    entity_name: str,
    entity_type: str,
    domain:      str,
    window:      dict,
) -> List[dict]:
    """
    Fetches news via NewsAPI.ai (EventRegistry) using the official eventregistry
    Python SDK (QueryArticlesIter).

    Called ONCE per entity (not per topic) — EventRegistry is entity-focused and
    appending topic words creates an AND query that returns 0 results.  Topic
    distribution is handled downstream by AI categorisation.

    Fetches NEWSAI_MAX_RESULTS * 4 articles to compensate for no topic pre-filter.
    Requires NEWSAI_API_KEY in .env.
    """
    if not NEWSAI_API_KEY or "newsapi_ai" in _exhausted:
        return []
    try:
        from eventregistry import (
            EventRegistry, QueryArticlesIter,
            ReturnInfo, ArticleInfoFlags,
        )

        keyword_str, kw_loc = _build_newsai_keywords(entity_name, entity_type, domain)

        max_items = NEWSAI_MAX_RESULTS * 4  # fetch more since no topic pre-filter

        def _sync_query() -> list:
            er = EventRegistry(apiKey=NEWSAI_API_KEY, allowUseOfArchive=True)
            q  = QueryArticlesIter(
                keywords=keyword_str,
                keywordsLoc=kw_loc,
                lang="eng",
                dateStart=window["from"],
                dateEnd=window["to"],
                isDuplicateFilter="skipDuplicates",
            )
            return list(q.execQuery(
                er,
                sortBy="date",
                sortByAsc=False,
                returnInfo=ReturnInfo(articleInfo=ArticleInfoFlags(bodyLen=500)),
                maxItems=max_items,
            ))

        results = await asyncio.to_thread(_sync_query)
        return results or []

    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ("429", "quota", "credit", "exceeded", "limit", "usage")):
            _exhausted.add("newsapi_ai")
            log.warning("NewsAPI.ai credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"NewsAPI.ai error for '{entity_name}': {e}")
        return []


# ── Source name helpers ────────────────────────────────────────────────────────
def _url_to_publisher(url: str) -> str:
    """
    Derive a clean publisher name from an article URL.
    Used by Tavily which returns no separate source field.
    e.g. 'https://www.reuters.com/article/...' → 'Reuters'
         'https://finance.yahoo.com/news/...'  → 'Yahoo'
         'https://techcrunch.com/2024/...'     → 'Techcrunch'
    """
    if not url:
        return "Unknown"
    try:
        host = (urlparse(url).hostname or "").lower()
        # Strip leading subdomain prefixes
        for pfx in ("www.", "www2.", "m.", "news.", "feeds.", "finance.", "blog.", "amp."):
            if host.startswith(pfx):
                host = host[len(pfx):]
                break
        # Take the first label only — drops TLD, ccTLD etc.
        # e.g. reuters.com → reuters,  bbc.co.uk → bbc
        name = host.split(".")[0]
        return name.replace("-", " ").title() if name else "Unknown"
    except Exception:
        return "Unknown"


def _slug_to_publisher(slug: str) -> str:
    """Format a NewsData source_id slug into a readable name.
    e.g. 'the-guardian' → 'The Guardian',  'reuters' → 'Reuters'
    """
    if not slug:
        return "Unknown"
    return slug.replace("-", " ").replace("_", " ").title()


# ── Result normalisers ─────────────────────────────────────────────────────────
def _norm_tavily(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title"):
        return None
    if r.get("score", 1.0) < TAVILY_MIN_SCORE:
        return None
    raw_date = r.get("published_date") or ""
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("url", ""),
        "source":           _url_to_publisher(r.get("url", "")),
        "published_date":   _normalise_date(raw_date, "") or None,
        "content":          r.get("content", ""),
        "fetch_source":     "tavily",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_serpapi(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title"):
        return None
    url = r.get("link", "")
    source_raw = r.get("source", "")
    source = source_raw.get("name", "") if isinstance(source_raw, dict) else str(source_raw or "Unknown")
    # published_at (ISO) preferred; fall back to relative-string parser; None if both fail
    pub_date = (
        _normalise_date(r.get("published_at") or "", "") or
        _parse_serpapi_date(r.get("date") or "", "") or
        None
    )
    return {
        "title":            r.get("title", "").strip(),
        "url":              url,
        "source":           source or "Unknown",
        "published_date":   pub_date,
        "content":          r.get("snippet", ""),
        "fetch_source":     "serpapi",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_newsdata(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title"):
        return None
    raw_date = r.get("pubDate") or ""
    source_name = r.get("source_name", "")
    source_id   = r.get("source_id", "")
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("link", ""),
        "source":           source_name or _slug_to_publisher(source_id) if (source_name or source_id) else "Unknown",
        "published_date":   _normalise_date(raw_date, "") or None,
        "content":          r.get("description", "") or r.get("content", ""),
        "language":         r.get("language", ""),
        "fetch_source":     "newsdata_io",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_newsai(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title"):
        return None
    source_obj = r.get("source", {})
    source = source_obj.get("title", "") if isinstance(source_obj, dict) else str(source_obj or "Unknown")
    raw_date = r.get("date") or ""
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("url", ""),
        "source":           source or "Unknown",
        "published_date":   _normalise_date(raw_date, "") or None,
        "content":          r.get("body", ""),
        "fetch_source":     "newsapi_ai",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


# ── Main fetch ─────────────────────────────────────────────────────────────────
# Each of the remaining topic-driven APIs (SerpAPI, NewsData) gets its own query
# builder so either one can be tuned — e.g. to anchor the entity name and reduce
# search drift — without affecting the other.

def _build_serpapi_query(entity_name: str, entity_type: str, domain: str, topic: str) -> str:
    """
    SerpAPI (Google News via tbm=nws) query.
    Natural-language phrasing, no quoted phrase — currently a candidate for tightening,
    since unanchored entity names can drift to topically-adjacent but unrelated results.
    """
    if domain:
        return f"Latest news on {entity_name} {domain} related to {topic}"
    if entity_type in ("client", "prospect"):
        return f"Latest news on {entity_name} company related to {topic}"
    return f"Latest news on {entity_name} related to {topic}"


def _build_newsdata_query(entity_name: str, entity_type: str, domain: str, topic: str) -> str:
    """
    NewsData.io query.
    Natural-language phrasing — avoids quoted strings, which NewsData's `q` param
    does not require and does not reliably improve precision for.
    """
    if domain:
        return f"Latest news on {entity_name} {domain} related to {topic}"
    if entity_type in ("client", "prospect"):
        return f"Latest news on {entity_name} company related to {topic}"
    return f"Latest news on {entity_name} related to {topic}"


def _build_newsai_keywords(entity_name: str, entity_type: str, domain: str) -> tuple:
    """
    NewsAPI.ai (EventRegistry) keyword string + keywordsLoc flag.
    Title-only for companies (precise, avoids arena/city name collisions).
    Title+body for industries (broad terms rarely appear in article titles alone).
    Returns (keyword_str, kw_loc).
    """
    kw_loc = "title" if entity_type in ("client", "prospect") else "title,body"
    parts  = [entity_name]
    if domain and kw_loc != "title":
        parts.append(domain)
    return " ".join(parts), kw_loc


def _build_tavily_company_query(entity_name: str) -> str:
    """
    Query for client/prospect entities.
    Quoted phrase anchors Tavily on the exact company name, preventing
    semantic drift to unrelated companies (e.g. "Mercury" → mining firms).
    """
    return f'"{entity_name}"'


def _build_tavily_industry_query(entity_name: str) -> str:
    """
    Query for industry entities.
    Industry names are meta-category labels ("Financial Services & Banking")
    that never appear verbatim in articles. Strip punctuation so Tavily does
    semantic/keyword matching across the topic area instead.
    """
    return re.sub(r"[&/]", " ", entity_name).strip()


async def fetch_all_news(
    entity_name:    str,
    topics:         List[str],
    window:         dict,
    api_sources:    Optional[List[str]] = None,
    entity_website: str = "",
    entity_type:    str = "client",
) -> Dict:
    """
    Queries all 12 TOPIC_CATEGORIES for the entity using the selected API sources.

    api_sources:  list of any combination of "tavily", "serpapi", "newsdata_io", "newsapi_ai".
                  Defaults to ["tavily"] when None or empty.

    entity_website: if provided, the bare domain is appended to each query so that
                    APIs can distinguish companies sharing a name
                    (e.g. "Mercury" → query includes "mercury.com").

    entity_type:  "client" | "prospect" | "industry" — used to add a "company"
                  qualifier for company entities without a website, preventing
                  name-ambiguity in search results.
    """
    sources      = api_sources if api_sources else DEFAULT_SOURCES
    all_articles = []
    topic_gaps   = []
    domain       = _extract_domain(entity_website)
    first_topic  = topics[0] if topics else (TOPIC_CATEGORIES[0] if TOPIC_CATEGORIES else "")
    is_industry  = entity_type == "industry"

    # Industry entities use higher per-call limits to compensate for broader queries
    _tav_n  = TAVILY_INDUSTRY_MAX_RESULTS   if is_industry else TAVILY_MAX_RESULTS
    _ser_n  = SERPAPI_INDUSTRY_MAX_RESULTS  if is_industry else SERPAPI_MAX_RESULTS
    _ndd_n  = NEWSDATA_INDUSTRY_MAX_RESULTS if is_industry else NEWSDATA_MAX_RESULTS

    # ── Tavily: one entity-level call, no topic subdivision ─────────────────────
    # One call per entity with topic="news" to restrict to news publishers.
    # AI categorises topics downstream — no value in 12 separate topic queries.
    if "tavily" in sources:
        if is_industry:
            _tavily_q = _build_tavily_industry_query(entity_name)
        else:
            _tavily_q = _build_tavily_company_query(entity_name)
        for r in await _tavily_query(_tavily_q, window, max_results=_tav_n):
            item = _norm_tavily(r, first_topic, topics)
            if item:
                all_articles.append(item)

    # ── Topic loop for SerpAPI / NewsData — each builds its own query ───────────
    for topic in TOPIC_CATEGORIES:
        results = []

        if "serpapi" in sources:
            _serpapi_q = _build_serpapi_query(entity_name, entity_type, domain, topic)
            for r in await _serpapi_query(_serpapi_q, window, max_results=_ser_n):
                item = _norm_serpapi(r, topic, topics)
                if item:
                    results.append(item)

        if "newsdata_io" in sources:
            _newsdata_q = _build_newsdata_query(entity_name, entity_type, domain, topic)
            for r in await _newsdata_query(_newsdata_q, window, max_results=_ndd_n):
                item = _norm_newsdata(r, topic, topics)
                if item:
                    results.append(item)

        if results:
            all_articles.extend(results)
        else:
            topic_gaps.append(topic)

    # NewsAPI.ai: one call per entity (not per topic — topic AND kills results)
    if "newsapi_ai" in sources:
        _newsai_n = NEWSAI_INDUSTRY_MAX_RESULTS if is_industry else NEWSAI_MAX_RESULTS
        for r in await _newsai_query(entity_name, entity_type, domain, window):
            item = _norm_newsai(r, first_topic, topics)
            if item:
                all_articles.append(item)

    def parse_date(d) -> datetime:
        if not d:
            return datetime.min
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_articles.sort(key=lambda x: parse_date(x.get("published_date")), reverse=True)

    raw_count = len(all_articles)
    if MAX_ARTICLES_PER_ENTITY and raw_count > MAX_ARTICLES_PER_ENTITY:
        all_articles = all_articles[:MAX_ARTICLES_PER_ENTITY]

    # Scrape real dates for articles where the API returned no date.
    # Run concurrently to keep it fast; assign "NA" if scraping also fails.
    undated = [i for i, a in enumerate(all_articles) if not a.get("published_date")]
    if undated:
        scraped = await asyncio.gather(
            *[_scrape_published_date(all_articles[i]["url"]) for i in undated]
        )
        for idx, date_val in zip(undated, scraped):
            all_articles[idx] = {
                **all_articles[idx],
                "published_date": date_val if date_val else "NA",
            }

    has_any = raw_count > 0
    log.info(
        f"{entity_name}: {raw_count} raw"
        + (f" -> capped to {len(all_articles)}" if MAX_ARTICLES_PER_ENTITY and raw_count > MAX_ARTICLES_PER_ENTITY else "")
        + f" | {len(TOPIC_CATEGORIES) - len(topic_gaps)}/{len(TOPIC_CATEGORIES)} topics hit"
        + f" | sources: {','.join(sources)}"
        + (f" | domain filter: {domain}" if domain else "")
        + f" | window: {window['from']} -> {window['to']}"
    )
    return {"articles": all_articles, "topic_gaps": topic_gaps, "has_any_news": has_any}


# ── Gap report builder ─────────────────────────────────────────────────────────
def build_gap_report(entity_results: List[Dict], window: dict,
                     exhausted_sources=None) -> Dict:
    no_news = []
    gap_map = {}

    for r in entity_results:
        name = r["entity_name"]
        if not r["has_any_news"]:
            no_news.append({"name": name, "type": r["entity_type"]})
        if r["topic_gaps"]:
            gap_map[name] = {
                "entity_type":    r["entity_type"],
                "missing_topics": r["topic_gaps"],
            }

    return {
        "no_news_at_all":    no_news,
        "topic_gaps":        gap_map,
        "period":            datetime.now().strftime("%B %Y"),
        "run_date":          datetime.now().strftime("%Y-%m-%d"),
        "window":            window,
        "exhausted_sources": sorted(exhausted_sources or []),
        "entity_summary": [
            {
                "name":  r["entity_name"],
                "type":  r["entity_type"],
                "raw":   r.get("raw_count",   0),
                "final": r.get("final_count", 0),
            }
            for r in entity_results
        ],
    }
