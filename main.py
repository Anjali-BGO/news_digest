import os
import uuid
import io
import html as _html
import pandas as pd
from datetime import datetime
from urllib.parse import quote_plus
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List

from models import Entity, EntityType, AuditEntry, NewsItem, TOPIC_CATEGORIES
from services.news_fetcher import get_current_window
from storage import (
get_entities, save_entity, delete_entity,
save_news_for_entity, get_all_news,
get_audit_log, save_gap_report, get_latest_gap_report,
save_run, log_error, get_run_history, append_audit,
)
from logger import get_logger, get_pipeline_logger

log = get_logger("main")
pipe_log = get_pipeline_logger()

app = FastAPI(title="News Digest Platform")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Source display name mapping ────────────────────────────────────────────────
_SOURCE_MAP: dict = {
"finance.yahoo.com": ("Yahoo Finance", "https://finance.yahoo.com"),
"yahoo finance":  ("Yahoo Finance", "https://finance.yahoo.com"),
"reuters.com":("Reuters",  "https://reuters.com"),
"reuters":   ("Reuters",  "https://reuters.com"),
"bloomberg.com":  ("Bloomberg", "https://bloomberg.com"),
"bloomberg": ("Bloomberg", "https://bloomberg.com"),
"ft.com":("Financial Times",  "https://ft.com"),
"financial times":   ("Financial Times",  "https://ft.com"),
"wsj.com":   ("Wall Street Journal",   "https://wsj.com"),
"wall street journal":("Wall Street Journal",   "https://wsj.com"),
"cnbc.com":  ("CNBC",  "https://cnbc.com"),
"cnbc":   ("CNBC",  "https://cnbc.com"),
"cnn.com":   ("CNN",   "https://cnn.com"),
"cnn":("CNN",   "https://cnn.com"),
"bbc.co.uk": ("BBC News", "https://bbc.co.uk"),
"bbc.com":   ("BBC News", "https://bbc.com"),
"bbc news":  ("BBC News", "https://bbc.com"),
"theguardian.com":   ("The Guardian",  "https://theguardian.com"),
"the guardian":   ("The Guardian",  "https://theguardian.com"),
"nytimes.com":("New York Times",   "https://nytimes.com"),
"new york times": ("New York Times",   "https://nytimes.com"),
"washingtonpost.com": ("Washington Post",  "https://washingtonpost.com"),
"washington post":   ("Washington Post",  "https://washingtonpost.com"),
"forbes.com": ("Forbes",   "https://forbes.com"),
"forbes":("Forbes",   "https://forbes.com"),
"businessinsider.com":("Business Insider", "https://businessinsider.com"),
"business insider":  ("Business Insider", "https://businessinsider.com"),
"techcrunch.com": ("TechCrunch","https://techcrunch.com"),
"techcrunch": ("TechCrunch","https://techcrunch.com"),
"wired.com": ("Wired","https://wired.com"),
"wired":  ("Wired","https://wired.com"),
"theverge.com":   ("The Verge", "https://theverge.com"),
"the verge": ("The Verge", "https://theverge.com"),
"marketwatch.com":   ("MarketWatch",   "https://marketwatch.com"),
"marketwatch":("MarketWatch",   "https://marketwatch.com"),
"seekingalpha.com":  ("Seeking Alpha", "https://seekingalpha.com"),
"seeking alpha":  ("Seeking Alpha", "https://seekingalpha.com"),
"fool.com":  ("The Motley Fool",  "https://fool.com"),
"the motley fool":   ("The Motley Fool",  "https://fool.com"),
"apnews.com": ("AP News",  "https://apnews.com"),
"ap news":   ("AP News",  "https://apnews.com"),
"axios.com": ("Axios","https://axios.com"),
"axios":  ("Axios","https://axios.com"),
"politico.com":   ("Politico", "https://politico.com"),
"politico":  ("Politico", "https://politico.com"),
"investopedia.com":  ("Investopedia",  "https://investopedia.com"),
"investopedia":   ("Investopedia",  "https://investopedia.com"),
"globenewswire.com": ("GlobeNewswire", "https://globenewswire.com"),
"prnewswire.com": ("PR Newswire",   "https://prnewswire.com"),
"businesswire.com":  ("Business Wire", "https://businesswire.com"),
}


def _source_link(source: str) -> str:
    """Returns an anchor tag for a news source, falling back to plain text."""
    if not source:
       return "Unknown"
    key = source.strip().lower()
    entry = _SOURCE_MAP.get(key)
    if entry:
       name, url = entry
       return (
f'<a href="{url}" target="_blank" rel="noopener" '
f'style="color:#2563EB;text-decoration:none;font-weight:500">{name}</a>'
   )
    # domain-like string → make clickable
    if "." in source and "/" not in source and " " not in source:
       safe = _html.escape(source)
       return (
f'<a href="https://{safe}" target="_blank" rel="noopener" '
f'style="color:#2563EB;text-decoration:none;font-weight:500">{safe}</a>'
   )
    return _html.escape(source)


templates.env.filters["source_link"] = _source_link

# ── In-memory job status ───────────────────────────────────────────────────────
job_status: dict = {
"running":   False,
"last_run":  None,
"message":   "",
"cancel":False,
"date_from": "",
"date_to":   "",
}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


    # ── Home: entity manager ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(
