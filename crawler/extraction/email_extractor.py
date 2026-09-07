"""
Multi-layer email extraction engine.
Combines all extraction methods and deduplicates results.
"""

import re
import html as html_module
from typing import Optional

from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod
from extraction.mailto_extractor import extract_mailto_emails
from extraction.obfuscation import extract_obfuscated_emails
from extraction.structured_data import (
    extract_emails_from_json_ld,
    extract_emails_from_meta_tags,
    extract_emails_from_javascript,
)


# Robust email regex
EMAIL_REGEX = re.compile(
    r'\b[A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?'
    r'@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'(?:\.[A-Za-z]{2,})+\b'
)


def extract_all_emails(html: str, source_url: str = "", page_type: str = "other") -> list[dict]:
    """
    Run all extraction methods and return unified, deduplicated results.

    Pipeline:
        1. mailto: links (highest confidence)
        2. Regex extraction from visible text
        3. Obfuscated email decoding
        4. HTML entity decoding
        5. JSON-LD structured data
        6. Meta tags
        7. JavaScript / inline data

    Returns list of dicts, each with:
        email, extraction_method, source_url, nearby_text, html_context,
        person_name, job_title
    """
    all_results = []
    seen_emails = set()

    # --- Method 1: mailto: links (highest confidence) ---
    mailto_results = extract_mailto_emails(html, source_url)
    for r in mailto_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 2: Visible HTML text regex ---
    regex_results = _extract_from_visible_text(html, source_url)
    for r in regex_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 3: Obfuscated emails ---
    obfuscated_results = extract_obfuscated_emails(html, source_url)
    for r in obfuscated_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 4: HTML attribute extraction ---
    attribute_results = _extract_from_html_attributes(html, source_url)
    for r in attribute_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 5: JSON-LD structured data ---
    jsonld_results = extract_emails_from_json_ld(html, source_url)
    for r in jsonld_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 6: Meta tags ---
    meta_results = extract_emails_from_meta_tags(html, source_url)
    for r in meta_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Method 7: JavaScript / inline data ---
    js_results = extract_emails_from_javascript(html, source_url)
    for r in js_results:
        email = r["email"]
        if email not in seen_emails:
            seen_emails.add(email)
            all_results.append(r)

    # --- Enrich with context ---
    for r in all_results:
        _enrich_context(r, html)

    return all_results


def _extract_from_visible_text(html: str, source_url: str = "") -> list[dict]:
    """Extract emails from visible page text using regex."""
    soup = BeautifulSoup(html, "lxml")

    # Remove script and style tags
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")

    # Decode any HTML entities in the text
    text = html_module.unescape(text)

    results = []
    seen = set()

    for match in EMAIL_REGEX.finditer(text):
        email = match.group().lower()

        if email in seen:
            continue
        seen.add(email)

        # Get surrounding text context
        start = max(0, match.start() - 150)
        end = min(len(text), match.end() + 150)
        nearby = text[start:end].strip()

        results.append({
            "email": email,
            "extraction_method": ExtractionMethod.REGEX,
            "source_url": source_url,
            "nearby_text": nearby[:500],
            "html_context": "",
        })

    return results


def _extract_from_html_attributes(html: str, source_url: str = "") -> list[dict]:
    """Extract emails from HTML attributes (data-email, onclick, etc.)."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for tag in soup.find_all(True):
        for attr_name in ("data-email", "data-contact", "data-contactemail", "data-contact-email"):
            value = tag.get(attr_name, "")
            if value and "@" in str(value):
                # Clean the value
                email = html_module.unescape(str(value)).strip().lower()
                if EMAIL_REGEX.match(email) and email not in seen:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.HTML_ATTRIBUTE,
                        "source_url": source_url,
                        "html_context": f"{tag.name}[{attr_name}]",
                    })

        # Check onclick for email patterns
        onclick = tag.get("onclick", "")
        if onclick and "@" in onclick:
            for match in EMAIL_REGEX.finditer(onclick):
                email = match.group().lower()
                if email not in seen:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.HTML_ATTRIBUTE,
                        "source_url": source_url,
                        "html_context": f"onclick: {onclick[:200]}",
                    })

    return results


def _enrich_context(result: dict, html: str):
    """Enrich a result with person name and job title from nearby DOM context."""
    if result.get("person_name") and result.get("job_title"):
        return  # Already enriched

    soup = BeautifulSoup(html, "lxml")
    email = result["email"]

    # Find the element containing this email
    for tag in soup.find_all(True):
        tag_text = tag.get_text(strip=True)
        tag_html = str(tag)

        if email in tag_text or email in tag_html:
            # Look at siblings and nearby elements for name/title
            parent = tag.parent
            if parent:
                siblings_text = parent.get_text(separator=" | ", strip=True)[:500]
                result["html_context"] = siblings_text
            break
