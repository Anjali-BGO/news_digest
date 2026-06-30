import os
import httpx
from typing import List
from dotenv import load_dotenv
from logger import get_logger

log = get_logger("hyperlink_validator")

load_dotenv()

REQUEST_TIMEOUT = 10
MAX_REDIRECTS   = 5

# Browser-like headers prevent CDN bot-blocks (403s) on legitimate news sites.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Only 404 (Not Found) and 410 (Gone) definitively mean the article no longer
# exists. Every other status — including 403 (CDN/Cloudflare bot-block), 429
# (rate limit), 5xx (server error) — means the server knows about the URL; it
# is NOT dead. Marking 403 as invalid is the #1 cause of false invalids because
# most major news sites (FinTech Magazine, Yahoo Finance, Construction News etc.)
# block automated requests but serve content fine to real browsers.
DEAD_STATUSES = {404, 410}

# DNS-failure substrings that indicate the domain itself doesn't resolve.
# A DNS failure is the one case other than 404/410 where we can call a URL dead.
_DNS_ERRORS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated with hostname",
)


async def _http_check(url: str) -> dict:
    """
    Two-phase HTTP check: HEAD then GET.

    Decision table:
      - 200 on HEAD or GET         -> ok
      - 404 / 410 on HEAD or GET   -> invalid  (article is genuinely gone)
      - 403 / 405 / 429 / 5xx      -> ok       (bot-blocked but URL exists)
      - Timeout / read error        -> ok       (benefit of the doubt;
                                                 Cloudflare hangs bots silently)
      - DNS failure                 -> invalid  (domain does not exist)
      - Too many redirects          -> invalid  (redirect loop)
    """
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers=BROWSER_HEADERS,
        ) as client:

            # Phase 1: HEAD — fast, no body download
            try:
                resp = await client.head(url)
                code = resp.status_code
                if code == 200:
                    final = str(resp.url)
                    return {
                        "status":      "redirect" if final != url else "ok",
                        "final_url":   final,
                        "status_code": code,
                    }
                if code in DEAD_STATUSES:
                    return {"status": "invalid", "final_url": url, "status_code": code}
                # 403 / 405 / 429 / 5xx / other -> fall through to GET
            except httpx.TooManyRedirects:
                return {"status": "invalid", "final_url": url, "status_code": 0}
            except Exception:
                pass  # connection error on HEAD -> try GET

            # Phase 2: GET — handles sites that block HEAD requests
            try:
                resp = await client.get(url)
                code = resp.status_code
                final = str(resp.url)
                if code == 200:
                    return {
                        "status":      "redirect" if final != url else "ok",
                        "final_url":   final,
                        "status_code": code,
                    }
                if code in DEAD_STATUSES:
                    return {"status": "invalid", "final_url": url, "status_code": code}
                # 403, 429, 5xx etc. -> bot-blocked but URL exists
                return {"status": "ok", "final_url": final, "status_code": code}

            except httpx.TooManyRedirects:
                return {"status": "invalid", "final_url": url, "status_code": 0}
            except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError):
                # Cloudflare and similar CDNs silently drop/hang bot connections.
                # A timeout does NOT mean the URL is dead — give benefit of the doubt.
                return {"status": "ok", "final_url": url, "status_code": 0}

    except httpx.ConnectError as e:
        err = str(e).lower()
        if any(marker in err for marker in _DNS_ERRORS):
            # Domain truly doesn't resolve -> dead URL
            return {"status": "invalid", "final_url": url, "status_code": 0}
        # Connection refused, SSL error etc. -> benefit of the doubt
        return {"status": "ok", "final_url": url, "status_code": 0}
    except Exception:
        return {"status": "ok", "final_url": url, "status_code": 0}


async def validate_hyperlinks(articles: List[dict]) -> List[dict]:
    """
    Hyperlink validation per article.

    Only HTTP 404, HTTP 410, and DNS-failure URLs are marked 'invalid'.
    All other results — including 403 bot-blocks, timeouts, and 5xx errors —
    are treated as 'ok' because the server confirmed the URL exists (or we
    cannot determine it is dead without a real browser rendering the page).

    Redirected URLs have their `url` field updated to the final destination.
    """
    ok      = 0
    invalid = 0
    results = []

    for art in articles:
        url = art.get("url", "")

        if not url:
            results.append({**art, "url_status": "invalid"})
            invalid += 1
            continue

        check = await _http_check(url)

        if check["status"] == "invalid":
            art = {**art, "url_status": "invalid"}
            invalid += 1
        else:
            # ok or redirect — update url to final destination if redirected
            art = {
                **art,
                "url":        check["final_url"],
                "url_status": check["status"],   # "ok" or "redirect"
            }
            ok += 1

        results.append(art)

    log.info(f"{len(articles)} checked -> {ok} ok | {invalid} invalid")
    return results


async def revalidate_stored_article(url: str) -> str:
    """
    Re-check a single stored article URL.  Used by the /revalidate-urls endpoint
    to fix articles that were incorrectly marked invalid in a previous run.

    Returns the new url_status string: 'ok', 'redirect', or 'invalid'.
    """
    if not url:
        return "invalid"
    check = await _http_check(url)
    return check["status"]