request: Request,
msg: str = "",
name:str = "",
added:   int = 0,
skipped: int = 0,
invalid: int = 0,
error:   str = "",
):
    entities  = get_entities()
    all_news  = get_all_news()
    total_news = sum(len(v) for v in all_news.values())

    # Find most recent period and count its approved articles
    period_dates: dict = {}
    for items in all_news.values():
       for item in items:
          if item.period:
             if item.period not in period_dates or item.fetched_date > period_dates[item.period]:
                period_dates[item.period] = item.fetched_date
    current_period = (
   sorted(period_dates.keys(), key=lambda p: period_dates[p], reverse=True)[0]
   if period_dates else ""
)
    current_period_news = sum(
   sum(1 for n in items if n.period == current_period)
   for items in all_news.values()
) if current_period else 0

    # Build source list from whichever API keys are actually configured in .env
    _source_defs = [
        ("tavily",  "Tavily",      "TAVILY_API_KEY",   "Deep semantic search",                True),
        ("serpapi", "SerpAPI",     "SERPAPI_API_KEY",  "Google News via SerpAPI",             False),
        ("newsapi", "NewsAPI",     "NEWSAPI_API_KEY",  "NewsAPI.org aggregator",              False),
        ("newsdata","NewsData",    "NEWSDATA_API_KEY", "NewsData.io aggregator",              False),
        ("newsai",  "NewsAPI.ai",  "NEWSAI_API_KEY",   "NewsAPI.ai / EventRegistry",          False),
    ]
    available_sources = [
        {"value": v, "label": lbl, "title": tip, "checked": default}
        for v, lbl, env_var, tip, default in _source_defs
        if os.getenv(env_var)
    ]

    return templates.TemplateResponse(
   request=request,
   name="index.html",
   context={
"request": request,
"clients": [e for e in entities if e.entity_type == EntityType.client],
"prospects":[e for e in entities if e.entity_type == EntityType.prospect],
"industries":   [e for e in entities if e.entity_type == EntityType.industry],
"topics":  TOPIC_CATEGORIES,
"job_status":   job_status,
"total_news":   total_news,
"current_period_news":  current_period_news,
"current_period":  current_period,
"news_counts":  {eid: len(arts) for eid, arts in all_news.items()},
"available_sources": available_sources,
"msg":  msg,
"msg_name": name,
"msg_added":added,
"msg_skipped":  skipped,
"msg_invalid":  invalid,
"error":   error,
   }
)


    # ── Add entity ─────────────────────────────────────────────────────────────────
@app.post("/add-entity")
async def add_entity(
name:  str  = Form(...),
entity_type:   str  = Form(...),
topics:   List[str] = Form(default=[]),
website:  str  = Form(default=""),
industry_type: str  = Form(default=""),
news_scope:str  = Form(default=""),
aliases:   str  = Form(default=""),
):
    entities = get_entities()
    exists   = any(
   e.name.strip().lower() == name.strip().lower() and
   e.entity_type.value== entity_type
   for e in entities
)
    if exists:
       return RedirectResponse(
f"/?msg=duplicate&name={quote_plus(name)}&type={entity_type}", status_code=303
   )
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    save_entity(Entity(
   id=str(uuid.uuid4()),
   name=name,
   entity_type=EntityType(entity_type),
   topics=topics,
   website=website.strip(),
   industry_type=industry_type.strip() if entity_type == "industry" else "",
   news_scope=news_scope.strip()  if entity_type == "industry" else "",
   aliases=alias_list,
))
    return RedirectResponse(f"/?msg=added&name={quote_plus(name)}", status_code=303)


    # ── Upload CSV / Excel ─────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    filename = file.filename or ""

    # Guard — no file selected
    if not filename:
       return RedirectResponse("/?msg=upload&added=0&skipped=0&invalid=0&error=no_file", status_code=303)

    # Guard — unsupported format
    if not filename.lower().endswith((".csv", ".xlsx", ".xls")):
       return RedirectResponse("/?msg=upload&added=0&skipped=0&invalid=0&error=bad_format", status_code=303)

    contents = await file.read()

    # Guard — empty file
    if not contents:
       return RedirectResponse("/?msg=upload&added=0&skipped=0&invalid=0&error=empty", status_code=303)

    try:
       df = pd.read_csv(io.BytesIO(contents)) \
     if filename.lower().endswith(".csv") \
     else pd.read_excel(io.BytesIO(contents))
    except Exception as e:
       log.error(f"File parse error: {e}")
       return RedirectResponse("/?msg=upload&added=0&skipped=0&invalid=0&error=parse", status_code=303)

    df.columns = [c.strip().lower() for c in df.columns]
    if not {"name", "type"}.issubset(set(df.columns)):
       return RedirectResponse("/?msg=upload&added=0&skipped=0&invalid=0&error=missing_cols", status_code=303)

    existing   = get_entities()
    exist_keys = {
   (e.name.strip().lower(), e.entity_type.value)
   for e in existing
}

    added = skipped = invalid = 0
    for _, row in df.iterrows():
       name  = str(row.get("name", "")).strip()
       etype = str(row.get("type", "client")).strip().lower()

       if not name or etype not in ("client", "prospect", "industry"):
          invalid += 1
          continue

       if (name.lower(), etype) in exist_keys:
          skipped += 1
          continue

       website_raw  = str(row.get("website",  "")).strip()
       industry_type_raw = str(row.get("industry_type", "")).strip()
       news_scope_raw= str(row.get("news_scope","")).strip()
       website  = website_raw  if website_raw  not in ("", "nan") else ""
       industry_type = industry_type_raw if industry_type_raw not in ("", "nan") else ""
       news_scope= news_scope_raw if news_scope_raw not in ("", "nan") else ""

       # Topics in CSV use ";" as separator (category names contain commas)
       topics_raw = str(row.get("topics", "")).strip()
       if topics_raw and topics_raw != "nan":
          sep = ";" if ";" in topics_raw else "|" if "|" in topics_raw else ","
          topics = [t.strip() for t in topics_raw.split(sep) if t.strip()]
       else:
          topics = []

       save_entity(Entity(
id=str(uuid.uuid4()),
name=name,
entity_type=EntityType(etype),
topics=topics,
website=website,
industry_type=industry_type if etype == "industry" else "",
news_scope=news_scope  if etype == "industry" else "",
   ))
       exist_keys.add((name.lower(), etype))
       added += 1

    return RedirectResponse(
   f"/?msg=upload&added={added}&skipped={skipped}&invalid={invalid}",
   status_code=303
)


    # ── Delete entity ──────────────────────────────────────────────────────────────
