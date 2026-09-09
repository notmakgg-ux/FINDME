"""
Phone number extraction from HTML content.

Uses multiple regex patterns to catch international, local, and
formatted phone numbers from visible text, href=tel: links,
and structured data.
"""

import re
from typing import Optional
from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod


# --- Regex patterns for phone numbers ---

# International format: +1 234 567 8901, +44 20 7946 0958, etc.
INTERNATIONAL_REGEX = re.compile(
    r'(?:\+\d{1,3}[\s\-.]?)?'
    r'(?:\(\d{2,4}\)[\s\-.]?)?'
    r'\d{2,4}[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}'
)

# US/Canada: (234) 567-8901, 234-567-8901, 234.567.8901
US_REGEX = re.compile(
    r'(?:\(\d{3}\)[\s\-.]?)?'
    r'\d{3}[\s\-.]?\d{3}[\s\-.]?\d{4}'
)

# Australian: (03) 1234 5678, 0412 345 678
AU_REGEX = re.compile(
    r'(?:\(0\d\)[\s\-.]?)?'
    r'0\d[\s\-.]?\d{4}[\s\-.]?\d{3,4}'
)

# UK: 020 7946 0958, 07911 123456
UK_REGEX = re.compile(
    r'0\d[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}'
)

# Fax indicator
FAX_KEYWORDS = {'fax', 'facsimile', 'faksimile'}

# Mobile indicators
MOBILE_KEYWORDS = {'mobile', 'cell', 'cellular', 'mobil', 'sms'}


def extract_all_phones(
    html: str,
    source_url: str = "",
    page_type: str = "other",
) -> list[dict]:
    """
    Run all phone extraction methods and return unified, deduplicated results.

    Pipeline:
        1. tel: links (highest confidence)
        2. CSS selector for phone-related elements
        3. Regex extraction from visible text

    Returns list of dicts with:
        phone, raw_phone, country_code, extraction_method, source_url,
        nearby_text, person_name, is_fax, is_mobile
    """
    all_results = []
    seen_phones = set()

    # --- Method 1: tel: links (highest confidence) ---
    tel_results = _extract_from_tel_links(html, source_url)
    for r in tel_results:
        normalized = _normalize_phone(r["phone"])
        if normalized and normalized not in seen_phones and _is_valid_phone(normalized):
            seen_phones.add(normalized)
            r["phone"] = normalized
            all_results.append(r)

    # --- Method 2: CSS selector for phone elements ---
    css_results = _extract_from_css_selectors(html, source_url)
    for r in css_results:
        normalized = _normalize_phone(r["phone"])
        if normalized and normalized not in seen_phones and _is_valid_phone(normalized):
            seen_phones.add(normalized)
            r["phone"] = normalized
            all_results.append(r)

    # --- Method 3: Regex from visible text ---
    regex_results = _extract_from_visible_text(html, source_url)
    for r in regex_results:
        normalized = _normalize_phone(r["phone"])
        if normalized and normalized not in seen_phones and _is_valid_phone(normalized):
            seen_phones.add(normalized)
            r["phone"] = normalized
            all_results.append(r)

    return all_results


def _extract_from_tel_links(html: str, source_url: str = "") -> list[dict]:
    """Extract phone numbers from href='tel:' links."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("tel:"):
            phone_raw = href[4:].strip()
            if phone_raw and phone_raw not in seen:
                seen.add(phone_raw)

                # Check for fax context
                parent_text = (tag.parent.get_text(strip=True) if tag.parent else "").lower()
                is_fax = any(kw in parent_text for kw in FAX_KEYWORDS)
                is_mobile = any(kw in parent_text for kw in MOBILE_KEYWORDS)

                # Get nearby text
                nearby = ""
                if tag.parent:
                    nearby = tag.parent.get_text(separator=" ", strip=True)[:300]

                results.append({
                    "phone": phone_raw,
                    "raw_phone": phone_raw,
                    "country_code": _detect_country_code(phone_raw),
                    "extraction_method": ExtractionMethod.CSS_SELECTOR,
                    "source_url": source_url,
                    "nearby_text": nearby,
                    "person_name": "",
                    "is_fax": is_fax,
                    "is_mobile": is_mobile,
                })

    return results


def _extract_from_css_selectors(html: str, source_url: str = "") -> list[dict]:
    """Extract phone numbers from common phone-related CSS patterns."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    # Common selectors for phone numbers
    selectors = [
        "[class*='phone']",
        "[class*='tel']",
        "[class*='contact']",
        "[id*='phone']",
        "[id*='tel']",
        "[itemprop='telephone']",
        "[itemprop='phone']",
    ]

    for selector in selectors:
        try:
            for tag in soup.select(selector):
                text = tag.get_text(strip=True)
                if not text:
                    continue

                # Extract phone from text
                phones = _find_phones_in_text(text)
                for phone_raw in phones:
                    if phone_raw not in seen:
                        seen.add(phone_raw)
                        nearby = text[:300]
                        parent_text = (tag.parent.get_text(strip=True) if tag.parent else "").lower()
                        is_fax = any(kw in parent_text for kw in FAX_KEYWORDS)
                        is_mobile = any(kw in parent_text for kw in MOBILE_KEYWORDS)

                        results.append({
                            "phone": phone_raw,
                            "raw_phone": phone_raw,
                            "country_code": _detect_country_code(phone_raw),
                            "extraction_method": ExtractionMethod.CSS_SELECTOR,
                            "source_url": source_url,
                            "nearby_text": nearby,
                            "person_name": "",
                            "is_fax": is_fax,
                            "is_mobile": is_mobile,
                        })
        except Exception:
            continue

    return results


