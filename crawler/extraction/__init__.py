"""Extraction package — multi-layer contact extraction from HTML content.

Extraction hierarchy:
    1. Scrapling — primary extractor (CSS selectors, regex, adaptive tracking)
    2. WebExtractor — quick secondary pass (regex-based, lightweight)
    3. Playwright — fallback for JS-heavy pages
"""

from extraction.email_extractor import extract_all_emails
from extraction.phone_extractor import extract_all_phones
from extraction.social_extractor import extract_all_social
from extraction.webextractor import webextract_all

__all__ = [
    "extract_all_emails",
    "extract_all_phones",
    "extract_all_social",
    "webextract_all",
]
