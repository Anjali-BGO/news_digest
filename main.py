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
get_audit_log, save_gap_report, get_latest_gap_report, get_all_gap_reports,
save_run, log_error, get_run_history, append_audit,
save_raw_articles, list_raw_runs, backfill_audit_entries,
save_industry_run_records, get_industry_all_records, save_industry_all_records,
save_industry_accepted, save_industry_rejected, update_industry_validation_status,
DATA_FILE, AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE,
INDUSTRY_ALL_RECORDS_FILE, INDUSTRY_ACCEPTED_FILE, INDUSTRY_REJECTED_FILE,
)
from services.drive_uploader import upload_bytes, upsert_bytes, startup_check as drive_startup_check
from logger import get_logger, get_pipeline_logger, LOG_DIR

log = get_logger("main")
pipe_log = get_pipeline_logger()

app = FastAPI(title="News Digest Platform")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def _check_drive_on_startup():
    # Surfaces Drive token status (ready / disabled + why) in the logs immediately,
    # instead of only finding out on the first pipeline run or export.
    drive_startup_check()

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


def _standardize_reason(action: str, reason: str) -> str:
    """Map a raw audit action+reason to a standardized rejection category label."""
    if action == "duplicate_removed":
        return "Duplicate Article"
    r = (reason or "").lower()
    if "before the window" in r or "too old" in r:
        return "Time Period Mismatch"
    if "future article" in r or "after the window end" in r:
        return "Date Validation Failure"
    if "parse" in r and "date" in r:
        return "Invalid Date Format"
    if "non-english" in r or "language field" in r:
        return "Non-English Content"
    if "domain blocked" in r or "social media" in r or "forum" in r:
        return "Source Validation Failure"
    if "not relevant" in r or "search drift" in r or "entity name not found" in r:
        return "Entity Name Mismatch"
    if "title is empty" in r or "[removed]" in r or "[deleted]" in r:
        return "Missing/Invalid Title"
    if action == "url_invalid" or ("url status" in r and "invalid" in r):
        return "Hyperlink Not Working"
    return "Validation Failure"


# Standardized reason color map: {label: (bg_hex, text_hex)}
_REASON_COLORS: dict = {
    "Duplicate Article":       ("EFF6FF", "1E40AF"),
    "Time Period Mismatch":    ("FFF7ED", "C2410C"),
    "Date Validation Failure": ("FFF7ED", "C2410C"),
    "Invalid Date Format":     ("FFF7ED", "C2410C"),
    "Non-English Content":     ("F5F3FF", "6D28D9"),
    "Source Validation Failure": ("FFF0F6", "BE185D"),
    "Entity Name Mismatch":    ("FFFBEB", "92400E"),
    "Missing/Invalid Title":   ("FEF2F2", "B91C1C"),
    "Hyperlink Not Working":   ("F0FDFA", "0F766E"),
    "Validation Failure":      ("F1F5F9", "475569"),
}


def _reason_badge_style(label: str) -> str:
    bg, fg = _REASON_COLORS.get(label, ("F1F5F9", "475569"))
    return f"background:#{bg};color:#{fg}"


templates.env.globals["standardize_reason"]   = _standardize_reason
templates.env.globals["reason_badge_style"]   = _reason_badge_style


# ── Shared Drive publishing (Team Reports) ──────────────────────────────────────
_DATA_FILES_TO_PUBLISH = [
    DATA_FILE, AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE,
    INDUSTRY_ALL_RECORDS_FILE, INDUSTRY_ACCEPTED_FILE, INDUSTRY_REJECTED_FILE,
]
_LOG_FILES_TO_PUBLISH = ["app.log", "errors.log", "pipeline.log", "audit.log"]


def _publish_run_to_drive() -> None:
    """
    Upload full, untrimmed copies of this machine's data files and technical
    logs to the shared Google account's Drive, namespaced by TEAM_MEMBER_ID so
    they never collide with a teammate's own upload. No-ops (with a one-time
    warning) if GOOGLE_DRIVE_*_FOLDER_ID / TEAM_MEMBER_ID aren't configured.
    """
    member = os.getenv("TEAM_MEMBER_ID", "unknown")
    data_folder = os.getenv("GOOGLE_DRIVE_DATA_FOLDER_ID", "")
    logs_folder = os.getenv("GOOGLE_DRIVE_LOGS_FOLDER_ID", "")

    for path in _DATA_FILES_TO_PUBLISH:
        if path.exists():
            upsert_bytes(f"{path.stem}_{member}{path.suffix}", path.read_bytes(), "application/json", data_folder)

    for log_file in _LOG_FILES_TO_PUBLISH:
        path = LOG_DIR / log_file
        if path.exists():
            upsert_bytes(f"{member}_{log_file}", path.read_bytes(), "text/plain", logs_folder)

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
        ("newsdata_io", "NewsData.io",  "NEWSDATA_API_KEY", "NewsData.io aggregator",     False),
        ("newsapi_ai",  "NewsAPI.ai",  "NEWSAI_API_KEY",   "NewsAPI.ai / EventRegistry", False),
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
            "request":            request,
            "clients":            [e for e in entities if e.entity_type == EntityType.client],
            "prospects":          [e for e in entities if e.entity_type == EntityType.prospect],
            "industries":         [e for e in entities if e.entity_type == EntityType.industry],
            "topics":             TOPIC_CATEGORIES,
            "job_status":         job_status,
            "total_news":         total_news,
            "current_period_news": current_period_news,
            "current_period":     current_period,
            "news_counts":        {eid: len(arts) for eid, arts in all_news.items()},
            "available_sources":  available_sources,
            "msg":                msg,
            "msg_name":           name,
            "msg_added":          added,
            "msg_skipped":        skipped,
            "msg_invalid":        invalid,
            "error":              error,
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
    if not name.strip() or entity_type not in ("client", "prospect", "industry"):
        return RedirectResponse(
            f"/?msg=invalid_entity&type={quote_plus(entity_type)}", status_code=303
        )

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


    # ── AI Validation for industry articles ───────────────────────────────────────