@app.post("/delete-entity/{entity_id}")
async def remove_entity(entity_id: str):
    entities = get_entities()
    entity   = next((e for e in entities if e.id == entity_id), None)
    name = entity.name if entity else "Entity"
    delete_entity(entity_id)
    return RedirectResponse(f"/?msg=deleted&name={quote_plus(name)}", status_code=303)


    # ── Cancel running job ─────────────────────────────────────────────────────────
@app.post("/cancel-fetch")
async def cancel_fetch():
    if job_status["running"]:
       job_status["cancel"]  = True
       job_status["message"] = "Cancelling…"
    return RedirectResponse("/", status_code=303)


    # ── Job status (polled by app.js) ──────────────────────────────────────────────
@app.get("/job-status")
async def job_status_endpoint():
    return JSONResponse(job_status)


    # ── Trigger full refresh (background) ─────────────────────────────────────────
@app.post("/fetch-all-news")
async def fetch_all_news_route(
background_tasks:   BackgroundTasks,
period_days:   str  = Form(default="30"),
date_from:  str  = Form(default=""),
date_to:str  = Form(default=""),
entity_type_filter: str  = Form(default="all"),
api_sources:   List[str] = Form(default=[]),
):
    if job_status["running"]:
       return RedirectResponse("/?msg=already_running", status_code=303)

    from services.news_fetcher import resolve_window, DEFAULT_SOURCES
    window  = resolve_window(period_days, date_from, date_to)
    sources = api_sources if api_sources else DEFAULT_SOURCES

    job_status["running"]   = True
    job_status["cancel"]= False
    job_status["message"]   = "Processing…"
    job_status["date_from"] = window["from"]
    job_status["date_to"]   = window["to"]

    background_tasks.add_task(run_full_pipeline, window, entity_type_filter, sources)
    return RedirectResponse("/", status_code=303)


    # ── Single entity refresh (background) ────────────────────────────────────────
@app.post("/fetch-news/{entity_id}")
async def fetch_one(
entity_id:   str,
background_tasks: BackgroundTasks,
period_days: str = Form(default="30"),
date_from:   str = Form(default=""),
date_to: str = Form(default=""),
):
    if job_status["running"]:
       return RedirectResponse("/?msg=already_running", status_code=303)
    entities = get_entities()
    entity   = next((e for e in entities if e.id == entity_id), None)
    if not entity:
       raise HTTPException(404, "Entity not found")
    from services.news_fetcher import resolve_window
    window = resolve_window(period_days, date_from, date_to)
    job_status["running"] = True
    job_status["cancel"]  = False
    job_status["message"] = f"Fetching news for {entity.name}…"
    background_tasks.add_task(run_entity_pipeline, entity, window)
    return RedirectResponse("/digest", status_code=303)


    # ── Digest view ────────────────────────────────────────────────────────────────
@app.get("/digest", response_class=HTMLResponse)
async def digest(request: Request, tab: str = "client", period: str = ""):
    entities   = get_entities()
    all_news   = get_all_news()
    gap_report = get_latest_gap_report()

    # Collect unique periods sorted by most-recent fetched_date within each period
    period_dates = {}
    for items in all_news.values():
       for item in items:
          if item.period:
             if item.period not in period_dates or item.fetched_date > period_dates[item.period]:
                period_dates[item.period] = item.fetched_date
    all_periods = sorted(period_dates.keys(), key=lambda p: period_dates[p], reverse=True)

    if period == "all":
        selected_period = "all"
    else:
        selected_period = period if period in all_periods else (all_periods[0] if all_periods else "")

    def entity_count(etype):
       return sum(1 for e in entities if e.entity_type == etype)

    if selected_period == "all":
        window_label = "All Time"
    else:
        window_label = selected_period or get_current_window()["label"]

    return templates.TemplateResponse(
   request=request,
   name="digest.html",
   context={
"request": request,
"active_tab": tab,
"clients":    _group_news_by_entity(entities, all_news, EntityType.client,    selected_period, require_news=True),
"prospects":  _group_news_by_entity(entities, all_news, EntityType.prospect,  selected_period, require_news=True),
"industries": _group_news_by_entity(entities, all_news, EntityType.industry,  selected_period, require_news=True),
"total_clients":   entity_count(EntityType.client),
"total_prospects": entity_count(EntityType.prospect),
"total_industries":entity_count(EntityType.industry),
"job_status": job_status,
"gap_report": gap_report,
"window":  {"label": window_label},
"all_periods": all_periods,
"selected_period": selected_period,
   }
)


    # ── Gap report view ────────────────────────────────────────────────────────────
