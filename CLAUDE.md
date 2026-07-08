# News Digest Platform — CLAUDE.md

Internal news intelligence tool that automates fetching, deduplicating, validating, summarising, and categorising business news for a defined list of clients, prospects, and industries. Built in Python with FastAPI; no third-party automation frameworks.

---

## How to Run

```bash
cd d:\Code\news_digest
.venv\Scripts\activate
uvicorn main:app --reload
# App opens at http://localhost:8000
```

---

## Project Structure

```
news_digest/
├── main.py                 # FastAPI app, all routes, background pipeline orchestration
├── models.py               # Pydantic models: Entity, NewsItem, AuditEntry, TOPIC_CATEGORIES
├── storage.py              # JSON file persistence (data.json, audit.json, industry files, etc.)
├── logger.py               # Rotating log handlers (app, errors, pipeline, audit)
├── services/
│   ├── news_fetcher.py     # Tavily + SerpAPI + NewsData + NewsAPI.ai calls, date window helpers, gap report builder
│   ├── deduplicator.py     # URL + headline similarity dedup (no AI, pure Python)
│   ├── validator.py        # 8-point article validation checklist (client/prospect only)
│   ├── hyperlink_validator.py  # 4-layer URL reachability check (client/prospect pipeline)
│   ├── ai_summarizer.py    # GPT-4o-mini: relevance gate + 2–3 sentence summary + 12-category tagging (client/prospect)
│   ├── industry_validator.py   # Industry-only AI validation: HTTP check + OpenAI web search + chat categorisation
│   └── excel_exporter.py   # 5-sheet openpyxl Excel report
├── templates/              # Jinja2 HTML: base, index, digest, industry, gaps, audit, run_history
├── static/app.js           # Auto-polls /job-status and reloads page on completion
├── requirements.txt
├── .env                    # API keys — never committed
└── sample_upload.csv       # Example CSV format for bulk entity import
```

---

## Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Entities management page |
| GET | `/digest` | News digest — Clients and Prospects only (Industries → `/industry`) |
| GET | `/industry` | Dedicated industry news page with entity/period filters |
| GET | `/gaps` | Gap report |
| GET | `/audit` | Audit log |
| GET | `/run-history` | Run history |
| POST | `/fetch-all-news` | Trigger full pipeline (all entities, or filtered by `entity_type_filter`) |
| POST | `/fetch-news/{entity_id}` | Trigger single-entity pipeline |
| POST | `/ai-validate-industry` | Re-validate stored industry articles using AI (period + sector_id filters optional) |
| POST | `/cancel-fetch` | Cancel running pipeline |

---

## Background Pipeline (run order)

### Client / Prospect entities
```
fetch (Tavily + SerpAPI + NewsData + NewsAPI.ai, 12 topics each)
  → save raw (raw_news/<run_id>.json — permanent, before any processing)
    → deduplicate (URL match + headline similarity)
      → validate (8-point checklist)
        → hyperlink check (HTTP HEAD → GET → OpenAI web search → content rescue)
          → AI summarize + relevance gate (GPT-4o-mini, ai_summarizer.py)
            → save + audit (storage.py → data.json)
```

### Industry entities
```
fetch (Tavily + SerpAPI + NewsData + NewsAPI.ai — industry max_results, 12 topics each)
  → save raw (raw_news/<run_id>.json)
    → deduplicate
      → validate (8-point checklist, skipping point g entity-name check)
        → hyperlink check
          → save pre-validation snapshot (industry_all_records.json — keeps content field)
            → AI summarize (ai_summarizer.py — basic relevance gate)
              → save + audit (data.json)
            → AUTO AI VALIDATION (runs immediately, no manual step — see below)
```

Immediately after the hyperlink check (both the full pipeline and the single-entity pipeline), the industry articles just fetched are run through the full industry validator automatically — the AI Validation step is no longer a separate manual action:
```
industry_all_records.json (just-fetched records for this run)
  → Step 1: duplicate URL check
    → Step 2: date validation (OpenAI web search fallback for missing dates)
      → Step 3: URL validation (HTTP first; OpenAI web search fallback)
        → Step 4: AI categorisation (sector assignment + topic category + summary)
          → save industry_accepted.json / industry_rejected.json
            → mirror validation_status badges back to data.json
```

Triggered via `POST /fetch-all-news` (all entities, optional `entity_type_filter=industry`) or `POST /fetch-news/{entity_id}` (single).  
Job state lives in the in-memory `job_status` dict in `main.py`. Frontend polls `/job-status` every few seconds. The final job-status message includes the industry validation breakdown (validated / rejected / review) alongside the article count.