@app.post("/ai-validate-industry")
async def ai_validate_industry_route(
    background_tasks: BackgroundTasks,
    period:    str = Form(default=""),
    sector_id: str = Form(default=""),
):
    if job_status.get("running"):
        return RedirectResponse("/industry", status_code=303)
    job_status.update({
        "running": True,
        "cancel":  False,
        "message": "AI Validation running — checking URLs, extracting dates, categorising industry articles…",
    })
    background_tasks.add_task(run_industry_ai_validation, period, sector_id)
    return RedirectResponse("/industry", status_code=303)


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

    if not api_sources:
       return RedirectResponse("/?msg=no_source_selected", status_code=303)

    from services.news_fetcher import resolve_window
    window  = resolve_window(period_days, date_from, date_to)
    sources = api_sources

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
    # Industry news has its own dedicated page
    if tab == "industry":
        return RedirectResponse("/industry", status_code=302)

    entities   = get_entities()
    all_news   = get_all_news()
    gap_report = get_latest_gap_report()

    # Collect unique periods from client + prospect entities only
    client_prospect_ids = {e.id for e in entities if e.entity_type.value in ("client", "prospect")}
    period_dates = {}
    for eid, items in all_news.items():
       if eid not in client_prospect_ids:
          continue
       for item in items:
          if item.period:
             if item.period not in period_dates or item.fetched_date > period_dates[item.period]:
                period_dates[item.period] = item.fetched_date
    all_periods = sorted(period_dates.keys(), key=lambda p: period_dates[p], reverse=True)

    if period == "all":
        selected_period = "all"
    else:
        selected_period = period if period in all_periods else (all_periods[0] if all_periods else "")

    # Clamp active_tab to valid values
    if tab not in ("client", "prospect"):
        tab = "client"

    def entity_count(etype):
       return sum(1 for e in entities if e.entity_type == etype)

    window_label = "All Time" if selected_period == "all" else (selected_period or get_current_window()["label"])

    return templates.TemplateResponse(
   request=request,
   name="digest.html",
   context={
"request":         request,
"active_tab":      tab,
"clients":         _group_news_by_entity(entities, all_news, EntityType.client,   selected_period, require_news=True),
"prospects":       _group_news_by_entity(entities, all_news, EntityType.prospect, selected_period, require_news=True),
"total_clients":   entity_count(EntityType.client),
"total_prospects": entity_count(EntityType.prospect),
"job_status":      job_status,
"gap_report":      gap_report,
"window":          {"label": window_label},
"all_periods":     all_periods,
"selected_period": selected_period,
   }
)


    # ── Industry validation stats helper ────────────────────────────────────────
def _industry_validation_stats(records: list) -> dict:
    """Breaks down a list of industry_all_records entries into collection/validation counters."""
    total = len(records)
    duplicates = url_errors = date_na = out_of_window = 0
    not_relevant = category_review = 0
    validated = non_validated = review = pending = 0

    for r in records:
        status = r.get("validation_status")
        reason = (r.get("validation_reason") or "")
        url_status = r.get("url_status")

        if status == "Validated News":
            validated += 1
        elif status == "Non Validated News":
            non_validated += 1
        elif status == "Review":
            review += 1
        else:
            pending += 1

        if reason == "Duplicate article URL":
            duplicates += 1
        elif "date not available" in reason:
            date_na += 1
        elif "before window" in reason or "after window" in reason or "outside window" in reason:
            out_of_window += 1
        elif "URL" in reason or url_status in ("invalid", "missing", "archive"):
            url_errors += 1
        elif reason == "Not relevant to any tracked industry sector":
            not_relevant += 1
        elif reason == "Category unclear — human review required":
            category_review += 1

    return {
        "total":            total,
        "validated":        validated,
        "non_validated":    non_validated,
        "review":           review,
        "pending":          pending,
        "duplicates":       duplicates,
        "url_errors":       url_errors,
        "date_na":          date_na,
        "out_of_window":    out_of_window,
        "not_relevant":     not_relevant,
        "category_review":  category_review,
        "processed":        validated + non_validated + review,
        "accuracy_pct":     round(validated / (validated + non_validated + review) * 100, 1)
                             if (validated + non_validated + review) else 0,
    }


    # ── Industry News dedicated view ───────────────────────────────────────────────
