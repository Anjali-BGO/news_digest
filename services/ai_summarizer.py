import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
from models import TOPIC_CATEGORIES
from logger import get_logger

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
log    = get_logger("ai_summarizer")

# ── Category definitions ───────────────────────────────────────────────────────
CATEGORY_LIST = "\n".join(f"{i+1}. {c}" for i, c in enumerate(TOPIC_CATEGORIES))

# Only the 12 specific categories are valid for primary/secondary.
# "General" in any form is treated as a failed parse and triggers a retry —
# forcing the model to commit to the most relevant specific category.
VALID_CATEGORIES = set(TOPIC_CATEGORIES)

# ── Priority and per-category disambiguation rules ────────────────────────────
DISAMBIGUATION = """
PRIORITY RULES (apply strictly in this order when a category decision is ambiguous):
P1. "Crisis / Bankruptcy & Insolvency" overrides ALL others when the article covers financial distress, administration, liquidation, restructuring under court protection, or imminent insolvency risk.
P2. "Mergers, Acquisitions & Asset Transfers" overrides "Expansion, Collaborations & Strategic Alliances" when ownership, equity stake, or controlling interest changes hands. If only a partnership/JV with no ownership transfer → Expansion/Collaborations.
P3. "Compliance Monitoring" overrides "Regulatory Developments & RFP/RFI" when the article is primarily about the company passing/failing an audit, certification, data breach penalty, or internal control finding. If it is mainly a new law/regulation being announced → Regulatory.
P4. "Digital Transformation & AI" vs "New Technologies & AI Adoption": Digital Transformation = company-wide digital strategy, transformation programme, or AI/cloud roadmap announced at executive level. New Technologies = a specific product, tool, platform, or vendor being deployed/evaluated (e.g. "Company X selects SAP for…", "deploys GPT-4 for customer service").

PER-CATEGORY GUIDANCE — what qualifies for each category:
1. Accounts Receivable / Payable & Operational Efficiency — cost reduction, process automation, invoice/payment processing, working capital improvements, operational KPIs, shared service centre changes, outsourcing of finance or back-office functions, efficiency programmes.
2. Company Finances & Results — earnings releases, revenue/profit/loss reports, EBITDA, analyst ratings, financial guidance updates, restatements, capital raises, IPO/SPAC activity, credit rating changes, dividend announcements.
3. Compliance Monitoring — AML/KYC compliance actions, GDPR/CCPA violations or certifications, audit outcomes (internal or external), ISO/SOC certification, sanctions screening findings, internal control reviews, regulatory fines directly imposed on the company.
4. Crisis / Bankruptcy & Insolvency — administration, Chapter 11/15, CVA, liquidation, debt restructuring under court protection, going-concern audit warnings, emergency bailouts, credit default events.
5. Customer Service & Experience Innovations — NPS/CSAT score announcements, contact centre transformations, CX programme launches, customer satisfaction improvements, self-service channel rollouts, omnichannel strategy, voice-of-customer initiatives.
6. Digital Transformation & AI — enterprise-wide digital or AI strategy announcements, cloud migration programmes, CDO/CIO appointments with transformation mandates, multi-year digital roadmaps, organisation-wide automation strategies.
7. Expansion, Collaborations & Strategic Alliances — new market or geographic entry, joint ventures, strategic partnerships, licensing/distribution agreements, franchise deals, referral or channel partner agreements (no ownership change).
8. Infrastructure Projects & Initiatives — data centre construction or expansion, physical network builds, facility construction or refurbishment, capex-heavy infrastructure investment, logistics network expansion, utilities infrastructure.
9. Mergers, Acquisitions & Asset Transfers — confirmed or rumoured acquisitions, mergers, divestitures, asset sales, minority or majority stake purchases, takeover bids, management buyouts, carve-outs.
10. New Projects / Initiatives — product launches, new business lines, R&D programmes, innovation hubs, pilots, strategic initiatives or programmes not better covered by another category.
11. New Technologies & AI Adoption — deployment of a specific software package, platform, or tool; vendor selection announcements; AI model integration; robotics/automation tool rollout; technology proof-of-concept or pilot for a named system.
12. Regulatory Developments & RFP / RFI Announcements — new laws, government policy changes, central bank or industry regulator announcements, government tender notices, RFP/RFI/RFQ issuances, public procurement updates.

SECONDARY CATEGORY: assign only when the article genuinely and substantially covers a second topic — not a passing mention. Leave blank otherwise.
""".strip()

