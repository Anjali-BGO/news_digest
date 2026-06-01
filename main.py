import uuid
import io
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List

from models import Entity, EntityType, TOPIC_CATEGORIES
from storage import (
    get_entities, save_entity, delete_entity,
    save_news_for_entity, get_all_news,
    get_audit_log, save_gap_report, get_latest_gap_report,
    save_run, log_error,
)
from logger import get_logger, get_pipeline_logger

log      = get_logger("main")
pipe_log = get_pipeline_logger()

app = FastAPI(title="News Digest Platform")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── In-memory job status ───────────────────────────────────────────────────────
job_status: dict = {
    "running":   False,
    "last_run":  None,
    "message":   "",
    "cancel":    False,
    "date_from": "",
    "date_to":   "",
}


# ── Home: entity manager ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    msg:     str = "",
    name:    str = "",
    added:   int = 0,
    skipped: int = 0,
    invalid: int = 0,
    error:   str = "",
):
    entities  = get_entities()
    all_news  = get_all_news()
    total_news = sum(len(v) for v in all_news.values())
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request":     request,
            "clients":     [e for e in entities if e.entity_type == EntityType.client],
            "prospects":   [e for e in entities if e.entity_type == EntityType.prospect],
            "industries":  [e for e in entities if e.entity_type == EntityType.industry],
            "topics":      TOPIC_CATEGORIES,
            "job_status":  job_status,
            "total_news":  total_news,
            "news_counts": {eid: len(arts) for eid, arts in all_news.items()},
            "msg":         msg,
            "msg_name":    name,
            "msg_added":   added,
            "msg_skipped": skipped,
            "msg_invalid": invalid,
            "error":       error,
        }
    )


# ── Add entity ─────────────────────────────────────────────────────────────────
@app.post("/add-entity")
async def add_entity(
    name:          str       = Form(...),
    entity_type:   str       = Form(...),
    topics:        List[str] = Form(default=[]),
    website:       str       = Form(default=""),
    industry_type: str       = Form(default=""),
    news_scope:    str       = Form(default=""),
):
    entities = get_entities()
    exists   = any(
        e.name.strip().lower() == name.strip().lower() and
        e.entity_type.value    == entity_type
        for e in entities
    )
    if exists:
        return RedirectResponse(
            f"/?msg=duplicate&name={name}&type={entity_type}", status_code=303
        )
    save_entity(Entity(
        id=str(uuid.uuid4()),
        name=name,
        entity_type=EntityType(entity_type),
        topics=topics,
        website=website.strip(),
        industry_type=industry_type.strip() if entity_type == "industry" else "",
        news_scope=news_scope.strip()       if entity_type == "industry" else "",
    ))
    return RedirectResponse(f"/?msg=added&name={name}", status_code=303)


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
        raise HTTPException(400, "File must have 'name' and 'type' columns.")

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

        website_raw       = str(row.get("website",       "")).strip()
        industry_type_raw = str(row.get("industry_type", "")).strip()
        news_scope_raw    = str(row.get("news_scope",    "")).strip()
        website       = website_raw       if website_raw       not in ("", "nan") else ""
        industry_type = industry_type_raw if industry_type_raw not in ("", "nan") else ""
        news_scope    = news_scope_raw    if news_scope_raw    not in ("", "nan") else ""

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
            news_scope=news_scope       if etype == "industry" else "",
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
    name     = entity.name if entity else "Entity"
    delete_entity(entity_id)
    return RedirectResponse(f"/?msg=deleted&name={name}", status_code=303)


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
    period_days:        str       = Form(default="30"),
    date_from:          str       = Form(default=""),
    date_to:            str       = Form(default=""),
    entity_type_filter: str       = Form(default="all"),
    api_sources:        List[str] = Form(default=[]),
):
    if job_status["running"]:
        return RedirectResponse("/?msg=already_running", status_code=303)

    from services.news_fetcher import resolve_window, DEFAULT_SOURCES
    window  = resolve_window(period_days, date_from, date_to)
    sources = api_sources if api_sources else DEFAULT_SOURCES

    job_status["running"]   = True
    job_status["cancel"]    = False
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
    date_to:     str = Form(default=""),
):
    entities = get_entities()
    entity   = next((e for e in entities if e.id == entity_id), None)
    if not entity:
        raise HTTPException(404, "Entity not found")
    from services.news_fetcher import resolve_window
    window = resolve_window(period_days, date_from, date_to)
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

    selected_period = period if period in all_periods else (all_periods[0] if all_periods else "")

    def build(etype):
        result = []
        for e in entities:
            if e.entity_type == etype:
                news = all_news.get(e.id, [])
                if selected_period:
                    news = [n for n in news if n.period == selected_period]
                result.append({"entity": e, "news": news})
        return result

    from services.news_fetcher import get_current_window
    window_label = selected_period or get_current_window()["label"]

    return templates.TemplateResponse(
        request=request,
        name="digest.html",
        context={
            "request":         request,
            "active_tab":      tab,
            "clients":         build(EntityType.client),
            "prospects":       build(EntityType.prospect),
            "industries":      build(EntityType.industry),
            "job_status":      job_status,
            "gap_report":      gap_report,
            "window":          {"label": window_label},
            "all_periods":     all_periods,
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
            "request":    request,
            "gap_report": gap_report,
        }
    )