def _extract_from_visible_text(html: str, source_url: str = "") -> list[dict]:
    """Extract phone numbers from visible page text using regex."""
    soup = BeautifulSoup(html, "lxml")

    # Remove script and style tags
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    results = []
    seen = set()

    # Try all phone regex patterns
    for pattern in [INTERNATIONAL_REGEX, US_REGEX, AU_REGEX, UK_REGEX]:
        for match in pattern.finditer(text):
            phone_raw = match.group().strip()
            if phone_raw not in seen and _is_valid_phone(phone_raw):
                seen.add(phone_raw)

                # Get surrounding context
                start = max(0, match.start() - 100)
                end = min(len(text), match.end() + 100)
                nearby = text[start:end].strip()

                # Check for fax/mobile in context
                context_lower = nearby.lower()
                is_fax = any(kw in context_lower for kw in FAX_KEYWORDS)
                is_mobile = any(kw in context_lower for kw in MOBILE_KEYWORDS)

                results.append({
                    "phone": phone_raw,
                    "raw_phone": phone_raw,
                    "country_code": _detect_country_code(phone_raw),
                    "extraction_method": ExtractionMethod.REGEX,
                    "source_url": source_url,
                    "nearby_text": nearby[:300],
                    "person_name": "",
                    "is_fax": is_fax,
                    "is_mobile": is_mobile,
                })

    return results


def _find_phones_in_text(text: str) -> list[str]:
    """Find all phone numbers in a text string."""
    phones = []
    for pattern in [INTERNATIONAL_REGEX, US_REGEX, AU_REGEX, UK_REGEX]:
        for match in pattern.finditer(text):
            phone = match.group().strip()
            if _is_valid_phone(phone):
                phones.append(phone)
    return phones


def _normalize_phone(phone: str) -> str:
    """Normalize a phone number to digits with optional + prefix."""
    if not phone:
        return ""

    # Keep + prefix if present
    has_plus = phone.startswith("+")

    # Extract only digits
    digits = re.sub(r'[^\d]', '', phone)

    if not digits:
        return ""

    if has_plus:
        return "+" + digits
    return digits


def _is_valid_phone(phone: str) -> bool:
    """Check if a phone number looks valid (at least 7 digits, at most 15)."""
    digits = re.sub(r'[^\d]', '', phone)
    return 7 <= len(digits) <= 15


def _detect_country_code(phone: str) -> str:
    """Detect country code from phone number."""
    digits = re.sub(r'[^\d]', '', phone)

    if phone.startswith("+"):
        if digits.startswith("1"):
            return "US/CA"
        elif digits.startswith("44"):
            return "UK"
        elif digits.startswith("61"):
            return "AU"
        elif digits.startswith("91"):
            return "IN"
        elif digits.startswith("86"):
            return "CN"
        elif digits.startswith("81"):
            return "JP"
        elif digits.startswith("49"):
            return "DE"
        elif digits.startswith("33"):
            return "FR"
        elif digits.startswith("39"):
            return "IT"
        elif digits.startswith("34"):
            return "ES"
        elif digits.startswith("31"):
            return "NL"
        elif digits.startswith("46"):
            return "SE"
        elif digits.startswith("47"):
            return "NO"
        elif digits.startswith("45"):
            return "DK"
        elif digits.startswith("358"):
            return "FI"
        elif digits.startswith("48"):
            return "PL"
        elif digits.startswith("420"):
            return "CZ"

    # Check local patterns
    if re.match(r'^0\d', phone):
        return "AU/UK"

    return ""
