from pydantic import BaseModel
from typing import List, Optional
from enum import Enum


class EntityType(str, Enum):
    client   = "client"
    prospect = "prospect"
    industry = "industry"


TOPIC_CATEGORIES = [
    "Accounts Receivable / Payable & Operational Efficiency",
    "Company Finances & Results",
    "Compliance Monitoring",
    "Crisis / Bankruptcy & Insolvency",
    "Customer Service & Experience Innovations",
    "Digital Transformation & AI",
    "Expansion, Collaborations & Strategic Alliances",
    "Infrastructure Projects & Initiatives",
    "Mergers, Acquisitions & Asset Transfers",
    "New Projects / Initiatives",
    "New Technologies & AI Adoption",
    "Regulatory Developments & RFP / RFI Announcements",
]


class Entity(BaseModel):
    id:            str
    name:          str
    entity_type:   EntityType
    topics:        List[str] = []
    website:       Optional[str] = ""   # company website — used to sharpen search queries
    industry_type: Optional[str] = ""   # sector / sub-type (industries only)
    news_scope:    Optional[str] = ""   # what kinds of news to focus on (industries only)
    aliases:       Optional[List[str]] = []  # alternate names / short names for relevance matching


class NewsItem(BaseModel):
    # NOTE: this is the shape of articles stored in data.json (what get_all_news()
    # deserializes). industry_all_records.json / industry_accepted.json /
    # industry_rejected.json are a deliberately separate, un-validated raw-dict
    # representation used internally by services/industry_validator.py — they
    # carry pipeline-only bookkeeping fields not listed here (industry_sector,
    # run_id, clean_title, secondary_categories [plural], ai_confidence, ai_note).
    # Only validation_status/validation_reason/url_status/title/summary/category
    # get mirrored from there into a NewsItem via storage.update_industry_validation_status().

    # ── Core article fields ────────────────────────────────────────────────────
    title:              str            # original title — never modified
    url:                str            # final URL (updated if redirect followed)
    source:             str
    published_date:     str            # YYYY-MM-DD
    fetched_date:       str            # YYYY-MM-DD — date pipeline ran
    period:             str            # "May 2026" — digest period label

    # ── AI-generated fields ────────────────────────────────────────────────────
    summary:            str
    primary_category:   str
    secondary_category: Optional[str] = ""

    # ── Entity linkage ─────────────────────────────────────────────────────────
    entity_id:          str
    entity_type:        str            # "client" | "prospect" | "industry"

    # ── Quality / audit fields ─────────────────────────────────────────────────
    url_status:         Optional[str]  = "ok"    # ok | redirect | archive | invalid | unknown | missing
    duplicate_flag:     Optional[bool] = False
    validation_status:  Optional[str]  = None    # None | "Validated News" | "Non Validated News" | "Review"
    rejection_reason:   Optional[str]  = ""      # legacy — kept for backward compat
    validation_reason:  Optional[str]  = None    # human-readable validation outcome (AI Validation)
    industry_type:      Optional[str]  = None    # entity's industry_type (industry articles only)
    content:            Optional[str]  = None    # raw article body preserved for AI re-use

    # ── Paywall handling fields ────────────────────────────────────────────────
    original_url:       Optional[str]  = ""      # set when paywall alt link used
    paywall_note:       Optional[str]  = ""      # shown in digest UI + Excel

    # ── Fetch metadata ─────────────────────────────────────────────────────────
    topic_queried:      Optional[str]  = ""      # which of 12 topics found this
    is_primary_topic:   Optional[bool] = False   # was topic assigned to entity?
    fetch_source:       Optional[str]  = ""      # "tavily" | "serpapi" | "newsdata" | "newsai"


class AuditEntry(BaseModel):
    run_date:      str
    entity_id:     str
    entity_name:   str
    entity_type:   Optional[str] = ""   # "client" | "prospect" | "industry"
    article_title: str
    action:        str    # "accepted" | "duplicate_removed" | "validation_rejected" | "url_invalid"
                           # | "industry_validated" | "industry_non_validated" | "industry_review"
    reason:        str
    source_url:    str
    fetch_source:  Optional[str] = ""   # "tavily" | "serpapi" | "newsdata" | "newsai"
    window_from:   Optional[str] = ""   # report period start YYYY-MM-DD
    window_to:     Optional[str] = ""   # report period end YYYY-MM-DD
    