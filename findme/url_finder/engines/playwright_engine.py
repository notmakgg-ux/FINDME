"""
Playwright-based search engine for real estate URL discovery.

Uses a real headless Chromium browser (via Playwright) to search Google
and Bing — no API keys, no ddgs/httpx H2 errors, no rate-limit bans.

Strategy:
  1. Try Google (primary)  — paginate up to `max_pages` result pages
  2. Fall back to Bing      — if Google blocks or returns < 5 results
  3. Extract organic result hrefs from known CSS selectors
  4. Respect request_delay between pages (cooperative control aware)
"""

from __future__ import annotations

import random
import time
import logging
from typing import Any
from urllib.parse import urlparse, quote_plus

from engines.base import BaseEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSS selectors for organic result links (regularly maintained)
# ---------------------------------------------------------------------------

_GOOGLE_SELECTORS = [
    "div#search a[href]",          # primary container
    "div.g a[href]",               # classic result card
    "div[data-sokoban-container] a[href]",  # newer layout
    "h3 + div a[href]",
    "a[ping]",                     # Google marks outbound links with ping=
]

_BING_SELECTORS = [
    "li.b_algo h2 a[href]",        # Bing organic results
    "li.b_algo a[href]",
    "div.b_title a[href]",
    "#b_results h2 a[href]",
]

# Patterns that indicate a link is an organic result, not UI chrome
_SKIP_DOMAINS = {
    "google.com", "goo.gl", "google.co", "googleapis.com",
    "bing.com", "microsoftonline.com", "microsoft.com",
    "youtube.com", "accounts.google.com", "support.google.com",
    "maps.google.com", "play.google.com",
}

# Realistic browser viewport & user-agent
_VIEWPORT = {"width": 1366, "height": 768}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_organic(href: str) -> bool:
    """Return True if the URL looks like an organic search result."""
    if not href or not href.startswith("http"):
        return False
    try:
        parsed = urlparse(href)
        domain = parsed.netloc.lower().lstrip("www.")
        if any(skip in domain for skip in _SKIP_DOMAINS):
            return False
        # Must have a real hostname
        if not parsed.netloc or "." not in parsed.netloc:
            return False
        return True
    except Exception:
        return False


def _extract_links_from_page(page) -> list[dict[str, str]]:
    """Extract organic result links from an already-loaded Playwright page."""
    results: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()

    # Try Google selectors first, then Bing
    for selector in _GOOGLE_SELECTORS + _BING_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
            for el in elements:
                href = el.get_attribute("href") or ""
                if href in seen_hrefs or not _is_organic(href):
                    continue
                seen_hrefs.add(href)
                # Get visible text (title)
                try:
                    title = el.inner_text().strip()[:120]
                except Exception:
                    title = ""
                # Look for a sibling/parent snippet
                snippet = ""
                try:
                    parent = el.evaluate(
                        "el => el.closest('li, div.g, div[data-sokoban-container]')"
                        "?.innerText || ''"
                    )
                    if parent:
                        snippet = parent[:200]
                except Exception:
                    pass
                results.append({
                    "url": href,
                    "title": title,
                    "snippet": snippet[:200],
                })
        except Exception:
            continue

    return results


