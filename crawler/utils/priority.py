"""
URL priority assignment for crawl queue management.
"""

from urllib.parse import urlparse

from config import PRIORITY_KEYWORDS, LOW_PRIORITY_PATTERNS
from models.schemas import CrawlURL, PageType


def classify_page_type(url: str) -> PageType:
    """Classify a URL into a page type based on path patterns."""
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")

    if not path or path == "":
        return PageType.HOMEPAGE

    from config import PAGE_TYPE_KEYWORDS

    for page_type_name, keywords in PAGE_TYPE_KEYWORDS.items():
        for kw in keywords:
            if f"/{kw}" in path:
                try:
                    return PageType(page_type_name)
                except ValueError:
                    pass

    # Check for agent profile patterns: /agents/name, /team/name
    parts = path.strip("/").split("/")
    if len(parts) == 2:
        if parts[0] in ("agents", "agent", "team", "realtors", "brokers"):
            if parts[1] not in ("", "page", "search"):
                return PageType.AGENT_PROFILE
        if parts[0] in ("brokerage", "offices"):
            return PageType.AGENT_PROFILE if parts[1] else PageType.OFFICE

    return PageType.OTHER


def assign_priority(url: str) -> int:
    """Assign a priority score to a URL. Higher = crawl first."""
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")

    # Homepage gets highest priority
    if not path or path == "/":
        return 110

    # Check priority keywords (highest match wins)
    best_priority = 0
    for priority, keywords in PRIORITY_KEYWORDS.items():
        for kw in keywords:
            if f"/{kw}" in path:
                best_priority = max(best_priority, priority)

    if best_priority > 0:
        return best_priority

    # Check low-priority patterns
    for pattern in LOW_PRIORITY_PATTERNS:
        if pattern in path:
            return 5

    return 10  # Default for unknown internal pages


def is_low_priority_url(url: str) -> bool:
    """Check if a URL should be skipped entirely."""
    parsed = urlparse(url)
    path = parsed.path.lower()

    for pattern in LOW_PRIORITY_PATTERNS:
        if pattern in path:
            return True

    return False


def prioritize_urls(urls: list[str], base_url: str = "") -> list[CrawlURL]:
    """Sort URLs by priority for crawl queue."""
    crawl_urls = []
    seen = set()

    for url in urls:
        if url in seen:
            continue
        seen.add(url)

        if is_low_priority_url(url):
            continue

        page_type = classify_page_type(url)
        priority = assign_priority(url)

        crawl_urls.append(CrawlURL(
            url=url,
            priority=priority,
            page_type=page_type,
            source_url=base_url,
        ))

    crawl_urls.sort(key=lambda x: x.priority, reverse=True)
    return crawl_urls
