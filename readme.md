# News Digest Platform

An internal productivity tool that fully automates the end-to-end news intelligence pipeline — fetching, deduplicating, validating, summarising, and categorising business news for a defined list of clients, prospects, and industries. Built from scratch in Python with FastAPI; no dependency on third-party automation frameworks.

> Replaces the manual GCS validation process and AI team audit reports with a fully automated, auditable pipeline.

---

## What It Does

- Fetches news for every entity across **all 12 business topic categories** individually
- Supports **5 selectable API sources**: Tavily (default), SerpAPI (Google News), NewsData.io, NewsAPI.org, and NewsAPI.ai — mix and match per run
- Builds **context-aware search queries** — appends entity website domain or "company" qualifier to prevent name ambiguity in search results
- Runs **two-pass deduplication** (exact URL + headline similarity) — no AI, pure Python
- Validates every article through an **8-point checklist** — 7 hard rejects + 1 soft paywall tag
- Validates every hyperlink via **4-layer URL checking** (HTTP HEAD → GET → OpenAI web search → content rescue)
- Generates **AI-powered 2–3 sentence summaries and category tags** via OpenAI GPT-4o-mini
- Stores original article titles **unchanged** for reliable record matching
- Tracks **period-tagged digests** — every run is historically identifiable
- Produces a **gap report** — entities with no news and entities missing specific topics
- Exports a **5-sheet formatted Excel report** per run
- Exports a **full analytics Excel report** including all accepted articles and full audit trail
- Generates a full **audit log** of every removed, rejected, and accepted article
- Saves a **run history** with duration, article counts, AI usage, and status per run
- Logs all application events and errors to **rotating log files**
- All heavy processing runs **in the background** — no waiting on loading screens

---

## Topic Categories

Every entity is queried against all 12 categories regardless of which are assigned:

1. Accounts Receivable / Payable & Operational Efficiency
2. Company Finances & Results
3. Compliance Monitoring
4. Crisis / Bankruptcy & Insolvency
5. Customer Service & Experience Innovations
6. Digital Transformation & AI
7. Expansion, Collaborations & Strategic Alliances
8. Infrastructure Projects & Initiatives
9. Mergers, Acquisitions & Asset Transfers
10. New Projects / Initiatives
11. New Technologies & AI Adoption
12. Regulatory Developments & RFP / RFI Announcements

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Frontend | Jinja2 + Tailwind CSS (CDN) |
| News source 1 | Tavily API (primary, default) |
| News source 2 | SerpAPI — Google News tab (engine=google, tbm=nws) |
| News source 3 | NewsData.io |
| News source 4 | NewsAPI.org |
| News source 5 | NewsAPI.ai (EventRegistry) — entity-focused POST API |
| AI summarisation | OpenAI GPT-4o-mini |
| Deduplication | Python `difflib` (no AI) |
| Hyperlink check | `httpx` + OpenAI web search fallback (Layer 3) |
| Excel export | openpyxl |
| File parsing | Pandas + openpyxl |
| Logging | Python `logging` + `RotatingFileHandler` |
| Storage — Phase 1 | Local JSON files (atomic temp→rename writes) |
| Storage — Phase 2 | PostgreSQL (planned) |

---

## Project Structure

