# News Digest Platform — CLAUDE.md

Internal news intelligence tool that automates fetching, deduplicating, validating, summarising, and categorising business news for a defined list of clients, prospects, and industries. Built in Python with FastAPI; no third-party automation frameworks.

---

## How to Run

```bash
cd d:\Code\news_digest
venv\Scripts\activate
uvicorn main:app --reload
# App opens at http://localhost:8000
```

---

## Project Structure

```
news_digest/
├── main.py                 # FastAPI app, all routes, background pipeline orchestration
├── models.py               # Pydantic models: Entity, NewsItem, AuditEntry, TOPIC_CATEGORIES
├── storage.py              # JSON file persistence (data.json, audit.json, etc.)
├── logger.py               # Rotating log handlers (app, errors, pipeline, audit)
├── services/
│   ├── news_fetcher.py     # Tavily + SerpAPI + NewsData + NewsAPI + NewsAPI.ai calls, date window helpers, gap report builder
│   ├── deduplicator.py     # URL + headline similarity dedup (no AI, pure Python)
│   ├── validator.py        # 8-point article validation checklist
│   ├── hyperlink_validator.py  # 4-layer URL reachability check
│   ├── ai_summarizer.py    # GPT-4o-mini: 2–3 sentence summary + 12-category tagging
│   └── excel_exporter.py   # 5-sheet openpyxl Excel report
├── templates/              # Jinja2 HTML: base, index, digest, gaps, audit, run_history
├── static/app.js           # Auto-polls /job-status and reloads page on completion
├── requirements.txt
├── .env                    # API keys — never committed
└── sample_upload.csv       # Example CSV format for bulk entity import
```

---

## Background Pipeline (run order)

```
fetch (Tavily + SerpAPI + NewsData + NewsAPI, 12 topics each)
  → deduplicate (URL match + headline similarity)
    → validate (8-point checklist)
      → hyperlink check (HTTP HEAD → GET → OpenAI web search → content rescue)
        → AI summarize (GPT-4o-mini)
          → save + audit (storage.py)
```

Triggered via `POST /fetch-all-news` (all entities) or `POST /fetch-news/{entity_id}` (single).  
Job state lives in the in-memory `job_status` dict in `main.py`. Frontend polls `/job-status` every few seconds.

---

## Key Design Rules

### Titles are never modified
`ai_summarizer.py` generates a fresh `summary` field. The original `title` field is always preserved exactly as fetched. Do not touch it.

### Deduplication logic (deduplicator.py)
- Same URL + ≥85% headline similarity → reject as duplicate
- Same URL + different headline → keep (different article, same URL)
- Different URL + same headline → keep the higher-authority source (Reuters/Bloomberg/FT > others)
- Different URL + different headline → keep both

### Validation checklist (validator.py — 8 points)
a) Missing/removed title — empty, null, [Removed], [Deleted]  
b) Future date — published after window end  
c) Outside window start — older than selected period  
d) Limited content preview — under 150 chars AND title under 60 chars → **soft tag only**, article kept (SerpAPI/NewsAPI return short snippets by design)  
e) Non-English — language field not `en`/`english`  
f) Blocked domain — social media and forums (Pinterest, Reddit, YouTube, Twitter, TikTok, Facebook, Instagram, Quora, Scribd, SlideShare)  
g) Entity relevance — entity name (or alias or auto short-name) not found capitalised in title or content → hard reject. Capitalised match applies to all sources to prevent common-word drift (e.g. "Apple" the company vs fruit) and verb false-positives ("officials affirm readiness"). Auto short-name strips trailing suffixes (Group, Ltd, PLC, Inc, Corp, etc.) so "Barclays Group" also matches "Barclays" in text. Explicit aliases can be set per entity.  
h) Metered paywall (Reuters, Bloomberg, FT, WSJ, etc.) → **soft tag only**, article kept with subscription warning. Article continues through hyperlink validation → AI summarise → categorise → save.

### Date windows
- Today is always excluded from the window end (window ends at yesterday)
- Custom range is also capped at yesterday
- Deduplication handles repeated articles across overlapping runs

