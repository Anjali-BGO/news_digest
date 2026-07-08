"""
Standalone test script for the Tavily fetch pipeline.

Runs fetch -> deduplicate -> validate for a single entity over a short window,
so results can be inspected before rolling out to all entities.

Usage:
    python test_tavily.py                        # lists available entities
    python test_tavily.py "Barclays"             # fetch by name (partial match)
    python test_tavily.py "Barclays" --max 20    # cap raw articles at 20
"""

import os
import sys
import asyncio
from datetime import datetime, timedelta

os.environ.setdefault("TAVILY_MIN_SCORE", "0.05")

from dotenv import load_dotenv
load_dotenv(override=False)

from storage import get_entities
from services.news_fetcher import fetch_all_news, TAVILY_MIN_SCORE
from services.deduplicator import deduplicate
from services.validator import validate_articles


def _two_day_window(days: int = 2) -> dict:
    today    = datetime.now()
    yesterday = today - timedelta(days=1)
    start     = today - timedelta(days=days)
    fmt = "%Y-%m-%d"
    return {
        "from":  start.strftime(fmt),
        "to":    yesterday.strftime(fmt),
        "label": f"{start.strftime('%d %b %Y')} - {yesterday.strftime('%d %b %Y')}",
        "days":  days,
    }


def _pick_entity(search: str):
    entities = get_entities()
    if not search:
        print("\nAvailable entities:\n")
        for e in entities:
            print(f"  [{e.entity_type.value:8}]  {e.name}")
        print("\nRun: python test_tavily.py \"<entity name>\"")
        sys.exit(0)
    lower = search.lower()
    matches = [e for e in entities if lower in e.name.lower()]
    if not matches:
        print(f"No entity matching '{search}'. Run without arguments to list all.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple matches for '{search}':")
        for e in matches:
            print(f"  {e.name}  ({e.entity_type.value})")
        print("Be more specific.")
        sys.exit(1)
    return matches[0]


async def run_test(entity_name: str, max_articles: int, days: int = 7) -> None:
    entity = _pick_entity(entity_name)
    window = _two_day_window(days)

    if max_articles:
        os.environ["MAX_ARTICLES_PER_ENTITY"] = str(max_articles)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Entity  : {entity.name}  ({entity.entity_type.value})")
    print(f"  Window  : {window['label']}")
    print(f"  Topics  : {len(entity.topics) if entity.topics else 'all 12 (none configured)'}")
    print(f"  Website : {entity.website or '(none)'}")
    print(f"  MIN_SCORE: {TAVILY_MIN_SCORE}")
    if max_articles:
        print(f"  Cap     : {max_articles} raw articles")
    print(f"{sep}\n")

    # 1. Fetch
    print("Fetching...")
    result = await fetch_all_news(
        entity_name=entity.name,
        topics=entity.topics or [],
        window=window,
        api_sources=None,           # uses DEFAULT_SOURCES (tavily)
        entity_website=entity.website or "",
        entity_type=entity.entity_type.value,
    )
    raw = result["articles"]
    print(f"  Raw fetched      : {len(raw)}")

    # ── 2. Deduplicate ─────────────────────────────────────────────────────────
    deduped, dup_log = deduplicate(raw)
    print(f"  After dedup      : {len(deduped)}  (-{len(dup_log)} dupes removed)")

    # ── 3. Validate ────────────────────────────────────────────────────────────
    validated, val_log = await validate_articles(
        deduped,
        window=window,
        entity_name=entity.name,
        entity_website=entity.website or "",
        entity_aliases=entity.aliases or [],
        entity_type=entity.entity_type.value,
    )
    print(f"  After validation : {len(validated)}  (-{len(val_log)} rejected)")

    # Results summary
    accept_rate = (len(validated) / len(raw) * 100) if raw else 0
    sep2 = "-" * 60
    print(f"\n{sep2}")
    print(f"  RESULT  {len(raw)} raw  ->  -{len(dup_log)} dupes  ->  -{len(val_log)} rejected  ->  {len(validated)} accepted")
    print(f"  Accept rate: {accept_rate:.1f}%")
    print(sep2)

    # Rejection breakdown
    if val_log:
        from collections import Counter
        reasons = Counter(v["reason"].split("--")[0].split(" - ")[0][:60].strip() for v in val_log)
        print("\nRejection breakdown:")
        for reason, count in reasons.most_common():
            print(f"  {count:4}  {reason}")

    # Accepted articles
    if validated:
        print(f"\nAccepted articles ({len(validated)}):")
        for i, a in enumerate(validated, 1):
            score = f"  score={a.get('score', '?')}" if "score" in a else ""
            src   = a.get("fetch_source", "")
            date  = a.get("published_date", "")
            print(f"  {i:2}. [{date}] [{src}]{score}")
            print(f"      {a['title'][:90]}")
            print(f"      {a['url'][:90]}")
    else:
        print("\nNo articles passed validation.")

    print()


if __name__ == "__main__":
    search  = sys.argv[1] if len(sys.argv) > 1 else ""
    max_cap = 0
    days    = 7  # default window
    if "--max" in sys.argv:
        idx = sys.argv.index("--max")
        try:
            max_cap = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            pass
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        try:
            days = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            pass

    asyncio.run(run_test(search, max_cap, days))