class PlaywrightEngine(BaseEngine):
    """
    Search engine using Playwright headless Chromium.

    Searches Google (primary) with Bing as fallback when Google yields
    too few results or detects a bot.
    """

    name = "playwright"

    def __init__(self, request_delay: float = 2.0):
        self._delay = request_delay

    # ------------------------------------------------------------------
    # BaseEngine interface
    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        """
        Synchronous search — runs Playwright in a new browser context.
        Returns list of {url, title, snippet} dicts.
        """
        results: list[dict[str, Any]] = []

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            return []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = browser.new_context(
                viewport=_VIEWPORT,
                user_agent=_USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
                java_script_enabled=True,
                # Block images/fonts/media to speed things up
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            ctx.set_default_timeout(20_000)  # 20s per action

            try:
                results = self._search_google(ctx, query, max_results)
                if len(results) < 5:
                    logger.info(f"Google returned {len(results)} results for '{query[:40]}', trying Bing")
                    bing_results = self._search_bing(ctx, query, max_results)
                    # Merge, dedup by URL
                    seen = {r["url"] for r in results}
                    for r in bing_results:
                        if r["url"] not in seen:
                            results.append(r)
                            seen.add(r["url"])
            except Exception as e:
                logger.warning(f"Playwright search failed for '{query[:40]}': {e}")
                # Last-ditch: try Bing directly
                try:
                    results = self._search_bing(ctx, query, max_results)
                except Exception as e2:
                    logger.error(f"Bing fallback also failed: {e2}")
            finally:
                try:
                    ctx.close()
                    browser.close()
                except Exception:
                    pass

        return results[:max_results]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_google(self, ctx, query: str, max_results: int) -> list[dict]:
        """Search Google and paginate to collect up to max_results links."""
        results: list[dict] = []
        page = ctx.new_page()

        # Dismiss automation detection
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        try:
            encoded = quote_plus(query)
            url = f"https://www.google.com/search?q={encoded}&hl=en&gl=us&num=10"

            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            self._dismiss_cookie_banner(page)
            self._check_captcha(page, "Google")

            # Page 1
            page_links = _extract_links_from_page(page)
            results.extend(page_links)

            # Paginate if we need more
            page_num = 2
            while len(results) < max_results and page_num <= 4:
                try:
                    next_btn = page.query_selector("a#pnnext, a[aria-label='Next page']")
                    if not next_btn:
                        break
                    next_btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    self._sleep_jitter()
                    more = _extract_links_from_page(page)
                    if not more:
                        break
                    seen = {r["url"] for r in results}
                    results.extend(r for r in more if r["url"] not in seen)
                    page_num += 1
                except Exception:
                    break
        finally:
            try:
                page.close()
            except Exception:
                pass

        return results

    def _search_bing(self, ctx, query: str, max_results: int) -> list[dict]:
        """Search Bing as a fallback."""
        results: list[dict] = []
        page = ctx.new_page()

        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        try:
            encoded = quote_plus(query)
            url = f"https://www.bing.com/search?q={encoded}&setlang=en-us&count=20"

            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            self._sleep_jitter(0.5)

            page_links = _extract_links_from_page(page)
            results.extend(page_links)

            # Paginate once if needed
            if len(results) < max_results:
                try:
                    next_btn = page.query_selector("a.sb_pagN, a[title='Next page']")
                    if next_btn:
                        next_btn.click()
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        self._sleep_jitter()
                        more = _extract_links_from_page(page)
                        seen = {r["url"] for r in results}
                        results.extend(r for r in more if r["url"] not in seen)
                except Exception:
                    pass
        finally:
            try:
                page.close()
            except Exception:
                pass

        return results

    def _dismiss_cookie_banner(self, page):
        """Dismiss GDPR/cookie consent overlays if present."""
        dismiss_selectors = [
            "button[id*='accept']", "button[id*='agree']",
            "button[class*='accept']", "button[class*='agree']",
            "#L2AGLb",          # Google's "Accept all" button
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Reject all')",  # Some show this
        ]
        for sel in dismiss_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(timeout=3_000)
                    time.sleep(0.5)
                    return
            except Exception:
                continue

    def _check_captcha(self, page, engine_name: str):
        """Log a warning if a CAPTCHA page is detected."""
        try:
            title = page.title().lower()
            content = page.content().lower()
            if "captcha" in title or "captcha" in content or "unusual traffic" in content:
                logger.warning(
                    f"{engine_name} CAPTCHA detected for query. "
                    "Results may be empty. Consider adding longer delays."
                )
        except Exception:
            pass

    def _sleep_jitter(self, base: float | None = None):
        """Sleep with jitter to appear more human."""
        wait = (base if base is not None else self._delay) + random.uniform(0.3, 1.2)
        time.sleep(wait)