### Storage (Phase 1)
All persistence is flat JSON files auto-created on first run:

| File | Contents |
|------|----------|
| `data.json` | Entities + fetched news per entity |
| `audit.json` | Every removed/rejected article with reason |
| `gap_report.json` | Gap report snapshots per run |
| `run_history.json` | Duration, counts, status per run |
| `error_log.json` | Application errors |

All JSON files are gitignored. Safe to delete to reset data.

---

## Environment Variables (.env)

| Variable | Required | Notes |
|----------|----------|-------|
| `OPENAI_API_KEY` | Yes | GPT-4o-mini summarization + OpenAI web search for hyperlink validation |
| `TAVILY_API_KEY` | Yes | Primary news source (sync TavilyClient, search_depth=basic) |
| `SERPAPI_API_KEY` | No | Google News via SerpAPI engine=google+tbm=nws (secondary source) |
| `NEWSDATA_API_KEY` | No | NewsData.io (secondary source) |
| `NEWSAPI_API_KEY` | No | NewsAPI.org — English, up to 30-day history on free tier |
| `NEWSAI_API_KEY` | No | NewsAPI.ai / EventRegistry — entity-focused POST API (newsapi.ai) |
| `TAVILY_MAX_RESULTS` | No | Articles per topic from Tavily (default 4) |
| `SERPAPI_MAX_RESULTS` | No | Articles per topic from SerpAPI (default 4) |
| `NEWSDATA_MAX_RESULTS` | No | Articles per topic from NewsData (default 4) |
| `NEWSAPI_MAX_RESULTS` | No | Articles per topic from NewsAPI (default 4) |
| `NEWSAI_MAX_RESULTS` | No | Articles per entity from NewsAPI.ai (default 4) |
| `MAX_ARTICLES_PER_ENTITY` | No | Hard cap before dedup/validation; `0` = no cap |

---

## Data Models (models.py)

- `EntityType` enum: `client | prospect | industry`
- `Entity`: id, name, entity_type, topics (optional list), website (optional), industry_type (optional, industries only), news_scope (optional, industries only), aliases (optional list — alternate names checked during relevance validation)
- `NewsItem`: title, url, source, published_date, fetched_date, period, summary, primary_category, secondary_category, entity_id, entity_type, url_status, original_url, paywall_note, topic_queried, is_primary_topic, fetch_source
- `TOPIC_CATEGORIES`: hardcoded list of 12 business topic categories used for fetching and AI categorisation

---

## AI Summarizer (services/ai_summarizer.py)

- Model: `gpt-4o-mini`
- Input: title + content (up to 1800 chars) + entity name
- Output: 2–3 sentence business-focused summary + `primary_category` (1 of 12) + optional `secondary_category`
- Fallback: on API failure, summary = title; category = "General — review required"
- Boundary disambiguation rules are defined in the system prompt (Digital Transformation vs. New Tech, M&A vs. Strategic Alliances, etc.)

---

## Logging (logger.py)

Rotating file handlers, 5 MB max per file, 3 backups:

| Logger | File | Level |
|--------|------|-------|
| `get_logger(name)` | `logs/app.log` | INFO+ |
| `get_logger(name)` | `logs/errors.log` | ERROR+ |
| `get_pipeline_logger()` | `logs/pipeline.log` | DEBUG |
| `get_audit_logger()` | `logs/audit.log` | structured audit entries |

---

## Phase 2 Roadmap (not yet started)

- PostgreSQL replacing JSON storage
- Scheduled auto-refresh (Monday 8 AM via APScheduler)
- Email digest delivery
- Admin panel for validation review
- Concurrent entity processing (`asyncio.gather`)
- Cloud deployment (Railway / AWS)

---

## Code Style

- Flat, direct style — `os.getenv` / `load_dotenv` directly in files, no config abstraction helpers
- No premature type annotations on simple glue/config code
- Comments only when the WHY is non-obvious
- No trailing summaries needed in responses — diffs are self-explanatory