@app.get("/gaps", response_class=HTMLResponse)
async def gap_view(request: Request):
    gap_report = get_latest_gap_report()
    return templates.TemplateResponse(
   request=request,
   name="gaps.html",
   context={
"request":request,
"gap_report": gap_report,
   }
)


    # ── Audit log view ─────────────────────────────────────────────────────────────
@app.get("/audit", response_class=HTMLResponse)
async def audit_view(request: Request):
    all_entries  = get_audit_log()
    total_count  = len(all_entries)
    limit   = 500
    entries = all_entries[-limit:] if total_count > limit else all_entries
    return templates.TemplateResponse(
   request=request,
   name="audit.html",
   context={
"request": request,
"entries": entries,
"total_count": total_count,
"limit":  limit,
   }
)


    # ── Run history view ──────────────────────────────────────────────────────────
@app.get("/run-history", response_class=HTMLResponse)
async def run_history_view(request: Request):
    runs = get_run_history()
    return templates.TemplateResponse(
   request=request,
   name="run_history.html",
   context={
"request":request,
"runs":  list(reversed(runs)),   # newest first
"job_status": job_status,
   }
)


    # ── Re-validate stored URLs (background) ──────────────────────────────────────
@app.post("/revalidate-urls")
async def revalidate_urls_route(
    background_tasks: BackgroundTasks,
    return_tab: str    = Form(default="client"),
    return_period: str = Form(default=""),
):
    """
    Re-checks every stored article that is currently marked url_status='invalid'.
    Uses the updated validator logic (only 404/410/DNS = truly invalid) so that
    articles incorrectly flagged by Cloudflare bot-blocks get corrected without
    re-fetching any news or consuming API credits.
    """
    if job_status["running"]:
        return RedirectResponse("/?msg=already_running", status_code=303)
    job_status["running"] = True
    job_status["cancel"]  = False
    job_status["message"] = "Re-validating stored URLs..."
    background_tasks.add_task(_run_revalidation)
    dest = f"/digest?tab={return_tab}"
    if return_period:
        dest += f"&period={quote_plus(return_period)}"
    return RedirectResponse(dest, status_code=303)


async def _run_revalidation() -> None:
    import asyncio
    from services.hyperlink_validator import revalidate_stored_article
    from storage import DATA_FILE, _load, _save

    fixed   = 0
    checked = 0
    try:
        data = _load(DATA_FILE)
        news = data.get("news", {})

        # Collect every article across all entities
        all_refs = [
            (eid, idx, art)
            for eid, arts in news.items()
            for idx, art in enumerate(arts)
            if art.get("url")
        ]
        checked = len(all_refs)

        job_status["message"] = f"Re-validating {checked} article URLs..."

        # Check all URLs concurrently
        new_statuses = await asyncio.gather(
            *[revalidate_stored_article(art.get("url", "")) for _, _, art in all_refs]
        )

        changed_eids = set()
        for (eid, idx, art), new_status in zip(all_refs, new_statuses):
            old_status = art.get("url_status", "")
            if new_status != old_status:
                news[eid][idx] = {**art, "url_status": new_status}
                fixed += 1
                changed_eids.add(eid)
                log.info(
                    f"URL status {old_status!r} -> {new_status!r}: {art.get('url', '')[:80]}"
                )

        if fixed:
            data["news"] = news
            _save(DATA_FILE, data)

        msg = f"URL re-validation done — {fixed} of {checked} statuses updated"
    except Exception as e:
        log.error(f"_run_revalidation failed: {e}")
        msg = f"URL re-validation failed: {e}"
    finally:
        completed_at = datetime.now()
        _finish_job(completed_at, msg)
    log.info(f"Re-validation complete: {fixed}/{checked} articles checked")


    # ── Fix stored published dates (background) ────────────────────────────────────
@app.post("/fix-dates")
async def fix_dates_route(
    background_tasks: BackgroundTasks,
    return_tab: str    = Form(default="client"),
    return_period: str = Form(default=""),
):
    """
    Scrapes every stored article page to recover the real published date.
    Targets articles whose date is still the pipeline fallback value (the date the
    pipeline ran) — i.e. all articles with the same date, which indicates Tavily
    returned null and we assigned yesterday as the fallback.
    """
    if job_status["running"]:
        return RedirectResponse("/?msg=already_running", status_code=303)
    job_status["running"] = True
    job_status["cancel"]  = False
    job_status["message"] = "Scraping article pages to recover real published dates..."
    background_tasks.add_task(_run_fix_dates)
    dest = f"/digest?tab={return_tab}"
    if return_period:
        dest += f"&period={quote_plus(return_period)}"
    return RedirectResponse(dest, status_code=303)


