import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from models import Entity, NewsItem, AuditEntry
from logger import get_logger, get_audit_logger

log       = get_logger("storage")
audit_log = get_audit_logger()

DATA_FILE    = Path("data.json")
AUDIT_FILE   = Path("audit.json")
GAP_FILE     = Path("gap_report.json")
RUN_FILE     = Path("run_history.json")    # every pipeline run + outcome
ERROR_FILE   = Path("error_log.json")      # all application errors


# ── Internal helpers ───────────────────────────────────────────────────────────
def _load(path: Path) -> Any:
    if not path.exists():
        return [] if path in (AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE) else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Failed to load {path}: {e}")
        return [] if path in (AUDIT_FILE, GAP_FILE, RUN_FILE, ERROR_FILE) else {}

def _save(path: Path, data: Any) -> bool:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception as e:
        log.error(f"Failed to save {path}: {e}")
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
    data["news"][entity_id] = [i.model_dump() for i in items]
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
    from datetime import datetime
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