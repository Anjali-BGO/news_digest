import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from models import Entity, NewsItem, AuditEntry
from logger import get_logger, get_audit_logger

log       = get_logger("storage")
audit_log = get_audit_logger()

DATA_FILE      = Path("data.json")
AUDIT_FILE     = Path("audit.json")
GAP_FILE       = Path("gap_report.json")
RUN_FILE       = Path("run_history.json")    # every pipeline run + outcome
ERROR_FILE     = Path("error_log.json")      # all application errors
SNAPSHOTS_DIR  = Path("pipeline_snapshots")  # per-run, per-entity stage snapshots (rotated)
RAW_NEWS_DIR   = Path("raw_news")            # permanent raw API fetch store (never rotated)

# Industry-specific storage — completely separate from client/prospect data.json
INDUSTRY_ALL_RECORDS_FILE = Path("industry_all_records.json")  # all industry articles + validation status
INDUSTRY_ACCEPTED_FILE    = Path("industry_accepted.json")      # Validated News records
INDUSTRY_REJECTED_FILE    = Path("industry_rejected.json")      # Non Validated News records


# ── Internal helpers ───────────────────────────────────────────────────────────
_LIST_FILES = None  # lazily initialised after all file constants are defined

def _load(path: Path) -> Any:
    global _LIST_FILES
    if _LIST_FILES is None:
        _LIST_FILES = {AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE,
                       INDUSTRY_ALL_RECORDS_FILE, INDUSTRY_ACCEPTED_FILE, INDUSTRY_REJECTED_FILE}
    if not path.exists():
        return [] if path in _LIST_FILES else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Failed to load {path}: {e}")
        # Move corrupt file aside so the next _save does not silently wipe it.
        # The .corrupt backup is recoverable; a silent {} overwrite is not.
        backup = path.with_suffix(".corrupt.json")
        try:
            path.replace(backup)  # replace() works even if backup already exists on Windows
            log.error(f"Corrupt file backed up to {backup} — manual recovery possible")
        except Exception as rename_err:
            log.error(
                f"Could not back up corrupt file {path} to {backup}: {rename_err} "
                "— the next _save() call will overwrite it; manual recovery may not be possible"
            )
        return [] if path in (AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE) else {}