```
news_digest/
├── main.py                          # FastAPI app, all routes, background pipelines, pipeline helpers
├── models.py                        # Pydantic models — Entity, NewsItem, AuditEntry, TOPIC_CATEGORIES
├── storage.py                       # Read/write — entities, news, audit, gap, run history (atomic writes)
├── logger.py                        # Structured rotating file logger
│
├── services/
│   ├── news_fetcher.py              # Tavily + SerpAPI + NewsData + NewsAPI — per-topic querying
│   ├── deduplicator.py              # URL match + difflib headline similarity
│   ├── validator.py                 # 8-point article validation (7 hard rejects + 1 soft paywall tag)
│   ├── hyperlink_validator.py       # 4-layer URL reachability check
│   ├── ai_summarizer.py             # GPT-4o-mini — summary + 12-category tagging
│   └── excel_exporter.py            # Standard 5-sheet + full analytics .xlsx reports
│
├── templates/
│   ├── base.html                    # Base layout, navbar, footer, job status banner
│   ├── macros.html                  # Shared Jinja2 macros — empty_state, entity_type_badge
│   ├── index.html                   # Entity manager — add, upload, delete, fetch, period selector
│   ├── digest.html                  # News digest — 3 tabs, period selector, gap alert
│   ├── gaps.html                    # Gap report — no news + topic-level gaps
│   ├── audit.html                   # Audit log — all removed/rejected/accepted articles (last 500)
│   └── run_history.html             # Run history — duration, counts, AI usage, status per run
│
├── static/
│   └── app.js                       # Auto-polls job status, reloads on completion
│
├── logs/                            # Auto-created — gitignored
│   ├── app.log                      # All INFO+ events (rotating 5MB × 3)
│   ├── errors.log                   # ERROR+ only (rotating 5MB × 3)
│   ├── pipeline.log                 # Step-by-step pipeline progress per entity
│   └── audit.log                    # Structured audit entries (rotating 5MB × 3)
│
├── data.json                        # Auto-created — entities + news storage
├── audit.json                       # Auto-created — audit log per run
├── gap_report.json                  # Auto-created — gap report history
├── run_history.json                 # Auto-created — every run record
├── error_log.json                   # Auto-created — all application errors
│
├── requirements.txt
├── .env                             # Your API keys — gitignored
├── .env.example                     # Safe template to commit
├── .gitignore
└── sample_upload.csv
```

---

## Prerequisites

- Python 3.11 or higher
- API key for OpenAI (required)
- API key for at least one news source (Tavily recommended)

---

## Quickstart

### 1. Clone or create the project folder

```bash
mkdir news_digest && cd news_digest
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add your API keys

```bash
cp .env.example .env
```

Edit `.env`:

```
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...

# Optional additional news sources
SERPAPI_API_KEY=your_serpapi_key_here
NEWSDATA_API_KEY=your_newsdata_key_here
NEWSAPI_API_KEY=your_newsapi_key_here

# Articles fetched per topic per source (default: 5)
# Set to 2–3 for testing to minimise API usage
TAVILY_MAX_RESULTS=5
SERPAPI_MAX_RESULTS=5
NEWSDATA_MAX_RESULTS=5
NEWSAPI_MAX_RESULTS=5

# Hard cap on raw articles per entity before dedup/validation (0 = no cap)
MAX_ARTICLES_PER_ENTITY=0
```

### 5. Run the app

```bash
uvicorn main:app --reload