@app.get("/industry", response_class=HTMLResponse)
async def industry_news_view(
    request:    Request,
    entity_id:  str = "",
    period:     str = "",
    date:       str = "",   # single published-date filter (YYYY-MM-DD)
    date_from:  str = "",   # published-date range start (YYYY-MM-DD)
    date_to:    str = "",   # published-date range end (YYYY-MM-DD)
    source:     str = "",
    topic:      str = "",
    status:     str = "",   # validation status: Validated News | Non Validated News | Review | Pending
):
    entities = [e for e in get_entities() if e.entity_type.value == "industry"]
    all_news = get_all_news()
    industry_ids = {e.id for e in entities}

    # Collect periods + sources from industry entities only
    period_dates: dict = {}
    all_sources_set: set = set()
    for eid, items in all_news.items():
        if eid not in industry_ids:
            continue
        for item in items:
            if item.period:
                if item.period not in period_dates or item.fetched_date > period_dates[item.period]:
                    period_dates[item.period] = item.fetched_date
            if item.source:
                all_sources_set.add(item.source)
    all_periods = sorted(period_dates.keys(), key=lambda p: period_dates[p], reverse=True)
    all_sources = sorted(all_sources_set, key=str.lower)

    selected_period    = period if (period == "all" or period in all_periods) else (all_periods[0] if all_periods else "all")
    selected_entity_id = entity_id if entity_id in industry_ids else ""
    selected_date       = date
    selected_date_from  = date_from
    selected_date_to    = date_to
    selected_source     = source if source in all_sources else ""
    selected_topic      = topic if topic in TOPIC_CATEGORIES else ""
    selected_status     = status if status in ("Validated News", "Non Validated News", "Review", "Pending") else ""

    def _matches_filters(published_date: str, item_source: str, primary_cat: str, secondary_cat: str, validation_status: str = None) -> bool:
        if selected_date and published_date != selected_date:
            return False
        if selected_date_from and (not published_date or published_date < selected_date_from):
            return False
        if selected_date_to and (not published_date or published_date > selected_date_to):
            return False
        if selected_source and item_source != selected_source:
            return False
        if selected_topic and selected_topic != primary_cat and selected_topic not in (secondary_cat or ""):
            return False
        if selected_status:
            status_val = validation_status or "Pending"
            if status_val != selected_status:
                return False
        return True

    entity_map = {e.id: e for e in entities}
    articles   = []
    for eid, items in all_news.items():
        if eid not in industry_ids:
            continue
        if selected_entity_id and eid != selected_entity_id:
            continue
        entity = entity_map.get(eid)
        if not entity:
            continue
        for item in items:
            if selected_period and selected_period != "all" and item.period != selected_period:
                continue
            if not _matches_filters(item.published_date, item.source, item.primary_category, item.secondary_category, item.validation_status):
                continue
            articles.append({"entity": entity, "news": item})

    articles.sort(key=lambda x: x["news"].published_date or "0000-00-00", reverse=True)

    # ── Collection / validation stats: current run (filtered) vs overall (all-time) ──
    all_industry_records = [r for r in get_industry_all_records() if r.get("entity_id") in industry_ids]
    run_records = [
        r for r in all_industry_records
        if (not selected_entity_id or r.get("entity_id") == selected_entity_id)
        and (selected_period == "all" or not selected_period or r.get("period") == selected_period)
        and _matches_filters(
            r.get("published_date", ""), r.get("source", ""),
            r.get("primary_category", ""),
            r.get("secondary_categories") or r.get("secondary_category") or "",
            r.get("validation_status"),
        )
    ]
    run_stats     = _industry_validation_stats(run_records)
    overall_stats = _industry_validation_stats(all_industry_records)

    return templates.TemplateResponse(
        request=request,
        name="industry.html",
        context={
            "request":            request,
            "entities":           entities,
            "articles":           articles,
            "all_periods":        all_periods,
            "all_sources":        all_sources,
            "all_topics":         TOPIC_CATEGORIES,
            "selected_period":    selected_period,
            "selected_entity_id": selected_entity_id,
            "selected_date":      selected_date,
            "selected_date_from": selected_date_from,
            "selected_date_to":   selected_date_to,
            "selected_source":    selected_source,
            "selected_topic":     selected_topic,
            "selected_status":    selected_status,
            "job_status":         job_status,
            "total_articles":     len(articles),
            "run_stats":          run_stats,
            "overall_stats":      overall_stats,
        },
    )


    # ── Gap report view ────────────────────────────────────────────────────────────
@app.get("/gaps", response_class=HTMLResponse)
async def gap_view(request: Request, run_idx: int = 0):
    all_reports = get_all_gap_reports()
    all_sorted = sorted(
        [r for r in all_reports if isinstance(r, dict)],
        key=lambda r: r.get("started_at", r.get("run_date", "")),
        reverse=True,
    )
    if not all_sorted:
        selected = None
        safe_idx = 0
    else:
        safe_idx = max(0, min(run_idx, len(all_sorted) - 1))
        selected = all_sorted[safe_idx]

    # Enrich entity_summary with topic coverage + filtered count
    enriched_summary = []
    if selected:
        topic_gaps_map = selected.get("topic_gaps", {})
        for item in selected.get("entity_summary", []):
            name = item["name"]
            gaps = topic_gaps_map.get(name, {}).get("missing_topics", [])
            enriched_summary.append({
                **item,
                "missing_topics": gaps,
                "topics_covered": 12 - len(gaps),
                "filtered": max(0, item.get("raw", 0) - item.get("final", 0)),
            })

    return templates.TemplateResponse(
        request=request,
        name="gaps.html",
        context={
            "request":         request,
            "gap_report":      selected,
            "all_reports":     all_sorted,
            "selected_idx":    safe_idx,
            "enriched_summary": enriched_summary,
            "job_status":      job_status,
        }
    )


    # ── Audit log view ─────────────────────────────────────────────────────────────
@app.get("/audit", response_class=HTMLResponse)
async def audit_view(request: Request, tab: str = "all"):
    all_entries = get_audit_log()
    # Count per entity type across all entries
    type_counts = {"all": len(all_entries), "client": 0, "prospect": 0, "industry": 0}
    for e in all_entries:
        et = (e.entity_type or "").lower()
        if et in type_counts:
            type_counts[et] += 1
    # Filter by selected tab
    if tab in ("client", "prospect", "industry"):
        filtered = [e for e in all_entries if (e.entity_type or "").lower() == tab]
    else:
        tab = "all"
        filtered = all_entries
    total_count = len(filtered)
    limit  = 500
    entries = list(reversed(filtered[-limit:] if total_count > limit else filtered))

    # Unique API sources present in this set (for JS filter dropdown)
    api_sources_present = sorted({(e.fetch_source or "").strip() for e in entries if e.fetch_source})
    # Unique run dates present (newest first)
    run_dates_present = sorted({e.run_date for e in entries if e.run_date}, reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context={
            "request":          request,
            "entries":          entries,
            "total_count":      total_count,
            "limit":            limit,
            "active_tab":       tab,
            "type_counts":      type_counts,
            "api_sources":      api_sources_present,
            "run_dates":        run_dates_present,
            "job_status":       job_status,
        }
    )


    # ── Admin: backfill audit entries + list raw runs ────────────────────────────
@app.post("/admin/backfill-audit")
async def admin_backfill_audit():
    """
    Patch existing audit.json entries with missing entity_type, fetch_source,
    window_from, and window_to from all available data sources.
    """
    result = backfill_audit_entries()
    return result

@app.get("/admin/raw-runs")
async def admin_raw_runs():
    """List all run IDs that have a permanent raw news file."""
    return {"runs": list_raw_runs()}


    # ── Clear job-done banner (called by close button in UI) ───────────────────
@app.post("/clear-job-message")
async def clear_job_message_route():
    job_status["last_run"] = None
    job_status["message"]  = ""
    return JSONResponse({"ok": True})


    # ── Separate report exports ────────────────────────────────────────────────
@app.get("/export/gap-report")
async def export_gap_report(background_tasks: BackgroundTasks):
    from services.excel_exporter import generate_gap_report_excel
    excel_bytes = generate_gap_report_excel(get_all_gap_reports(), get_run_history())
    fname = f"gap_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

@app.get("/export/run-report")
async def export_run_report(background_tasks: BackgroundTasks):
    from services.excel_exporter import generate_run_history_excel
    excel_bytes = generate_run_history_excel(get_run_history())
    fname = f"run_history_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

