import os
import httpx
from typing import List
from dotenv import load_dotenv

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# ── Configuration ──────────────────────────────────────────────────────────────
REQUEST_TIMEOUT  = 10    # seconds per HTTP check
MAX_REDIRECTS    = 5     # max redirect hops to follow

# HTTP status codes considered valid
VALID_STATUSES   = {200, 201, 301, 302, 303, 307, 308}

# HTTP status codes that mean the article is gone / blocked
INVALID_STATUSES = {404, 410, 403, 451}   # 451 = legally unavailable


# ── Layer 1 + 2: HTTP HEAD check with redirect follow ─────────────────────────
async def _http_check(url: str) -> dict:
    """
    Layer 1: HTTP HEAD request — fastest, free, no API.
    Layer 2: Follow redirects automatically (up to MAX_REDIRECTS).

    Returns:
        { "status": "ok" | "redirect" | "invalid" | "unknown",
          "final_url": str,
          "status_code": int }
    """
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            resp = await client.head(url)
            code = resp.status_code

            if code in VALID_STATUSES:
                final = str(resp.url)
                status = "redirect" if final != url else "ok"
                return {"status": status, "final_url": final, "status_code": code}

            if code in INVALID_STATUSES:
                return {"status": "invalid", "final_url": url, "status_code": code}

            # Some servers block HEAD — fall back to GET
            resp = await client.get(url)
            code = resp.status_code
            if code in VALID_STATUSES:
                return {"status": "ok", "final_url": str(resp.url), "status_code": code}

            return {"status": "invalid", "final_url": url, "status_code": code}

    except httpx.TooManyRedirects:
        return {"status": "invalid", "final_url": url, "status_code": 0}
    except Exception:
        return {"status": "unknown", "final_url": url, "status_code": 0}


# ── Layer 3: Tavily Extract — only for unknown/inconclusive results ────────────
async def _tavily_extract(url: str) -> dict:
    """
    Layer 3: Tavily Extract API — called only when HTTP check is inconclusive.
    Fetches and validates actual article content is accessible.

    Returns:
        { "status": "ok" | "invalid", "final_url": str }
    """
    if not TAVILY_API_KEY:
        return {"status": "unknown", "final_url": url}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/extract",
                json={"api_key": TAVILY_API_KEY, "urls": [url]},
            )
            resp.raise_for_status()
            data    = resp.json()
            results = data.get("results", [])
            failed  = data.get("failed_results", [])

            if results and results[0].get("raw_content"):
                return {"status": "ok", "final_url": url}
            if failed:
                return {"status": "invalid", "final_url": url}
            return {"status": "unknown", "final_url": url}

    except Exception as e:
        print(f"[HyperlinkValidator] Tavily Extract error for {url}: {e}")
        return {"status": "unknown", "final_url": url}


# ── Main validator ─────────────────────────────────────────────────────────────
async def validate_hyperlinks(articles: List[dict]) -> List[dict]:
    """
    4-layer hyperlink validation per article.

    Layer 1: HTTP HEAD check             → free, covers ~85% of URLs
    Layer 2: Auto-follow redirects       → free, resolves all 3xx chains
    Layer 3: Tavily Extract API          → paid, called only for unknowns (~10%)
    Layer 4: Fetched-content check       → free, called only when Layer 3 is
                                           still unknown but the article already
                                           has substantial content from the
                                           initial Tavily/GNews/SerpAPI fetch.
                                           If content >= 150 chars, the URL was
                                           clearly reachable at fetch time → ok.

    NOTE on ChatGPT / OpenAI for URL validation:
    OpenAI is NOT used for URL reachability checks. GPT has no live internet
    access, so asking it "is this URL valid?" would only produce a guess.
    URL validity is determined purely by HTTP status codes (Layers 1-2),
    Tavily Extract (Layer 3), and existing fetched content (Layer 4).
    OpenAI is used downstream only for summarisation and categorisation.

    Articles flagged invalid are kept (not removed here) so the caller can
    write an audit entry with the exact reason before deciding to drop them.
    Redirected URLs have their url field updated to the final destination.
    """
    ok      = 0
    invalid = 0
    tavily  = 0
    content_rescue = 0

    results = []

    for art in articles:
        url = art.get("url", "")

        if not url:
            results.append({**art, "url_status": "invalid"})
            invalid += 1
            continue

        # Layer 1 + 2: HTTP HEAD with redirect follow
        check = await _http_check(url)

        if check["status"] in ("ok", "redirect"):
            art = {**art, "url": check["final_url"], "url_status": check["status"]}
            ok += 1

        elif check["status"] == "invalid":
            art = {**art, "url_status": "invalid"}
            invalid += 1

        else:
            # Layer 3: Tavily Extract for unknowns
            tavily += 1
            extract = await _tavily_extract(url)

            if extract["status"] in ("ok",):
                art = {**art, "url": extract["final_url"], "url_status": "ok"}
                ok += 1
            elif extract["status"] == "invalid":
                art = {**art, "url_status": "invalid"}
                invalid += 1
            else:
                # Layer 4: Content-based rescue — if we already fetched substantial
                # content for this article, the URL was reachable at fetch time.
                existing_content = art.get("content", "")
                if existing_content and len(existing_content.strip()) >= 150:
                    art = {**art, "url": extract["final_url"], "url_status": "ok"}
                    ok += 1
                    content_rescue += 1
                else:
                    art = {**art, "url_status": "unknown"}
                    invalid += 1

        results.append(art)

    print(
        f"[HyperlinkValidator] {len(articles)} checked → "
        f"{ok} ok | {invalid} invalid/unknown | "
        f"{tavily} via Tavily Extract | {content_rescue} rescued by content check"
    )
    return results