# ── Audit log view ─────────────────────────────────────────────────────────────
@app.get("/audit", response_class=HTMLResponse)
async def audit_view(request: Request):
    log = get_audit_log()
    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context={
            "request": request,
            "entries": log,
        }
    )


# ── Run history view ──────────────────────────────────────────────────────────
@app.get("/run-history", response_class=HTMLResponse)
async def run_history_view(request: Request):
    from storage import get_run_history
    runs = get_run_history()
    return templates.TemplateResponse(
        request=request,
        name="run_history.html",
        context={
            "request":    request,
            "runs":       list(reversed(runs)),   # newest first
            "job_status": job_status,
        }
    )


# ── Excel export ───────────────────────────────────────────────────────────────
@app.get("/export/excel")
async def export_excel():
    from services.excel_exporter import generate_excel_report
    from services.news_fetcher   import get_current_window

    entities = get_entities()
    all_news = get_all_news()
    window   = get_current_window()

    def build(etype):
        return [
            {"entity": e, "news": all_news.get(e.id, [])}
            for e in entities if e.entity_type == etype
        ]

    excel_bytes = generate_excel_report(
        build(EntityType.client),
        build(EntityType.prospect),
        build(EntityType.industry),
        window=window,
        gap_report=get_latest_gap_report() or {},
    )
    fname = f"news_digest_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


