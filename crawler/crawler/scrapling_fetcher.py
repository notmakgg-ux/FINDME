"""
Micro-level fetcher using Scrapling.

Handles:
- Stealth HTTP requests with TLS fingerprint impersonation
- Anti-bot bypass (Cloudflare Turnstile, etc.)
- Browser-based fallback for JS-heavy pages
- Session management across requests
- Domain-aware fetch strategies
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result from a Scrapling fetch."""
    url: str
    status_code: int
    html: str
    success: bool
    fetcher_used: str  # "fast", "stealth", "dynamic"
    error: Optional[str] = None


class ScraplingFetcher:
    """
    Micro-level fetcher that picks the right Scrapling strategy per URL.

    Strategy:
    1. Try fast HTTP first (Fetcher) — TLS fingerprint impersonation
    2. If blocked/failed, try StealthyFetcher — anti-bot bypass
    3. If still failed and enabled, try DynamicFetcher — full browser
    """

    def __init__(self):
        self._fast_session = None
        self._stealth_session = None
        self._stats = {
            "requests": 0, "fast_ok": 0, "stealth_ok": 0,
            "dynamic_ok": 0, "failed": 0
        }

    async def _get_fast_session(self):
        """Get or create a Scrapling Fetcher session."""
        if self._fast_session is None:
            from scrapling.fetchers import FetcherSession
            self._fast_session = FetcherSession(impersonate='chrome')
        return self._fast_session

    async def _get_stealth_session(self):
        """Get or create a Scrapling StealthyFetcher session."""
        if self._stealth_session is None:
            from scrapling.fetchers import StealthySession
            self._stealth_session = StealthySession(
                headless=True,
                solve_cloudflare=True,
            )
        return self._stealth_session

    async def fetch(self, url: str) -> FetchResult:
        """
        Fetch a URL using the best strategy.
        Tries fast → stealth → dynamic (if enabled).
        """
        self._stats["requests"] += 1

        # Strategy 1: Fast HTTP with TLS impersonation
        result = await self._fetch_fast(url)
        if result.success:
            self._stats["fast_ok"] += 1
            return result

        # Strategy 2: Stealth fetcher (anti-bot bypass)
        result = await self._fetch_stealth(url)
        if result.success:
            self._stats["stealth_ok"] += 1
            return result

        # Strategy 3: Dynamic/browser fallback (if enabled)
        if settings.enable_playwright_fallback:
            result = await self._fetch_dynamic(url)
            if result.success:
                self._stats["dynamic_ok"] += 1
                return result

        self._stats["failed"] += 1
        return FetchResult(
            url=url, status_code=0, html="",
            success=False, fetcher_used="none",
            error=f"All fetch strategies failed for {url}"
        )

    async def _fetch_fast(self, url: str) -> FetchResult:
        """Fast HTTP with TLS fingerprint impersonation."""
        try:
            from scrapling.fetchers import Fetcher
            page = await asyncio.to_thread(
                Fetcher.get,
                url,
            )
            if page.status and page.status < 400:
                return FetchResult(
                    url=url, status_code=page.status,
                    html=str(page.html_content) if hasattr(page, 'html_content') else page.text,
                    success=True, fetcher_used="fast"
                )
            return FetchResult(
                url=url, status_code=page.status or 0, html="",
                success=False, fetcher_used="fast",
                error=f"HTTP {page.status}"
            )
        except Exception as e:
            logger.debug(f"Fast fetch failed for {url}: {e}")
            return FetchResult(
                url=url, status_code=0, html="",
                success=False, fetcher_used="fast", error=str(e)
            )

    async def _fetch_stealth(self, url: str) -> FetchResult:
        """Stealth fetcher — bypasses Cloudflare and anti-bot."""
        try:
            from scrapling.fetchers import StealthyFetcher
            page = await asyncio.to_thread(
                StealthyFetcher.fetch,
                url,
                headless=True,
                network_idle=True,
            )
            if page.status and page.status < 400:
                return FetchResult(
                    url=url, status_code=page.status,
                    html=str(page.html_content) if hasattr(page, 'html_content') else page.text,
                    success=True, fetcher_used="stealth"
                )
            return FetchResult(
                url=url, status_code=page.status or 0, html="",
                success=False, fetcher_used="stealth",
                error=f"HTTP {page.status}"
            )
        except Exception as e:
            logger.debug(f"Stealth fetch failed for {url}: {e}")
            return FetchResult(
                url=url, status_code=0, html="",
                success=False, fetcher_used="stealth", error=str(e)
            )

    async def _fetch_dynamic(self, url: str) -> FetchResult:
        """Dynamic/browser fetcher — full Playwright rendering."""
        try:
            from scrapling.fetchers import DynamicFetcher
            page = await asyncio.to_thread(
                DynamicFetcher.fetch,
                url,
                headless=True,
                network_idle=True,
            )
            if page.status and page.status < 400:
                return FetchResult(
                    url=url, status_code=page.status,
                    html=str(page.html_content) if hasattr(page, 'html_content') else page.text,
                    success=True, fetcher_used="dynamic"
                )
            return FetchResult(
                url=url, status_code=page.status or 0, html="",
                success=False, fetcher_used="dynamic",
                error=f"HTTP {page.status}"
            )
        except Exception as e:
            logger.debug(f"Dynamic fetch failed for {url}: {e}")
            return FetchResult(
                url=url, status_code=0, html="",
                success=False, fetcher_used="dynamic", error=str(e)
            )

    async def fetch_many(self, urls: list[str]) -> dict[str, FetchResult]:
        """Fetch multiple URLs with per-domain concurrency limits."""
        # Group by domain for per-domain throttling
        from utils.urls import get_root_domain
        domain_groups: dict[str, list[str]] = {}
        for url in urls:
            domain = get_root_domain(url)
            domain_groups.setdefault(domain, []).append(url)

        results = {}
        # Fetch per-domain with concurrency limit
        sem = asyncio.Semaphore(settings.max_concurrent_per_domain)

        async def _fetch_with_sem(url):
            async with sem:
                return url, await self.fetch(url)

        tasks = [_fetch_with_sem(url) for url in urls]
        for coro in asyncio.as_completed(tasks):
            try:
                url, result = await coro
                results[url] = result
            except Exception as e:
                logger.debug(f"Fetch task error: {e}")

        return results

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    async def close(self):
        """Clean up sessions."""
        if self._fast_session:
            try:
                await asyncio.to_thread(self._fast_session.close)
            except Exception:
                pass
        if self._stealth_session:
            try:
                await asyncio.to_thread(self._stealth_session.close)
            except Exception:
                pass
