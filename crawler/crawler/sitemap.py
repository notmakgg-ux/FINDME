"""
Sitemap and robots.txt discovery for intelligent URL collection.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx

from config import settings

logger = logging.getLogger(__name__)

STANDARD_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemaps.xml",
    "/robots.txt",
]


async def fetch_robots_txt(base_url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch and return robots.txt content."""
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    try:
        resp = await client.get(robots_url, timeout=10)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug(f"Failed to fetch robots.txt for {base_url}: {e}")
    return None


def extract_sitemap_urls_from_robots(robots_text: str) -> list[str]:
    """Extract sitemap URLs from robots.txt content."""
    sitemap_urls = []
    for line in robots_text.split("\n"):
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                sitemap_urls.append(url)
    return sitemap_urls


async def fetch_sitemap(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch sitemap XML content."""
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug(f"Failed to fetch sitemap {url}: {e}")
    return None


def parse_sitemap_xml(xml_content: str, base_url: str) -> list[str]:
    """Parse sitemap XML and extract URLs. Supports sitemap indexes."""
    urls = []

    try:
        soup = BeautifulSoup(xml_content, "lxml-xml")
    except Exception:
        try:
            soup = BeautifulSoup(xml_content, "xml")
        except Exception:
            soup = BeautifulSoup(xml_content, "lxml")

    # Check if this is a sitemap index
    sitemap_tags = soup.find_all("sitemap")
    if sitemap_tags:
        for tag in sitemap_tags:
            loc = tag.find("loc")
            if loc and loc.text:
                urls.append(loc.text.strip())
        return urls  # Return nested sitemap URLs

    # Regular sitemap with URLs
    url_tags = soup.find_all("url")
    for tag in url_tags:
        loc = tag.find("loc")
        if loc and loc.text:
            urls.append(loc.text.strip())

    return urls


def filter_relevant_urls(urls: list[str], base_url: str) -> list[str]:
    """Filter sitemap URLs to only include relevant pages."""
    from config import PRIORITY_KEYWORDS, LOW_PRIORITY_PATTERNS
    from utils.urls import is_same_domain, normalize_url

    relevant = []
    all_keywords = []
    for keywords in PRIORITY_KEYWORDS.values():
        all_keywords.extend(keywords)

    for url in urls:
        if not is_same_domain(url, base_url):
            continue

        url_lower = url.lower()

        # Skip obviously irrelevant files
        skip = False
        for pattern in LOW_PRIORITY_PATTERNS:
            if pattern in url_lower:
                skip = True
                break
        if skip:
            continue

        # Check if URL contains any priority keywords
        path = urlparse(url).path.lower()
        for kw in all_keywords:
            if kw in path:
                relevant.append(normalize_url(url))
                break
        else:
            # Include homepage and root-level pages
            parts = path.strip("/").split("/")
            if len(parts) <= 2 and parts[0]:
                relevant.append(normalize_url(url))

    return list(set(relevant))


async def discover_sitemap_urls(base_url: str, client: httpx.AsyncClient) -> list[str]:
    """Discover URLs from robots.txt and sitemaps."""
    all_urls = []

    # Step 1: Try robots.txt
    robots_text = await fetch_robots_txt(base_url, client)
    if robots_text:
        sitemap_urls = extract_sitemap_urls_from_robots(robots_text)
        all_urls.extend(sitemap_urls)

    # Step 2: Try standard sitemap locations
    for path in STANDARD_SITEMAP_PATHS:
        if path == "/robots.txt":
            continue
        sitemap_url = f"{base_url.rstrip('/')}{path}"
        if sitemap_url not in all_urls:
            all_urls.append(sitemap_url)

    # Step 3: Fetch and parse each sitemap
    discovered_urls = []
    processed = set()

    for sitemap_url in all_urls:
        if sitemap_url in processed:
            continue
        processed.add(sitemap_url)

        xml = await fetch_sitemap(sitemap_url, client)
        if not xml:
            continue

        urls = parse_sitemap_xml(xml, base_url)

        # If we got sitemap index entries, fetch those too
        if urls and any("sitemap" in u.lower() for u in urls):
            for nested_url in urls:
                if nested_url not in processed:
                    processed.add(nested_url)
                    nested_xml = await fetch_sitemap(nested_url, client)
                    if nested_xml:
                        nested_urls = parse_sitemap_xml(nested_xml, base_url)
                        discovered_urls.extend(nested_urls)
        else:
            discovered_urls.extend(urls)

    # Step 4: Filter to relevant URLs
    filtered = filter_relevant_urls(discovered_urls, base_url)
    logger.info(f"Discovered {len(filtered)} relevant URLs from sitemaps for {base_url}")
    return filtered