async def _run_fix_dates() -> None:
    import asyncio
    from collections import Counter
    from services.news_fetcher import _scrape_published_date
    from storage import DATA_FILE, _load, _save

    fixed = 0
    total_targets = 0
    try:
        data = _load(DATA_FILE)
        news = data.get("news", {})

        all_articles_flat = [
            (eid, idx, a)
            for eid, arts in news.items()
            for idx, a in enumerate(arts)
        ]

        # Detect suspect fallback dates two ways:
        # 1. Overall: if >= 50% of ALL articles share one date → likely a pipeline fallback
        # 2. SerpAPI-specific: if >= 60% of SerpAPI-sourced articles share one date that is
        #    recent (within 2 days of today) → the old _normalise_date bug assigned run-date
        from datetime import date as _date, timedelta as _td
        today_str = _date.today().isoformat()
        recent = {(_date.today() - _td(days=i)).isoformat() for i in range(3)}

        date_counts = Counter(
            a.get("published_date", "") for _, _, a in all_articles_flat
        )
        total = len(all_articles_flat)
        most_common_date, most_common_count = (
            date_counts.most_common(1)[0] if date_counts else ("", 0)
        )
        suspect_dates = set()
        if total > 0 and most_common_count / total >= 0.5 and most_common_date:
            suspect_dates.add(most_common_date)

        # Per-source: if >= 60% of a source's articles share one recent date,
        # that date was likely injected (e.g. SerpAPI relative dates → run-date,
        # or any future parser bug). Applied to every source generically.
        for src in ("serpapi", "tavily", "newsdata", "newsapi", "newsai"):
            src_arts = [(eid, idx, a) for eid, idx, a in all_articles_flat
                        if a.get("fetch_source") == src]
            if len(src_arts) < 5:
                continue
            src_counts = Counter(a.get("published_date", "") for _, _, a in src_arts)
            src_top_date, src_top_count = src_counts.most_common(1)[0]
            if src_top_date and src_top_count / len(src_arts) >= 0.6 and src_top_date in recent:
                suspect_dates.add(src_top_date)

        targets = [
            (eid, idx, a)
            for eid, idx, a in all_articles_flat
            if a.get("published_date") == "NA"
            or a.get("published_date") in suspect_dates
        ]
        total_targets = len(targets)

        if not targets:
            msg = "Fix dates: no suspicious dates found — all dates look real"
        else:
            job_status["message"] = (
                f"Scraping {total_targets} article pages"
                + (f" (suspect dates: {', '.join(sorted(suspect_dates))})" if suspect_dates else "")
                + "..."
            )
            log.info(f"Fix-dates: {total_targets} articles to scrape (suspect={suspect_dates})")

            urls    = [a["url"] for _, _, a in targets]
            scraped = await asyncio.gather(*[_scrape_published_date(u) for u in urls])

            for (eid, idx, _), new_date in zip(targets, scraped):
                art      = news[eid][idx]
                old_date = art.get("published_date", "")
                resolved = new_date if new_date else "NA"
                if resolved != old_date:
                    news[eid][idx] = {**art, "published_date": resolved}
                    fixed += 1
                    pipe_log.info(
                        f"  FIX-DATE [{eid}] {old_date} -> {resolved} | {art.get('title','')[:70]}"
                    )

            if fixed:
                data["news"] = news
                _save(DATA_FILE, data)

            msg = f"Date fix done — {fixed} of {total_targets} updated" + (f" | fixed dates: {', '.join(sorted(suspect_dates))}" if suspect_dates else "")

    except Exception as e:
        log.error(f"_run_fix_dates failed: {e}")
        msg = f"Date fix failed: {e}"
    finally:
        completed_at = datetime.now()
        _finish_job(completed_at, msg)
    log.info(f"Fix-dates complete: {fixed}/{total_targets} articles updated")


    # ── Excel export ───────────────────────────────────────────────────────────────
@app.get("/export/excel")
async def export_excel(period: str = ""):
    from services.excel_exporter import generate_excel_report

    entities = get_entities()
    all_news = get_all_news()
    window   = get_current_window()

    excel_bytes = generate_excel_report(
   _group_news_by_entity(entities, all_news, EntityType.client,   period),
   _group_news_by_entity(entities, all_news, EntityType.prospect, period),
   _group_news_by_entity(entities, all_news, EntityType.industry, period),
   window=window,
   gap_report=get_latest_gap_report() or {},
)
    fname = f"news_digest_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
   io.BytesIO(excel_bytes),
   media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
   headers={"Content-Disposition": f"attachment; filename={fname}"}
)


    # ── Full analytics export (all articles + audit trail) ────────────────────────
@app.get("/export/excel-full")
async def export_excel_full():
    from services.excel_exporter import generate_full_analytics_report

    entities  = get_entities()
    all_news  = get_all_news()
    audit  = get_audit_log()
    window = get_current_window()

    excel_bytes = generate_full_analytics_report(entities, all_news, audit, window=window)
    fname = f"news_full_analytics_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
   io.BytesIO(excel_bytes),
   media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
   headers={"Content-Disposition": f"attachment; filename={fname}"}
)


# ── Shared pipeline helpers ────────────────────────────────────────────────────

def _filtered_entities(entity_type_filter: str) -> list:
    """Return entities filtered by type; 'all' or '' returns every entity."""
    entities = get_entities()
    if entity_type_filter and entity_type_filter != "all":
        return [e for e in entities if e.entity_type.value == entity_type_filter]
    return entities


