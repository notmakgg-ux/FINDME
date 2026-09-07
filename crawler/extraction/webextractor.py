"""
WebExtractor adapter — lightweight regex-based contact extraction.

Quick pass over HTML to find emails, phones, and social links using
simple regex patterns. Used as the secondary extractor between Scrapling
(primary) and Playwright (fallback).

Inspired by the WebExtractor OSINT tool but integrated as a library module.
"""

import re
from typing import Optional
from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod


# --- Email patterns ---
EMAIL_REGEX = re.compile(
    r'[A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?'
    r'@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'(?:\.[A-Za-z]{2,})+'
)

# Image extensions to filter false positives
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'}

# --- Phone patterns ---
PHONE_REGEX = re.compile(
    r'(?:\+\d{1,3}[\s\-.]?)?'
    r'(?:\(\d{2,4}\)[\s\-.]?)?'
    r'\d{2,4}[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}'
)

# --- Social URL patterns ---
SOCIAL_DOMAINS = {
    "linkedin": ["linkedin.com"],
    "facebook": ["facebook.com", "fb.com", "fb.me"],
    "instagram": ["instagram.com"],
    "twitter": ["twitter.com", "x.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "tiktok": ["tiktok.com"],
}


def webextract_all(
    html: str,
    source_url: str = "",
    extract_emails: bool = True,
    extract_phones: bool = True,
    extract_social: bool = True,
) -> dict:
    """
    Quick WebExtractor-style pass over HTML.

    Returns dict with:
        emails: list of dicts (email, extraction_method, source_url)
        phones: list of dicts (phone, extraction_method, source_url)
        social: list of dicts (platform, url, extraction_method, source_url)
    """
    results = {
        "emails": [],
        "phones": [],
        "social": [],
    }

    if not html:
        return results

    soup = BeautifulSoup(html, "lxml")

    # Get clean text (strip scripts/styles)
    text = soup.get_text(separator=" ")

    # Get raw HTML for attribute parsing
    raw_html = html

    if extract_emails:
        results["emails"] = _extract_emails(text, raw_html, source_url)

    if extract_phones:
        results["phones"] = _extract_phones(text, source_url)

    if extract_social:
        results["social"] = _extract_social(raw_html, source_url)

    return results


def _extract_emails(text: str, raw_html: str, source_url: str) -> list[dict]:
    """Extract emails from text and HTML attributes."""
    results = []
    seen = set()

    # From visible text
    for match in EMAIL_REGEX.finditer(text):
        email = match.group().lower()

        # Filter false positives (image extensions)
        if any(email.endswith(ext) for ext in IMAGE_EXTENSIONS):
            continue

        # Filter common junk
        if any(junk in email for junk in ['example.com', 'test.com', 'sentry.io', 'wixpress.com']):
            continue

        if email not in seen:
            seen.add(email)
            # Get surrounding context
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            nearby = text[start:end].strip()

            results.append({
                "email": email,
                "extraction_method": ExtractionMethod.REGEX,
                "source_url": source_url,
                "nearby_text": nearby[:200],
            })

    # From HTML attributes (mailto:, data-email, onclick)
    soup = BeautifulSoup(raw_html, "lxml")

    # mailto: links
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0].strip().lower()
            if "@" in email and email not in seen:
                if not any(email.endswith(ext) for ext in IMAGE_EXTENSIONS):
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.MAILTO,
                        "source_url": source_url,
                        "nearby_text": tag.get_text(strip=True)[:200],
                    })

    # data-email attributes
    for tag in soup.find_all(True):
        for attr in ["data-email", "data-contact", "data-contactemail"]:
            value = tag.get(attr, "")
            if value and "@" in str(value):
                email = str(value).strip().lower()
                if EMAIL_REGEX.match(email) and email not in seen:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.HTML_ATTRIBUTE,
                        "source_url": source_url,
                        "nearby_text": "",
                    })

    return results


def _extract_phones(text: str, source_url: str) -> list[dict]:
    """Extract phone numbers from visible text."""
    results = []
    seen = set()

    for match in PHONE_REGEX.finditer(text):
        phone = match.group().strip()
        # Normalize to digits
        digits = re.sub(r'[^\d]', '', phone)

        if 7 <= len(digits) <= 15 and digits not in seen:
            seen.add(digits)

            # Get context
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            nearby = text[start:end].strip()

            # Check for fax
            context_lower = nearby.lower()
            is_fax = any(kw in context_lower for kw in ["fax", "facsimile"])
            is_mobile = any(kw in context_lower for kw in ["mobile", "cell", "cellular"])

            results.append({
                "phone": phone,
                "raw_phone": phone,
                "extraction_method": ExtractionMethod.REGEX,
                "source_url": source_url,
                "nearby_text": nearby[:200],
                "is_fax": is_fax,
                "is_mobile": is_mobile,
            })

    return results


def _extract_social(raw_html: str, source_url: str) -> list[dict]:
    """Extract social media links from HTML."""
    results = []
    seen = set()

    soup = BeautifulSoup(raw_html, "lxml")

    for tag in soup.find_all("a", href=True):
        href = tag["href"]

        for platform, domains in SOCIAL_DOMAINS.items():
            if any(d in href.lower() for d in domains):
                if href not in seen:
                    seen.add(href)

                    # Check if company page
                    is_company = False
                    if platform == "linkedin" and "/company/" in href.lower():
                        is_company = True
                    elif platform == "facebook" and "/pages/" in href.lower():
                        is_company = True

                    # Extract profile name
                    profile_name = ""
                    from urllib.parse import urlparse
                    try:
                        parsed = urlparse(href)
                        parts = [p for p in parsed.path.strip("/").split("/") if p]
                        if parts:
                            if platform == "linkedin" and len(parts) >= 2:
                                profile_name = parts[1].replace("-", " ").title()
                            else:
                                profile_name = parts[0]
                    except Exception:
                        pass

                    results.append({
                        "platform": platform,
                        "url": href,
                        "raw_url": href,
                        "profile_name": profile_name,
                        "extraction_method": ExtractionMethod.REGEX,
                        "source_url": source_url,
                        "is_company_page": is_company,
                        "is_personal_profile": not is_company,
                    })

                break  # Only match one platform per URL

    return results
