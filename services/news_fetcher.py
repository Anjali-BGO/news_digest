import os
import httpx
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from urllib.parse import urlparse
from dotenv import load_dotenv
from models import TOPIC_CATEGORIES
from logger import get_logger

load_dotenv()

log = get_logger("news_fetcher")

TAVILY_API_KEY        = os.getenv("TAVILY_API_KEY")
GNEWS_API_KEY         = os.getenv("GNEWS_API_KEY")
SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY")
NEWSDATA_API_KEY      = os.getenv("NEWSDATA_API_KEY")

TAVILY_MAX_RESULTS    = int(os.getenv("TAVILY_MAX_RESULTS",   5))
GNEWS_MAX_RESULTS     = int(os.getenv("GNEWS_MAX_RESULTS",    3))
SERPAPI_MAX_RESULTS   = int(os.getenv("SERPAPI_MAX_RESULTS",  5))
NEWSDATA_MAX_RESULTS  = int(os.getenv("NEWSDATA_MAX_RESULTS", 5))

MAX_ARTICLES_PER_ENTITY = int(os.getenv("MAX_ARTICLES_PER_ENTITY", 0))  # 0 = no cap

# Default sources when none are selected
DEFAULT_SOURCES = ["tavily", "gnews"]


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
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt[:len(raw[:19])]).strftime("%Y-%m-%d")
        except Exception:
            continue
    return fallback or datetime.now().strftime("%Y-%m-%d")


# ── Tavily ─────────────────────────────────────────────────────────────────────
async def _tavily_query(query: str, window: dict) -> List[dict]:
    if not TAVILY_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key":             TAVILY_API_KEY,
                    "query":               query,
                    "search_depth":        "advanced",
                    "topic":               "news",
                    "max_results":         TAVILY_MAX_RESULTS,
                    "include_raw_content": False,
                    "days":                max(1, window.get("days", 30)),
                }
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except Exception as e:
        log.error(f"Tavily error for '{query}': {e}")
        return []


# ── GNews ──────────────────────────────────────────────────────────────────────
async def _gnews_query(query: str, window: dict) -> List[dict]:
    if not GNEWS_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://gnews.io/api/v4/search",
                params={
                    "q":      query,
                    "token":  GNEWS_API_KEY,
                    "lang":   "en",
                    "max":    GNEWS_MAX_RESULTS,
                    "from":   window["from"],
                    "to":     window["to"],
                    "sortby": "publishedAt",
                }
            )
            resp.raise_for_status()
            return resp.json().get("articles", [])
    except Exception as e:
        log.error(f"GNews error for '{query}': {e}")
        return []


# ── SerpAPI (Google News) ──────────────────────────────────────────────────────
async def _serpapi_query(query: str, window: dict) -> List[dict]:
    """
    Fetches news via SerpAPI's Google News engine.
    Requires SERPAPI_API_KEY in .env.
    """
    if not SERPAPI_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":  "google_news",
                    "q":       query,
                    "api_key": SERPAPI_API_KEY,
                    "num":     SERPAPI_MAX_RESULTS,
                    "gl":      "us",
                    "hl":      "en",
                    # Date range via Google's tbs param: cdr=1, cd_min/cd_max
                    "tbs":     f"cdr:1,cd_min:{window['from']},cd_max:{window['to']}",
                }
            )
            resp.raise_for_status()
            return resp.json().get("news_results", [])
    except Exception as e:
        log.error(f"SerpAPI error for '{query}': {e}")
        return []


# ── NewsData.io ────────────────────────────────────────────────────────────────
async def _newsdata_query(query: str, window: dict) -> List[dict]:
    """
    Fetches news via NewsData.io.
    Requires NEWSDATA_API_KEY in .env.
    """
    if not NEWSDATA_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://newsdata.io/api/1/news",
                params={
                    "q":         query,
                    "apikey":    NEWSDATA_API_KEY,
                    "language":  "en",
                    "from_date": window["from"],
                    "to_date":   window["to"],
                    "size":      NEWSDATA_MAX_RESULTS,
                }
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except Exception as e:
        log.error(f"NewsData error for '{query}': {e}")
        return []