@app.get("/export/audit-report")
async def export_audit_report(background_tasks: BackgroundTasks):
    from services.excel_exporter import generate_audit_report_excel
    excel_bytes = generate_audit_report_excel(get_audit_log())
    fname = f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

@app.get("/export/validation-report")
async def export_validation_report(background_tasks: BackgroundTasks):
    from services.excel_exporter import generate_validation_report_excel
    entities = get_entities()
    all_news = get_all_news()
    audit    = get_audit_log()
    excel_bytes = generate_validation_report_excel(entities, all_news, audit)
    fname = f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


    # ── Run history view ──────────────────────────────────────────────────────────
@app.get("/run-history", response_class=HTMLResponse)
async def run_history_view(request: Request):
    runs = get_run_history()

    # Aggregate stats across all runs
    total_raw      = sum(r.get("raw_fetched", 0) for r in runs)
    total_accepted = sum(r.get("total_articles", 0) for r in runs)
    total_dupes    = sum(r.get("duplicates_removed", 0) for r in runs)
    total_rejected = sum(r.get("articles_rejected", 0) for r in runs)
    total_ai_calls = sum(r.get("ai_calls", 0) for r in runs)
    total_tokens   = sum((r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)) for r in runs)
    accept_rate    = round(total_accepted / total_raw * 100, 1) if total_raw else 0

    api_usage: dict = {}
    for r in runs:
        for src in r.get("api_sources_used", []):
            api_usage[src] = api_usage.get(src, 0) + 1

    # Enrich each run with computed accuracy fields
    enriched = []
    for r in reversed(runs):   # newest first
        raw       = r.get("raw_fetched") or (
            (r.get("total_articles", 0) + r.get("duplicates_removed", 0) + r.get("articles_rejected", 0))
        )
        after_dd  = max(0, raw - r.get("duplicates_removed", 0))
        accepted  = r.get("total_articles", 0)
        accuracy  = round(accepted / after_dd * 100, 1) if after_dd else 0
        acc_rate  = round(accepted / raw * 100, 1) if raw else 0
        enriched.append({**r, "_raw_calc": raw, "_accuracy": accuracy, "_accept_rate": acc_rate})

    return templates.TemplateResponse(
        request=request,
        name="run_history.html",
        context={
            "request":       request,
            "runs":          enriched,
            "job_status":    job_status,
            "total_runs":    len(runs),
            "total_raw":     total_raw,
            "total_accepted": total_accepted,
            "total_dupes":   total_dupes,
            "total_rejected": total_rejected,
            "total_ai_calls": total_ai_calls,
            "total_tokens":  total_tokens,
            "accept_rate":   accept_rate,
            "api_usage":     api_usage,
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
        for src in ("serpapi", "tavily", "newsdata_io", "newsapi_ai"):
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
async def export_excel(background_tasks: BackgroundTasks, period: str = ""):
    from services.excel_exporter import generate_excel_report

    entities = get_entities()
    all_news = get_all_news()
    window   = get_current_window()
    audit    = get_audit_log()

    excel_bytes = generate_excel_report(
   _group_news_by_entity(entities, all_news, EntityType.client,   period),
   _group_news_by_entity(entities, all_news, EntityType.prospect, period),
   _group_news_by_entity(entities, all_news, EntityType.industry, period),
   window=window,
   gap_report=get_latest_gap_report() or {},
   audit_entries=audit,
   entities=entities,
)
    fname = f"news_digest_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
    return StreamingResponse(
   io.BytesIO(excel_bytes),
   media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
   headers={"Content-Disposition": f"attachment; filename={fname}"}
)


    # ── Full analytics export (all articles + audit trail) ────────────────────────
@app.get("/export/excel-full")
async def export_excel_full(background_tasks: BackgroundTasks):
    from services.excel_exporter import generate_full_analytics_report

    entities  = get_entities()
    all_news  = get_all_news()
    audit  = get_audit_log()
    window = get_current_window()

    excel_bytes = generate_full_analytics_report(entities, all_news, audit, window=window)
    fname = f"news_full_analytics_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    background_tasks.add_task(upload_bytes, fname, excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        os.getenv("GOOGLE_DRIVE_EXPORTS_FOLDER_ID", ""))
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
                                   linked: list, window: dict) -> list:
    """Return AuditEntry objects for all duplicate/validation/hyperlink rejections."""
    today      = datetime.now().strftime("%Y-%m-%d")
    win_from   = window.get("from", "")
    win_to     = window.get("to", "")
    etype  = entity.entity_type.value if hasattr(entity.entity_type, "value") else str(entity.entity_type)
    entries = []
    for d in dup_log:
        entries.append(AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            entity_type=etype,
            article_title=d["title"], action="duplicate_removed",
            reason=d["reason"], source_url=d.get("url", ""),
            fetch_source=d.get("fetch_source", ""),
            window_from=win_from, window_to=win_to,
        ))
    for v in val_log:
        entries.append(AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            entity_type=etype,
            article_title=v["title"], action="validation_rejected",
            reason=v["reason"], source_url=v.get("url", ""),
            fetch_source=v.get("fetch_source", ""),
            window_from=win_from, window_to=win_to,
        ))
    for a in linked:
        if a.get("url_status") in ("invalid", "unknown"):
            entries.append(AuditEntry(
                run_date=today, entity_id=entity.id, entity_name=entity.name,
                entity_type=etype,
                article_title=a["title"], action="url_invalid",
                reason=f"URL status: {a.get('url_status')} — {a.get('url', '')}",
                source_url=a.get("url", ""),
                fetch_source=a.get("fetch_source", ""),
                window_from=win_from, window_to=win_to,
            ))
    return entries


def _build_accepted_audit_entries(entity, news_items: list, today: str, window: dict) -> list:
    """Return AuditEntry objects for every article that passed all pipeline checks."""
    win_from = window.get("from", "")
    win_to   = window.get("to", "")
    etype    = entity.entity_type.value if hasattr(entity.entity_type, "value") else str(entity.entity_type)
    return [
        AuditEntry(
            run_date=today, entity_id=entity.id, entity_name=entity.name,
            entity_type=etype,
            article_title=item.title, action="accepted",
            reason="Passed all checks", source_url=item.url,
            fetch_source=item.fetch_source or "",
            window_from=win_from, window_to=win_to,
        )
        for item in news_items
    ]


    # ── Industry AI Validation — shared batch runner ───────────────────────────────
