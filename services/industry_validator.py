"""
Industry AI Validation — two OpenAI call types + orchestrator.

Call 1 (web search):  client.responses.create — confirms URL liveness + extracts dates.
                      Only triggered when HTTP check is uncertain/invalid, or date is missing.
Call 2 (chat):        client.chat.completions.create — title clean + sector assignment
                      + topic categorization + summary. Runs for every non-duplicate article.

Decision tree per article:
  Step 1 — Duplicate URL check
  Step 2 — Date validation (HTTP-first; OpenAI fallback when date is missing)
  Step 3 — URL validation  (HTTP-first; OpenAI Call 1 fallback when uncertain)
  Step 4 — AI categorization (OpenAI Call 2 — always runs unless duplicate)
"""

import os
import httpx
import asyncio
from openai import AsyncOpenAI
from dotenv import load_dotenv
from models import TOPIC_CATEGORIES
from logger import get_logger

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
log    = get_logger("industry_validator")

REQUEST_TIMEOUT = 10
MAX_REDIRECTS   = 5

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DEAD_STATUSES = {404, 410}
_DNS_ERRORS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated with hostname",
)

# ── Topic category descriptions (used in the system prompt) ───────────────────
_CATEGORY_DETAIL = """1. Accounts Receivable / Payable & Operational Efficiency
   → Cost reduction, invoice/payment processing, working capital, operational KPIs, shared services,
     back-office outsourcing, efficiency programmes, AR/AP technology.

2. Company Finances & Results
   → Earnings releases, revenue/profit/loss, EBITDA, analyst ratings, financial guidance,
     capital raises, IPO/SPAC, credit rating changes, dividend announcements.

3. Compliance Monitoring
   → AML/KYC, GDPR/CCPA, audit outcomes, ISO/SOC certification, regulatory fines,
     sanctions screening, internal control reviews. (Fine on a specific firm → this; new law → Regulatory)

4. Crisis / Bankruptcy & Insolvency
   → Administration, Chapter 11/15, CVA, liquidation, court-protected restructuring,
     going-concern warnings, emergency bailouts, credit default events.

5. Customer Service & Experience Innovations
   → NPS/CSAT announcements, contact centre transformations, CX programme launches,
     self-service rollouts, omnichannel strategy, voice-of-customer initiatives.

6. Digital Transformation & AI
   → Enterprise-wide digital or AI strategy, cloud migration programmes, CDO/CIO appointments
     with transformation mandates, multi-year digital roadmaps, org-wide automation strategies.

7. Expansion, Collaborations & Strategic Alliances
   → New market/geographic entry, joint ventures, strategic partnerships, licensing/distribution
     agreements, franchise deals, referral/channel partner agreements (no ownership transfer).

8. Infrastructure Projects & Initiatives
   → Data centre construction, physical network builds, facility refurbishment,
     capex-heavy infrastructure investment, logistics network expansion, utilities infrastructure.

9. Mergers, Acquisitions & Asset Transfers
   → Confirmed or rumoured M&A, divestitures, asset sales, equity stake purchases,
     takeover bids, management buyouts, carve-outs. (Ownership changes → this; partnership only → Expansion)

10. New Projects / Initiatives
    → Product launches, new business lines, R&D programmes, innovation hubs, pilots,
      strategic initiatives not better covered by another category.

11. New Technologies & AI Adoption
    → Deployment of a specific software/platform/tool, vendor selection announcements,
      AI model integration, robotics/automation rollout, technology PoC for a named system.
      (Specific tool adoption → this; company-wide AI strategy → Digital Transformation)

12. Regulatory Developments & RFP / RFI Announcements
    → New laws, government policy changes, regulator announcements, government tender notices,
      RFP/RFI/RFQ issuances, public procurement updates. (New regulation → this; firm compliance → Compliance)"""

_VALID_CATEGORIES = set(TOPIC_CATEGORIES)