def _finish_job(completed_at: datetime, message: str) -> None:
    """Clear the in-memory job-status after a pipeline completes or is cancelled."""
    job_status["running"]  = False
    job_status["cancel"]   = False
    job_status["last_run"] = completed_at.strftime("%d %b %Y, %H:%M")
    job_status["message"]  = message


def _group_news_by_entity(entities, all_news, etype, period: str = "",
                          require_news: bool = False) -> list:
    """Group news items by entity for a given EntityType, optionally filtered by period."""
    result = []
    for e in entities:
        if e.entity_type == etype:
            news = all_news.get(e.id, [])
            if period and period != "all":
                news = [n for n in news if n.period == period]
            if require_news and not news:
                continue
            result.append({"entity": e, "news": news})
    return result


def _make_news_item(art: dict, result: dict, entity, window: dict) -> NewsItem:
    """Construct a NewsItem from a fetched article dict and AI summariser result."""
    return NewsItem(
        title=art["title"],
        url=art["url"],
        source=art["source"],
        published_date=art.get("published_date") or "NA",
        fetched_date=datetime.now().strftime("%Y-%m-%d"),
        period=window.get("label", datetime.now().strftime("%B %Y")),
        summary=result["summary"],
        primary_category=result["primary_category"],
        secondary_category=result.get("secondary_category", ""),
        entity_id=entity.id,
        entity_type=entity.entity_type.value,
        url_status=art.get("url_status", "ok"),
        original_url=art.get("original_url", ""),
        paywall_note=art.get("paywall_note", ""),
        topic_queried=art.get("topic_queried", ""),
        is_primary_topic=art.get("is_primary_topic", False),
        fetch_source=art.get("fetch_source", ""),
    )


def _build_rejection_audit_entries(entity, dup_log: list, val_log: list,
                                   linked: list) -> list:
    """Return AuditEntry objects for all duplicate/validation/hyperlink rejections."""
    today   = datetime.now().strftime("%Y-%m-%d")
    entries = []
    for d in dup_log:
        entries.append(AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            article_title=d["title"], action="duplicate_removed",
            reason=d["reason"], source_url=d.get("url", ""),
        ))
    for v in val_log:
        entries.append(AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            article_title=v["title"], action="validation_rejected",
            reason=v["reason"], source_url=v.get("url", ""),
        ))
    for a in linked:
        if a.get("url_status") in ("invalid", "unknown"):
            entries.append(AuditEntry(
                run_date=today, entity_id=entity.id, entity_name=entity.name,
                article_title=a["title"], action="url_invalid",
                reason=f"URL status: {a.get('url_status')} — {a.get('url', '')}",
                source_url=a.get("url", ""),
            ))
    return entries


def _build_accepted_audit_entries(entity, news_items: list, today: str) -> list:
    """Return AuditEntry objects for every article that passed all pipeline checks."""
    return [
        AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            article_title=item.title, action="accepted",
            reason="Passed all checks", source_url=item.url,
        )
        for item in news_items
    ]


    # ── Full pipeline (background task) ───────────────────────────────────────────
