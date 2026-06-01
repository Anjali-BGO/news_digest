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


class NewsItem(BaseModel):
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
    url_status:         Optional[str]  = "ok"    # ok | redirect | invalid | unknown
    duplicate_flag:     Optional[bool] = False
    validation_status:  Optional[str]  = "pass"  # pass | rejected
    rejection_reason:   Optional[str]  = ""

    # ── Paywall handling fields ────────────────────────────────────────────────
    original_url:       Optional[str]  = ""      # set when paywall alt link used
    paywall_note:       Optional[str]  = ""      # shown in digest UI + Excel

    # ── Fetch metadata ─────────────────────────────────────────────────────────
    topic_queried:      Optional[str]  = ""      # which of 12 topics found this
    is_primary_topic:   Optional[bool] = False   # was topic assigned to entity?
    fetch_source:       Optional[str]  = ""      # "tavily" | "gnews"


class AuditEntry(BaseModel):
    run_date:      str
    entity_id:     str
    entity_name:   str
    article_title: str
    action:        str    # "duplicate_removed" | "validation_rejected" | "url_invalid"
    reason:        str
    source_url:    str
    