# ── HTTP URL check ─────────────────────────────────────────────────────────────
async def _http_check_url(url: str) -> str:
    """Returns: 'ok' | 'invalid' | 'uncertain' | 'missing'"""
    if not url:
        return "missing"
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers=_BROWSER_HEADERS,
        ) as c:
            # HEAD first — fast, no body download
            try:
                resp = await c.head(url)
                if resp.status_code == 200:
                    return "ok"
                if resp.status_code in _DEAD_STATUSES:
                    return "invalid"
                if resp.status_code in (403, 405, 429):
                    return "ok"
            except Exception:
                pass

            # GET fallback
            try:
                resp = await c.get(url)
                if resp.status_code == 200:
                    return "ok"
                if resp.status_code in _DEAD_STATUSES:
                    return "invalid"
                if resp.status_code in (403, 429, 405):
                    return "ok"
                return "uncertain"
            except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError):
                return "ok"  # CDN/Cloudflare hang = URL exists but blocks bots
            except httpx.TooManyRedirects:
                return "invalid"

    except httpx.ConnectError as e:
        err = str(e).lower()
        if any(m in err for m in _DNS_ERRORS):
            return "invalid"
        return "ok"  # SSL/connection refused = URL exists
    except Exception:
        return "uncertain"


# ── OpenAI Call 1: Web Search ──────────────────────────────────────────────────
async def ai_check_url_and_date(url: str, title: str) -> dict:
    """
    Calls OpenAI web_search_preview to confirm URL liveness and extract article metadata.
    Only called when HTTP check is uncertain/invalid OR when article has no date.

    Returns all available metadata so downstream logic can use whichever fields it needs:
    {
        "reachable":      bool,         # True if page loads and is not 404/archive
        "is_archive":     bool,         # True if archive.org, webcache, cached copy
        "published_date": str | None,   # YYYY-MM-DD if found, else None
        "modified_date":  str | None,   # YYYY-MM-DD if different modified date found
        "canonical_url":  str | None,   # canonical URL if different from input
        "source_name":    str | None,   # publication name found on page
        "page_title":     str | None,   # actual headline as found on the page
        "note":           str,          # brief explanation
    }
    """
    # "checked" distinguishes a real OpenAI confirmation from a fallback default —
    # callers must NOT treat "reachable": True as a real confirmation unless
    # "checked" is also True, otherwise a transient API error would silently
    # override an HTTP-confirmed-dead URL into looking verified.
    _default = {
        "reachable": True, "is_archive": False,
        "published_date": None, "modified_date": None,
        "canonical_url": None, "source_name": None,
        "page_title": None, "note": "OpenAI web search not attempted",
        "checked": False, "prompt_tokens": 0, "completion_tokens": 0,
    }
    if not url:
        return {**_default, "reachable": False, "note": "URL missing"}

    prompt = (
        f"Visit this URL and answer the following questions:\n"
        f"1. Is the page live and accessible — not a 404, not a redirect loop, "
        f"not an archive.org or Google cache copy?\n"
        f"2. What is the published date shown on the page? (YYYY-MM-DD format or UNKNOWN)\n"
        f"3. Is there a separate modified/updated date? (YYYY-MM-DD or UNKNOWN)\n"
        f"4. What is the publication or source name shown on the page?\n"
        f"5. What is the canonical URL if shown in the page source?\n"
        f"6. What is the actual article headline as displayed on the page?\n\n"
        f"URL: {url}\n"
        f"Reference title (may differ from the live page): {title}\n\n"
        f"Respond with ONLY these fields, no extra prose:\n"
        f"REACHABLE: YES or NO\n"
        f"IS_ARCHIVE: YES or NO\n"
        f"PUBLISHED_DATE: YYYY-MM-DD or UNKNOWN\n"
        f"MODIFIED_DATE: YYYY-MM-DD or UNKNOWN\n"
        f"CANONICAL_URL: <url if different, or SAME>\n"
        f"SOURCE_NAME: <publication name or UNKNOWN>\n"
        f"PAGE_TITLE: <headline or UNKNOWN>\n"
        f"NOTE: <one sentence if anything unusual, else blank>"
    )
    try:
        response = await client.responses.create(
            model="gpt-4o-mini",
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )
        text = response.output_text or ""
        parsed = {}
        for line in text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                parsed[k.strip().upper()] = v.strip()

        def _yn(key: str, default: bool) -> bool:
            val = parsed.get(key, "").upper()
            if val == "YES":
                return True
            if val == "NO":
                return False
            return default

        def _date(key: str) -> str | None:
            val = parsed.get(key, "").strip()
            if val and val.upper() != "UNKNOWN" and len(val) >= 10:
                return val[:10]
            return None

        canonical = parsed.get("CANONICAL_URL", "").strip()
        if canonical.upper() in ("SAME", "UNKNOWN", ""):
            canonical = None

        source = parsed.get("SOURCE_NAME", "").strip()
        page_title = parsed.get("PAGE_TITLE", "").strip()

        usage = getattr(response, "usage", None)
        return {
            "reachable":      _yn("REACHABLE", True),
            "is_archive":     _yn("IS_ARCHIVE", False),
            "published_date": _date("PUBLISHED_DATE"),
            "modified_date":  _date("MODIFIED_DATE"),
            "canonical_url":  canonical if canonical and canonical != url else None,
            "source_name":    source if source and source.upper() != "UNKNOWN" else None,
            "page_title":     page_title if page_title and page_title.upper() != "UNKNOWN" else None,
            "note":           parsed.get("NOTE", ""),
            "checked":        True,
            "prompt_tokens":     getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        }
    except Exception as e:
        log.error(f"ai_check_url_and_date failed for {url[:80]}: {e}")
        return {**_default, "note": f"OpenAI web search error: {e}"}


