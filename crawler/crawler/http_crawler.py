"""
Asynchronous HTTP crawler — now backed by Scrapling for anti-bot bypass.

Uses ScraplingFetcher (micro-level) for stealth HTTP with TLS fingerprint
impersonation and Cloudflare bypass. Falls back to httpx if Scrapling unavailable.
"""

import asyncio
import logging
import time
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class HTTPCrawler:
    """Async HTTP crawler backed by Scrapling with fallback to httpx."""

    def __init__(self):
        self._scrapling = None
        self._httpx_client = None
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._stats = {"requests": 0, "success": 0, "failed": 0, "retries": 0,
                       "fast_ok": 0, "stealth_ok": 0, "dynamic_ok": 0}
        self._use_scrapling = True

    async def _get_scrapling(self):
        """Get or create ScraplingFetcher instance."""
        if self._scrapling is None:
            try:
                from crawler.scrapling_fetcher import ScraplingFetcher
                self._scrapling = ScraplingFetcher()
            except ImportError:
                logger.warning("Scrapling not available, falling back to httpx")
                self._use_scrapling = False
        return self._scrapling

    async def _get_httpx_client(self):
        """Fallback httpx client."""
        if self._httpx_client is None or self._httpx_client.is_closed:
            import httpx
            self._httpx_client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.http_timeout, connect=10),
                follow_redirects=settings.http_follow_redirects,
                limits=httpx.Limits(
                    max_connections=settings.max_concurrent_requests,
                    max_keepalive_connections=20,
                    keepalive_expiry=30,
                ),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        return self._httpx_client

    def _get_domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._domain_semaphores:
            self._domain_semaphores[domain] = asyncio.Semaphore(
                settings.max_concurrent_per_domain
            )
        return self._domain_semaphores[domain]

    async def fetch(self, url: str) -> Optional[object]:
        """Fetch a URL using Scrapling (with httpx fallback). Returns httpx-compatible response."""
        from utils.urls import get_root_domain

        domain = get_root_domain(url)
        domain_sem = self._get_domain_semaphore(domain)

        async with self._semaphore:
            async with domain_sem:
                self._stats["requests"] += 1

                # Try Scrapling first
                if self._use_scrapling:
                    try:
                        scrapling = await self._get_scrapling()
                        result = await scrapling.fetch(url)
                        if result.success:
                            if result.fetcher_used == "fast":
                                self._stats["fast_ok"] += 1
                            elif result.fetcher_used == "stealth":
                                self._stats["stealth_ok"] += 1
                            else:
                                self._stats["dynamic_ok"] += 1
                            self._stats["success"] += 1
                            # Return httpx-compatible object
                            return _ScraplingResponse(result)
                    except Exception as e:
                        logger.debug(f"Scrapling failed for {url}, falling back to httpx: {e}")

                # Fallback to httpx
                return await self._fetch_httpx(url)

    async def _fetch_httpx(self, url: str):
        """Fallback: fetch with httpx with retries."""
        import httpx
        from tenacity import retry, stop_after_attempt, wait_exponential

        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
        async def _do_fetch():
            client = await self._get_httpx_client()
            try:
                response = await client.get(url)
                self._stats["success"] += 1
                return response
            except (httpx.TimeoutException, httpx.RequestError) as e:
                self._stats["retries"] += 1
                raise

        try:
            return await _do_fetch()
        except Exception as e:
            self._stats["failed"] += 1
            logger.debug(f"All fetch attempts failed for {url}: {e}")
            return None

    async def fetch_many(self, urls: list[str]) -> dict[str, Optional[object]]:
        """Fetch multiple URLs concurrently."""
        tasks = {url: asyncio.create_task(self.fetch(url)) for url in urls}
        results = {}
        for url, task in tasks.items():
            try:
                results[url] = await task
            except Exception:
                results[url] = None
        return results

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    async def close(self):
        if self._scrapling:
            await self._scrapling.close()
        if self._httpx_client and not self._httpx_client.is_closed:
            await self._httpx_client.aclose()


class _ScraplingResponse:
    """Wrapper that makes ScraplingFetcher.FetchResult look like httpx.Response."""
    def __init__(self, fetch_result):
        self.status_code = fetch_result.status_code
        self.text = fetch_result.html
        self.url = fetch_result.url
        self._fetcher = fetch_result.fetcher_used

    @property
    def headers(self):
        return {}