```

Open **http://localhost:8000**

---

## API Keys

| API | Free tier | Get key |
|---|---|---|
| OpenAI | Pay-per-use (~$0.23/month at this scale) | https://platform.openai.com/api-keys |
| Tavily | 1,000 credits/month free (pilot only) | https://app.tavily.com |
| SerpAPI | 100 searches/month free | https://serpapi.com |
| NewsData.io | 200 requests/day free | https://newsdata.io |
| NewsAPI.org | 100 requests/day free, 30-day history | https://newsapi.org |
| NewsAPI.ai | 1,000 articles/month free | https://newsapi.ai |

> For production with 120 entities, switch Tavily to Pay-as-you-go ($0.008/credit). The other sources can supplement Tavily or replace it for higher volume.

---

## How to Use

### Adding entities

1. Go to **Entities** page (`/`)
2. Add manually (name + type + optional website + optional topics) or upload a CSV/Excel file

### Entity fields

| Field | Required | Notes |
|---|---|---|
| Name | Yes | Company or industry name |
| Type | Yes | `client`, `prospect`, or `industry` |
| Website | No | Company website — appended to search queries to disambiguate company names |
| Topics | No | Which of the 12 topic categories to prioritise (all 12 are always queried) |
| Industry type | No | Sector/sub-type — for industry entities only |
| News scope | No | What kinds of news to focus on — for industry entities only |

### CSV / Excel upload format

| Column | Required | Notes |
|---|---|---|
| `name` | Yes | Entity name |
| `type` | Yes | `client`, `prospect`, or `industry` |
| `website` | No | Company website URL |
| `industry_type` | No | Sector label (industries only) |
| `news_scope` | No | Focus description (industries only) |
| `topics` | No | Topic categories — use `;` or `\|` as separators (category names contain commas) |

Download the sample: `sample_upload.csv`

### Selecting a period

Before fetching, choose the date range using the **Period** selector on the Entities page:

| Option | Covers |
|---|---|
| Last 7 days | Yesterday minus 6 days → yesterday |
| Last 14 days | Yesterday minus 13 days → yesterday |
| Last 30 days (default) | Yesterday minus 29 days → yesterday |
| Custom range | Pick exact From / To dates (capped at yesterday) |

> The run date (today) is always excluded from the window.

### Selecting API sources

On the Entities page, choose which news APIs to query per run. Tavily is selected by default. You can combine multiple sources — results are merged and deduplicated.

### Fetching news

- Select a period and source(s), then click **Refresh All News** to run the full pipeline for all entities
- Use the **Type** filter to fetch only clients, prospects, or industries
- Or click **Fetch** on any individual entity (defaults to last 30 days, Tavily)
- Processing runs in the background — amber status banner shows while running
- Click **Stop** in the banner to cancel after the current entity finishes
- Page reloads automatically when the run completes

### Viewing the digest

- Go to **Digest** (`/digest`)
- Switch between **Clients**, **Prospects**, **Industries** tabs
- Use the **Period** selector to view previous runs
- Each article shows: original title, AI summary, primary + secondary category badge, source link, and any paywall or broken-link warnings
- Source names link directly to the publication's homepage

### Gap report

- Go to **Gap Report** (`/gaps`)
- Lists entities with zero news for the period
- Lists entities missing news for specific topic categories

### Audit log

- Go to **Audit Log** (`/audit`)
- Shows every article removed by deduplication or validation with the exact reason
- Also shows every accepted article for full traceability
- Displays the most recent 500 entries — the total count is shown in the header when the log exceeds this limit

### Run history

- Go to **Run History** (`/run-history`)
- Shows every pipeline run (newest first): start/end time, period covered, scope (all or single entity), article count, duplicates removed, rejections, AI calls, AI retries, tokens used, duration, and status

### Exporting to Excel

Two export formats are available from the digest navbar:

**Standard Report** — `⬇ Export Excel`
- Downloads: `news_digest_YYYYMMDD_HHMM.xlsx`
- 5 sheets: Summary, Client Report, Prospect Report, Industry News, Gap Report
- Optionally filtered to a specific period via the digest period selector
- Copyright footer on every sheet

**Full Analytics Report** — `⬇ Export Full Analytics`
- Downloads: `news_full_analytics_YYYYMMDD_HHMM.xlsx`
- All accepted articles across all entities + complete audit trail

---

## Validation Checklist (validator.py)

Articles pass through an 8-point checklist. Points 1–7 hard-reject the article; point 8 is a soft tag — the article is kept but annotated.

| # | Check | Action |
|---|---|---|
| 1 | Missing / removed title — empty, null, `[Removed]`, `[Deleted]`, `404` | Reject |
| 2 | Published date before window start — article is older than the selected period | Reject |
| 3 | Published date too far in future — more than 1 day after window end | Reject |
| 4 | Paywalled / empty content — content under 150 chars AND title under 60 chars | Reject |
| 5 | Non-English — only fires when `language` field is explicitly set to a non-English value | Reject |
| 6 | Blocked domain — social media and forums (Pinterest, Reddit, YouTube, Twitter/X, TikTok, Facebook, Instagram, Quora, Scribd, SlideShare) | Reject |
| 7 | Entity relevance — entity name not found in title, content, or URL domain (catches search drift) | Reject |
| 8 | Metered paywall domain — Reuters, Bloomberg, FT, WSJ, Economist, Times, Telegraph, Business Insider, HBR, Barron's, MarketWatch, The Atlantic | Tag with note, keep |

> Point 4 is intentionally lenient: Tavily returns snippets, not full articles. A long title (≥60 chars) is sufficient evidence that a real article exists.

> Point 8 first attempts to find an alternative link via Google News / Yahoo Finance. If no alternative is found, the article is kept with a paywall note shown in the digest.

---

## Deduplication Logic (deduplicator.py)

| Condition | Action |
|---|---|
| Same URL + ≥85% headline similarity | Reject as duplicate |
| Same URL + different headline | Keep (different article at same URL) |
| Different URL + same headline | Keep the higher-authority source (Reuters/Bloomberg/FT > others) |
| Different URL + different headline | Keep both |

---

## Hyperlink Validation (hyperlink_validator.py)

Each article URL passes through 4 layers in order:

| Layer | Method | Resolves |
|---|---|---|
| 1 | HTTP HEAD with browser User-Agent | ~75% — fast, no body download |
| 2 | HTTP GET with browser User-Agent | CDN bot-blocks, JS redirects, paywall landing pages |
| 3 | OpenAI GPT-4o-mini web_search_preview | ~5% still-unknown URLs — model searches and confirms content is accessible |
| 4 | Content rescue | If any source fetched ≥50 chars of content at search time, URL was reachable |

Articles with invalid URLs are kept (not dropped) and shown with a "⚠ Link broken" badge in the digest.

---

## Search Query Construction (news_fetcher.py)

Each entity + topic pair generates a natural-language query used by all four APIs:

| Entity config | Query format |
|---|---|
| Website set | `Latest news on Entity Name (domain.com) related to Topic Category` |
| Client/prospect, no website | `Latest news on Entity Name company related to Topic Category` |
| Industry entity, no website | `Latest news on Entity Name related to Topic Category` |

Natural-language format avoids quoted strings and bare `&` characters that cause parse errors in NewsData.io and NewsAPI.org. The domain or "company" qualifier disambiguates companies that share a keyword with an unrelated entity name.

Social media and forum domains are excluded at the Tavily API level (via `exclude_domains`) before results reach the validator.

---

## Background Pipeline — Step by Step

```
Trigger: POST /fetch-all-news (all entities) or POST /fetch-news/{entity_id} (single)
       ↓