# ── OpenAI Call 2: Chat Completions — categorize + clean title + summary ──────

def _build_sectors_block(industry_sectors: list) -> str:
    """Render the dynamic sector list into the system prompt."""
    lines = []
    for s in industry_sectors:
        name  = s.get("name", "")
        itype = s.get("industry_type", "")
        scope = s.get("news_scope", "")
        line  = f"• {name}"
        if itype:
            line += f"  [type: {itype}]"
        if scope:
            line += f"\n  Covers: {scope}"
        lines.append(line)
    return "\n".join(lines) if lines else "(no sectors configured)"


_BASE_SYSTEM_PROMPT = """You are Chief Editor of a business intelligence service that tracks news across multiple industry sectors. Your role is to evaluate incoming news items and complete four editorial tasks with precision.

────────────────────────────────────────────────
INDUSTRY SECTORS YOU COVER
────────────────────────────────────────────────
{sectors_block}

SECTOR ASSIGNMENT RULES:
- Assign to exactly ONE sector, or write "None" if the news does not fit any sector.
- "None" is correct for: electoral/political news with no business impact, personal lifestyle
  or entertainment content, single-firm HR appointments with no market significance, or articles
  about an industry not represented in the list above.
- When a sector has a "Covers:" description, use it as the primary guide for what qualifies.
- If unsure between two sectors, assign to the one whose description is the closer match.

────────────────────────────────────────────────
TOPIC CATEGORIES (12 — assign ONE primary, optionally more as secondary)
────────────────────────────────────────────────
{category_detail}

PRIORITY RULES (apply when a category decision is ambiguous):
P1. "Crisis / Bankruptcy & Insolvency" overrides ALL when the article covers financial distress,
    administration, liquidation, court-protected restructuring, or imminent insolvency risk.
P2. "Mergers, Acquisitions & Asset Transfers" overrides "Expansion, Collaborations & Strategic
    Alliances" when ownership or equity changes hands. Partnership/JV only → Expansion.
P3. "Compliance Monitoring" overrides "Regulatory Developments" when a specific company is
    penalised, audited, or certified. New law/regulation being announced → Regulatory.
P4. "Digital Transformation & AI" = company-wide strategy. "New Technologies & AI Adoption" =
    a specific tool/platform being deployed. Company-wide AI roadmap → Digital Transformation.

────────────────────────────────────────────────
SUMMARY WRITING RULES
────────────────────────────────────────────────
- Write 2–3 sentences in active, present tense.
- Do NOT begin with "The article", "This piece", "This news", "This report", or any phrase
  that refers to the article as an object. Write about the subject of the news directly.
  ✗ WRONG: "The article discusses how Revolut is expanding..."
  ✓ CORRECT: "Revolut is expanding its banking licence to three new European markets..."
- Focus on business significance and market impact.
- Use professional financial/business English. No casual language, no hedging.
- If article content is sparse, summarise from the clean headline only — do not invent details.
- If content is ambiguous or contradictory, set CONFIDENCE to low and explain in NOTE.

────────────────────────────────────────────────
TITLE CLEANING RULES
────────────────────────────────────────────────
Some headlines arrive with the publication name appended as a suffix:
  "Headline Text - Publication Name"  or  "Headline Text — Publication Name"
Strip the " - Publication Name" or " — Publication Name" suffix and return only the clean headline.
Do NOT remove any part of the actual headline. If no suffix is present, return the headline unchanged.

────────────────────────────────────────────────
OUTPUT FORMAT — respond ONLY with these fields, nothing else:
────────────────────────────────────────────────
CLEAN_TITLE: <cleaned headline, no publication suffix>
INDUSTRY_SECTOR: <exact sector name from the list above, or "None">
PRIMARY_CATEGORY: <exact category name from the 12 above>
SECONDARY_CATEGORIES: <comma-separated additional categories, or leave blank>
SUMMARY: <2–3 sentence business-focused summary>
CONFIDENCE: <high | medium | low>
NOTE: <one sentence on categorisation reasoning, or blank if high confidence>"""


