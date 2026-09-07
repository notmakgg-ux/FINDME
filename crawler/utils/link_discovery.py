"""
Internal link discovery from HTML content.
"""

from urllib.parse import urlparse

from bs4 import BeautifulSoup

from utils.urls import make_absolute_url, is_same_domain


def extract_internal_links(html: str, base_url: str) -> list[str]:
    """Extract all internal links from HTML content."""
    soup = BeautifulSoup(html, "lxml")
    links = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        absolute = make_absolute_url(base_url, href)
        if not absolute:
            continue

        if is_same_domain(absolute, base_url):
            links.add(absolute)

    return sorted(links)


def extract_emails_from_page(html: str) -> list[str]:
    """Quick extraction of visible email addresses from HTML."""
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "lxml")
    emails = set()

    # From mailto links
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if email:
                emails.add(email.lower())

    # From visible text
    text = soup.get_text()
    email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
    for match in email_pattern.finditer(text):
        emails.add(match.group().lower())

    return list(emails)