def _build_industry_validation_audit_entries(updated: list, window: dict) -> list:
    """
    AuditEntry rows for the AI-validation outcome of each industry article —
    separate from the earlier 'accepted' entry written when it first passed the
    pipeline (dedup + 8-point validation + hyperlink check), since industry AI
    validation can later override that with a Validated/Non Validated/Review
    verdict. Without these, /audit permanently showed "accepted" for an article
    that /industry displays as rejected, with no record of why they disagreed.
    """
    today    = datetime.now().strftime("%Y-%m-%d")
    win_from = window.get("from", "")
    win_to   = window.get("to", "")
    action_map = {
        "Validated News":     "industry_validated",
        "Non Validated News": "industry_non_validated",
        "Review":             "industry_review",
    }
    entries = []
    for rec in updated:
        status = rec.get("validation_status", "")
        entries.append(AuditEntry(
            run_date=today,
            entity_id=rec.get("entity_id", ""),
            entity_name=rec.get("industry_sector") or rec.get("entity_id", ""),
            entity_type="industry",
            article_title=rec.get("clean_title") or rec.get("title", ""),
            action=action_map.get(status, "industry_review"),
            reason=rec.get("validation_reason") or "",
            source_url=rec.get("url", ""),
            fetch_source=rec.get("fetch_source", ""),
            window_from=win_from, window_to=win_to,
        ))
    return entries


