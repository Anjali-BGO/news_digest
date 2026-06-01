import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
from models import TOPIC_CATEGORIES

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ── Category list for prompt ───────────────────────────────────────────────────
CATEGORY_LIST = "\n".join(f"{i+1}. {c}" for i, c in enumerate(TOPIC_CATEGORIES))

# ── Category guidance injected into prompt ────────────────────────────────────
DISAMBIGUATION = """
PRIORITY RULES (apply strictly in this order):
P1. Cat 4 (Crisis/Bankruptcy & Insolvency) overrides ALL others when the article covers financial distress, administration, liquidation, restructuring under court protection, or imminent insolvency risk.
P2. Cat 9 (Mergers, Acquisitions & Asset Transfers) overrides Cat 7 when ownership, equity stake, or controlling interest changes hands (acquisition announced, deal signed, merger agreed). If only a partnership/JV with no ownership transfer → Cat 7.
P3. Cat 3 (Compliance Monitoring) overrides Cat 12 when the article is primarily about the company passing/failing an audit, certification, data breach penalty, or internal control finding. If it is mainly a new law/regulation being announced → Cat 12.
P4. Cat 6 vs Cat 11: Cat 6 (Digital Transformation & AI) = company-wide digital strategy, transformation programme, or AI/cloud adoption roadmap announced at the executive level. Cat 11 (New Technologies & AI Adoption) = specific product, tool, platform, or vendor being deployed or evaluated (e.g. "Company X selects SAP for…", "deploys GPT-4 for…").

PER-CATEGORY GUIDANCE:
1. AR/AP & Operational Efficiency — cost reduction, process automation, invoice/payment processing, operational KPIs, shared service centre changes, outsourcing efficiency.
2. Company Finances & Results — earnings releases, revenue/profit/loss, EBITDA, analyst ratings, financial guidance, restatements, capital raises, credit ratings.
3. Compliance Monitoring — AML/KYC compliance, GDPR/CCPA violations or certifications, audit outcomes, ISO certification, sanctions screening, internal control reviews.
4. Crisis/Bankruptcy & Insolvency — administration, chapter 11, liquidation, debt restructuring under court protection, going-concern warnings, bailouts.
5. Customer Service & Experience — NPS scores, contact centre transformations, CX programme launches, customer satisfaction improvements, self-service channel rollouts.
6. Digital Transformation & AI — enterprise-wide digital/AI strategy, cloud migration programmes, CDO/CIO appointments with transformation mandates, multi-year digital roadmaps.
7. Expansion, Collaborations & Strategic Alliances — new market entry, geographic expansion, joint ventures, strategic partnerships, licensing/distribution agreements (no ownership transfer).
8. Infrastructure Projects & Initiatives — data centres, physical network builds, facility construction, capex-heavy infrastructure, logistics network expansion.
9. Mergers, Acquisitions & Asset Transfers — confirmed or rumoured acquisitions, mergers, divestitures, asset sales, stake purchases, takeover bids.
10. New Projects / Initiatives — product launches, new business lines, innovation programmes, pilots, or initiatives not better covered by other categories.
11. New Technologies & AI Adoption — deployment of specific software/platform/tool, vendor selection, technology pilot, AI model integration, automation tool rollout.
12. Regulatory Developments & RFP/RFI — new laws, government policy changes, industry regulatory announcements, government tenders, RFP/RFI/RFQ notices.

SECONDARY CATEGORY: only assign if the article genuinely and substantially covers a second topic (not just a passing mention). Leave blank otherwise.
""".strip()


async def summarize_article(
    title:       str,
    content:     str,
    entity_name: str,
) -> dict:
    """
    Calls GPT-4o-mini to:
      1. Generate a 2-3 sentence business-focused summary.
      2. Assign a primary category (1 of 12).
      3. Assign a secondary category only if strongly applicable.

    IMPORTANT: The original article title is NEVER modified.
    The title passed in is returned as-is in all cases.
    OpenAI only generates the summary and categories — not the title.

    Returns:
        {
            "title":              str,   # original, unchanged
            "summary":            str,
            "primary_category":   str,
            "secondary_category": str,
        }
    """
    # Use title as content fallback if body is too short
    body = content.strip() if content and len(content.strip()) >= 60 else ""
    input_text = f"Title: {title}\n\nContent: {body[:1800]}" \
                 if body else f"Title: {title}"

    prompt = f"""You are a business intelligence analyst reviewing news about '{entity_name}'.

STRICT RULES:
- Do NOT rewrite, rephrase, or modify the article title in any way.
- Do NOT include the title in your response.
- Only generate: summary and category assignments.

Task:
1. Write a 2-3 sentence business-focused summary highlighting key business impact.
   If only the title is available, summarise based on the title alone.
2. Assign the single best PRIMARY category from the list below.
3. Assign a SECONDARY category only if the article genuinely spans two categories.
   Leave blank if not applicable.
4. Use "General — review required" if no category fits.

{DISAMBIGUATION}

Categories:
{CATEGORY_LIST}

Article:
{input_text}

Respond in this exact format with no extra text:
SUMMARY: <your 2-3 sentence summary>
PRIMARY: <exact category name from list>
SECONDARY: <exact category name or leave blank>"""

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.2,
        )
        text  = resp.choices[0].message.content.strip()
        lines = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                lines[key.strip().upper()] = val.strip()

        return {
            "title":              title,   # always original — never from OpenAI
            "summary":            lines.get("SUMMARY", title),
            "primary_category":   lines.get("PRIMARY", "General — review required"),
            "secondary_category": lines.get("SECONDARY", ""),
        }

    except Exception as e:
        print(f"[AISummarizer] Error for '{title[:60]}': {e}")
        return {
            "title":              title,   # original preserved on error too
            "summary":            title,   # fallback to title
            "primary_category":   "General — review required",
            "secondary_category": "",
        }