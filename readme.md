# 📰 News Digest Platform

An internal productivity tool that fully automates the end-to-end news intelligence pipeline — fetching, deduplicating, validating, summarising, and categorising the last 30 days of business news for a defined list of clients, prospects, and industries. Built from scratch in Python with no dependency on third-party automation tools.

> Replaces the manual GCS validation process and AI team audit reports with a fully automated, auditable pipeline.

---

## What It Does

- Fetches news for every entity across **all 12 business topic categories** individually
- Runs **two-pass deduplication** (exact URL + headline similarity) — no AI, pure Python. Also, verify the content of the url is headline is different on the same url. then it can't be rejected.
- Validates every article through an **8-point checklist** including paywall detection with automatic alternative link lookup. 8-point validation checklist (in validator.py): 
a) Missing/removed title — empty, null or [Removed] 
b) Future date — published after window end date
c) Outside window start — older than selected period
d) Paywalled/empty content — under 150 chars AND title under 60 chars
e) Non-English — language field not en/english
f) Blocked domain — social media, forums (Pinterest, Reddit, YouTube etc.)
g) Domain flooding — more than 3 articles from same domain per entity (removed this from code - not required)
h) Metered paywall — Reuters, Bloomberg, FT etc. → tries Google News/Yahoo alternative, if none found - add tag or comment no alternative found.
- Validates every hyperlink via **4-layer URL checking** (HTTP → redirect → Tavily Extract)
- Generates **AI-powered summaries and category tags** via OpenAI GPT-4o-mini
- Stores original article titles **unchanged** for reliable record matching
- Tracks **period-tagged digests** — every run is historically identifiable
- Produces a **gap report** — entities with no news and entities missing specific topics
- Exports a **5-sheet formatted Excel report** per run
- Generates a full **audit log** of every removed or rejected article
- Saves a **run history** with duration, article counts, and status per run
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
| News source 1 | Tavily API (primary) |
| News source 2 | GNews API (secondary) |
| AI summarisation | OpenAI GPT-4o-mini |
| Deduplication | Python `difflib` (no AI) |
| Hyperlink check | `httpx` + Tavily Extract fallback |
| Excel export | openpyxl |
| File parsing | Pandas + openpyxl |
| Logging | Python `logging` + `RotatingFileHandler` |
| Storage — Phase 1 | Local JSON files or create csv or excel for storing records |
| Storage — Phase 2 | PostgreSQL (planned) |

---

## Project Structure

```
news_digest/
├── main.py                          # FastAPI app, all routes, background pipelines
├── models.py                        # Pydantic models — Entity, NewsItem, AuditEntry
├── storage.py                       # Read/write — entities, news, audit, gap, run history
├── logger.py                        # Structured rotating file logger
│
├── services/
│   ├── news_fetcher.py              # Tavily + GNews — per-topic querying, date window
│   ├── deduplicator.py              # URL match + difflib headline similarity
│   ├── validator.py                 # 8-point article validation + paywall resolver
│   ├── hyperlink_validator.py       # 4-layer URL reachability check
│   ├── ai_summarizer.py             # GPT-4o-mini — summary + 12-category tagging
│   └── excel_exporter.py            # 5-sheet formatted .xlsx report generator
│
├── templates/
│   ├── base.html                    # Base layout, navbar, footer, job status banner
│   ├── index.html                   # Entity manager — add, upload, delete, fetch, period selector
│   ├── digest.html                  # News digest — 3 tabs, period label, gap alert
│   ├── gaps.html                    # Gap report — no news + topic-level gaps
│   └── audit.html                   # Audit log — all removed/rejected articles
│
├── static/
│   └── app.js                       # Auto-polls job status, reloads on completion
│
├── logs/                            # Auto-created — gitignored
│   ├── app.log                      # All INFO+ events (rotating 5MB × 3)
│   ├── errors.log                   # ERROR+ only (rotating 5MB × 3)
│   └── pipeline.log                 # Step-by-step pipeline progress
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
- API keys for: OpenAI, Tavily, GNews

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
GNEWS_API_KEY=your_gnews_key_here

# Controls articles fetched per topic per source (default: 2)
# Set to 1 for testing to minimise API usage
TAVILY_MAX_RESULTS=2
GNEWS_MAX_RESULTS=2
```

### 5. Run the app

```bash
cd news_digest
uvicorn main:app --reload
```

