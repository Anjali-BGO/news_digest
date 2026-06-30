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
SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY")
NEWSDATA_API_KEY      = os.getenv("NEWSDATA_API_KEY")
NEWSAPI_API_KEY       = os.getenv("NEWSAPI_API_KEY")
NEWSAI_API_KEY        = os.getenv("NEWSAI_API_KEY")

TAVILY_MAX_RESULTS    = int(os.getenv("TAVILY_MAX_RESULTS",   4))
SERPAPI_MAX_RESULTS   = int(os.getenv("SERPAPI_MAX_RESULTS",  4))
NEWSDATA_MAX_RESULTS  = int(os.getenv("NEWSDATA_MAX_RESULTS", 4))
NEWSAPI_MAX_RESULTS   = int(os.getenv("NEWSAPI_MAX_RESULTS",  4))
NEWSAI_MAX_RESULTS    = int(os.getenv("NEWSAI_MAX_RESULTS",   4))

MAX_ARTICLES_PER_ENTITY = int(os.getenv("MAX_ARTICLES_PER_ENTITY", 0))  # 0 = no cap

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
        netloc = urlparse(url).netloc
        return netloc.lstrip("www.").lower()
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
async def _tavily_query(query: str, window: dict) -> List[dict]:
    if not TAVILY_API_KEY or "tavily" in _exhausted:
        return []
    try:
        def _sync_search():
            client = TavilyClient(TAVILY_API_KEY)
            return client.search(
                query=query,
                topic="news",
                search_depth="basic",
                max_results=TAVILY_MAX_RESULTS,
                start_date=window["from"],
                end_date=window["to"],
                exclude_domains=TAVILY_EXCLUDE_DOMAINS,
            )
        response = await asyncio.to_thread(_sync_search)
        return response.get("results", [])
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ("429", "quota", "credit", "exceeded", "limit", "usage")):
            _exhausted.add("tavily")
            log.warning("Tavily credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"Tavily error for '{query}': {e}")
        return []


def _to_google_date(yyyymmdd: str) -> str:
    """Converts YYYY-MM-DD to M/D/YYYY — the format Google's cdr tbs filter requires."""
    dt = datetime.strptime(yyyymmdd, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}/{dt.year}"


# ── SerpAPI (Google News tab) ──────────────────────────────────────────────────
async def _serpapi_query(query: str, window: dict) -> List[dict]:
    """
    Fetches news via SerpAPI using Google Web Search + tbm=nws (News tab).
    Returns published_at (ISO datetime) for reliable date parsing.
    Requires SERPAPI_API_KEY in .env.
    """
    if not SERPAPI_API_KEY or "serpapi" in _exhausted:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":        "google",
                    "tbm":           "nws",
                    "q":             query,
                    "api_key":       SERPAPI_API_KEY,
                    "num":           SERPAPI_MAX_RESULTS,
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
async def _newsdata_query(query: str, window: dict) -> List[dict]:
    """
    Fetches news via NewsData.io.
    Requires NEWSDATA_API_KEY in .env.
    """
    if not NEWSDATA_API_KEY or "newsdata" in _exhausted:
        return []
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
                    "size":            NEWSDATA_MAX_RESULTS,
                    "image":           0,
                    "removeduplicate": 1,
                }
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            _exhausted.add("newsdata")
            log.warning("NewsData.io credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"NewsData error for '{query}': {e}")
        return []
    except Exception as e:
        log.error(f"NewsData error for '{query}': {e}")
        return []


# ── NewsAPI.org ────────────────────────────────────────────────────────────────
async def _newsapi_query(query: str, window: dict) -> List[dict]:
    """
    Fetches news via NewsAPI.org (everything endpoint).
    Requires NEWSAPI_API_KEY in .env.
    Free tier: English news, up to 100 req/day, 30-day history.
    """
    if not NEWSAPI_API_KEY or "newsapi" in _exhausted:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q":        query,
                    "apiKey":   NEWSAPI_API_KEY,
                    "language": "en",
                    "from":     window["from"],
                    "to":       window["to"],
                    "pageSize": NEWSAPI_MAX_RESULTS,
                    "sortBy":   "publishedAt",
                }
            )
            resp.raise_for_status()
            # NewsAPI.org returns 200 with status:"error" for rate-limit on some plans
            data = resp.json()
            if data.get("status") == "error" and data.get("code") in ("rateLimited", "maximumResultsReached"):
                _exhausted.add("newsapi")
                log.warning("NewsAPI.org credits exhausted — source disabled for remainder of this run")
                return []
            return data.get("articles", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (426, 429):
            _exhausted.add("newsapi")
            log.warning("NewsAPI.org credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"NewsAPI error for '{query}': {e}")
        return []
    except Exception as e:
        log.error(f"NewsAPI error for '{query}': {e}")
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
    if not NEWSAI_API_KEY or "newsai" in _exhausted:
        return []
    try:
        from eventregistry import (
            EventRegistry, QueryArticlesIter,
            ReturnInfo, ArticleInfoFlags,
        )

        # Title-only for companies (precise, avoids arena/city name collisions).
        # Title+body for industries (broad terms rarely appear in article titles alone).
        kw_loc = "title" if entity_type in ("client", "prospect") else "title,body"

        # For title-only search the domain URL never appears in headlines — omit it.
        # For body search, domain helps disambiguate companies sharing a common name.
        parts = [entity_name]
        if domain and kw_loc != "title":
            parts.append(domain)
        keyword_str = " ".join(parts)

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
            _exhausted.add("newsai")
            log.warning("NewsAPI.ai credits exhausted — source disabled for remainder of this run")
        else:
            log.error(f"NewsAPI.ai error for '{entity_name}': {e}")
        return []


# ── Result normalisers ─────────────────────────────────────────────────────────
def _norm_tavily(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title"):
        return None
    raw_date = r.get("published_date") or ""
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("url", ""),
        "source":           r.get("url", "").split("/")[2] if r.get("url") else "Unknown",
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
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("link", ""),
        "source":           r.get("source_name", "") or r.get("source_id", "Unknown"),
        "published_date":   _normalise_date(raw_date, "") or None,
        "content":          r.get("description", "") or r.get("content", ""),
        "language":         r.get("language", ""),
        "fetch_source":     "newsdata",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_newsapi(r: dict, topic: str, topics: List[str]) -> Optional[dict]:
    if not r.get("title") or r.get("title") == "[Removed]":
        return None
    source_obj = r.get("source", {})
    source = source_obj.get("name", "") if isinstance(source_obj, dict) else str(source_obj)
    raw_date = r.get("publishedAt") or ""
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("url", ""),
        "source":           source or "Unknown",
        "published_date":   _normalise_date(raw_date, "") or None,
        "content":          r.get("description", "") or r.get("content", ""),
        "fetch_source":     "newsapi",
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
        "fetch_source":     "newsai",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


# ── Main fetch ─────────────────────────────────────────────────────────────────
def _build_nl_query(entity_name: str, entity_type: str, domain: str, topic: str) -> str:
    """
    Natural-language query used by all four APIs.
    Format: "Latest news on Stripe (stripe.com) related to <topic>"
    Avoids quoted strings and bare & characters that cause parse errors in some APIs.
    """
    if domain:
        return f"Latest news on {entity_name} {domain} related to {topic}"
    if entity_type in ("client", "prospect"):
        return f"Latest news on {entity_name} company related to {topic}"
    return f"Latest news on {entity_name} related to {topic}"


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

    api_sources:  list of any combination of "tavily", "serpapi", "newsdata", "newsapi", "newsai".
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

    for topic in TOPIC_CATEGORIES:
        nl_query = _build_nl_query(entity_name, entity_type, domain, topic)

        results = []

        if "tavily" in sources:
            for r in await _tavily_query(nl_query, window):
                item = _norm_tavily(r, topic, topics)
                if item:
                    results.append(item)

        if "serpapi" in sources:
            for r in await _serpapi_query(nl_query, window):
                item = _norm_serpapi(r, topic, topics)
                if item:
                    results.append(item)

        if "newsdata" in sources:
            for r in await _newsdata_query(nl_query, window):
                item = _norm_newsdata(r, topic, topics)
                if item:
                    results.append(item)

        if "newsapi" in sources:
            for r in await _newsapi_query(nl_query, window):
                item = _norm_newsapi(r, topic, topics)
                if item:
                    results.append(item)

        if results:
            all_articles.extend(results)
        else:
            topic_gaps.append(topic)

    # NewsAPI.ai: one call per entity (not per topic — topic AND kills results)
    if "newsai" in sources:
        first_topic = topics[0] if topics else (TOPIC_CATEGORIES[0] if TOPIC_CATEGORIES else "")
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
        + f" | {len(TOPIC_CATEGORIES) - len(topic_gaps)}/12 topics hit"
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