async def run_full_pipeline(window: dict, entity_type_filter: str = "all", api_sources: list | None = None) -> None:
    from services.news_fetcher   import fetch_all_news, build_gap_report, reset_exhausted_sources, get_exhausted_sources
    from services.deduplicator   import deduplicate
    from services.validator      import validate_articles
    from services.ai_summarizer  import summarize_article
    from services.hyperlink_validator import validate_hyperlinks
    from storage import save_stage_snapshot, rotate_snapshots

    started_at = datetime.now()
    run_id     = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    rotate_snapshots(keep=5)   # keep last 5 runs; remove older ones
    entities   = _filtered_entities(entity_type_filter)
    audit_entries  = []
    entity_results = []
    total_articles = 0
    total_dupes = 0
    total_rejected = 0
    total_ai_calls = 0   # every OpenAI call made (incl. retries)
    total_ai_retries= 0   # articles that needed a second call
    total_prompt_tokens = 0
    total_completion_tokens = 0

    job_status["cancel"] = False
    reset_exhausted_sources()
    pipe_log.info(f"Pipeline started — window: {window.get('label')} | entities: {len(entities)}")

    for entity in entities:
       if job_status["cancel"]:
          pipe_log.info("Pipeline cancelled by user.")
          break

       # Stop early if every selected source has run out of credits
       exhausted_now = get_exhausted_sources()
       if api_sources and all(s in exhausted_now for s in api_sources):
          pipe_log.warning(
              f"All selected API sources exhausted ({', '.join(sorted(exhausted_now))}) "
              f"— stopping pipeline after {len(entity_results)} entities"
          )
          job_status["message"] = (
              f"Stopped — API credits exhausted for: {', '.join(sorted(exhausted_now))}"
          )
          break

       try:
          pipe_log.info(f"Processing: {entity.name}")

          # 1. Fetch
          fetch_result = await fetch_all_news(
   entity.name, entity.topics, window,
   api_sources=api_sources,
   entity_website=entity.website or "",
   entity_type=entity.entity_type.value,
)
          raw  = fetch_result["articles"]
          topic_gaps   = fetch_result["topic_gaps"]
          has_any_news = fetch_result["has_any_news"]

          entity_results.append({
   "entity_name":  entity.name,
   "entity_type":  entity.entity_type.value,
   "topic_gaps":   topic_gaps,
   "has_any_news": has_any_news,
   "raw_count":    len(raw),
   "final_count":  0,   # filled in after save
})

          # 1b. Log every raw article fetched (title + source + topic + raw date)
          for a in raw:
             pipe_log.info(
f"  FETCHED [{entity.name}] {a.get('fetch_source','?')} | "
f"topic={a.get('topic_queried','?')} | "
f"date={a.get('published_date') or 'NA'} | "
f"{a.get('title','')[:80]} | {a.get('url','')}"
   )
          save_stage_snapshot(run_id, entity.name, "1_raw", raw)

          # 2. Deduplicate
          deduped, dup_log = deduplicate(raw)
          total_dupes += len(dup_log)
          save_stage_snapshot(run_id, entity.name, "2_deduped",         deduped)
          save_stage_snapshot(run_id, entity.name, "2_dedup_rejected",  dup_log)

          # 3. Validate
          validated, val_log = await validate_articles(
   deduped, window,
   entity_name=entity.name,
   entity_website=entity.website or "",
   entity_aliases=entity.aliases or [],
)
          total_rejected += len(val_log)
          save_stage_snapshot(run_id, entity.name, "3_validated",            validated)
          save_stage_snapshot(run_id, entity.name, "3_validation_rejected",  val_log)

          # 4. Hyperlink check
          linked = await validate_hyperlinks(validated)
          save_stage_snapshot(run_id, entity.name, "4_hyperlinked", linked)

          # 5. Audit entries — dedup + validation + hyperlink rejections
          audit_entries.extend(_build_rejection_audit_entries(entity, dup_log, val_log, linked))
          today = datetime.now().strftime("%Y-%m-%d")

          # 6. Summarise + categorise
          news_items = []
          for art in linked:
             try:
                result = await summarize_article(
   art["title"], art.get("content", ""), entity.name
)
                # Accumulate AI usage stats from every article
                calls = result.get("ai_calls", 1)
                total_ai_calls += calls
                total_ai_retries+= 1 if calls > 1 else 0
                total_prompt_tokens += result.get("prompt_tokens", 0)
                total_completion_tokens += result.get("completion_tokens", 0)

                news_items.append(_make_news_item(art, result, entity, window))
             except Exception as e:
                log_error("summarize_article", str(e), entity.name)

          # 7. Save — even if partial
          save_news_for_entity(entity.id, news_items)
          save_stage_snapshot(run_id, entity.name, "5_summarized", [i.model_dump() for i in news_items])
          total_articles += len(news_items)
          entity_results[-1]["final_count"] = len(news_items)

          # 7b. Audit entries — every accepted/saved article
          audit_entries.extend(_build_accepted_audit_entries(entity, news_items, today))

          pipe_log.info(f"Done: {entity.name} — {len(news_items)} articles saved")

       except Exception as e:
          log_error("run_full_pipeline", str(e), entity.name)
          pipe_log.error(f"Failed: {entity.name} — {e}")
          # continue to next entity rather than crashing entire pipeline

    # 8. Save audit + gap report
    exhausted_sources = get_exhausted_sources()
    try:
       if audit_entries:
          append_audit(audit_entries)
       save_gap_report(build_gap_report(entity_results, window, exhausted_sources))
    except Exception as e:
       log_error("save_gap_report", str(e))

    # 9. Save run history
    completed_at = datetime.now()
    save_run({
   "run_date":  started_at.strftime("%Y-%m-%d"),
   "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
   "completed_at":   completed_at.strftime("%Y-%m-%d %H:%M:%S"),
   "duration_seconds":  (completed_at - started_at).seconds,
   "status":"cancelled" if job_status["cancel"] else "completed",
   "window_label":   window.get("label", ""),
   "total_entities": len(entities),
   "total_articles": total_articles,
   "duplicates_removed": total_dupes,
   "articles_rejected": total_rejected,
   "ai_calls":  total_ai_calls,
   "ai_retries": total_ai_retries,
   "prompt_tokens":  total_prompt_tokens,
   "completion_tokens": total_completion_tokens,
   "exhausted_sources": sorted(exhausted_sources),
})

    was_cancelled = job_status["cancel"]   # capture before clearing
    exhausted_msg = (
        f" | credits exhausted: {', '.join(sorted(exhausted_sources))}"
        if exhausted_sources else ""
    )
    _finish_job(completed_at, (
        "Cancelled by user." if was_cancelled
        else f"Done — {completed_at.strftime('%d %b %Y, %H:%M')} | {total_articles} articles{exhausted_msg}"
    ))
    pipe_log.info(
        f"Pipeline complete — {total_articles} articles | "
        f"{total_dupes} dupes | {total_rejected} rejected | "
        f"{(completed_at - started_at).seconds}s"
        + exhausted_msg
    )


    # ── Single entity pipeline (background task) ──────────────────────────────────