# ── Result normalisers ─────────────────────────────────────────────────────────
def _norm_tavily(r: dict, topic: str, topics: List[str], fallback_date: str) -> Optional[dict]:
    if not r.get("title"):
        return None
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("url", ""),
        "source":           r.get("url", "").split("/")[2] if r.get("url") else "Unknown",
        "published_date":   _normalise_date(r.get("published_date", fallback_date), fallback_date),
        "content":          r.get("content", ""),
        "fetch_source":     "tavily",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_gnews(a: dict, topic: str, topics: List[str], fallback_date: str) -> Optional[dict]:
    if not a.get("title"):
        return None
    return {
        "title":            a.get("title", "").strip(),
        "url":              a.get("url", ""),
        "source":           a.get("source", {}).get("name", "Unknown"),
        "published_date":   _normalise_date(a.get("publishedAt", fallback_date), fallback_date),
        "content":          a.get("description", "") or a.get("content", ""),
        "fetch_source":     "gnews",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_serpapi(r: dict, topic: str, topics: List[str], fallback_date: str) -> Optional[dict]:
    if not r.get("title"):
        return None
    # SerpAPI may return nested stories; take first story's link if top-level link missing
    url = r.get("link", "")
    if not url and r.get("stories"):
        url = r["stories"][0].get("link", "")
    source = r.get("source", {}).get("name", "") if isinstance(r.get("source"), dict) else str(r.get("source", "Unknown"))
    return {
        "title":            r.get("title", "").strip(),
        "url":              url,
        "source":           source or "Unknown",
        "published_date":   _normalise_date(r.get("date", fallback_date), fallback_date),
        "content":          r.get("snippet", ""),
        "fetch_source":     "serpapi",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


def _norm_newsdata(r: dict, topic: str, topics: List[str], fallback_date: str) -> Optional[dict]:
    if not r.get("title"):
        return None
    return {
        "title":            r.get("title", "").strip(),
        "url":              r.get("link", ""),
        "source":           r.get("source_name", "") or r.get("source_id", "Unknown"),
        "published_date":   _normalise_date(r.get("pubDate", fallback_date), fallback_date),
        "content":          r.get("description", "") or r.get("content", ""),
        "fetch_source":     "newsdata",
        "topic_queried":    topic,
        "is_primary_topic": topic in topics,
    }


# ── Main fetch ─────────────────────────────────────────────────────────────────
async def fetch_all_news(
    entity_name:    str,
    topics:         List[str],
    window:         dict,
    api_sources:    Optional[List[str]] = None,
    entity_website: str = "",
) -> Dict:
    """
    Queries all 12 TOPIC_CATEGORIES for the entity using the selected API sources.

    api_sources: list of any combination of "tavily", "gnews", "serpapi", "newsdata".
                 Defaults to ["tavily", "gnews"] when None or empty.

    entity_website: if provided, the bare domain is appended to each query so that
                    search engines can distinguish between companies sharing a name
                    (e.g. "Mercury" → query includes "mercury.com").
    """
    sources      = api_sources if api_sources else DEFAULT_SOURCES
    all_articles = []
    topic_gaps   = []
    domain       = _extract_domain(entity_website)

    for topic in TOPIC_CATEGORIES:
        # Build query — include domain for disambiguation when available
        if domain:
            query = f'"{entity_name}" ({domain}) {topic}'
        else:
            query = f'"{entity_name}" {topic}'

        results       = []
        fallback_date = window["to"]

        if "tavily" in sources:
            for r in await _tavily_query(query, window):
                item = _norm_tavily(r, topic, topics, fallback_date)
                if item:
                    results.append(item)

        if "gnews" in sources:
            for a in await _gnews_query(query, window):
                item = _norm_gnews(a, topic, topics, fallback_date)
                if item:
                    results.append(item)

        if "serpapi" in sources:
            for r in await _serpapi_query(query, window):
                item = _norm_serpapi(r, topic, topics, fallback_date)
                if item:
                    results.append(item)

        if "newsdata" in sources:
            for r in await _newsdata_query(query, window):
                item = _norm_newsdata(r, topic, topics, fallback_date)
                if item:
                    results.append(item)

        if results:
            all_articles.extend(results)
        else:
            topic_gaps.append(topic)

    def parse_date(d: str) -> datetime:
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_articles.sort(key=lambda x: parse_date(x["published_date"]), reverse=True)

    raw_count = len(all_articles)
    if MAX_ARTICLES_PER_ENTITY and raw_count > MAX_ARTICLES_PER_ENTITY:
        all_articles = all_articles[:MAX_ARTICLES_PER_ENTITY]

    has_any = raw_count > 0
    log.info(
        f"{entity_name}: {raw_count} raw"
        + (f" → capped to {len(all_articles)}" if MAX_ARTICLES_PER_ENTITY and raw_count > MAX_ARTICLES_PER_ENTITY else "")
        + f" | {len(TOPIC_CATEGORIES) - len(topic_gaps)}/12 topics hit"
        + f" | sources: {','.join(sources)}"
        + (f" | domain filter: {domain}" if domain else "")
        + f" | window: {window['from']} → {window['to']}"
    )
    return {"articles": all_articles, "topic_gaps": topic_gaps, "has_any_news": has_any}


# ── Gap report builder ─────────────────────────────────────────────────────────
def build_gap_report(entity_results: List[Dict], window: dict) -> Dict:
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
        "no_news_at_all": no_news,
        "topic_gaps":     gap_map,
        "period":         datetime.now().strftime("%B %Y"),
        "run_date":       datetime.now().strftime("%Y-%m-%d"),
        "window":         window,
    }