# ── Full pipeline (background task) ───────────────────────────────────────────
async def run_full_pipeline(window: dict, entity_type_filter: str = "all", api_sources: list = None) -> None:
    from services.news_fetcher        import fetch_all_news, build_gap_report
    from services.deduplicator        import deduplicate
    from services.validator           import validate_articles
    from services.ai_summarizer       import summarize_article
    from services.hyperlink_validator import validate_hyperlinks
    from storage                      import append_audit
    from models                       import AuditEntry, NewsItem

    started_at = datetime.now()
    entities   = get_entities()
    if entity_type_filter and entity_type_filter != "all":
        entities = [e for e in entities if e.entity_type.value == entity_type_filter]
    audit_entries  = []
    entity_results = []
    total_articles = 0
    total_dupes    = 0
    total_rejected = 0

    job_status["cancel"] = False
    pipe_log.info(f"Pipeline started — window: {window.get('label')} | entities: {len(entities)}")

    for entity in entities:
        if job_status["cancel"]:
            pipe_log.info("Pipeline cancelled by user.")
            break

        try:
            pipe_log.info(f"Processing: {entity.name}")

            # 1. Fetch
            fetch_result = await fetch_all_news(
                entity.name, entity.topics, window,
                api_sources=api_sources,
                entity_website=entity.website or "",
            )
            raw          = fetch_result["articles"]
            topic_gaps   = fetch_result["topic_gaps"]
            has_any_news = fetch_result["has_any_news"]

            entity_results.append({
                "entity_name":  entity.name,
                "entity_type":  entity.entity_type.value,
                "topic_gaps":   topic_gaps,
                "has_any_news": has_any_news,
            })

            # 1b. Log every raw article fetched (title + source + topic)
            for a in raw:
                pipe_log.info(
                    f"  FETCHED [{entity.name}] {a.get('fetch_source','?')} | "
                    f"topic={a.get('topic_queried','?')} | "
                    f"{a.get('title','')[:80]} | {a.get('url','')}"
                )

            # 2. Deduplicate
            deduped, dup_log = deduplicate(raw)
            total_dupes += len(dup_log)

            # 3. Validate
            validated, val_log = await validate_articles(deduped, window)
            total_rejected += len(val_log)

            # 4. Hyperlink check
            linked = await validate_hyperlinks(validated)

            # 5. Audit entries — dedup + validation rejections
            today = datetime.now().strftime("%Y-%m-%d")
            for d in dup_log:
                audit_entries.append(AuditEntry(
                    run_date=today,
                    entity_id=entity.id,      entity_name=entity.name,
                    article_title=d["title"], action="duplicate_removed",
                    reason=d["reason"],       source_url=d.get("url", ""),
                ))
            for v in val_log:
                audit_entries.append(AuditEntry(
                    run_date=today,
                    entity_id=entity.id,      entity_name=entity.name,
                    article_title=v["title"], action="validation_rejected",
                    reason=v["reason"],       source_url=v.get("url", ""),
                ))
            # 5b. Audit entries — hyperlink failures
            for a in linked:
                if a.get("url_status") in ("invalid", "unknown"):
                    audit_entries.append(AuditEntry(
                        run_date=today,
                        entity_id=entity.id,      entity_name=entity.name,
                        article_title=a["title"], action="url_invalid",
                        reason=f"URL status: {a.get('url_status')} — {a.get('url','')}",
                        source_url=a.get("url", ""),
                    ))

            # 6. Summarise + categorise
            news_items = []
            for art in linked:
                try:
                    result = await summarize_article(
                        art["title"], art.get("content", ""), entity.name
                    )
                    news_items.append(NewsItem(
                        title=art["title"],
                        url=art["url"],
                        source=art["source"],
                        published_date=art["published_date"],
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
                    ))
                except Exception as e:
                    log_error("summarize_article", str(e), entity.name)

            # 7. Save — even if partial
            save_news_for_entity(entity.id, news_items)
            total_articles += len(news_items)

            # 7b. Audit entries — every accepted/saved article
            for item in news_items:
                audit_entries.append(AuditEntry(
                    run_date=today,
                    entity_id=entity.id,       entity_name=entity.name,
                    article_title=item.title,  action="accepted",
                    reason="Passed all checks",
                    source_url=item.url,
                ))

            pipe_log.info(f"Done: {entity.name} — {len(news_items)} articles saved")

        except Exception as e:
            log_error("run_full_pipeline", str(e), entity.name)
            pipe_log.error(f"Failed: {entity.name} — {e}")
            # continue to next entity rather than crashing entire pipeline

    # 8. Save audit + gap report
    try:
        if audit_entries:
            append_audit(audit_entries)
        save_gap_report(build_gap_report(entity_results, window))
    except Exception as e:
        log_error("save_gap_report", str(e))

    # 9. Save run history
    completed_at = datetime.now()
    save_run({
        "run_date":          started_at.strftime("%Y-%m-%d"),
        "started_at":        started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at":      completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds":  (completed_at - started_at).seconds,
        "status":            "cancelled" if job_status["cancel"] else "completed",
        "window_label":      window.get("label", ""),
        "total_entities":    len(entities),
        "total_articles":    total_articles,
        "duplicates_removed": total_dupes,
        "articles_rejected": total_rejected,
    })

    job_status["running"]  = False
    job_status["cancel"]   = False
    job_status["last_run"] = completed_at.strftime("%d %b %Y, %H:%M")
    job_status["message"]  = (
        "Cancelled by user."
        if job_status.get("cancel")
        else f"Done — {completed_at.strftime('%d %b %Y, %H:%M')} | {total_articles} articles"
    )
    pipe_log.info(
        f"Pipeline complete — {total_articles} articles | "
        f"{total_dupes} dupes | {total_rejected} rejected | "
        f"{(completed_at - started_at).seconds}s"
    )


