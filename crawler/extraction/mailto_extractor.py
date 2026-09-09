"""
Extract email addresses from mailto: links in HTML.
Highest confidence extraction method.
"""

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod


def extract_mailto_emails(html: str, source_url: str = "") -> list[dict]:
    """
    Extract emails from mailto: links.

    Returns list of dicts with:
        email, extraction_method, source_url, html_context
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href.lower().startswith("mailto:"):
            continue

        # Extract email from mailto: link
        email_part = href[7:]  # Remove "mailto:"
        # Remove query parameters (subject, body, etc.)
        email = email_part.split("?")[0].strip()
        email = unquote(email).strip().lower()

        if not email or "@" not in email:
            continue

        if email in seen:
            continue
        seen.add(email)

        # Get the visible text (often contains the person's name)
        visible_text = tag.get_text(strip=True)

        # Get HTML context (parent element text)
        parent = tag.parent
        html_context = ""
        if parent:
            html_context = parent.get_text(separator=" ", strip=True)[:500]

        results.append({
            "email": email,
            "extraction_method": ExtractionMethod.MAILTO,
            "source_url": source_url,
            "nearby_text": visible_text,
            "html_context": html_context,
        })

    return results