Resolve date window (preset or custom, always capped at yesterday)
       ↓
Load entities (optionally filtered by type: client / prospect / industry)
       ↓
For each entity:
  ├── 1. FETCH    — build search queries, query all 12 topics across selected APIs
  ├── 2. DEDUP    — URL exact match + ≥85% headline similarity
  ├── 3. VALIDATE — 8-point checklist (7 hard rejects + 1 paywall soft tag)
  ├── 4. HYPERLINK CHECK — HTTP HEAD → GET → OpenAI web search → content rescue
  ├── 5. SUMMARISE — GPT-4o-mini: 2–3 sentence summary + primary/secondary category
  ├── 6. SAVE — merge by period into data.json (atomic write, period-safe)
  └── 7. AUDIT ENTRIES — log duplicates, rejections, and accepted articles
       ↓
Save gap report (no news + topic gaps per entity)
Save run history (duration, article counts, AI usage, status)
       ↓
Notify user — digest ready (page reloads automatically)
```

### Pipeline helper functions (main.py)

| Function | Purpose |
|---|---|
| `_filtered_entities(filter)` | Returns entities filtered by type; `"all"` returns all |
| `_finish_job(completed_at, message)` | Clears in-memory job state after completion or cancel |
| `_group_news_by_entity(entities, all_news, etype, period, require_news)` | Groups news by entity for digest and export routes |
| `_make_news_item(art, result, entity, window)` | Builds a `NewsItem` from a fetched article and AI result |
| `_build_rejection_audit_entries(entity, dup_log, val_log, linked)` | Builds `AuditEntry` objects for all rejections |
| `_build_accepted_audit_entries(entity, news_items, today)` | Builds `AuditEntry` objects for all accepted articles |

---

## Log Files

All logs are written to the `logs/` folder automatically:

| File | Contents |
|---|---|
| `logs/app.log` | All INFO+ events — fetches, saves, route hits |
| `logs/errors.log` | ERROR+ only — API failures, parse errors |
| `logs/pipeline.log` | Step-by-step pipeline progress per entity |
| `logs/audit.log` | Structured one-line entry per audited article |

Logs rotate at 5MB with 3 backups kept. The `logs/` folder is gitignored.

---

## Data Files

All JSON data files are auto-created on first run. All writes use an atomic temp→rename pattern — a crash mid-write cannot corrupt the file.

| File | Contents |
|---|---|
| `data.json` | All entities and fetched news articles |
| `audit.json` | Every duplicate removed, article rejected, and article accepted |
| `gap_report.json` | Gap report history across all runs |
| `run_history.json` | Every pipeline run — start/end time, duration, article counts, AI usage, status |
| `error_log.json` | All application errors with timestamp and context |

If a JSON file becomes corrupt on disk, it is automatically renamed to `.corrupt.json` and the app continues with an empty state — no data is silently overwritten.

---

## Run History Fields

Each entry in `run_history.json` records:

| Field | Description |
|---|---|
| `started_at` / `completed_at` | ISO timestamps for start and end |
| `duration_seconds` | Wall-clock seconds for the run |
| `status` | `completed`, `cancelled`, or `failed` |
| `window_label` | Period covered, e.g. `"Jun 2026"` |
| `entity_name` | Set only for single-entity runs |
| `total_entities` | Number of entities processed |
| `total_articles` | Articles saved after all checks |
| `duplicates_removed` | Articles dropped by deduplication |
| `articles_rejected` | Articles dropped by validation |
| `ai_calls` | Total OpenAI API calls (including retries) |
| `ai_retries` | Articles that required a second GPT attempt |
| `prompt_tokens` / `completion_tokens` | Token usage for cost tracking |

---

## Digest Run Schedule

| Run | Date example | Covers |
|---|---|---|
| Biweekly Run 1 | Tuesday 20 May | 20 Apr → 19 May |
| Biweekly Run 2 | Tuesday 3 Jun | 4 May → 2 Jun |

- Run date is always **excluded** from the window
- Runs overlap by ~16 days — duplicates from the overlap are automatically removed

---

## Cost Estimate (120 entities, biweekly, 500+ articles)

| Scenario | Monthly |
|---|---|
| Pilot — Tavily free tier + OpenAI only | ~$0.23 |
| Production — Tavily PAYG only | ~$4–6 |
| Production — Tavily PAYG + SerpAPI/NewsData | varies by plan |

---

## Resetting Data

```bash
# Windows
del data.json audit.json gap_report.json run_history.json error_log.json

# macOS / Linux
rm data.json audit.json gap_report.json run_history.json error_log.json
```

Files are recreated automatically on next run.

---

## .gitignore

```
.env
data.json
audit.json
gap_report.json
run_history.json
error_log.json
logs/
venv/
__pycache__/
*.pyc
*.pyo
.DS_Store
*.xlsx
```

---

## Phase 2 Roadmap

- [ ] PostgreSQL — multi-user, production-grade storage
- [ ] Scheduled auto-refresh (Monday 8 AM via APScheduler)
- [ ] Email digest delivery
- [ ] Admin panel — validation log review, category spot-check UI
- [ ] Concurrent entity processing (asyncio.gather)
- [ ] Cloud deployment (Railway / AWS)

---

*© 2026 News Digest Platform · Internal use only · All rights reserved.*
