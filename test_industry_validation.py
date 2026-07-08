"""
Run AI Validation on existing data.json industry articles.
Seeds industry_all_records.json then runs validate_industry_article_with_ai on each article.
Prints per-entity and overall accuracy stats.

WARNING: this script calls the same storage.py read-modify-write functions the
live FastAPI app uses, directly on data.json / industry_all_records.json /
industry_accepted.json / industry_rejected.json — with no awareness of the
app's in-memory job_status mutex. Running it while `uvicorn` is serving (or
mid-pipeline) can silently clobber whichever write lands last. The startup
check below refuses to run if something is already listening on the app's
default port, as a best-effort guard against that.
"""
import asyncio
import json
import socket
from pathlib import Path
from collections import Counter, defaultdict


def _server_appears_running(host: str = "127.0.0.1", port: int = 8000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

async def main():
    from dotenv import load_dotenv
    load_dotenv()

    from services.industry_validator import validate_industry_article_with_ai
    from storage import (
        get_entities, save_industry_all_records,
        save_industry_accepted, save_industry_rejected,
        update_industry_validation_status,
    )

    # ── Load entities + industry articles ─────────────────────────────────────
    entities = get_entities()
    industry_entities = [e for e in entities if e.entity_type.value == "industry"]
    entity_map = {e.id: e for e in industry_entities}

    sector_info = [
        {
            "name":          e.name,
            "entity_type":   "industry",
            "industry_type": e.industry_type or "",
            "news_scope":    e.news_scope or "",
        }
        for e in industry_entities
    ]

    data = json.loads(Path("data.json").read_text(encoding="utf-8"))
    news = data.get("news", {})

    # Collect all industry articles and enrich with entity metadata
    all_arts = []
    for eid, items in news.items():
        ent = entity_map.get(eid)
        if not ent:
            continue
        for art in items:
            all_arts.append({
                **art,
                "entity_id":       eid,
                "entity_type":     "industry",
                "industry_sector": ent.name,
                "industry_type":   ent.industry_type or "",
                "run_id":          "test_revalidation",
                "content":         art.get("content") or "",
            })

    print(f"\n{'='*60}")
    print(f" Industry AI Validation Test — {len(all_arts)} articles")
    print(f"{'='*60}")

    # ── Determine window from the most recent period ───────────────────────────
    # Use the widest window across all stored periods so nothing gets date-rejected
    run_history = json.loads(Path("run_history.json").read_text(encoding="utf-8")) if Path("run_history.json").exists() else []
    window = {"from": "2026-01-01", "to": "2026-12-31", "label": "2026"}
    if run_history:
        r = run_history[-1]
        window = {
            "from":  r.get("window_from", "2026-01-01"),
            "to":    r.get("window_to",   "2026-12-31"),
            "label": r.get("window_label", "2026"),
        }
        print(f" Window: {window['from']} to {window['to']}")

    print(f" Sectors: {len(industry_entities)}")
    print(f"{'='*60}\n")

    # ── Run validation ─────────────────────────────────────────────────────────
    seen_urls = set()
    results   = []
    status_counts  = Counter()
    per_entity     = defaultdict(lambda: {"total": 0, "validated": 0, "rejected": 0, "review": 0})
    cat_changes    = 0
    sector_changes = 0

    for i, art in enumerate(all_arts, 1):
        ename = art.get("industry_sector", "?")
        print(f"[{i:>3}/{len(all_arts)}] {ename[:30]:<30} | {art.get('title','')[:50]}")

        result = await validate_industry_article_with_ai(art, sector_info, window, seen_urls)
        results.append(result)

        status = result.get("validation_status", "?")
        status_counts[status] += 1
        per_entity[ename]["total"] += 1
        if status == "Validated News":
            per_entity[ename]["validated"] += 1
        elif status == "Non Validated News":
            per_entity[ename]["rejected"] += 1
        else:
            per_entity[ename]["review"] += 1

        # Track category changes (AI Validation vs original)
        orig_cat = art.get("primary_category", "")
        new_cat  = result.get("primary_category", "")
        if orig_cat and new_cat and orig_cat != new_cat:
            cat_changes += 1

        # Track sector assignment
        new_sector = result.get("industry_sector", "")
        if new_sector and new_sector != ename:
            sector_changes += 1

        reason = result.get("validation_reason", "")
        conf   = result.get("ai_confidence", "")
        print(f"         → {status} | cat: {new_cat[:35]} | conf: {conf} | {reason[:50]}")

    # ── Save results ───────────────────────────────────────────────────────────
    save_industry_all_records(results)
    save_industry_accepted([r for r in results if r.get("validation_status") == "Validated News"])
    save_industry_rejected([r for r in results if r.get("validation_status") == "Non Validated News"])
    update_industry_validation_status(results)
    print(f"\nSaved {len(results)} records to industry_all_records.json")
    print("data.json updated with validation_status badges.\n")

    # ── Print accuracy report ──────────────────────────────────────────────────
    total = len(results)
    validated = status_counts["Validated News"]
    rejected  = status_counts["Non Validated News"]
    review    = status_counts["Review"]

    print(f"{'='*60}")
    print(f" ACCURACY REPORT — {total} articles")
    print(f"{'='*60}")
    print(f"  Validated News     : {validated:>3}  ({validated/total*100:.1f}%)")
    print(f"  Non Validated News : {rejected:>3}  ({rejected/total*100:.1f}%)")
    print(f"  Review             : {review:>3}  ({review/total*100:.1f}%)")
    print(f"  Category changed   : {cat_changes:>3}  ({cat_changes/total*100:.1f}%)")
    print(f"  Sector re-assigned : {sector_changes:>3}  ({sector_changes/total*100:.1f}%)")
    print(f"\n  Per entity:")
    for ename, cnts in sorted(per_entity.items(), key=lambda x: x[0]):
        t = cnts["total"]
        v = cnts["validated"]
        rate = f"{v/t*100:.0f}%" if t else "—"
        print(f"    {ename[:45]:<45} {v}/{t}  ({rate})")

    # Rejection breakdown
    rej_reasons = Counter(
        r.get("validation_reason", "?")
        for r in results
        if r.get("validation_status") != "Validated News"
    )
    if rej_reasons:
        print(f"\n  Non-validated reasons:")
        for reason, count in rej_reasons.most_common(10):
            print(f"    {count:>2}×  {reason[:70]}")

    print(f"{'='*60}")


if __name__ == "__main__":
    if _server_appears_running():
        raise SystemExit(
            "Refusing to run: the News Digest app appears to be running on "
            "127.0.0.1:8000. This script writes directly to the same JSON files "
            "the live app uses with no cross-process locking — running both at "
            "once can silently clobber data. Stop the server first."
        )
    asyncio.run(main())