# ── Retry suffix — injected when first attempt returns "General" or no valid primary
FORCE_CATEGORY_SUFFIX = """

IMPORTANT — OVERRIDE REQUIRED:
Your previous response either returned "General" or an unrecognised category, which is not acceptable.
You MUST select one of the 12 specific categories listed above.
Rules:
- "General — review required" and "General" are PROHIBITED in this response.
- Every business news article maps to at least one of the 12 categories.
- Choose the closest match even if the fit is imperfect.
- If the article overlaps two categories, pick the single most dominant one."""


# ── System prompt (chief editor role) ─────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a chief editor at a leading business intelligence firm with 20 years of experience curating corporate and industry news for C-suite executives. You specialise in accounts receivable/payable operations, financial services, B2B outsourcing, and enterprise technology.

Your responsibilities:
- Assign every article to exactly one of the 12 approved categories using the guidance below.
- Write concise, executive-level summaries that focus on business impact — not just what happened, but why it matters.
- Apply priority rules strictly when a category decision is ambiguous.
- NEVER use "General — review required" unless the content is completely off-topic, nonsensical, or contains no classifiable business information whatsoever. This should apply to fewer than 1% of articles — every real business news article belongs in one of the 12 categories.

{DISAMBIGUATION}"""


# ── Response parser ────────────────────────────────────────────────────────────
def _parse_response(text: str) -> dict:
    """
    Parses the structured SUMMARY / PRIMARY / SECONDARY output from GPT.

    Returns a dict with `is_valid = True` only when both `summary` and
    `primary_category` were successfully extracted AND `primary_category`
    is a recognised category name. Invalid category names are cleared so
    that the caller can detect the failure and trigger a retry rather than
    silently accepting garbage.
    """
    lines = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            lines[key.strip().upper()] = val.strip()

    summary   = lines.get("SUMMARY", "").strip()
    primary   = lines.get("PRIMARY", "").strip()
    secondary = lines.get("SECONDARY", "").strip()

    # Clear any value the model invented outside the approved category list
    if primary not in VALID_CATEGORIES:
        primary = ""
    if secondary and secondary not in VALID_CATEGORIES:
        secondary = ""

    return {
        "summary":            summary,
        "primary_category":   primary,
        "secondary_category": secondary,
        "is_valid":           bool(summary) and bool(primary),
    }


# ── Main summariser ────────────────────────────────────────────────────────────
async def summarize_article(
    title:       str,
    content:     str,
    entity_name: str,
) -> dict:
    """
    Calls GPT-4o-mini to generate a 2–3 sentence summary and assign categories.

    Retry behaviour
    ---------------
    If the first response is malformed (SUMMARY or PRIMARY missing, or PRIMARY is
    not a recognised category name), one automatic retry is made at a lower
    temperature. The retry result is accepted in one go — no further validation
    loop. This prevents articles from being silently downgraded to title-only
    summaries when the model returns a parseable but structurally wrong response.

    Token accounting
    ----------------
    Tokens from BOTH the first call and any retry are accumulated and logged,
    so usage is never undercounted when a retry occurs. The cumulative total
    appears in app.log under the "ai_summarizer" logger.

    Title preservation
    ------------------
    The original `title` is always returned unchanged. GPT only produces the
    summary and categories — it never touches the title field.

    Returns:
        {
            "title":              str,   # original, unchanged
            "summary":            str,
            "primary_category":   str,
            "secondary_category": str,
        }
    """
    # Build input — use content if substantial, otherwise title only
    body = content.strip() if content and len(content.strip()) >= 60 else ""
    input_text = f"Title: {title}\n\nContent: {body[:1800]}" if body else f"Title: {title}"

    user_prompt = f"""Review this article about '{entity_name}' and complete the following tasks.

STRICT RULES:
- Do NOT rewrite, rephrase, or modify the article title.
- Do NOT include the title in your response.
- Only output the three fields below — nothing else.
- You MUST assign one of the 12 specific categories. "General" is only acceptable if
  the article contains no classifiable business information whatsoever.

