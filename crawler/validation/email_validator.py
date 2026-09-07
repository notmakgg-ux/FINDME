"""
Email validation — syntax, MX records, disposable detection, domain matching.
"""

import re
import logging
from typing import Optional

import dns.resolver
from email_validator import validate_email, EmailNotValidError

from config import settings, DISPOSABLE_DOMAINS
from utils.urls import get_domain_from_email, get_root_domain

logger = logging.getLogger(__name__)

# Cache MX check results to avoid repeated DNS lookups
_mx_cache: dict[str, Optional[bool]] = {}


def validate_syntax(email: str) -> bool:
    """Validate email syntax using email-validator library."""
    try:
        result = validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def check_mx_record(domain: str) -> bool:
    """Check if domain has valid MX records."""
    if domain in _mx_cache:
        return _mx_cache[domain]

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        has_mx = len(answers) > 0
        _mx_cache[domain] = has_mx
        return has_mx
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout,
            dns.resolver.YXDOMAIN):
        _mx_cache[domain] = False
        return False
    except Exception:
        _mx_cache[domain] = False
        return False


def is_disposable(email: str) -> bool:
    """Check if email domain is a known disposable email provider."""
    domain = get_domain_from_email(email)
    return domain in DISPOSABLE_DOMAINS


def is_noreply(email: str) -> bool:
    """Check if email is a noreply/bounce address."""
    local = email.split("@")[0].lower()
    noreply_prefixes = [
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "mailer-daemon", "postmaster", "bounce", "auto",
    ]
    return any(prefix in local for prefix in noreply_prefixes)


def check_domain_match(email_domain: str, website_domain: str) -> bool:
    """Check if email domain matches the company website domain."""
    email_domain = email_domain.lower().strip()
    website_domain = website_domain.lower().strip()

    # Exact match
    if email_domain == website_domain:
        return True

    # Email domain is a subdomain of website domain
    if email_domain.endswith("." + website_domain):
        return True

    # Website domain is a subdomain of email domain (less common but valid)
    if website_domain.endswith("." + email_domain):
        return True

    # Both share the same root domain
    email_root = get_root_domain(f"https://{email_domain}")
    website_root = get_root_domain(f"https://{website_domain}")
    if email_root and website_root and email_root == website_root:
        return True

    return False


def is_personal_provider(email: str) -> bool:
    """Check if email is from a personal email provider (Gmail, Yahoo, etc.)."""
    domain = get_domain_from_email(email)
    personal_providers = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "aol.com", "icloud.com", "mail.com", "protonmail.com",
        "protonmail.ch", "zoho.com", "yandex.com", "gmx.com",
        "live.com", "msn.com", "me.com", "comcast.net",
        "att.net", "verizon.net", "cox.net",
    }
    return domain in personal_providers


def validate_email_comprehensive(
    email: str,
    website_domain: str = "",
) -> dict:
    """
    Run all validation checks on an email.

    Returns dict with:
        syntax_valid, mx_valid, domain_match, is_disposable_email,
        is_noreply_email, is_personal_email, overall_valid
    """
    email_lower = email.lower().strip()
    domain = get_domain_from_email(email_lower)

    syntax_valid = validate_syntax(email_lower)
    mx_valid = check_mx_record(domain) if domain else False
    disposable = is_disposable(email_lower)
    noreply = is_noreply(email_lower)
    personal = is_personal_provider(email_lower)
    domain_match = check_domain_match(domain, website_domain) if website_domain else False

    # Overall validity: must be syntactically valid and not disposable/noreply
    overall_valid = syntax_valid and not disposable and not noreply

    return {
        "syntax_valid": syntax_valid,
        "mx_valid": mx_valid,
        "domain_match": domain_match,
        "is_disposable_email": disposable,
        "is_noreply_email": noreply,
        "is_personal_email": personal,
        "overall_valid": overall_valid,
    }