async def _run_industry_validation_batch(filtered: list, sector_info: list, window: dict) -> dict:
    """
    Runs the AI validation decision tree (URL check → OpenAI web search fallback →
    OpenAI categorize) over `filtered` industry records, merges results back into
    industry_all_records.json (matched by URL — never drops unrelated records),
    refreshes the accepted/rejected split files, and mirrors validation_status back
    to data.json so the /industry page shows badges.

    Shared by the automatic post-pipeline validation (runs right after the hyperlink
    check, no user action needed) and the manual "Re-validate" button (for re-running
    validation on demand, e.g. after a transient URL/date lookup failure).
    """
    from services.industry_validator import validate_industry_article_with_ai

    if not filtered:
        return {"total": 0, "validated": 0, "non_validated": 0, "review": 0,
                "ai_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    seen_urls: set = set()
    updated:   list = []
    validated_count = rejected_count = review_count = 0
    total_ai_calls = total_prompt_tokens = total_completion_tokens = 0

    for i, art in enumerate(filtered, 1):
        if job_status.get("cancel"):
            pipe_log.info("Industry AI Validation cancelled by user")
            break

        if i % 10 == 0 or i == 1:
            job_status["message"] = f"AI Validation — {i}/{len(filtered)} articles processed…"

        try:
            result = await validate_industry_article_with_ai(art, sector_info, window, seen_urls)
            updated.append(result)
            total_ai_calls += result.get("ai_calls", 0)
            total_prompt_tokens += result.get("ai_prompt_tokens", 0)
            total_completion_tokens += result.get("ai_completion_tokens", 0)
            status = result.get("validation_status", "")
            if status == "Validated News":
                validated_count += 1
            elif status == "Non Validated News":
                rejected_count += 1
            else:
                review_count += 1
        except Exception as e:
            log_error("industry_ai_validation", str(e))
            updated.append({**art, "validation_status": "Review",
                             "validation_reason": f"Validation error: {e}"})
            review_count += 1

    if updated:
        # Merge by URL so unrelated periods/sectors already in the store are preserved.
        all_records = get_industry_all_records()
        all_records_map = {r["url"]: r for r in all_records if r.get("url")}
        for rec in updated:
            if rec.get("url"):
                all_records_map[rec["url"]] = rec
        save_industry_all_records(list(all_records_map.values()))

        save_industry_accepted([r for r in updated if r.get("validation_status") == "Validated News"])
        save_industry_rejected([r for r in updated if r.get("validation_status") == "Non Validated News"])
        update_industry_validation_status(updated)

        try:
            append_audit(_build_industry_validation_audit_entries(updated, window))
        except Exception as e:
            log_error("industry_validation_audit", str(e))

    return {
        "total":            len(filtered),
        "validated":        validated_count,
        "non_validated":    rejected_count,
        "review":           review_count,
        "ai_calls":         total_ai_calls,
        "prompt_tokens":    total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
    }


def _industry_sector_info() -> list:
    """Sector list (all configured industry entities) passed to the AI categorizer."""
    return [
        {
            "name":          e.name,
            "entity_type":   "industry",
            "industry_type": e.industry_type or "",
            "news_scope":    e.news_scope or "",
        }
        for e in get_entities() if e.entity_type.value == "industry"
    ]


    # ── Industry AI Validation (background task — manual re-validate) ──────────────
async def run_industry_ai_validation(
    period_filter:    str = "",
    sector_id_filter: str = "",
) -> None:
    """Guards _run_industry_ai_validation_body so any unhandled exception still
    clears job_status instead of leaving it stuck at "running"."""
    try:
        await _run_industry_ai_validation_body(period_filter, sector_id_filter)
    except Exception as e:
        log_error("run_industry_ai_validation", str(e))
        pipe_log.error(f"Industry AI Validation crashed: {e}")
        _finish_job(datetime.now(), f"Failed — AI Validation error: {e}")


async def _run_industry_ai_validation_body(
    period_filter:    str = "",
    sector_id_filter: str = "",
) -> None:
    """
    Manual re-validation entry point (the "Re-validate" button on /industry).
    Reads from industry_all_records.json (populated automatically by the pipeline
    right after the hyperlink check), then re-runs AI validation on the selected
    period/sector slice — useful if a transient failure left articles stuck in
    "Review", or sector definitions changed since the last run.
    """
    started_at = datetime.now()

    sector_info = _industry_sector_info()
    if not sector_info:
        _finish_job(datetime.now(), "No industry entities found")
        return

    # Build window from period_filter or derive from the newest run history entry
    window: dict = {}
    if period_filter:
        # period_filter = "DD Mon YYYY – DD Mon YYYY" (the label stored in .period on records)
        run_history = get_run_history()
        for r in reversed(run_history):
            if r.get("window_label") == period_filter:
                window = {
                    "from":  r.get("window_from", ""),
                    "to":    r.get("window_to", ""),
                    "label": r.get("window_label", ""),
                }
                break
    if not window:
        run_history = get_run_history()
        if run_history:
            r = run_history[-1]
            window = {
                "from":  r.get("window_from", ""),
                "to":    r.get("window_to", ""),
                "label": r.get("window_label", ""),
            }

    # Load all stored pre-validation industry records
    all_records = get_industry_all_records()
    if not all_records:
        _finish_job(datetime.now(), "No industry records found — run the Industry Pipeline first")
        return

    # Apply filters
    filtered = all_records
    if period_filter:
        filtered = [r for r in filtered if r.get("period") == period_filter]
    if sector_id_filter:
        filtered = [r for r in filtered if r.get("entity_id") == sector_id_filter]

    if not filtered:
        _finish_job(datetime.now(), "No matching industry records for the selected filters")
        return

    pipe_log.info(
        f"Industry AI Validation (manual re-run) — {len(filtered)} articles"
        + (f" | period: {period_filter}" if period_filter else "")
        + (f" | sector: {sector_id_filter}" if sector_id_filter else "")
        + f" | window: {window.get('from', '?')} → {window.get('to', '?')}"
    )
    job_status["message"] = f"AI Validation — validating {len(filtered)} industry articles…"

    result = await _run_industry_validation_batch(filtered, sector_info, window)

    completed_at = datetime.now()
    duration     = (completed_at - started_at).seconds
    msg = (
        f"AI Validation complete — {result['validated']} validated / "
        f"{result['non_validated']} rejected / {result['review']} review "
        f"from {result['total']} articles ({duration}s)"
    )
    _finish_job(completed_at, msg)
    pipe_log.info(msg)


    # ── Full pipeline (background task) ───────────────────────────────────────────
async def run_full_pipeline(window: dict, entity_type_filter: str = "all", api_sources: list | None = None) -> None:
    """Guards _run_full_pipeline_body so any unhandled exception still clears
    job_status — otherwise every future fetch would redirect as "already running"
    until the process restarts."""
    try:
        await _run_full_pipeline_body(window, entity_type_filter, api_sources)
    except Exception as e:
        log_error("run_full_pipeline", str(e))
        pipe_log.error(f"Full pipeline crashed: {e}")
        _finish_job(datetime.now(), f"Failed — pipeline error: {e}")


async def _run_full_pipeline_body(window: dict, entity_type_filter: str = "all", api_sources: list | None = None) -> None:
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
    industry_records_this_run = []
    total_raw_fetched = 0
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
          total_raw_fetched += len(raw)

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
          # Permanent raw store — written before any processing
          save_raw_articles(
   run_id, entity.id, entity.name,
   entity.entity_type.value, window, raw
)

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
   entity_type=entity.entity_type.value,
)
          total_rejected += len(val_log)
          save_stage_snapshot(run_id, entity.name, "3_validated",            validated)
          save_stage_snapshot(run_id, entity.name, "3_validation_rejected",  val_log)

          # 4. Hyperlink check
          linked = await validate_hyperlinks(validated)
          save_stage_snapshot(run_id, entity.name, "4_hyperlinked", linked)

          # 4b. For industry entities: save post-hyperlink articles to industry_all_records.json
          # These are stored before AI summarize so AI Validation can re-process them independently.
          if entity.entity_type.value == "industry":
              today_str    = datetime.now().strftime("%Y-%m-%d")
              period_label = window.get("label", "")
              ind_records  = [
                  {
                      **art,
                      "entity_id":          entity.id,
                      "entity_type":        "industry",
                      "industry_sector":    entity.name,
                      "industry_type":      entity.industry_type or "",
                      "fetched_date":       today_str,
                      "period":             period_label,
                      "run_id":             run_id,
                      "validation_status":  None,
                      "validation_reason":  None,
                  }
                  for art in linked
              ]
              save_industry_run_records(run_id, period_label, entity.id, ind_records)
              industry_records_this_run.extend(ind_records)

          # 5. Audit entries — dedup + validation + hyperlink rejections
          audit_entries.extend(_build_rejection_audit_entries(entity, dup_log, val_log, linked, window))
          today = datetime.now().strftime("%Y-%m-%d")

          # 6. Summarise + categorise
          _cats      = entity.topics if entity.entity_type.value == "industry" and entity.topics else None
          _etype     = entity.entity_type.value
          ai_rejected = []
          news_items = []
          for art in linked:
             if job_status["cancel"]:
                pipe_log.info(f"Pipeline cancelled during summarize: {entity.name}")
                break
             try:
                result = await summarize_article(
   art["title"], art.get("content", ""), entity.name,
   categories=_cats, entity_type=_etype,
   news_scope=entity.news_scope or "" if _etype == "industry" else "",
)
                # Accumulate AI usage stats from every article
                calls = result.get("ai_calls", 1)
                total_ai_calls += calls
                total_ai_retries+= 1 if calls > 1 else 0
                total_prompt_tokens += result.get("prompt_tokens", 0)
                total_completion_tokens += result.get("completion_tokens", 0)

                if not result.get("is_relevant", True):
                   ai_rejected.append({**art, "reason": f"Not relevant to '{entity.name}' — AI relevance assessment"})
                   total_rejected += 1
                else:
                   news_items.append(_make_news_item(art, result, entity, window))
             except Exception as e:
                log_error("summarize_article", str(e), entity.name)

          # 7. Save — even if partial
          save_news_for_entity(entity.id, news_items)
          save_stage_snapshot(run_id, entity.name, "5_summarized", [i.model_dump() for i in news_items])
          total_articles += len(news_items)
          entity_results[-1]["final_count"] = len(news_items)

          # 7b. Audit entries — AI-rejected + every accepted/saved article
          if ai_rejected:
             audit_entries.extend(_build_rejection_audit_entries(entity, [], ai_rejected, [], window))
          audit_entries.extend(_build_accepted_audit_entries(entity, news_items, today, window))

          pipe_log.info(f"Done: {entity.name} — {len(news_items)} articles saved")

       except Exception as e:
          log_error("run_full_pipeline", str(e), entity.name)
          pipe_log.error(f"Failed: {entity.name} — {e}")
          # continue to next entity rather than crashing entire pipeline

    # 7c. Auto-validate industry articles right after the hyperlink check — no manual
    # button needed. The "Re-validate" button on /industry remains for re-running it
    # on demand (e.g. after a transient failure or a sector definition change).
    industry_validation = None
    if industry_records_this_run and not job_status["cancel"]:
        pipe_log.info(f"Auto-validating {len(industry_records_this_run)} industry articles…")
        job_status["message"] = f"Validating {len(industry_records_this_run)} industry articles…"
        industry_validation = await _run_industry_validation_batch(
            industry_records_this_run, _industry_sector_info(), window
        )
        total_ai_calls += industry_validation.get("ai_calls", 0)
        total_prompt_tokens += industry_validation.get("prompt_tokens", 0)
        total_completion_tokens += industry_validation.get("completion_tokens", 0)
        pipe_log.info(
            f"Industry auto-validation complete — {industry_validation['validated']} validated / "
            f"{industry_validation['non_validated']} rejected / {industry_validation['review']} review"
        )

    # 8. Save audit + gap report
    exhausted_sources = get_exhausted_sources()
    completed_at = datetime.now()
    try:
       if audit_entries:
          append_audit(audit_entries)
       _gap = build_gap_report(entity_results, window, exhausted_sources)
       _gap.update({
           "run_id":             run_id,
           "api_sources_used":   sorted(api_sources or ["tavily"]),
           "duration_seconds":   (completed_at - started_at).seconds,
           "started_at":         started_at.strftime("%Y-%m-%d %H:%M:%S"),
           "completed_at":       completed_at.strftime("%Y-%m-%d %H:%M:%S"),
           "raw_fetched":        total_raw_fetched,
           "total_articles":     total_articles,
           "duplicates_removed": total_dupes,
           "articles_rejected":  total_rejected,
           "status":             "cancelled" if job_status["cancel"] else "completed",
       })
       save_gap_report(_gap)
    except Exception as e:
       log_error("save_gap_report", str(e))

    # 9. Save run history
    save_run({
   "run_id":     run_id,
   "run_date":  started_at.strftime("%Y-%m-%d"),
   "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
   "completed_at":   completed_at.strftime("%Y-%m-%d %H:%M:%S"),
   "duration_seconds":  (completed_at - started_at).seconds,
   "status":"cancelled" if job_status["cancel"] else "completed",
   "window_label":   window.get("label", ""),
   "window_from":    window.get("from", ""),
   "window_to":      window.get("to", ""),
   "api_sources_used": sorted(api_sources or ["tavily"]),
   "total_entities": len(entities),
   "raw_fetched":    total_raw_fetched,
   "total_articles": total_articles,
   "duplicates_removed": total_dupes,
   "articles_rejected": total_rejected,
   "ai_calls":  total_ai_calls,
   "ai_retries": total_ai_retries,
   "prompt_tokens":  total_prompt_tokens,
   "completion_tokens": total_completion_tokens,
   "exhausted_sources": sorted(exhausted_sources),
})

    _publish_run_to_drive()

    was_cancelled = job_status["cancel"]   # capture before clearing
    exhausted_msg = (
        f" | credits exhausted: {', '.join(sorted(exhausted_sources))}"
        if exhausted_sources else ""
    )
    industry_msg = (
        f" | industry: {industry_validation['validated']} validated / "
        f"{industry_validation['non_validated']} rejected / {industry_validation['review']} review"
        if industry_validation else ""
    )
    _finish_job(completed_at, (
        "Cancelled by user." if was_cancelled
        else f"Done — {completed_at.strftime('%d %b %Y, %H:%M')} | {total_articles} articles{industry_msg}{exhausted_msg}"
    ))
    pipe_log.info(
        f"Pipeline complete — {total_articles} articles | "
        f"{total_dupes} dupes | {total_rejected} rejected | "
        f"{(completed_at - started_at).seconds}s"
        + industry_msg + exhausted_msg
    )


    # ── Single entity pipeline (background task) ──────────────────────────────────