Open **http://localhost:8000**

---

## API Keys

| API | Free tier | Get key |
|---|---|---|
| OpenAI | Pay-per-use (~$0.23/month at this scale) | https://platform.openai.com/api-keys |
| Tavily | 1,000 credits/month free (pilot only) | https://app.tavily.com |
| GNews | 100 requests/day free (pilot only) | https://gnews.io |

> For production with 120 entities, switch Tavily to Pay-as-you-go ($0.008/credit). GNews Basic (~$49–84/month) recommended for production runs.

---

## How to Use

### Adding entities
1. Go to **Entities** page (`/`)
2. Add manually (name + type + topics) or upload a CSV/Excel file

### CSV upload format

| Column | Required | Values |
|---|---|---|
| `name` | Yes | Entity name |
| `type` | Yes | `client`, `prospect`, or `industry` |
| `topics` | No | Comma-separated category names |

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

### Fetching news
- Select a period, then click **Refresh All News** to run the full pipeline for all entities
- Or click **⟳ Fetch** on any individual entity (defaults to last 30 days)
- Processing runs in the background — amber status banner shows while running
- Click **✕ Stop** in the banner to cancel after the current entity finishes
- Page reloads automatically when the run completes

### Viewing the digest
- Go to **Digest** (`/digest`)
- Switch between **Clients**, **Prospects**, **Industries** tabs
- Period label shows the exact date range covered
- Each article shows: original title, summary, primary + secondary category, source, fetch source, topic that found it, and any paywall or broken link warnings

### Gap report
- Go to **Gap Report** (`/gaps`)
- Lists entities with zero news for the period
- Lists entities missing news for specific topic categories

### Audit log
- Go to **Audit Log** (`/audit`)
- Shows every article removed by deduplication or validation with the exact reason

### Exporting to Excel
- Click **⬇ Export Excel** in the navbar
- Downloads: `news_digest_YYYYMMDD_HHMM.xlsx`
- 5 sheets: Summary, Client Report, Prospect Report, Industry News, Gap Report
- Copyright footer on every sheet

---

## Log Files

All logs are written to the `logs/` folder automatically:

| File | Contents |
|---|---|
| `logs/app.log` | All INFO+ events — fetches, saves, route hits |
| `logs/errors.log` | ERROR+ only — API failures, parse errors |
| `logs/pipeline.log` | Step-by-step pipeline progress per entity |

Logs rotate at 5MB with 3 backups kept. The `logs/` folder is gitignored.

---

## Data Files

All JSON data files are auto-created on first run:

| File | Contents |
|---|---|
| `data.json` | All entities and fetched news articles |
| `audit.json` | Every duplicate removed and article rejected |
| `gap_report.json` | Gap report history across all runs |
| `run_history.json` | Every pipeline run — start time, duration, article counts, status |
| `error_log.json` | All application errors with timestamp and context |

---

## Digest Run Schedule

| Run | Date example | Covers |
|---|---|---|
| Biweekly Run 1 | Tuesday 20 May | 20 Apr → 19 May |
| Biweekly Run 2 | Tuesday 3 Jun | 4 May → 2 Jun |

- Run date is always **excluded** from the window
- Runs overlap by ~16 days — duplicates from the overlap are automatically removed

---

## Background Pipeline — Step by Step

```
Trigger (manual click or scheduled run)
       ↓
Resolve date window (preset or custom)
       ↓
Load all entities
       ↓
For each entity:
  ├── Query all 12 topic categories (Tavily + GNews)
  ├── Merge results, sort newest → oldest
  ├── Deduplicate (URL exact match → headline similarity)
  ├── Validate (8-point checklist + paywall alt link lookup)
  ├── Validate hyperlinks (HTTP → redirect → Tavily Extract)
  ├── Summarise + categorise (GPT-4o-mini)
  ├── Save with period tag + fetch metadata
  └── Log all steps to logs/pipeline.log
       ↓
Save audit log (duplicates + rejections)
Save gap report (no news + topic gaps)
Save run history (duration, counts, status)
       ↓
Notify user — digest ready
```

---

## Cost Estimate (120 entities, biweekly, 500+ articles)

| Scenario | Monthly |
|---|---|
| Pilot — free tiers only | ~$0.23 |
| Production — Tavily PAYG only | ~$4–6 |
| Production — Tavily + GNews Basic | ~$53–88 |

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