def _save(path: Path, data: Any) -> bool:
    # Write to a sibling .tmp file first, then atomically rename over the target.
    # This prevents a crash mid-write from leaving a half-written (corrupt) JSON file.
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic on Windows: MoveFileExW with MOVEFILE_REPLACE_EXISTING
        return True
    except Exception as e:
        log.error(f"Failed to save {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ── Entity operations ──────────────────────────────────────────────────────────
def get_entities() -> List[Entity]:
    data = _load(DATA_FILE)
    try:
        return [Entity(**e) for e in data.get("entities", [])]
    except Exception as e:
        log.error(f"Failed to parse entities: {e}")
        return []

def save_entity(entity: Entity):
    data = _load(DATA_FILE)
    data.setdefault("entities", [])
    data["entities"] = [e for e in data["entities"] if e["id"] != entity.id]
    data["entities"].append(entity.model_dump())
    _save(DATA_FILE, data)
    log.info(f"Saved entity: {entity.name} ({entity.entity_type})")

def delete_entity(entity_id: str):
    data = _load(DATA_FILE)
    before = len(data.get("entities", []))
    data["entities"] = [e for e in data.get("entities", []) if e["id"] != entity_id]
    data.get("news", {}).pop(entity_id, None)
    _save(DATA_FILE, data)
    log.info(f"Deleted entity {entity_id} — removed {before - len(data['entities'])} record(s)")


# ── News operations ────────────────────────────────────────────────────────────
def save_news_for_entity(entity_id: str, items: List[NewsItem]):
    data = _load(DATA_FILE)
    data.setdefault("news", {})

    # Merge by (period, fetch_source): preserve articles from other periods,
    # and preserve articles from sources this batch didn't touch, replacing
    # only the (period, source) pairs actually present in the incoming batch.
    # Replacing by period alone would let a rerun of the SAME 7-day window
    # with a DIFFERENT api_sources selection silently wipe the previous
    # source's already-accepted articles for that period instead of
    # accumulating across sources — see CLAUDE.md root-cause notes.
    incoming_periods = {i.period for i in items if i.period}
    if incoming_periods:
        incoming_sources = {i.fetch_source for i in items if i.fetch_source}
        existing = data["news"].get(entity_id, [])
        kept = [
            e for e in existing
            if not (e.get("period", "") in incoming_periods
                    and e.get("fetch_source", "") in incoming_sources)
        ]
        data["news"][entity_id] = kept + [i.model_dump() for i in items]
    else:
        # No period set on any incoming item — preserve existing articles rather
        # than wiping them.  An empty period means something went wrong upstream;
        # losing previously-fetched data would be far worse than a silent append.
        log.warning(
            f"save_news_for_entity: no period set on {len(items)} incoming items "
            f"for entity {entity_id} — preserving existing articles and appending"
        )
        existing = data["news"].get(entity_id, [])
        data["news"][entity_id] = existing + [i.model_dump() for i in items]

    _save(DATA_FILE, data)
    log.info(f"Saved {len(items)} articles for entity {entity_id}")

def get_news_for_entity(entity_id: str) -> List[NewsItem]:
    data = _load(DATA_FILE)
    try:
        return [NewsItem(**i) for i in data.get("news", {}).get(entity_id, [])]
    except Exception as e:
        log.error(f"Failed to parse news for {entity_id}: {e}")
        return []

def get_all_news() -> Dict[str, List[NewsItem]]:
    data = _load(DATA_FILE)
    result = {}
    for eid, items in data.get("news", {}).items():
        try:
            result[eid] = [NewsItem(**i) for i in items]
        except Exception as e:
            log.error(f"Failed to parse news for entity {eid}: {e}")
            result[eid] = []
    return result


# ── Audit log operations ───────────────────────────────────────────────────────
def append_audit(entries: List[AuditEntry]):
    log_data = _load(AUDIT_FILE)
    if not isinstance(log_data, list):
        log_data = []
    log_data.extend([e.model_dump() for e in entries])
    _save(AUDIT_FILE, log_data)
    log.info(f"Appended {len(entries)} audit entries")
    # also write each entry as a structured line to audit.log
    for e in entries:
        period = f"{e.window_from}→{e.window_to}" if e.window_from else ""
        audit_log.info(
            f"{e.run_date} | {period:<23} | {e.action:<22} | {e.entity_name:<20} | "
            f"{e.fetch_source or '':<10} | {e.reason:<40} | {e.article_title[:80]} | {e.source_url}"
        )

def get_audit_log(entity_id: Optional[str] = None) -> List[AuditEntry]:
    log_data = _load(AUDIT_FILE)
    if not isinstance(log_data, list):
        return []
    try:
        entries = [AuditEntry(**e) for e in log_data]
        if entity_id:
            entries = [e for e in entries if e.entity_id == entity_id]
        return entries
    except Exception as e:
        log.error(f"Failed to parse audit log: {e}")
        return []


# ── Gap report operations ──────────────────────────────────────────────────────
def save_gap_report(report: Dict):
    existing = _load(GAP_FILE)
    if not isinstance(existing, list):
        existing = []
    existing.append(report)
    _save(GAP_FILE, existing)
    log.info(f"Gap report saved — period: {report.get('window', {}).get('label', '')}")

def get_latest_gap_report() -> Optional[Dict]:
    data = _load(GAP_FILE)
    if not isinstance(data, list) or not data:
        return None
    return data[-1]

def get_all_gap_reports() -> List[Dict]:
    data = _load(GAP_FILE)
    return data if isinstance(data, list) else []


# ── Run history ────────────────────────────────────────────────────────────────
def save_run(run: Dict):
    """
    Saves a record of every pipeline run.
    Fields: run_date, started_at, completed_at, status,
            window_label, total_entities, total_articles,
            duplicates_removed, articles_rejected, cancelled
    """
    existing = _load(RUN_FILE)
    if not isinstance(existing, list):
        existing = []
    existing.append(run)
    _save(RUN_FILE, existing)
    log.info(f"Run saved — status: {run.get('status')} | articles: {run.get('total_articles')}")

def get_run_history() -> List[Dict]:
    data = _load(RUN_FILE)
    return data if isinstance(data, list) else []


# ── Error log ──────────────────────────────────────────────────────────────────
def log_error(context: str, error: str, entity_name: str = ""):
    """
    Persists application errors to error_log.json.
    Captured separately from audit log so errors are easy to find.
    """
    existing = _load(ERROR_FILE)
    if not isinstance(existing, list):
        existing = []
    existing.append({
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "context":     context,
        "entity_name": entity_name,
        "error":       str(error),
    })
    _save(ERROR_FILE, existing)
    log.error(f"[{context}] {entity_name} — {error}")

def get_error_log() -> List[Dict]:
    data = _load(ERROR_FILE)
    return data if isinstance(data, list) else []


# ── Pipeline stage snapshots ───────────────────────────────────────────────────
def save_stage_snapshot(run_id: str, entity_name: str, stage: str, records: list) -> None:
    """
    Persists pipeline records at a specific stage so data is never lost.

    Files land at: pipeline_snapshots/<run_id>/<entity>.json
    Each entity file is a dict keyed by stage name:
      "1_raw", "2_deduped", "2_dedup_rejected",
      "3_validated", "3_validation_rejected",
      "4_hyperlinked", "5_summarized"

    Written incrementally — each call appends/updates one key so a crash
    mid-pipeline still leaves everything up to that stage on disk.
    """
    try:
        run_dir = SNAPSHOTS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        safe = re.sub(r'[^\w\-]', '_', entity_name).strip('_') or "entity"
        entity_file = run_dir / f"{safe}.json"

        existing: dict = {}
        if entity_file.exists():
            try:
                existing = json.loads(entity_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        existing["_entity"] = entity_name
        existing["_run_id"] = run_id
        existing[stage]     = records

        # Atomic write so a crash never corrupts the snapshot file
        tmp = entity_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(entity_file)
    except Exception as e:
        log.error(f"save_stage_snapshot failed [{entity_name} / {stage}]: {e}")


def rotate_snapshots(keep: int = 5) -> None:
    """Delete the oldest run directories, keeping only the most recent `keep` runs."""
    if not SNAPSHOTS_DIR.exists():
        return
    runs = sorted(
        [p for p in SNAPSHOTS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )
    for old_run in runs[:-keep]:
        try:
            shutil.rmtree(old_run)
            log.info(f"Rotated old snapshot: {old_run.name}")
        except Exception as e:
            log.warning(f"Could not remove old snapshot dir {old_run}: {e}")


# ── Permanent raw news store ───────────────────────────────────────────────────
# Every article returned by any API, before dedup/validation, stored permanently.
# One JSON file per pipeline run: raw_news/<run_id>.json
# Format: {run_id, run_date, window, entities: {entity_id: {name, type, articles:[...]}}}

def save_raw_articles(run_id: str, entity_id: str, entity_name: str,
                      entity_type: str, window: dict, articles: list) -> None:
    """Append raw fetched articles for one entity into the run's permanent raw file."""
    try:
        RAW_NEWS_DIR.mkdir(parents=True, exist_ok=True)
        run_file = RAW_NEWS_DIR / f"{run_id}.json"

        existing: dict = {}
        if run_file.exists():
            try:
                existing = json.loads(run_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        existing.setdefault("run_id",   run_id)
        existing.setdefault("run_date", run_id[:10])
        existing.setdefault("window",   window)
        existing.setdefault("entities", {})
        existing["entities"][entity_id] = {
            "name":          entity_name,
            "type":          entity_type,
            "article_count": len(articles),
            "articles":      articles,
        }

        tmp = run_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(run_file)
    except Exception as e:
        log.error(f"save_raw_articles failed [{entity_name}]: {e}")


def get_raw_run(run_id: str) -> Optional[Dict]:
    """Load the raw article store for a specific run."""
    path = RAW_NEWS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"get_raw_run failed [{run_id}]: {e}")
        return None


def list_raw_runs() -> List[str]:
    """Return sorted list of run IDs that have a raw news file."""
    if not RAW_NEWS_DIR.exists():
        return []
    return sorted(p.stem for p in RAW_NEWS_DIR.glob("*.json"))


# ── Audit backfill ─────────────────────────────────────────────────────────────
def backfill_audit_entries() -> Dict:
    """
    Patch existing audit.json entries with missing entity_type, fetch_source,
    window_from, and window_to using every available data source.

    Sources consulted (in order):
      entity_type  → current entity list (by entity_id)
      window       → run_history window_label parsed to dates; skipped when ambiguous
      fetch_source → pipeline_snapshots 1_raw + raw_news store (matched by URL then title)

    Returns a summary dict describing how many entries were patched.
    """
    # ── Build entity_type lookup ───────────────────────────────────────────────
    entities_map: Dict[str, str] = {}
    for e in _load(DATA_FILE).get("entities", []):
        entities_map[e["id"]] = e.get("entity_type", "")

    # ── Build run_date → set of (from, to) tuples from run_history ────────────
    date_windows: Dict[str, set] = {}
    for r in (_load(RUN_FILE) if isinstance(_load(RUN_FILE), list) else []):
        label    = r.get("window_label", "")
        run_date = r.get("run_date", "")
        # Also accept explicit fields if already stored
        if r.get("window_from") and r.get("window_to"):
            date_windows.setdefault(run_date, set()).add(
                (r["window_from"], r["window_to"])
            )
            continue
        # Parse "27 May 2026 — 25 Jun 2026" (any dash variant)
        parts = re.split(r"\s+[–—\-]+\s+", label)
        if len(parts) == 2:
            try:
                from_d = datetime.strptime(parts[0].strip(), "%d %b %Y").strftime("%Y-%m-%d")
                to_d   = datetime.strptime(parts[1].strip(), "%d %b %Y").strftime("%Y-%m-%d")
                date_windows.setdefault(run_date, set()).add((from_d, to_d))
            except ValueError:
                pass

    # ── Build URL/title → fetch_source lookup from all available snapshots ─────
    url_to_source:   Dict[str, str] = {}
    title_to_source: Dict[str, str] = {}

    def _index_articles(articles: list) -> None:
        for art in articles:
            src = art.get("fetch_source", "")
            if not src:
                continue
            url = (art.get("url") or "").rstrip("/")
            if url:
                url_to_source.setdefault(url, src)
            title = (art.get("title") or "")[:100]
            if title:
                title_to_source.setdefault(title, src)

    # pipeline_snapshots (rotated, may be partial)
    if SNAPSHOTS_DIR.exists():
        for run_dir in SNAPSHOTS_DIR.iterdir():
            if not run_dir.is_dir():
                continue
            for ef in run_dir.glob("*.json"):
                try:
                    data = json.loads(ef.read_text(encoding="utf-8"))
                    _index_articles(data.get("1_raw", []))
                except Exception:
                    pass

    # raw_news store (permanent)
    if RAW_NEWS_DIR.exists():
        for rf in RAW_NEWS_DIR.glob("*.json"):
            try:
                data = json.loads(rf.read_text(encoding="utf-8"))
                for edata in data.get("entities", {}).values():
                    _index_articles(edata.get("articles", []))
            except Exception:
                pass

    # ── Patch audit entries ────────────────────────────────────────────────────
    raw = _load(AUDIT_FILE)
    if not isinstance(raw, list):
        return {"patched_entity_type": 0, "patched_window": 0,
                "patched_fetch_source": 0, "total": 0}

    p_type = p_window = p_source = 0

    for entry in raw:
        if not entry.get("entity_type"):
            et = entities_map.get(entry.get("entity_id", ""), "")
            if et:
                entry["entity_type"] = et
                p_type += 1

        if not entry.get("window_from"):
            run_date = entry.get("run_date", "")
            windows  = date_windows.get(run_date, set())
            if len(windows) == 1:
                from_d, to_d = next(iter(windows))
                entry["window_from"] = from_d
                entry["window_to"]   = to_d
                p_window += 1

        if not entry.get("fetch_source"):
            url   = (entry.get("source_url") or "").rstrip("/")
            title = (entry.get("article_title") or "")[:100]
            src   = url_to_source.get(url) or title_to_source.get(title)
            if src:
                entry["fetch_source"] = src
                p_source += 1

    _save(AUDIT_FILE, raw)
    log.info(
        f"Audit backfill complete — type: {p_type} | window: {p_window} | source: {p_source}"
    )
    return {
        "total":                 len(raw),
        "patched_entity_type":   p_type,
        "patched_window":        p_window,
        "patched_fetch_source":  p_source,
    }


# ── Industry AI Validation storage ────────────────────────────────────────────
# Industry articles flow:
#   pipeline → save_industry_run_records (pre-AI, with content)
#   AI Validation → save_industry_all_records (updates validation_status in place)
#   AI Validation → update_industry_validation_status (mirrors status to data.json)

def get_industry_all_records() -> List[Dict]:
    data = _load(INDUSTRY_ALL_RECORDS_FILE)
    return data if isinstance(data, list) else []


def save_industry_run_records(run_id: str, period: str, entity_id: str, records: list) -> None:
    """Replace existing records for this entity+period combination, append new ones."""
    existing = get_industry_all_records()
    kept = [r for r in existing if not (
        r.get("entity_id") == entity_id and r.get("period") == period
    )]
    kept.extend(records)
    _save(INDUSTRY_ALL_RECORDS_FILE, kept)
    log.info(f"Saved {len(records)} industry records | entity: {entity_id} | period: {period}")


def save_industry_all_records(records: list, replace_period: str = None) -> None:
    """Save industry records after AI Validation. Optionally replace only one period."""
    if replace_period:
        existing = get_industry_all_records()
        kept = [r for r in existing if r.get("period") != replace_period]
        kept.extend(records)
        _save(INDUSTRY_ALL_RECORDS_FILE, kept)
    else:
        _save(INDUSTRY_ALL_RECORDS_FILE, records)
    log.info(
        f"Saved {len(records)} industry AI-validated records"
        + (f" | period: {replace_period}" if replace_period else "")
    )


def save_industry_accepted(records: list) -> None:
    """
    Refresh industry_accepted.json with these Validated-News records, merging by
    URL — removes any prior copy (from either accepted or rejected) so
    re-validating the same article updates it in place instead of appending a
    duplicate, and correctly moves it here if its status flipped from
    Non Validated on a re-run.
    """
    if not records:
        return
    urls = {r["url"] for r in records if r.get("url")}

    existing = _load(INDUSTRY_ACCEPTED_FILE)
    existing = existing if isinstance(existing, list) else []
    existing = [r for r in existing if r.get("url") not in urls]
    existing.extend(records)
    _save(INDUSTRY_ACCEPTED_FILE, existing)

    rejected = _load(INDUSTRY_REJECTED_FILE)
    if isinstance(rejected, list):
        pruned = [r for r in rejected if r.get("url") not in urls]
        if len(pruned) != len(rejected):
            _save(INDUSTRY_REJECTED_FILE, pruned)


def save_industry_rejected(records: list) -> None:
    """
    Refresh industry_rejected.json with these Non-Validated-News records, merging
    by URL — removes any prior copy (from either file) so re-validating the same
    article updates it in place instead of appending a duplicate, and correctly
    moves it here if its status flipped from Validated on a re-run.
    """
    if not records:
        return
    urls = {r["url"] for r in records if r.get("url")}

    existing = _load(INDUSTRY_REJECTED_FILE)
    existing = existing if isinstance(existing, list) else []
    existing = [r for r in existing if r.get("url") not in urls]
    existing.extend(records)
    _save(INDUSTRY_REJECTED_FILE, existing)

    accepted = _load(INDUSTRY_ACCEPTED_FILE)
    if isinstance(accepted, list):
        pruned = [r for r in accepted if r.get("url") not in urls]
        if len(pruned) != len(accepted):
            _save(INDUSTRY_ACCEPTED_FILE, pruned)


def update_industry_validation_status(records: list) -> None:
    """
    Mirror validation_status, validation_reason, clean_title, summary, and categories
    from AI-validated records back to data.json so the /industry page shows badges.

    Matches articles by URL. Non-industry or unmatched articles are left untouched.
    """
    data = _load(DATA_FILE)
    news = data.get("news", {})
    url_map = {r["url"]: r for r in records if r.get("url")}

    changed = 0
    for eid, items in news.items():
        for i, art in enumerate(items):
            if art.get("entity_type") == "industry" and art.get("url") in url_map:
                upd = url_map[art["url"]]
                news[eid][i] = {
                    **art,
                    "validation_status":  upd.get("validation_status"),
                    "validation_reason":  upd.get("validation_reason") or "",
                    "title":              upd.get("clean_title") or art.get("title", ""),
                    "summary":            upd.get("summary") or art.get("summary", ""),
                    "primary_category":   upd.get("primary_category") or art.get("primary_category", ""),
                    "secondary_category": upd.get("secondary_categories") or art.get("secondary_category", ""),
                    "url_status":         upd.get("url_status") or art.get("url_status", ""),
                }
                changed += 1

    if changed:
        data["news"] = news
        _save(DATA_FILE, data)
        log.info(f"Updated validation_status for {changed} industry articles in data.json")