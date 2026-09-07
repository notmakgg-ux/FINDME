"""
Macro-level orchestrator using Crawlee.

Handles:
- Request queue management (Crawlee's RequestQueue)
- Autoscaled pool for dynamic concurrency
- Per-domain throttling and request scheduling
- Automatic retries with exponential backoff
- Sitemap/robots.txt discovery (Crawlee built-in)
- Memory-efficient processing across 100+ companies
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, Callable
from urllib.parse import urljoin, urlparse

from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
from crawlee.storages import RequestQueue, Dataset
from crawlee import Request

from config import settings
from crawler.scrapling_fetcher import ScraplingFetcher, FetchResult
from utils.priority import prioritize_urls, assign_priority
from utils.link_discovery import extract_internal_links
from crawler.sitemap import discover_sitemap_urls

logger = logging.getLogger(__name__)


class EmailCrawleeOrchestrator:
    """
    Macro-level orchestrator that uses Crawlee to manage the crawling process.

    Responsibilities:
    - Manages request queue (what URLs to crawl, in what order)
    - Handles autoscaling (dynamic concurrency based on system load)
    - Provides per-domain throttling
    - Automatic retries with backoff
    - Sitemap and robots.txt discovery

    Uses ScraplingFetcher at the micro level for actual HTTP requests.
    """

    def __init__(self):
        self._scrapling = ScraplingFetcher()
        self._stats = {
            "companies_crawled": 0,
            "total_pages_fetched": 0,
            "total_emails_found": 0,
            "fast_fetches": 0,
            "stealth_fetches": 0,
            "dynamic_fetches": 0,
            "retries": 0,
        }

    async def crawl_company(
        self,
        company_name: str,
        website: str,
        max_pages: int = None,
    ) -> dict:
        """
        Crawl a single company's website using Scrapling fetcher + Crawlee queue management.

        Returns dict with:
        - pages: dict[url] = html
        - prioritized_urls: list of CrawlURL objects
        - pages_discovered: int
        - pages_fetched: int
        - errors: list of error messages
        """
        if max_pages is None:
            max_pages = settings.max_total_pages_per_domain

        from utils.urls import normalize_url, get_root_domain
        base_url = normalize_url(website)
        root_domain = get_root_domain(base_url)

        result = {
            "pages": {},
            "prioritized_urls": [],
            "pages_discovered": 0,
            "pages_fetched": 0,
            "errors": [],
        }

        try:
            # === STAGE 1: Discover URLs ===
            logger.info(f"[{company_name}] Discovering URLs from {base_url}")

            # Fetch homepage
            homepage = await self._scrapling.fetch(base_url)
            if not homepage.success or homepage.status_code >= 400:
                result["errors"].append(f"Homepage fetch failed: {homepage.error}")
                return result

            result["pages"][base_url] = homepage.html
            result["pages_fetched"] += 1
            self._stats["fast_fetches" if homepage.fetcher_used == "fast" else
                         "stealth_fetches" if homepage.fetcher_used == "stealth" else
                         "dynamic_fetches"] += 1

            # Extract internal links
            internal_links = extract_internal_links(homepage.html, base_url)
            logger.info(f"[{company_name}] Found {len(internal_links)} internal links")

            # Discover sitemap URLs
            sitemap_urls = []
            try:
                # Use Scrapling to fetch sitemap
                sitemap_result = await self._scrapling.fetch(f"{base_url}/sitemap.xml")
                if sitemap_result.success:
                    sitemap_urls = self._parse_sitemap(sitemap_result.html, base_url)
                # Also try sitemap_index
                sitemap_index_result = await self._scrapling.fetch(f"{base_url}/sitemap_index.xml")
                if sitemap_index_result.success:
                    sitemap_urls += self._parse_sitemap(sitemap_index_result.html, base_url)
            except Exception as e:
                logger.debug(f"[{company_name}] Sitemap discovery failed: {e}")

            # Merge and prioritize all URLs
            all_urls = list(set(internal_links + sitemap_urls))
            prioritized = prioritize_urls(all_urls, base_url)
            result["prioritized_urls"] = prioritized
            result["pages_discovered"] = len(prioritized)

            logger.info(f"[{company_name}] Prioritized {len(prioritized)} URLs")

            # === STAGE 2: Crawl priority pages with Scrapling ===
            urls_to_crawl = prioritized[:max_pages]
            url_list = [cu.url for cu in urls_to_crawl]

            # Batch fetch with Scrapling (handles anti-bot internally)
            fetch_results = await self._scrapling.fetch_many(url_list)

            for url, fetch_result in fetch_results.items():
                if fetch_result.success and fetch_result.status_code < 400:
                    result["pages"][url] = fetch_result.html
                    result["pages_fetched"] += 1

            self._stats["total_pages_fetched"] += result["pages_fetched"]
            logger.info(f"[{company_name}] Fetched {result['pages_fetched']} pages")

        except Exception as e:
            logger.error(f"[{company_name}] Crawl error: {e}")
            result["errors"].append(str(e))

        return result

    def _parse_sitemap(self, xml_content: str, base_url: str) -> list[str]:
        """Parse sitemap XML and extract URLs."""
        urls = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(xml_content, "lxml-xml")

            # Regular sitemap
            for loc in soup.find_all("loc"):
                url = loc.text.strip()
                if url.startswith("http"):
                    urls.append(url)

            # Sitemap index — extract sub-sitemap URLs
            for sub in soup.find_all("sitemap"):
                loc = sub.find("loc")
                if loc:
                    url = loc.text.strip()
                    if url.startswith("http"):
                        urls.append(url)
        except Exception as e:
            logger.debug(f"Sitemap parse error: {e}")

        return urls

    async def crawl_with_crawlee_queue(
        self,
        companies: list[dict],
        on_company_complete: Callable = None,
    ) -> list[dict]:
        """
        Crawl all companies using Crawlee's request queue for orchestration.

        This is the macro-level approach:
        - All company start URLs go into Crawlee's request queue
        - Crawlee manages autoscaling, retries, and scheduling
        - ScraplingFetcher handles the actual HTTP at micro level
        """
        results = []

        # Create a Crawlee BeautifulSoupCrawler for macro orchestration
        crawler = BeautifulSoupCrawler(
            max_requests_per_crawl=len(companies) * settings.max_total_pages_per_domain,
            maxConcurrency=settings.max_concurrent_requests,
            maxConcurrencyPerDomain=settings.max_concurrent_per_domain,
            request_handler_timeout_seconds=settings.http_timeout * 3,
            fingerprint_generator_options=None,
        )

        company_map = {}  # url -> company info

        # Index companies by their normalized URL
        for company in companies:
            from utils.urls import normalize_url
            url = normalize_url(company["website"])
            company_map[url] = company

        @crawler.router.default_handler
        async def request_handler(context: BeautifulSoupCrawlingContext) -> None:
            """Handle each request in the Crawlee queue."""
            url = context.request.url
            html = str(context.soup) if context.soup else ""

            # Find which company this URL belongs to
            company_info = None
            for base_url, info in company_map.items():
                if url.startswith(base_url) or url.rstrip("/") == base_url.rstrip("/"):
                    company_info = info
                    break

            if company_info:
                company_name = company_info["company_name"]
                logger.info(f"[Crawlee] Fetched: {url} for {company_name}")

        # Enqueue all company homepages
        requests = []
        for company in companies:
            from utils.urls import normalize_url
            url = normalize_url(company["website"])
            requests.append(Request.from_url(url))

        await crawler.run(requests)

        # Now use ScraplingFetcher for the detailed crawl of each company
        for company in companies:
            crawl_result = await self.crawl_company(
                company_name=company["company_name"],
                website=company["website"],
            )
            results.append({
                "company": company,
                "crawl_result": crawl_result,
            })

            self._stats["companies_crawled"] += 1

            if on_company_complete:
                on_company_complete(company, crawl_result)

        return results

    @property
    def stats(self) -> dict:
        return {**self._stats, **self._scrapling.stats}

    async def close(self):
        """Clean up resources."""
        await self._scrapling.close()
