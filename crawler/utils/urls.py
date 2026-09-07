"""
URL normalization and utility functions.
"""

from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import tldextract


def normalize_url(url: str) -> str:
    """Normalize a URL by removing fragments, normalizing scheme, etc."""
    if not url:
        return ""

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    # Lowercase scheme and netloc
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Remove trailing dot
    netloc = netloc.rstrip(".")

    # Normalize path
    path = parsed.path
    if path == "/":
        path = "/"
    elif path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    # Remove empty query strings
    query = parsed.query
    if query == "?":
        query = ""

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def get_root_domain(url: str) -> str:
    """Extract the root domain from a URL (e.g., abc-realty.com)."""
    extracted = tldextract.extract(url)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return extracted.registered_domain or extracted.domain


def get_full_domain(url: str) -> str:
    """Extract the full domain including subdomain."""
    extracted = tldextract.extract(url)
    return extracted.registered_domain or extracted.domain


def make_absolute_url(base_url: str, href: str) -> str:
    """Convert a relative URL to absolute."""
    if not href:
        return ""
    return urljoin(base_url, href)


def is_same_domain(url1: str, url2: str) -> bool:
    """Check if two URLs share the same root domain."""
    return get_root_domain(url1) == get_root_domain(url2)


def get_domain_from_email(email: str) -> str:
    """Extract domain from email address."""
    if "@" in email:
        return email.split("@")[1].lower().strip()
    return ""


def remove_tracking_params(url: str) -> str:
    """Remove common tracking parameters from URL."""
    parsed = urlparse(url)
    if not parsed.query:
        return url

    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
        "_ga", "_gl", "msclkid", "twclid",
    }

    params = parse_qs(parsed.query)
    cleaned = {k: v for k, v in params.items() if k.lower() not in tracking_params}

    if cleaned:
        new_query = urlencode(cleaned, doseq=True)
    else:
        new_query = ""

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
