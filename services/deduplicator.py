from difflib import SequenceMatcher
from typing import List, Tuple

# Similarity threshold — 0.85 = 85% headline match = same story
SIMILARITY_THRESHOLD = 0.85

# Source authority ranking — higher = preferred when deduplicating
SOURCE_PRIORITY = {
    "reuters.com": 10, "bloomberg.com": 10, "ft.com": 10,
    "wsj.com": 10, "bbc.com": 9, "bbc.co.uk": 9,
    "cnbc.com": 8, "forbes.com": 8, "businessinsider.com": 7,
    "techcrunch.com": 7, "theguardian.com": 7, "economist.com": 9,
}

def _priority(url: str) -> int:
    for domain, score in SOURCE_PRIORITY.items():
        if domain in url:
            return score
    return 5  # default mid-priority

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def deduplicate(articles: List[dict]) -> Tuple[List[dict], List[dict]]:
    """
    Single-pass deduplication.  Four possible outcomes per article:

      ┌─────────────┬──────────────┬────────────────────────────────────────────┐
      │  URL match  │  Headline    │  Decision                                  │
      │             │  match       │                                            │
      ├─────────────┼──────────────┼────────────────────────────────────────────┤
      │  same       │  same (≥85%) │  REJECT — true duplicate                   │
      │  same       │  different   │  KEEP   — different article at same URL    │
      │  different  │  same (≥85%) │  KEEP higher authority, REJECT the other   │
      │  different  │  different   │  KEEP both                                 │
      └─────────────┴──────────────┴────────────────────────────────────────────┘

    Returns:
        clean   — deduplicated article list (one winner per story)
        dup_log — removed articles with reason (for audit)
    """
    clean   = []
    dup_log = []

    for art in articles:
        url   = art.get("url", "").strip().rstrip("/")
        title = art.get("title", "")

        # disposition tracks what to do after scanning clean:
        #   None      → not matched yet, append to clean at the end
        #   "reject"  → already added to dup_log, skip
        #   "swapped" → replaced an existing clean entry, skip re-append
        disposition = None

        for i, existing in enumerate(clean):
            ex_url  = existing.get("url", "").strip().rstrip("/")
            score   = _similarity(title, existing.get("title", ""))
            same_url = (url == ex_url)
            same_hdl = (score >= SIMILARITY_THRESHOLD)

            # ── Case 1: same URL + same headline → true duplicate ──────────────
            if same_url and same_hdl:
                dup_log.append({
                    **art,
                    "reason": f"Duplicate URL and headline ({score:.0%}) — {url}",
                })
                disposition = "reject"
                break

            # ── Case 2: same URL + different headline → different article ──────
            if same_url and not same_hdl:
                # Not a duplicate — keep this article; stop scanning
                break   # disposition stays None → appended below

            # ── Case 3: different URL + same headline → same story, pick best ──
            if not same_url and same_hdl:
                p_new = _priority(url)
                p_old = _priority(ex_url)
                keep_new = False

                if p_new > p_old:
                    keep_new = True
                elif p_new == p_old:
                    # Tiebreak 1: earlier publish date
                    if art.get("published_date", "") < existing.get("published_date", ""):
                        keep_new = True
                    # Tiebreak 2: prefer Tavily (richer content)
                    elif art.get("fetch_source") == "tavily" \
                            and existing.get("fetch_source") != "tavily":
                        keep_new = True

                if keep_new:
                    dup_log.append({
                        **existing,
                        "reason": (
                            f"Headline similarity {score:.0%} — "
                            f"replaced by higher-authority source ({url})"
                        ),
                    })
                    clean[i]    = art
                    disposition = "swapped"
                else:
                    dup_log.append({
                        **art,
                        "reason": (
                            f"Headline similarity {score:.0%} — "
                            f"lower authority than existing ({ex_url})"
                        ),
                    })
                    disposition = "reject"
                break

            # ── Case 4: different URL + different headline → no match, continue ─
            # (loop continues to next existing article)

        if disposition is None:
            clean.append(art)

    print(
        f"[Dedup] {len(articles)} in → {len(clean)} clean | {len(dup_log)} removed"
    )
    return clean, dup_log