def _build_system_prompt(industry_sectors: list) -> str:
    sectors_block = _build_sectors_block(industry_sectors)
    return _BASE_SYSTEM_PROMPT.format(
        sectors_block=sectors_block,
        category_detail=_CATEGORY_DETAIL,
    )


async def ai_categorize_industry_article(
    title:            str,
    content:          str,
    industry_sectors: list,
) -> dict:
    """
    Single OpenAI chat call that performs four editorial tasks:
    title clean + sector assignment + topic categorization + summary.

    Returns all fields so any can be used for downstream validation or storage:
    {
        "clean_title":          str,   # title with ' - Publisher Name' suffix stripped
        "industry_sector":      str,   # matched sector name, or "None"
        "industry_type":        str,   # industry_type of matched sector
        "primary_category":     str,   # one of the 12 TOPIC_CATEGORIES
        "secondary_categories": str,   # comma-separated additional categories (may be blank)
        "summary":              str,   # 2-3 sentence editorial summary
        "is_relevant":          bool,  # False when industry_sector == "None"
        "confidence":           str,   # "high" | "medium" | "low"
        "note":                 str,   # brief categorisation reasoning
    }
    """
    _fallback = {
        "clean_title":          title,
        "industry_sector":      industry_sectors[0]["name"] if industry_sectors else "",
        "industry_type":        "",
        "primary_category":     "General — review required",
        "secondary_categories": "",
        "summary":              title,
        "is_relevant":          True,
        "confidence":           "low",
        "note":                 "AI categorization failed — review required",
        "prompt_tokens":        0,
        "completion_tokens":    0,
    }

    body = content.strip() if content and len(content.strip()) >= 60 else ""
    input_text = f"Title: {title}\n\nContent: {body[:2000]}" if body else f"Title: {title}"
    system_prompt = _build_system_prompt(industry_sectors)

    # Build sector name → industry_type lookup for result enrichment
    sector_type_map = {s.get("name", ""): s.get("industry_type", "") for s in industry_sectors}
    valid_sectors   = {s.get("name", "") for s in industry_sectors} | {"None"}

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"Evaluate and categorise this industry news article:\n\n{input_text}"},
            ],
            max_tokens=450,
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()

        parsed = {}
        for line in text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                parsed[k.strip().upper()] = v.strip()

        clean_title    = parsed.get("CLEAN_TITLE", "").strip() or title
        sector         = parsed.get("INDUSTRY_SECTOR", "").strip()
        primary        = parsed.get("PRIMARY_CATEGORY", "").strip()
        secondary_raw  = parsed.get("SECONDARY_CATEGORIES", "").strip()
        summary        = parsed.get("SUMMARY", "").strip()
        confidence     = parsed.get("CONFIDENCE", "medium").strip().lower()
        note           = parsed.get("NOTE", "").strip()

        # Validate sector — reject hallucinated names
        if sector not in valid_sectors:
            sector = "None"

        # Validate primary category — reject hallucinated names
        if primary not in _VALID_CATEGORIES:
            primary = "General — review required"

        # Validate secondary categories
        secondary_parts = []
        for part in secondary_raw.split(","):
            s = part.strip()
            if s and s in _VALID_CATEGORIES and s != primary:
                secondary_parts.append(s)
        secondary = ", ".join(secondary_parts)

        is_relevant   = sector != "None"
        industry_type = sector_type_map.get(sector, "") if is_relevant else ""
        usage = getattr(resp, "usage", None)

        return {
            "clean_title":          clean_title,
            "industry_sector":      sector,
            "industry_type":        industry_type,
            "primary_category":     primary,
            "secondary_categories": secondary,
            "summary":              summary or title,
            "is_relevant":          is_relevant,
            "confidence":           confidence if confidence in ("high", "medium", "low") else "medium",
            "note":                 note,
            "prompt_tokens":        getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens":    getattr(usage, "completion_tokens", 0) or 0,
        }

    except Exception as e:
        log.error(f"ai_categorize_industry_article failed for '{title[:60]}': {e}")
        return _fallback


