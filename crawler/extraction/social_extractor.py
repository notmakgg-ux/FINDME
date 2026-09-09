"""
Social media link extraction from HTML content.

Extracts LinkedIn, Facebook, Instagram, Twitter/X, YouTube, and TikTok
links from anchor tags, meta tags, and structured data.
"""

import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod


# --- Platform detection patterns ---

SOCIAL_PLATFORMS = {
    "linkedin": {
        "domains": ["linkedin.com", "linkedin.cn"],
        "company_patterns": ["/company/", "/school/", "/showcase/"],
        "personal_patterns": ["/in/", "/pub/", "/profile/"],
    },
    "facebook": {
        "domains": ["facebook.com", "fb.com", "fb.me"],
        "company_patterns": ["/pages/", "/pg/"],
        "personal_patterns": [],  # Personal profiles are just the root
    },
    "instagram": {
        "domains": ["instagram.com"],
        "company_patterns": [],  # Business accounts look the same
        "personal_patterns": [],
    },
    "twitter": {
        "domains": ["twitter.com", "x.com"],
        "company_patterns": [],
        "personal_patterns": [],
    },
    "youtube": {
        "domains": ["youtube.com", "youtu.be"],
        "company_patterns": ["/channel/", "/c/", "/@"],
        "personal_patterns": [],
    },
    "tiktok": {
        "domains": ["tiktok.com"],
        "company_patterns": [],
        "personal_patterns": [],
    },
}

# JSON-LD social schema patterns
JSONLD_SOCIAL_KEYS = {
    "linkedin": ["sameAs"],
    "facebook": ["sameAs"],
    "instagram": ["sameAs"],
    "twitter": ["sameAs"],
    "youtube": ["sameAs"],
    "tiktok": ["sameAs"],
}


def extract_all_social(
    html: str,
    source_url: str = "",
    page_type: str = "other",
) -> list[dict]:
    """
    Run all social extraction methods and return unified, deduplicated results.

    Pipeline:
        1. JSON-LD structured data (highest confidence)
        2. Meta tag links
        3. CSS selector for social links
        4. Regex from anchor hrefs

    Returns list of dicts with:
        platform, url, raw_url, profile_name, source_url,
        source_page_type, extraction_method, is_company_page, is_personal_profile
    """
    all_results = []
    seen_urls = set()

    # --- Method 1: JSON-LD structured data ---
    jsonld_results = _extract_from_jsonld(html, source_url)
    for r in jsonld_results:
        normalized = _normalize_social_url(r["url"], r["platform"])
        if normalized and normalized not in seen_urls:
            seen_urls.add(normalized)
            r["url"] = normalized
            all_results.append(r)

    # --- Method 2: Meta tag links ---
    meta_results = _extract_from_meta_tags(html, source_url)
    for r in meta_results:
        normalized = _normalize_social_url(r["url"], r["platform"])
        if normalized and normalized not in seen_urls:
            seen_urls.add(normalized)
            r["url"] = normalized
            all_results.append(r)

    # --- Method 3: CSS selector for social links ---
    css_results = _extract_from_css_selectors(html, source_url)
    for r in css_results:
        normalized = _normalize_social_url(r["url"], r["platform"])
        if normalized and normalized not in seen_urls:
            seen_urls.add(normalized)
            r["url"] = normalized
            all_results.append(r)

    # --- Method 4: Regex from all anchor hrefs ---
    regex_results = _extract_from_anchor_hrefs(html, source_url)
    for r in regex_results:
        normalized = _normalize_social_url(r["url"], r["platform"])
        if normalized and normalized not in seen_urls:
            seen_urls.add(normalized)
            r["url"] = normalized
            all_results.append(r)

    return all_results


def _extract_from_jsonld(html: str, source_url: str = "") -> list[dict]:
    """Extract social links from JSON-LD structured data."""
    import json
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = [data]
                # Check @graph
                if "@graph" in data:
                    items.extend(data["@graph"])
            else:
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                # Check sameAs field
                same_as = item.get("sameAs", [])
                if isinstance(same_as, str):
                    same_as = [same_as]
                if not isinstance(same_as, list):
                    continue

                for url in same_as:
                    if not isinstance(url, str):
                        continue
                    platform = _detect_platform(url)
                    if platform and url not in seen:
                        seen.add(url)
                        is_company = _is_company_page(url, platform)
                        profile_name = _extract_profile_name(url, platform)

                        results.append({
                            "platform": platform,
                            "url": url,
                            "raw_url": url,
                            "profile_name": profile_name,
                            "source_url": source_url,
                            "source_page_type": "other",
                            "extraction_method": ExtractionMethod.JSON_LD,
                            "is_company_page": is_company,
                            "is_personal_profile": not is_company,
                        })

        except (json.JSONDecodeError, TypeError):
            continue

    return results


