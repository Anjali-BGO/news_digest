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
SNAPSHOTS_DIR  = Path("pipeline_snapshots")  # per-run, per-entity stage snapshots


# ── Internal helpers ───────────────────────────────────────────────────────────
def _load(path: Path) -> Any:
    if not path.exists():
        return [] if path in (AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE) else {}
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

    # Merge by period: preserve articles from other periods, replace only the
    # period(s) present in the incoming batch. Without this, a 7-day re-run
    # would wipe all articles from previous 30-day runs for the same entity.
    incoming_periods = {i.period for i in items if i.period}
    if incoming_periods:
        existing = data["news"].get(entity_id, [])
        kept = [e for e in existing if e.get("period", "") not in incoming_periods]
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
        audit_log.info(
            f"{e.run_date} | {e.action:<22} | {e.entity_name:<20} | "
            f"{e.reason:<40} | {e.article_title[:80]} | {e.source_url}"
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