# ── Single entity pipeline (background task) ──────────────────────────────────
async def run_entity_pipeline(entity, window: dict, api_sources: list = None) -> None:
    from services.news_fetcher        import fetch_all_news, DEFAULT_SOURCES
    from services.deduplicator        import deduplicate
    from services.validator           import validate_articles
    from services.ai_summarizer       import summarize_article
    from services.hyperlink_validator import validate_hyperlinks
    from storage                      import append_audit
    from models                       import AuditEntry, NewsItem

    sources = api_sources if api_sources else DEFAULT_SOURCES
    pipe_log.info(f"Single entity pipeline: {entity.name} | window: {window.get('label')} | sources: {sources}")

    fetch_result = await fetch_all_news(
        entity.name, entity.topics, window,
        api_sources=sources,
        entity_website=entity.website or "",
    )
    raw = fetch_result["articles"]

    # log each raw article so we can see what was fetched
    for a in raw:
        pipe_log.info(
            f"  FETCHED [{entity.name}] {a.get('fetch_source','?')} | "
            f"topic={a.get('topic_queried','?')} | "
            f"{a.get('title','')[:80]}"
        )

    deduped, dup_log   = deduplicate(raw)
    validated, val_log = await validate_articles(deduped, window)
    linked             = await validate_hyperlinks(validated)

    # Build audit entries for every rejection
    today         = datetime.now().strftime("%Y-%m-%d")
    audit_entries = []
    for d in dup_log:
        audit_entries.append(AuditEntry(
            run_date=today,
            entity_id=entity.id,      entity_name=entity.name,
            article_title=d["title"], action="duplicate_removed",
            reason=d["reason"],       source_url=d.get("url", ""),
        ))
    for v in val_log:
        audit_entries.append(AuditEntry(
            run_date=today,
            entity_id=entity.id,      entity_name=entity.name,
            article_title=v["title"], action="validation_rejected",
            reason=v["reason"],       source_url=v.get("url", ""),
        ))
    for a in linked:
        if a.get("url_status") in ("invalid", "unknown"):
            audit_entries.append(AuditEntry(
                run_date=today,
                entity_id=entity.id,      entity_name=entity.name,
                article_title=a["title"], action="url_invalid",
                reason=f"URL status: {a.get('url_status')} — {a.get('url','')}",
                source_url=a.get("url", ""),
            ))

    pipe_log.info(
        f"  {entity.name}: {len(raw)} raw | {len(dup_log)} dupes | "
        f"{len(val_log)} rejected | {len(linked)} passed validation"
    )

    news_items = []
    for art in linked:
        try:
            result = await summarize_article(
                art["title"], art.get("content", ""), entity.name
            )
            news_items.append(NewsItem(
                title=art["title"],
                url=art["url"],
                source=art["source"],
                published_date=art["published_date"],
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
            ))
        except Exception as e:
            log_error("run_entity_pipeline.summarize", str(e), entity.name)

    save_news_for_entity(entity.id, news_items)

    # Audit — accepted articles + all rejections
    for item in news_items:
        audit_entries.append(AuditEntry(
            run_date=today,
            entity_id=entity.id,       entity_name=entity.name,
            article_title=item.title,  action="accepted",
            reason="Passed all checks",
            source_url=item.url,
        ))

    if audit_entries:
        try:
            append_audit(audit_entries)
        except Exception as e:
            log_error("run_entity_pipeline.audit", str(e), entity.name)

    pipe_log.info(f"Single entity done: {entity.name} — {len(news_items)} articles saved")