async def run_entity_pipeline(entity, window: dict, api_sources: list | None = None) -> None:
    from services.news_fetcher   import fetch_all_news, DEFAULT_SOURCES, reset_exhausted_sources, get_exhausted_sources
    from services.deduplicator   import deduplicate
    from services.validator      import validate_articles
    from services.ai_summarizer  import summarize_article
    from services.hyperlink_validator import validate_hyperlinks
    from storage import save_stage_snapshot, rotate_snapshots

    started_at = datetime.now()
    run_id     = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    rotate_snapshots(keep=5)
    total_ai_calls = 0
    total_ai_retries= 0
    total_prompt_tokens = 0
    total_completion_tokens = 0

    reset_exhausted_sources()
    sources = api_sources if api_sources else DEFAULT_SOURCES
    pipe_log.info(f"Single entity pipeline: {entity.name} | window: {window.get('label')} | sources: {sources}")

    fetch_result = await fetch_all_news(
   entity.name, entity.topics, window,
   api_sources=sources,
   entity_website=entity.website or "",
   entity_type=entity.entity_type.value,
)
    raw = fetch_result["articles"]

    if job_status["cancel"]:
       pipe_log.info(f"Single entity pipeline cancelled after fetch: {entity.name}")
       completed_at = datetime.now()
       save_run({
"run_date": started_at.strftime("%Y-%m-%d"),
"started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
"completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
"duration_seconds": (completed_at - started_at).seconds,
"status": "cancelled",
"window_label": window.get("label", ""),
"entity_name": entity.name,
"total_entities": 1,
"total_articles": 0,
"duplicates_removed": 0,
"articles_rejected": 0,
"ai_calls": 0, "ai_retries": 0,
"prompt_tokens": 0, "completion_tokens": 0,
   })
       _finish_job(completed_at, f"Cancelled — {entity.name}")
       return

    # log each raw article so we can see what was fetched
    for a in raw:
       pipe_log.info(
f"  FETCHED [{entity.name}] {a.get('fetch_source','?')} | "
f"topic={a.get('topic_queried','?')} | "
f"date={a.get('published_date') or 'NA'} | "
f"{a.get('title','')[:80]}"
   )
    save_stage_snapshot(run_id, entity.name, "1_raw", raw)

    deduped, dup_log   = deduplicate(raw)
    save_stage_snapshot(run_id, entity.name, "2_deduped",        deduped)
    save_stage_snapshot(run_id, entity.name, "2_dedup_rejected", dup_log)

    validated, val_log = await validate_articles(
   deduped, window,
   entity_name=entity.name,
   entity_website=entity.website or "",
   entity_aliases=entity.aliases or [],
)
    save_stage_snapshot(run_id, entity.name, "3_validated",           validated)
    save_stage_snapshot(run_id, entity.name, "3_validation_rejected", val_log)

    linked = await validate_hyperlinks(validated)
    save_stage_snapshot(run_id, entity.name, "4_hyperlinked", linked)

    # Build audit entries for every rejection
    today         = datetime.now().strftime("%Y-%m-%d")
    audit_entries = _build_rejection_audit_entries(entity, dup_log, val_log, linked)

    pipe_log.info(
   f"  {entity.name}: {len(raw)} raw | {len(dup_log)} dupes | "
   f"{len(val_log)} rejected | {len(linked)} passed validation"
)

    news_items = []
    for art in linked:
       if job_status["cancel"]:
          pipe_log.info(f"Single entity pipeline cancelled during summarize: {entity.name}")
          break
       try:
          result = await summarize_article(
   art["title"], art.get("content", ""), entity.name
)
          calls = result.get("ai_calls", 1)
          total_ai_calls += calls
          total_ai_retries+= 1 if calls > 1 else 0
          total_prompt_tokens += result.get("prompt_tokens", 0)
          total_completion_tokens += result.get("completion_tokens", 0)
          news_items.append(_make_news_item(art, result, entity, window))
       except Exception as e:
          log_error("run_entity_pipeline.summarize", str(e), entity.name)

    save_news_for_entity(entity.id, news_items)
    save_stage_snapshot(run_id, entity.name, "5_summarized", [i.model_dump() for i in news_items])

    # Audit — accepted articles + all rejections
    audit_entries.extend(_build_accepted_audit_entries(entity, news_items, today))

    if audit_entries:
       try:
          append_audit(audit_entries)
       except Exception as e:
          log_error("run_entity_pipeline.audit", str(e), entity.name)

    completed_at  = datetime.now()
    was_cancelled = job_status["cancel"]
    save_run({
   "run_date":   started_at.strftime("%Y-%m-%d"),
   "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
   "completed_at":  completed_at.strftime("%Y-%m-%d %H:%M:%S"),
   "duration_seconds":   (completed_at - started_at).seconds,
   "status": "cancelled" if was_cancelled else "completed",
   "window_label":  window.get("label", ""),
   "entity_name":   entity.name,
   "total_entities": 1,
   "total_articles": len(news_items),
   "duplicates_removed": len(dup_log),
   "articles_rejected":  len(val_log),
   "ai_calls":   total_ai_calls,
   "ai_retries": total_ai_retries,
   "prompt_tokens": total_prompt_tokens,
   "completion_tokens":  total_completion_tokens,
   "exhausted_sources": sorted(get_exhausted_sources()),
})

    _finish_job(completed_at, (
        f"Cancelled — {entity.name}: {len(news_items)} articles saved before cancel"
        if was_cancelled
        else f"Done — {entity.name}: {len(news_items)} articles saved"
    ))
    pipe_log.info(
        f"Single entity {'cancelled' if was_cancelled else 'done'}: "
        f"{entity.name} — {len(news_items)} articles | "
        f"{total_ai_calls} AI calls | {total_prompt_tokens}p + {total_completion_tokens}c tokens"
    )