"""
Page type classification for crawled pages.
Classifies pages as contact, team, agent_profile, etc.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from config import PAGE_TYPE_KEYWORDS
from models.schemas import PageType


def classify_page(url: str, html: str = "", title: str = "") -> PageType:
    """
    Classify a page's type based on URL, title, and content.

    Uses multiple signals:
        1. URL path patterns (strongest signal)
        2. Page title
        3. Heading text
        4. Metadata
    """
    # --- Signal 1: URL path ---
    url_type = _classify_by_url(url)
    if url_type != PageType.OTHER:
        return url_type

    # --- Signal 2: Title ---
    if title:
        title_type = _classify_by_text(title.lower())
        if title_type != PageType.OTHER:
            return title_type

    # --- Signal 3: HTML content ---
    if html:
        content_type = _classify_by_content(html)
        if content_type != PageType.OTHER:
            return content_type

    return PageType.OTHER


def _classify_by_url(url: str) -> PageType:
    """Classify page type based on URL path."""
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")

    if not path or path == "":
        return PageType.HOMEPAGE

    for page_type_name, keywords in PAGE_TYPE_KEYWORDS.items():
        for kw in keywords:
            if f"/{kw}" in path or path.endswith(kw):
                try:
                    return PageType(page_type_name)
                except ValueError:
                    continue

    # Check for agent profile patterns
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        if parts[0] in ("agents", "agent", "realtors", "brokers", "team"):
            # Check second part isn't a known sub-section
            if parts[1] not in ("page", "search", "login", "register", "create"):
                if page_type_name == "agent_profile":
                    return PageType.AGENT_PROFILE
                return PageType.AGENT_PROFILE

    return PageType.OTHER


def _classify_by_text(text: str) -> PageType:
    """Classify by page title or heading text."""
    contact_signals = ["contact", "get in touch", "reach us", "email us", "call us"]
    team_signals = ["team", "our team", "staff", "people", "leadership", "meet the team"]
    about_signals = ["about", "our story", "company", "who we are", "our mission"]
    agent_signals = ["agents", "find an agent", "our agents", "realtors", "brokers"]

    for signal in contact_signals:
        if signal in text:
            return PageType.CONTACT
    for signal in team_signals:
        if signal in text:
            return PageType.TEAM
    for signal in about_signals:
        if signal in text:
            return PageType.ABOUT
    for signal in agent_signals:
        if signal in text:
            return PageType.AGENT_DIRECTORY

    return PageType.OTHER


def _classify_by_content(html: str) -> PageType:
    """Classify by page content analysis."""
    soup = BeautifulSoup(html, "lxml")

    # Check main heading
    for tag in soup.find_all(["h1", "h2"]):
        text = tag.get_text(strip=True).lower()
        result = _classify_by_text(text)
        if result != PageType.OTHER:
            return result

    # Check page title
    title_tag = soup.find("title")
    if title_tag:
        result = _classify_by_text(title_tag.get_text(strip=True).lower())
        if result != PageType.OTHER:
            return result

    # Check meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        result = _classify_by_text(meta.get("content", "").lower())
        if result != PageType.OTHER:
            return result

    return PageType.OTHER