Tasks:
1. Write a 2–3 sentence business-focused summary highlighting the key business impact.
   If only the title is available, summarise from the title alone.
2. Assign the single best PRIMARY category from the 12 categories below.
3. Assign a SECONDARY category only if the article genuinely and substantially covers
   a second topic. Leave blank if not applicable.

Categories:
{CATEGORY_LIST}

Article:
{input_text}

Respond in this exact format with no extra text:
SUMMARY: <2–3 sentence summary>
PRIMARY: <exact category name>
SECONDARY: <exact category name or leave blank>"""

    total_prompt_tokens     = 0
    total_completion_tokens = 0

    def _accumulate(usage) -> None:
        nonlocal total_prompt_tokens, total_completion_tokens
        if usage:
            total_prompt_tokens     += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens

    # ── First attempt ──────────────────────────────────────────────────────────
    first_parsed = None
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=350,
            temperature=0.2,
        )
        _accumulate(resp.usage)
        first_parsed = _parse_response(resp.choices[0].message.content.strip())

        if first_parsed["is_valid"]:
            log.info(
                f"'{title[:50]}' — tokens: {total_prompt_tokens}p + {total_completion_tokens}c"
            )
            return {
                "title":              title,
                "summary":            first_parsed["summary"],
                "primary_category":   first_parsed["primary_category"],
                "secondary_category": first_parsed["secondary_category"],
                "ai_calls":           1,
                "prompt_tokens":      total_prompt_tokens,
                "completion_tokens":  total_completion_tokens,
            }

        log.warning(
            f"First draft rejected for '{title[:50]}' — "
            f"primary='{first_parsed['primary_category'] if first_parsed else 'parse error'}' "
            f"summary_present={bool(first_parsed['summary']) if first_parsed else False} — retrying with force-category prompt"
        )

    except Exception as e:
        log.error(f"First attempt error for '{title[:60]}': {e}")

    # ── Retry — force-category suffix added to prohibit "General" ─────────────
    # Lower temperature + explicit override prompt pushes the model to commit
    # to the most relevant specific category instead of taking the easy "General" exit.
    try:
        retry_prompt = user_prompt + FORCE_CATEGORY_SUFFIX
        resp2 = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": retry_prompt},
            ],
            max_tokens=350,
            temperature=0.1,
        )
        _accumulate(resp2.usage)
        parsed2 = _parse_response(resp2.choices[0].message.content.strip())

        log.info(
            f"'{title[:50]}' retry accepted — primary='{parsed2['primary_category']}' "
            f"cumulative tokens: {total_prompt_tokens}p + {total_completion_tokens}c"
        )
        return {
            "title":              title,
            "summary":            parsed2["summary"] or title,
            # Only reach "General" here if the retry response was also unparseable
            "primary_category":   parsed2["primary_category"] or "General — review required",
            "secondary_category": parsed2["secondary_category"],
            "ai_calls":           2,         # both calls counted
            "prompt_tokens":      total_prompt_tokens,
            "completion_tokens":  total_completion_tokens,
        }

    except Exception as e:
        log.error(
            f"Retry also failed for '{title[:60]}': {e} — "
            f"tokens used before fallback: {total_prompt_tokens}p + {total_completion_tokens}c"
        )

    # ── Hard fallback — both attempts failed ───────────────────────────────────
    # Use whatever the first attempt produced (may be partial) before
    # falling back entirely to the article title.
    if first_parsed:
        return {
            "title":              title,
            "summary":            first_parsed["summary"] or title,
            "primary_category":   first_parsed["primary_category"] or "General — review required",
            "secondary_category": first_parsed["secondary_category"],
            "ai_calls":           2,
            "prompt_tokens":      total_prompt_tokens,
            "completion_tokens":  total_completion_tokens,
        }
    return {
        "title":              title,
        "summary":            title,
        "primary_category":   "General — review required",
        "secondary_category": "",
        "ai_calls":           2,   # both attempts were made before reaching this fallback
        "prompt_tokens":      total_prompt_tokens,
        "completion_tokens":  total_completion_tokens,
    }