Both entry points share the same batch runner — `main._run_industry_validation_batch()` — which merges results back into `industry_all_records.json` **by URL** (so it never drops unrelated periods/sectors already stored), then refreshes `industry_accepted.json` / `industry_rejected.json` and mirrors `validation_status` into `data.json`.

### Re-validate (POST /ai-validate-industry)
The **Re-validate** button on `/industry` (relabelled from "AI Validation") calls the same `_run_industry_validation_batch()` runner manually, scoped by optional `period` and `sector_id` form filters. It exists only for re-running validation on demand — e.g. a transient URL/date lookup failure left articles stuck in "Review", or a sector's `news_scope` changed and old articles should be re-categorised. It is not required for normal operation since the pipeline now validates automatically.

---

## Key Design Rules

### Titles are never modified
`ai_summarizer.py` generates a fresh `summary` field. The original `title` field is always preserved exactly as fetched. `industry_validator.py` populates `clean_title` as a separate field — `title` remains untouched.

### Deduplication logic (deduplicator.py)
- Same URL + ≥85% headline similarity → reject as duplicate
- Same URL + different headline → keep (different article, same URL)
- Different URL + same headline → keep the higher-authority source (Reuters/Bloomberg/FT > others)
- Different URL + different headline → keep both

### Validation checklist (validator.py — 8 points, client/prospect only)
a) Missing/removed title — empty, null, [Removed], [Deleted]  
b) Future date — published after window end  
c) Outside window start — older than selected period  
d) Limited content preview — under 150 chars AND title under 60 chars → **soft tag only**, article kept  
e) Non-English — language field not `en`/`english`  
f) Blocked domain — social media and forums (Pinterest, Reddit, YouTube, Twitter, TikTok, Facebook, Instagram, Quora, Scribd, SlideShare)  
g) Entity relevance — for **clients and prospects only**: entity name (or alias or auto short-name) not found capitalised in title or content → hard reject. Industry entities skip this check entirely.  
h) Metered paywall (Reuters, Bloomberg, FT, WSJ, etc.) → **soft tag only**, article kept with subscription warning.

### Industry AI Validation (services/industry_validator.py)
Industry articles bypass validator point g and the ai_summarizer relevance gate. Immediately after the pipeline saves them to `industry_all_records.json`, they're run automatically through a dedicated 4-step pipeline (also re-runnable manually via the **Re-validate** button on `/industry`):

**Step 1 — Duplicate URL check:** URL already seen in this validation run → `Non Validated News` immediately, skip all AI calls.

**Step 2 — Date validation:** Article date checked against run window. Missing date → OpenAI web search (`client.responses.create` with `web_search_preview`) to extract date from the live page. Outside window → `Non Validated News`. Date still not found → `Review`.

**Step 3 — URL validation:** HTTP HEAD → GET first. Only calls OpenAI web search when HTTP returns invalid/uncertain. Archive.org / cached copy → `Non Validated News`. Confirmed unreachable → `Non Validated News`.

**Step 4 — AI categorisation** (`client.chat.completions.create`, gpt-4o-mini, always runs for non-duplicates): assigns article to one of the 9 configured industry sectors (by name + `news_scope` if set), picks primary/secondary topic category from 12, writes a 2–3 sentence summary, cleans title suffix. Sector = "None" → `Non Validated News`. Valid sector + specific category → `Validated News`. Valid sector + "General — review required" category → `Review`.

**Key rule:** `validation_status` is reset to `None` at the start of each re-validation run so legacy values ("pass") never persist.

**Sector matching relies on entity `news_scope`:** If `news_scope` is blank or identical across entities, the AI can only distinguish sectors by name. Add a specific plain-English `news_scope` per entity (via the Entities management page) to improve accuracy on ambiguous sector names.

### Source name normalisation (news_fetcher.py)
- `_url_to_publisher(url)` — converts Tavily's raw URL source to a human-readable publisher name
- `_slug_to_publisher(slug)` — converts NewsData's lowercase `source_id` fallback to a readable name

### Date windows
- Today is always excluded from the window end (window ends at yesterday)
- Custom range is also capped at yesterday
- Deduplication handles repeated articles across overlapping runs

### Storage (Phase 1)
All persistence is flat JSON files auto-created on first run:

| File / Dir | Contents |
|------------|----------|
| `data.json` | Entities + fetched news per entity (all types) |
| `audit.json` | Every article action with entity_type, fetch_source, window_from/to |
| `gap_report.json` | Gap report snapshots per run |
| `run_history.json` | Duration, counts, status, window_from/to per run |
| `raw_news/<run_id>.json` | **Permanent** raw API fetch — all articles before dedup/validation, never rotated |
| `pipeline_snapshots/<run_id>/` | Per-entity stage snapshots (rotated, keep last 5 runs) |
| `industry_all_records.json` | All industry articles post-hyperlink-check, with `content` field preserved for AI re-use. Populated by pipeline; read by AI Validation. |
| `industry_accepted.json` | Articles with `validation_status = "Validated News"` from last AI Validation run |
| `industry_rejected.json` | Articles with `validation_status = "Non Validated News"` from last AI Validation run |
| `error_log.json` | Application errors |