def _extract_from_meta_tags(html: str, source_url: str = "") -> list[dict]:
    """Extract social links from meta tags."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    # Check og:url, twitter:url, and other social meta tags
    meta_attrs = [
        ("property", "og:url"),
        ("name", "twitter:url"),
        ("property", "article:author"),
        ("property", "article:publisher"),
    ]

    for attr_name, attr_value in meta_attrs:
        for tag in soup.find_all("meta", attrs={attr_name: attr_value}):
            content = tag.get("content", "")
            if content:
                platform = _detect_platform(content)
                if platform and content not in seen:
                    seen.add(content)
                    results.append({
                        "platform": platform,
                        "url": content,
                        "raw_url": content,
                        "profile_name": _extract_profile_name(content, platform),
                        "source_url": source_url,
                        "source_page_type": "other",
                        "extraction_method": ExtractionMethod.CSS_SELECTOR,
                        "is_company_page": _is_company_page(content, platform),
                        "is_personal_profile": not _is_company_page(content, platform),
                    })

    return results


def _extract_from_css_selectors(html: str, source_url: str = "") -> list[dict]:
    """Extract social links from common CSS patterns."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    # Common selectors for social media links
    selectors = [
        "a[class*='social']",
        "a[class*='linkedin']",
        "a[class*='facebook']",
        "a[class*='instagram']",
        "a[class*='twitter']",
        "a[class*='youtube']",
        "a[class*='tiktok']",
        "a[href*='linkedin.com']",
        "a[href*='facebook.com']",
        "a[href*='instagram.com']",
        "a[href*='twitter.com']",
        "a[href*='x.com']",
        "a[href*='youtube.com']",
        "a[href*='tiktok.com']",
        "[class*='social-links'] a",
        "[class*='social-links']",
        ".social a",
        ".footer a[href*='linkedin']",
        ".footer a[href*='facebook']",
        ".footer a[href*='instagram']",
        ".footer a[href*='twitter']",
        ".footer a[href*='youtube']",
    ]

    for selector in selectors:
        try:
            for tag in soup.select(selector):
                href = tag.get("href", "")
                if not href:
                    continue

                platform = _detect_platform(href)
                if platform and href not in seen:
                    seen.add(href)
                    results.append({
                        "platform": platform,
                        "url": href,
                        "raw_url": href,
                        "profile_name": _extract_profile_name(href, platform),
                        "source_url": source_url,
                        "source_page_type": "other",
                        "extraction_method": ExtractionMethod.CSS_SELECTOR,
                        "is_company_page": _is_company_page(href, platform),
                        "is_personal_profile": not _is_company_page(href, platform),
                    })
        except Exception:
            continue

    return results


def _extract_from_anchor_hrefs(html: str, source_url: str = "") -> list[dict]:
    """Extract social links from all anchor hrefs using platform detection."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        platform = _detect_platform(href)

        if platform and href not in seen:
            # Skip very short or obviously non-profile URLs
            parsed = urlparse(href)
            path = parsed.path.strip("/")
            if not path or path in ("", "feed", "search", "help", "about"):
                continue

            seen.add(href)
            results.append({
                "platform": platform,
                "url": href,
                "raw_url": href,
                "profile_name": _extract_profile_name(href, platform),
                "source_url": source_url,
                "source_page_type": "other",
                "extraction_method": ExtractionMethod.REGEX,
                "is_company_page": _is_company_page(href, platform),
                "is_personal_profile": not _is_company_page(href, platform),
            })

    return results


def _detect_platform(url: str) -> str | None:
    """Detect social media platform from URL."""
    url_lower = url.lower()
    for platform, info in SOCIAL_PLATFORMS.items():
        for domain in info["domains"]:
            if domain in url_lower:
                return platform
    return None


def _is_company_page(url: str, platform: str) -> bool:
    """Check if URL is a company/organization page vs personal profile."""
    url_lower = url.lower()
    info = SOCIAL_PLATFORMS.get(platform, {})

    for pattern in info.get("company_patterns", []):
        if pattern in url_lower:
            return True

    # LinkedIn company pages
    if platform == "linkedin" and "/company/" in url_lower:
        return True

    # Facebook pages
    if platform == "facebook" and "/pages/" in url_lower:
        return True

    return False


def _extract_profile_name(url: str, platform: str) -> str:
    """Extract profile/page name from URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    if not path:
        return ""

    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""

    # LinkedIn: /in/john-doe or /company/acme
    if platform == "linkedin" and len(parts) >= 2:
        return parts[1].replace("-", " ").title()

    # Facebook: /pages/Company-Name/123456
    if platform == "facebook" and len(parts) >= 1:
        return parts[0].replace("-", " ").title()

    # Instagram, Twitter, YouTube, TikTok: /username
    if platform in ("instagram", "twitter", "youtube", "tiktok") and parts:
        return parts[0]

    return parts[0] if parts else ""


def _normalize_social_url(url: str, platform: str) -> str:
    """Normalize a social media URL to its canonical form."""
    try:
        parsed = urlparse(url)
        # Rebuild with only scheme + netloc + path (no query/fragment)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return normalized
    except Exception:
        return url