# ── Orchestrator ───────────────────────────────────────────────────────────────
async def validate_industry_article_with_ai(
    article:          dict,
    industry_sectors: list,
    window:           dict,
    seen_urls:        set,
) -> dict:
    """
    Full four-step validation decision tree. Returns the article dict with all
    validation fields populated. Never discards — every article gets a status.

    Step 1 — Duplicate URL check
    Step 2 — Date validation (HTTP-first; OpenAI web search when date is missing)
    Step 3 — URL validation   (HTTP-first; OpenAI web search fallback)
    Step 4 — AI categorization (OpenAI Chat — runs for all non-duplicate articles)
    """
    art = dict(article)  # work on a copy
    url   = art.get("url", "").strip()
    title = art.get("title", "").strip()
    pub_date = art.get("published_date", "") or ""

    win_from = window.get("from", "")
    win_to   = window.get("to", "")

    # Cache OpenAI web search result so we don't call it twice
    _openai_url_result = None

    # ── Step 1: Duplicate check ────────────────────────────────────────────────
    if url and url in seen_urls:
        art["validation_status"] = "Non Validated News"
        art["validation_reason"] = "Duplicate article URL"
        art["ai_calls"] = 0
        art["ai_prompt_tokens"] = 0
        art["ai_completion_tokens"] = 0
        log.debug(f"DUPLICATE: {url[:80]}")
        return art

    if url:
        seen_urls.add(url)

    # Reset any legacy validation_status so re-validation always produces a fresh result
    art["validation_status"] = None
    art["validation_reason"] = None

    # ── Step 2: Date validation ────────────────────────────────────────────────
    has_date = bool(pub_date and pub_date.upper() not in ("NA", "NONE", "UNKNOWN", ""))

    if has_date:
        # Compare YYYY-MM-DD strings lexicographically
        if pub_date < win_from:
            art["validation_status"] = "Non Validated News"
            art["validation_reason"] = f"Published {pub_date} — before window start {win_from}"
        elif pub_date > win_to:
            art["validation_status"] = "Non Validated News"
            art["validation_reason"] = f"Published {pub_date} — after window end {win_to}"
        # else: in-window → proceed (validation_status not set yet)
    else:
        # No date — call OpenAI web search to try to extract it
        log.info(f"Missing date — calling OpenAI web search for: {title[:60]}")
        _openai_url_result = await ai_check_url_and_date(url, title)
        found_date = _openai_url_result.get("published_date")

        if found_date:
            art["published_date"] = found_date
            if found_date < win_from or found_date > win_to:
                art["validation_status"] = "Non Validated News"
                art["validation_reason"] = f"Published {found_date} — outside window {win_from}–{win_to}"
            # else: in-window → proceed
        else:
            # Date not found even via web search
            art["validation_status"] = "Review"
            art["validation_reason"] = "Publication date not available — manual review required"

    # ── Step 3: URL validation ─────────────────────────────────────────────────
    if not url:
        if art.get("validation_status") in (None, ""):
            art["validation_status"] = "Non Validated News"
            art["validation_reason"] = "Article URL is missing"
        art["url_status"] = "missing"
    else:
        http_status = await _http_check_url(url)

        if http_status == "ok":
            art["url_status"] = "ok"
        elif http_status == "invalid":
            # HTTP says dead — use OpenAI to confirm (if not already called)
            if _openai_url_result is None:
                _openai_url_result = await ai_check_url_and_date(url, title)

            if not _openai_url_result.get("checked", False):
                # The AI safety-net call itself errored (rate limit, timeout,
                # malformed response) — never let that silently upgrade an
                # HTTP-confirmed-dead URL to "ok". Trust the HTTP verdict.
                art["url_status"] = "invalid"
                if art.get("validation_status") in (None, ""):
                    art["validation_status"] = "Non Validated News"
                    art["validation_reason"] = "URL not accessible — page does not exist"
            elif not _openai_url_result.get("reachable", True):
                art["url_status"] = "invalid"
                if art.get("validation_status") in (None, ""):
                    art["validation_status"] = "Non Validated News"
                    art["validation_reason"] = "URL not accessible — page does not exist"
            elif _openai_url_result.get("is_archive", False):
                art["url_status"] = "archive"
                if art.get("validation_status") in (None, ""):
                    art["validation_status"] = "Non Validated News"
                    art["validation_reason"] = "URL is an archived/cached copy — not the live article"
            else:
                art["url_status"] = "ok"   # OpenAI confirmed it's reachable
        else:
            # uncertain — try OpenAI if not already called
            if _openai_url_result is None:
                _openai_url_result = await ai_check_url_and_date(url, title)

            if not _openai_url_result.get("checked", False):
                art["url_status"] = "unknown"   # AI safety-net failed too — genuinely uncertain, leave for review
            elif _openai_url_result.get("reachable", True) and not _openai_url_result.get("is_archive", False):
                art["url_status"] = "ok"
            elif _openai_url_result.get("is_archive", False):
                art["url_status"] = "archive"
                if art.get("validation_status") in (None, ""):
                    art["validation_status"] = "Non Validated News"
                    art["validation_reason"] = "URL is an archived/cached copy"
            else:
                art["url_status"] = "unknown"   # genuinely uncertain — leave for review

    # Enrich with canonical URL and source name if OpenAI found better values
    if _openai_url_result:
        if _openai_url_result.get("canonical_url"):
            art["original_url"] = url
            art["url"] = _openai_url_result["canonical_url"]
        if _openai_url_result.get("source_name") and not art.get("source"):
            art["source"] = _openai_url_result["source_name"]

    # ── Step 4: AI categorization ──────────────────────────────────────────────
    # Runs for ALL articles that are not plain duplicates, regardless of status so far.
    # This ensures every article has populated fields for the human reviewer.
    cat_result = await ai_categorize_industry_article(
        title=art.get("title", ""),
        content=art.get("content", ""),
        industry_sectors=industry_sectors,
    )

    art["clean_title"]          = cat_result["clean_title"]
    art["industry_sector"]      = cat_result["industry_sector"]
    art["industry_type"]        = cat_result["industry_type"]
    art["primary_category"]     = cat_result["primary_category"]
    art["secondary_categories"] = cat_result["secondary_categories"]
    art["summary"]              = cat_result["summary"]
    art["ai_confidence"]        = cat_result["confidence"]
    art["ai_note"]              = cat_result["note"]

    # Token/call usage — accumulated so run-history AI usage totals include
    # industry validation calls, not just ai_summarizer's. One categorize call
    # always runs here; _openai_url_result adds one more if Step 2/3 used it.
    _url_check_calls = 1 if _openai_url_result else 0
    art["ai_calls"] = 1 + _url_check_calls
    art["ai_prompt_tokens"] = (
        cat_result.get("prompt_tokens", 0)
        + (_openai_url_result.get("prompt_tokens", 0) if _openai_url_result else 0)
    )
    art["ai_completion_tokens"] = (
        cat_result.get("completion_tokens", 0)
        + (_openai_url_result.get("completion_tokens", 0) if _openai_url_result else 0)
    )

    # Determine final validation_status if not already set by steps 2–3
    if art.get("validation_status") in (None, ""):
        if not cat_result["is_relevant"]:
            art["validation_status"] = "Non Validated News"
            art["validation_reason"] = "Not relevant to any tracked industry sector"
        elif cat_result["primary_category"] in ("General — review required", ""):
            art["validation_status"] = "Review"
            art["validation_reason"] = "Category unclear — human review required"
        else:
            art["validation_status"] = "Validated News"
            art["validation_reason"] = f"Sector: {cat_result['industry_sector']}"

    # If a previously-set Non Validated status was date-related, still categorize fully
    # (category+summary already updated above — just keep the rejection status)

    log.info(
        f"[{art['validation_status']}] {art.get('clean_title', title)[:60]} | "
        f"sector={cat_result['industry_sector']} | cat={cat_result['primary_category']}"
    )
    return art