All JSON files are gitignored. Safe to delete to reset data.

---

## Environment Variables (.env)

| Variable | Required | Notes |
|----------|----------|-------|
| `OPENAI_API_KEY` | Yes | GPT-4o-mini summarization + OpenAI web search (both pipelines) |
| `TAVILY_API_KEY` | Yes | Primary news source (sync TavilyClient, search_depth=basic) |
| `SERPAPI_API_KEY` | No | Google News via SerpAPI (secondary source) |
| `NEWSDATA_API_KEY` | No | NewsData.io (secondary source) |
| `NEWSAI_API_KEY` | No | NewsAPI.ai / EventRegistry — entity-focused POST API |
| `TAVILY_MAX_RESULTS` | No | Articles per topic from Tavily for client/prospect (default 4) |
| `SERPAPI_MAX_RESULTS` | No | Articles per topic from SerpAPI for client/prospect (default 4) |
| `NEWSDATA_MAX_RESULTS` | No | Articles per topic from NewsData for client/prospect (default 4) |
| `NEWSAI_MAX_RESULTS` | No | Articles per entity from NewsAPI.ai for client/prospect (default 4) |
| `TAVILY_INDUSTRY_MAX_RESULTS` | No | Tavily articles per industry entity (default 20) |
| `SERPAPI_INDUSTRY_MAX_RESULTS` | No | SerpAPI articles per industry entity (default 20) |
| `NEWSDATA_INDUSTRY_MAX_RESULTS` | No | NewsData articles per industry entity (default 20) |
| `NEWSAI_INDUSTRY_MAX_RESULTS` | No | NewsAPI.ai articles per industry entity (default 20) |
| `MAX_ARTICLES_PER_ENTITY` | No | Hard cap before dedup/validation; `0` = no cap |

---

## Data Models (models.py)

- `EntityType` enum: `client | prospect | industry`
- `Entity`: id, name, entity_type, topics (optional list), website (optional), industry_type (optional), news_scope (optional — plain-English description of what the sector covers; used as the primary guide for AI sector matching in industry_validator.py), aliases (optional list)
- `NewsItem`: title, url, source, published_date, fetched_date, period, summary, primary_category, secondary_category, entity_id, entity_type, url_status, original_url, paywall_note, topic_queried, is_primary_topic, fetch_source, validation_status (`None` | `"Validated News"` | `"Non Validated News"` | `"Review"`), validation_reason, industry_type, content
- `AuditEntry`: run_date, entity_id, entity_name, entity_type, article_title, action, reason, source_url, fetch_source, window_from, window_to
- `TOPIC_CATEGORIES`: hardcoded list of 12 business topic categories used for fetching and AI categorisation

---

## AI Summarizer (services/ai_summarizer.py) — Client/Prospect

### Signature
```python
async def summarize_article(
    title:       str,
    content:     str,
    entity_name: str,
    categories:  list | None = None,
    entity_type: str = "",
    news_scope:  str = "",
) -> dict
```

### Behaviour
- **Client/prospect entities**: uses `SYSTEM_PROMPT`; `is_relevant` always True.
- One automatic retry at lower temperature if first response is malformed.
- Fallback on both failures: summary = title; category = "General — review required"; `is_relevant` = True.

---

## Industry Validator (services/industry_validator.py) — Industry only

### Two OpenAI call types

**Call 1 — Web search** (`client.responses.create`, `web_search_preview` tool):
```python
async def ai_check_url_and_date(url: str, title: str) -> dict
# Returns: reachable, is_archive, published_date, modified_date, canonical_url, source_name, page_title, note
```
Only triggered when HTTP check returns invalid/uncertain OR article has no date.

**Call 2 — Chat completions** (`client.chat.completions.create`, gpt-4o-mini, temp=0.2, max_tokens=450):
```python
async def ai_categorize_industry_article(title: str, content: str, industry_sectors: list) -> dict
# Returns: clean_title, industry_sector, industry_type, primary_category, secondary_categories, summary, is_relevant, confidence, note
```
Always runs for non-duplicate articles. Sector list built dynamically from configured industry entities.

**Orchestrator:**
```python
async def validate_industry_article_with_ai(article, industry_sectors, window, seen_urls) -> dict
```

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
