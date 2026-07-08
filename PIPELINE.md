# How the Pipeline Works

This document explains the current end-to-end behavior of the News Digest pipeline — what happens from the moment a fetch is triggered to the moment an article shows up (or doesn't) on `/digest` or `/industry`. It reflects the pipeline **after** the crash-safety, validation-accuracy, and audit-consistency fixes made in this session. For a quick reference (routes, file layout, env vars), see `CLAUDE.md`. This file is the narrative walkthrough.

---

## 1. The four news sources

| Source | Query style | Notes |
|---|---|---|
| **Tavily** | One call per entity (no topic loop) — quoted exact-phrase query for companies (`"Airbnb"`), unquoted for industries | Own dedicated query builders: `_build_tavily_company_query` / `_build_tavily_industry_query` |
| **SerpAPI** | One call per topic (12 calls/entity) — natural-language query, no quoting | `_build_serpapi_query()` |
| **NewsData.io** | One call per topic (12 calls/entity) — natural-language query | `_build_newsdata_query()` |
| **NewsAPI.ai** (EventRegistry) | One call per entity (not per topic — topic keywords AND-kill results on this API) | `_build_newsai_keywords()` returns a title-only or title+body keyword string depending on entity type |

Each source has its own query builder function (`services/news_fetcher.py`) so any one of them can be tuned — e.g. tightening entity-name anchoring to reduce search drift — without touching the other three. `NewsAPI.org` was removed entirely; it is not one of the four sources this app uses.

**Source selection is explicit — there is no silent default.** On the "Run Pipeline" form (`/`, posting to `POST /fetch-all-news`), Tavily is pre-checked by default, but if you uncheck every box the pipeline is refused outright (`msg=no_source_selected`) rather than silently substituting Tavily back in. Whatever you check is exactly what runs — nothing more. The single-entity "Fetch News" quick action (`POST /fetch-news/{entity_id}`) has no source-selection UI at all and always uses Tavily only; that's a separate, simpler action, not the multi-source pipeline.

A per-run `_exhausted` set tracks which sources hit a 429/quota error mid-run — once a source is exhausted, it's skipped for the rest of that run so a dead API key doesn't slow every subsequent entity down with doomed retries.

---

## 2. The pipeline, stage by stage

Both `run_full_pipeline` (all entities / filtered by type) and `run_entity_pipeline` (single entity) run the same nine stages. Every stage's intermediate output is written to `pipeline_snapshots/<run_id>/<entity>.json` (rotated — only the last 5 runs are kept) for debugging.

```
1. FETCH        → raw articles from each selected source, across all 12 topics
2. SAVE RAW     → raw_news/<run_id>.json  (permanent, never rotated, never touched again)
3. DEDUPLICATE  → services/deduplicator.py
4. VALIDATE     → services/validator.py — 8-point checklist
5. HYPERLINK    → services/hyperlink_validator.py — 4-layer URL check
6. [industry only] SAVE + AUTO-VALIDATE → services/industry_validator.py
7. SUMMARIZE    → services/ai_summarizer.py (GPT-4o-mini)
8. SAVE         → data.json (via storage.save_news_for_entity)
9. AUDIT + GAP REPORT + RUN HISTORY
```

### 2.1 Deduplication (`services/deduplicator.py`)

Pure Python, no AI. Compares every article against every other article already kept:

- **Same URL + ≥85% headline similarity** → duplicate, rejected
- **Same URL + different headline** → kept (genuinely a different article at the same URL)
- **Different URL + same headline** → keep the higher-authority source (Reuters/Bloomberg/FT beat everything else); on a tie, prefer the earlier publish date, then prefer Tavily (richer content)
- **Different URL + different headline** → both kept

Comparisons that touch `published_date` guard against a literal `None` value (not just a missing key) — some source normalizers can emit `published_date: None` before the undated-article date-scrape backfill runs.

### 2.2 Validation — the 8-point checklist (`services/validator.py`)

Every article passes through these checks in order. Points 1–3, 5–7 are **hard rejects** (article dropped, logged to `val_log`); points 4 and 8 are **soft tags** (article kept, annotated).

1. **Missing/removed title** — empty, null, `[Removed]`, `[Deleted]`
2. **Future date** — published after the window end
3. **Too old** — published before the window start
4. **Limited content preview** (soft tag) — content < 150 chars *and* title < 60 chars. SerpAPI/NewsData snippets are short by design; hard-rejecting on length alone would silently drop valid articles.
5. **Non-English** — only fires when the source explicitly reports a non-English language field
6. **Blocked domain** — social media / forums (Reddit, Pinterest, YouTube, etc.)
7. **Entity relevance** *(client/prospect only — industry entities skip this)* — entity name not found in title or content, catching search drift (e.g. a "Stripe" query returning an unrelated mining company)
8. **Metered paywall domain** (soft tag) — Reuters/Bloomberg/FT/WSJ. If point 4 already set a "limited preview" note, this **combines** with it rather than overwriting it — a short-snippet WSJ article now correctly shows both notes instead of losing the paywall warning.

Domain extraction (used by both the paywall check and the entity-website disambiguation) strips a leading `www.` using an explicit prefix check — not `str.lstrip("www.")`, which strips *any* leading `w`/`.` characters and was silently turning `wsj.com` into `sj.com`, breaking paywall detection for WSJ specifically.

### 2.3 Hyperlink validation — 4 layers (`services/hyperlink_validator.py`)

```
Layer 1: HTTP HEAD  ─┐
Layer 2: HTTP GET    ├─→ "ok" for 200, 403/405/429/5xx (bot-blocked but exists), timeouts (benefit of doubt)
                      └─→ "invalid" only for 404/410, DNS failure, or a redirect loop
Layer 3: OpenAI web-search fallback  — only runs when layers 1-2 say "invalid"
Layer 4: Content rescue               — only runs when layer 3 finds an archived/cached copy
```

Only a **narrow** set of HTTP outcomes are ever treated as dead: 404, 410, DNS resolution failure, and redirect loops. Everything else — 403 bot-blocks, 5xx errors, timeouts — is treated as "ok" because the server confirmed the URL exists, or a real browser might succeed where a bot request didn't.

When an article clears layers 1–2 as dead, layer 3 asks OpenAI (`web_search_preview` tool) to double-check: is the page actually live? Is it an archive.org/cache copy? Did the article move to a new canonical URL? Three outcomes:

- **AI confirms it's genuinely reachable** → rescued, `url_status = "ok"`, URL updated to the canonical address if one was found
- **AI confirms it's only an archived/cached copy, but the article already has usable fetched content** → layer 4 content-rescue kicks in: `url_status = "archive"` instead of a hard "invalid" — the live link is gone, but the article is still summarizable from what was already fetched at fetch time
- **AI also can't confirm it's alive, or the AI call itself errors** → stays `"invalid"`

The critical safety rule here: **an OpenAI call failure can never silently upgrade a confirmed-dead URL to "ok."** Every AI response carries a `checked: bool` flag distinguishing "the AI really answered" from "the call errored and we're looking at a fallback default." Only `checked: True` responses are trusted to override the HTTP verdict — a rate limit or timeout on the safety-net call just leaves the article marked exactly as the HTTP check found it.

`url_status` values you'll see: `ok`, `redirect`, `archive`, `invalid`, `unknown`, `missing`.

For client/prospect entities, none of this gates whether an article is *saved* — `url_status` is purely informational (a badge on `/digest`, an `url_invalid` audit entry). The article is kept either way and proceeds to summarization.

### 2.4 Industry-only: automatic AI validation (`services/industry_validator.py`)

Industry articles skip the entity-relevance check (point 7) and the AI-summarizer's relevance gate — a hard-coded entity name check makes no sense for a sector label like "Financial Services & Banking." Instead, right after the hyperlink check, every industry article is saved to `industry_all_records.json` and **immediately** run through a dedicated 4-step AI decision tree — this used to be a manual "AI Validation" button; it now runs automatically as part of the pipeline for both the full-fleet run and single-entity runs.

```
Step 1 — Duplicate URL check       (within this validation run's seen_urls set)
Step 2 — Date validation           (HTTP-derived date; OpenAI web search if missing)
Step 3 — URL validation            (HTTP-first; same OpenAI fallback pattern as above)
Step 4 — AI categorization         (always runs for non-duplicates — sector + topic + summary)
```

Every article is guaranteed to leave this pipeline with a `validation_status` of exactly one of:

- **`Validated News`** — matched a tracked sector, category is specific (not the generic fallback)
- **`Non Validated News`** — duplicate, outside the date window, URL confirmed dead, or not relevant to any tracked sector
- **`Review`** — date genuinely couldn't be determined, or the AI categorizer's confidence was low enough to fall back to "General — review required"

The same `checked`-flag safety rule from the hyperlink layer applies here too: if the AI URL-confirmation call errors after HTTP already said a URL was dead, the article stays `Non Validated News` — it is never silently upgraded to "ok" just because the safety-net call happened to fail.

Results are written to three places, all now **merged by URL** rather than blindly appended:
- `industry_all_records.json` — the full running set (all periods/sectors), matched-and-replaced by URL
- `industry_accepted.json` / `industry_rejected.json` — the Validated / Non-Validated subsets. Re-validating the same article (e.g. after fixing a sector's `news_scope`) updates it in place instead of appending a duplicate, and correctly *moves* it between the two files if its status flips.
- `data.json` — `validation_status`, `validation_reason`, cleaned title/summary/category, **and `url_status`** get mirrored back onto the matching NewsItem so `/industry`'s badges reflect the authoritative post-AI-validation result, not just the earlier hyperlink-check pass.

The **Re-validate** button on `/industry` (`POST /ai-validate-industry`) runs this exact same batch function manually, scoped to a period/sector filter — useful after a transient failure left articles stuck in Review, or after changing a sector's scope. It is no longer required for normal operation.

### 2.5 AI summarization (`services/ai_summarizer.py`)

GPT-4o-mini generates a fresh 2–3 sentence summary and assigns primary/secondary topic categories. For client/prospect entities this call also acts as a final relevance gate (`is_relevant`) — industry entities skip that gate since they already went through the dedicated validator above. One retry at lower temperature happens automatically if the first response is malformed; if both attempts fail, the article is kept with `summary = title` and a generic category rather than being dropped. **The original article `title` is never modified** — a separate `clean_title` field (industry only) carries any AI-cleaned version.

---

## 3. Crash safety — job_status can no longer get stuck

Each of the three background pipeline entry points (`run_full_pipeline`, `run_entity_pipeline`, `run_industry_ai_validation`) is now a thin wrapper around its real implementation:

```python
async def run_entity_pipeline(entity, window, api_sources=None):
    try:
        await _run_entity_pipeline_body(entity, window, api_sources)
    except Exception as e:
        log_error("run_entity_pipeline", str(e), entity.name)
        _finish_job(datetime.now(), f"Failed — {entity.name}: {e}")
```

Previously, `run_entity_pipeline` had **no top-level exception handling at all** — a crash anywhere inside it (a network error, a disk write failure) would leave the in-memory `job_status["running"]` flag permanently `True`, and every subsequent fetch request would just redirect back with "a pipeline is already running" until the whole app was restarted. Now any unhandled exception, anywhere in the pipeline, is caught, logged, and `job_status` is force-reset — the UI shows "Failed — <reason>" instead of silently locking up.

Cancel (`job_status["cancel"]`) is checked at the top of every entity loop, and now also inside the per-article AI-summarize loop in both the full and single-entity pipelines — clicking Stop mid-summarize interrupts that entity immediately instead of waiting for it to finish every remaining article first.

`job_status` is in-memory and per-process — it has no protection against a second OS process writing to the same JSON files concurrently. The standalone `test_industry_validation.py` script (which calls the same storage functions the live app uses) now refuses to run if it detects something listening on the app's default port, as a best-effort guard against that specific collision.

---

## 4. The audit trail

`audit.json` (via `storage.append_audit`) records one entry per article per meaningful event, keyed by `action`:

| Action | When |
|---|---|
| `accepted` | Article passed dedup + 8-point validation + hyperlink check + AI relevance gate, and was saved |
| `duplicate_removed` | Deduplicator rejected it |
| `validation_rejected` | 8-point checklist hard-rejected it |
| `url_invalid` | Hyperlink check marked it invalid or unknown |
| `industry_validated` | Industry AI validation returned `Validated News` |
| `industry_non_validated` | Industry AI validation returned `Non Validated News` |
| `industry_review` | Industry AI validation returned `Review` |

The three `industry_*` actions are new. Previously, an industry article got only one audit entry — `accepted`, written the moment it passed the initial pipeline — and that entry never changed even if the AI validator later rejected it. `/audit` and `/industry` could permanently disagree about the same article with no way to see why. Now both entries exist: the original `accepted` (still true — it *did* pass the initial checks) and a later `industry_*` entry recording what the dedicated AI validation step actually decided, with the real reason attached.

---

## 5. Accuracy, at three different levels

**Run-level** (`/run-history`): `accuracy = accepted ÷ (raw_fetched − duplicates_removed) × 100` — of what survived deduplication, what fraction made it all the way to being saved. `accept_rate = accepted ÷ raw_fetched × 100` is the same thing measured against the raw haul before any filtering.

**Industry validation stats** (`/industry`'s "Current Run" / "Overall" panels): `accuracy_pct = validated ÷ (validated + non_validated + review) × 100` — of everything that's actually been AI-validated (excluding anything still pending), what fraction is clean. Pending articles are excluded from the denominator since they haven't been judged yet.

**AI usage totals** (`/run-history`'s AI calls/tokens tiles): now include the industry validator's own OpenAI calls (URL-confirmation web searches + categorization chat calls), not just `ai_summarizer`'s. Previously these were undercounted for any run that touched industry entities.

None of the bug fixes in this pass change what a *correctly functioning* pipeline reports — where a number moves at all (e.g. an article that used to get silently upgraded to "ok" on an API error now correctly stays "invalid"), it's because a false positive got corrected, not because real validation quality dropped.

---

## 6. Storage layout

| File | Contents | Rotation |
|---|---|---|
| `data.json` | Every entity's saved articles (all types) | Never — this is the live dataset |
| `audit.json` | Every article-level event (see §4) | Never |
| `run_history.json` | One row per pipeline run — timing, counts, AI usage | Never |
| `gap_report.json` | Per-run snapshot of entities/topics with no news found | Never |
| `raw_news/<run_id>.json` | Permanent raw API fetch, before any processing | **Never** (deliberate — this is the audit-of-record for what was actually returned by each API) |
| `pipeline_snapshots/<run_id>/` | Per-entity, per-stage intermediate output, for debugging | Keeps last 5 runs |
| `industry_all_records.json` | All industry articles post-hyperlink-check, full validation history | Merged by URL, not rotated |
| `industry_accepted.json` / `industry_rejected.json` | Validated / Non-Validated subsets | Merged by URL |

All files are flat JSON with atomic writes (write to a temp file, then rename) — a crash mid-write can't corrupt them. There is currently no cross-process locking beyond the in-memory `job_status` flag, so only one pipeline (and no external script) should touch these files at a time. This is a known Phase 1 limitation; Phase 2's planned move to PostgreSQL replaces it with real transactions.

---

## 7. What's still a known, accepted limitation

- **`raw_news/` never rotates**, unlike `pipeline_snapshots/`. This is intentional — it's the permanent record of what each source actually returned — but it will grow without bound over time. Not fixed in this pass because CLAUDE.md explicitly documents it as a deliberate design choice, not a bug.
- **In-memory `job_status`** only protects against overlapping runs within a single server process. It does not protect against a second process (a stray script, a second `uvicorn` worker) writing to the same files at the same time.
- **Older `audit.json`/`gap_report.json` entries** predate fields the current code expects (e.g. `window_from`, `run_id`). Every template already guards these with `{% if %}` — old rows render fine, just with blank cells where the field doesn't exist historically.