async def run_entity_pipeline(entity, window: dict, api_sources: list | None = None) -> None:
    """Guards _run_entity_pipeline_body so any unhandled exception still clears
    job_status — otherwise every future fetch would redirect as "already running"
    until the process restarts. run_entity_pipeline previously had no top-level
    exception handling at all, unlike the full pipeline's per-entity try/except."""
    try:
        await _run_entity_pipeline_body(entity, window, api_sources)
    except Exception as e:
        log_error("run_entity_pipeline", str(e), entity.name)
        pipe_log.error(f"Single entity pipeline crashed: {entity.name} — {e}")
        _finish_job(datetime.now(), f"Failed — {entity.name}: {e}")


async def _run_entity_pipeline_body(entity, window: dict, api_sources: list | None = None) -> None:
    from services.news_fetcher   import fetch_all_news, build_gap_report, DEFAULT_SOURCES, reset_exhausted_sources, get_exhausted_sources
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
"run_id": run_id,
"run_date": started_at.strftime("%Y-%m-%d"),
"started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
"completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
"duration_seconds": (completed_at - started_at).seconds,
"status": "cancelled",
"window_label": window.get("label", ""),
"window_from":  window.get("from", ""),
"window_to":    window.get("to", ""),
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
    # Permanent raw store — written before any processing
    save_raw_articles(run_id, entity.id, entity.name, entity.entity_type.value, window, raw)

    deduped, dup_log   = deduplicate(raw)
    save_stage_snapshot(run_id, entity.name, "2_deduped",        deduped)
    save_stage_snapshot(run_id, entity.name, "2_dedup_rejected", dup_log)

    validated, val_log = await validate_articles(
   deduped, window,
   entity_name=entity.name,
   entity_website=entity.website or "",
   entity_aliases=entity.aliases or [],
   entity_type=entity.entity_type.value,
)
    save_stage_snapshot(run_id, entity.name, "3_validated",           validated)
    save_stage_snapshot(run_id, entity.name, "3_validation_rejected", val_log)

    linked = await validate_hyperlinks(validated)
    save_stage_snapshot(run_id, entity.name, "4_hyperlinked", linked)

    # For industry entities: save post-hyperlink articles to industry_all_records.json,
    # then auto-validate immediately — no manual button needed. The "Re-validate" button
    # on /industry remains for re-running it on demand.
    industry_validation = None
    if entity.entity_type.value == "industry":
        today_str    = datetime.now().strftime("%Y-%m-%d")
        period_label = window.get("label", "")
        ind_records  = [
            {
                **art,
                "entity_id":          entity.id,
                "entity_type":        "industry",
                "industry_sector":    entity.name,
                "industry_type":      entity.industry_type or "",
                "fetched_date":       today_str,
                "period":             period_label,
                "run_id":             run_id,
                "validation_status":  None,
                "validation_reason":  None,
            }
            for art in linked
        ]
        save_industry_run_records(run_id, period_label, entity.id, ind_records)

        if ind_records and not job_status["cancel"]:
            pipe_log.info(f"Auto-validating {len(ind_records)} industry articles for {entity.name}…")
            job_status["message"] = f"Validating {len(ind_records)} industry articles…"
            industry_validation = await _run_industry_validation_batch(
                ind_records, _industry_sector_info(), window
            )
            total_ai_calls += industry_validation.get("ai_calls", 0)
            total_prompt_tokens += industry_validation.get("prompt_tokens", 0)
            total_completion_tokens += industry_validation.get("completion_tokens", 0)

    # Build audit entries for every rejection
    today         = datetime.now().strftime("%Y-%m-%d")
    audit_entries = _build_rejection_audit_entries(entity, dup_log, val_log, linked, window)

    pipe_log.info(
   f"  {entity.name}: {len(raw)} raw | {len(dup_log)} dupes | "
   f"{len(val_log)} rejected | {len(linked)} passed validation"
)

    _cats    = entity.topics if entity.entity_type.value == "industry" and entity.topics else None
    _etype   = entity.entity_type.value
    ai_rejected = []
    news_items = []
    for art in linked:
       if job_status["cancel"]:
          pipe_log.info(f"Single entity pipeline cancelled during summarize: {entity.name}")
          break
       try:
          result = await summarize_article(
   art["title"], art.get("content", ""), entity.name,
   categories=_cats, entity_type=_etype,
   news_scope=entity.news_scope or "" if _etype == "industry" else "",
)
          calls = result.get("ai_calls", 1)
          total_ai_calls += calls
          total_ai_retries+= 1 if calls > 1 else 0
          total_prompt_tokens += result.get("prompt_tokens", 0)
          total_completion_tokens += result.get("completion_tokens", 0)
          if not result.get("is_relevant", True):
             ai_rejected.append({**art, "reason": f"Not relevant to '{entity.name}' — AI relevance assessment"})
          else:
             news_items.append(_make_news_item(art, result, entity, window))
       except Exception as e:
          log_error("run_entity_pipeline.summarize", str(e), entity.name)

    save_news_for_entity(entity.id, news_items)
    save_stage_snapshot(run_id, entity.name, "5_summarized", [i.model_dump() for i in news_items])

    # Audit — AI-rejected + accepted articles + all rejections
    if ai_rejected:
       audit_entries.extend(_build_rejection_audit_entries(entity, [], ai_rejected, [], window))
    audit_entries.extend(_build_accepted_audit_entries(entity, news_items, today, window))

    if audit_entries:
       try:
          append_audit(audit_entries)
       except Exception as e:
          log_error("run_entity_pipeline.audit", str(e), entity.name)

    completed_at  = datetime.now()
    was_cancelled = job_status["cancel"]
    _exhausted_ent = get_exhausted_sources()
    save_run({
   "run_id":     run_id,
   "run_date":   started_at.strftime("%Y-%m-%d"),
   "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
   "completed_at":  completed_at.strftime("%Y-%m-%d %H:%M:%S"),
   "duration_seconds":   (completed_at - started_at).seconds,
   "status": "cancelled" if was_cancelled else "completed",
   "window_label":  window.get("label", ""),
   "window_from":   window.get("from", ""),
   "window_to":     window.get("to", ""),
   "api_sources_used": sorted(sources),
   "entity_name":   entity.name,
   "total_entities": 1,
   "raw_fetched":   len(raw),
   "total_articles": len(news_items),
   "duplicates_removed": len(dup_log),
   "articles_rejected":  len(val_log),
   "ai_calls":   total_ai_calls,
   "ai_retries": total_ai_retries,
   "prompt_tokens": total_prompt_tokens,
   "completion_tokens":  total_completion_tokens,
   "exhausted_sources": sorted(_exhausted_ent),
})
    # Save gap report for this single-entity run
    try:
        _gap_e = build_gap_report(
            [{"entity_name": entity.name, "entity_type": entity.entity_type.value,
              "topic_gaps": fetch_result.get("topic_gaps", []),
              "has_any_news": fetch_result.get("has_any_news", False),
              "raw_count": len(raw), "final_count": len(news_items)}],
            window, _exhausted_ent,
        )
        _gap_e.update({
            "run_id":             run_id,
            "entity_name":        entity.name,
            "api_sources_used":   sorted(sources),
            "duration_seconds":   (completed_at - started_at).seconds,
            "started_at":         started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "completed_at":       completed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "raw_fetched":        len(raw),
            "total_articles":     len(news_items),
            "duplicates_removed": len(dup_log),
            "articles_rejected":  len(val_log),
            "status":             "cancelled" if was_cancelled else "completed",
        })
        save_gap_report(_gap_e)
    except Exception as _e:
        log_error("save_gap_report_entity", str(_e))

    _publish_run_to_drive()

    industry_msg = (
        f" | industry: {industry_validation['validated']} validated / "
        f"{industry_validation['non_validated']} rejected / {industry_validation['review']} review"
        if industry_validation else ""
    )
    _finish_job(completed_at, (
        f"Cancelled — {entity.name}: {len(news_items)} articles saved before cancel"
        if was_cancelled
        else f"Done — {entity.name}: {len(news_items)} articles saved{industry_msg}"
    ))
    pipe_log.info(
        f"Single entity {'cancelled' if was_cancelled else 'done'}: "
        f"{entity.name} — {len(news_items)} articles | "
        f"{total_ai_calls} AI calls | {total_prompt_tokens}p + {total_completion_tokens}c tokens"
    )