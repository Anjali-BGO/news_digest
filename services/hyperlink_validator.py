import os
import httpx
from typing import List
from dotenv import load_dotenv
from openai import AsyncOpenAI
from logger import get_logger

log = get_logger("hyperlink_validator")

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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


async def _ai_confirm_dead_url(url: str, title: str) -> dict:
    """
    Layer 3 — OpenAI web-search fallback. Only called when the HTTP check
    (layers 1-2) says a URL is dead (404/410/DNS failure/redirect loop), to give
    a genuinely-alive-but-misreported URL a second chance before it gets a
    permanent "broken link" flag.

    "checked" distinguishes a real OpenAI confirmation from a fallback default —
    on any API error we return checked=False and reachable=False, so a transient
    failure here can NEVER silently upgrade an HTTP-confirmed-dead URL to "ok".
    """
    _default = {"reachable": False, "is_archive": False, "canonical_url": None, "checked": False}
    if not url:
        return _default

    prompt = (
        f"Visit this URL and answer the following questions:\n"
        f"1. Is the page live and accessible — not a 404, not a redirect loop, "
        f"not an archive.org or Google cache copy?\n"
        f"2. Is it an archive.org or Google cache copy of the article rather than the live page?\n"
        f"3. If the article moved, what is its current canonical URL? (or SAME)\n\n"
        f"URL: {url}\n"
        f"Reference title (may differ from the live page): {title}\n\n"
        f"Respond with ONLY these fields, no extra prose:\n"
        f"REACHABLE: YES or NO\n"
        f"IS_ARCHIVE: YES or NO\n"
        f"CANONICAL_URL: <url if different, or SAME>"
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

        canonical = parsed.get("CANONICAL_URL", "").strip()
        if canonical.upper() in ("SAME", "UNKNOWN", ""):
            canonical = None

        return {
            "reachable":     parsed.get("REACHABLE", "").upper() == "YES",
            "is_archive":    parsed.get("IS_ARCHIVE", "").upper() == "YES",
            "canonical_url": canonical if canonical and canonical != url else None,
            "checked":       True,
        }
    except Exception as e:
        log.error(f"AI URL confirmation failed for {url[:80]}: {e}")
        return _default


async def validate_hyperlinks(articles: List[dict]) -> List[dict]:
    """
    Hyperlink validation per article — 4 layers:
      1-2. HTTP HEAD -> GET. Only HTTP 404, HTTP 410, and DNS-failure URLs are
           marked dead. Everything else (403 bot-blocks, timeouts, 5xx) is
           treated as 'ok' because the server confirmed the URL exists (or we
           cannot determine it is dead without a real browser rendering the page).
      3.   When HTTP says dead, an OpenAI web-search fallback gets a second
           opinion before the article is permanently flagged 'invalid' — this
           rescues URLs that moved (canonical redirect) or are blocked in ways
           HTTP alone can't distinguish from genuinely gone.
      4.   Content rescue: if the AI fallback finds only an archived/cached copy
           (not a live rescue) but the article already has usable content from
           the original fetch, keep the article tagged 'archive' instead of a
           hard 'invalid' — the live link is gone but the article is still
           summarizable from what we already fetched.

    Redirected/rescued URLs have their `url` field updated to the live destination.
    """
    ok      = 0
    invalid = 0
    rescued = 0
    results = []

    for art in articles:
        url   = art.get("url", "")
        title = art.get("title", "")

        if not url:
            results.append({**art, "url_status": "invalid"})
            invalid += 1
            continue

        check = await _http_check(url)

        if check["status"] != "invalid":
            # ok or redirect — update url to final destination if redirected
            art = {
                **art,
                "url":        check["final_url"],
                "url_status": check["status"],   # "ok" or "redirect"
            }
            ok += 1
            results.append(art)
            continue

        # Layer 3 — HTTP says dead; ask OpenAI for a second opinion before
        # permanently flagging the article as broken.
        ai_result = await _ai_confirm_dead_url(url, title)

        if ai_result["checked"] and ai_result["reachable"] and not ai_result["is_archive"]:
            new_url = ai_result["canonical_url"] or url
            art = {
                **art,
                "url":          new_url,
                "original_url": url if ai_result["canonical_url"] else art.get("original_url", ""),
                "url_status":   "ok",
            }
            ok += 1
            rescued += 1
        elif ai_result["checked"] and ai_result["is_archive"] and (art.get("content") or "").strip():
            # Layer 4 — content rescue: live link is gone, but we already
            # fetched usable content at fetch time, so keep the article
            # (tagged "archive") instead of a hard "broken link" flag.
            art = {**art, "url_status": "archive"}
            invalid += 1
        else:
            # AI safety-net either agreed it's dead, found nothing, or itself
            # errored (checked=False) — never silently upgrade to ok on an
            # unrelated API failure. Trust the HTTP verdict.
            art = {**art, "url_status": "invalid"}
            invalid += 1

        results.append(art)

    log.info(f"{len(articles)} checked -> {ok} ok ({rescued} AI-rescued) | {invalid} invalid")
    return results


async def revalidate_stored_article(url: str) -> str:
    """
    Re-check a single stored article URL.  Used by the /revalidate-urls endpoint
    to fix articles that were incorrectly marked invalid in a previous run.

    Returns the new url_status string: 'ok', 'redirect', 'archive', or 'invalid'.
    """
    if not url:
        return "invalid"
    check = await _http_check(url)
    if check["status"] != "invalid":
        return check["status"]

    ai_result = await _ai_confirm_dead_url(url, "")
    if ai_result["checked"] and ai_result["reachable"] and not ai_result["is_archive"]:
        return "ok"
    if ai_result["checked"] and ai_result["is_archive"]:
        return "archive"
    return "invalid"
