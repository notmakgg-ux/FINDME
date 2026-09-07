"""
Extract emails from JSON-LD structured data and schema.org markup.
"""

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from models.schemas import ExtractionMethod


def extract_emails_from_json_ld(html: str, source_url: str = "") -> list[dict]:
    """
    Extract emails from <script type="application/ld+json"> blocks.
    Searches for Organization, Person, LocalBusiness, etc.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle both single objects and arrays
        items = data if isinstance(data, list) else [data]
        emails = _extract_from_jsonld_items(items)

        for email_info in emails:
            email = email_info["email"].lower()
            if email in seen:
                continue
            seen.add(email)

            results.append({
                "email": email,
                "extraction_method": ExtractionMethod.JSON_LD,
                "source_url": source_url,
                "person_name": email_info.get("name", ""),
                "job_title": email_info.get("title", ""),
                "html_context": email_info.get("context", ""),
            })

    return results


def _extract_from_jsonld_items(items: list) -> list[dict]:
    """Recursively extract email info from JSON-LD items."""
    results = []

    for item in items:
        if not isinstance(item, dict):
            continue

        # Direct email field
        email = item.get("email", "")
        if email and isinstance(email, str) and "@" in email:
            results.append({
                "email": email.strip(),
                "name": item.get("name", ""),
                "title": item.get("jobTitle", item.get("title", "")),
                "context": json.dumps(item)[:500],
            })

        # contactPoint
        contact_points = item.get("contactPoint", [])
        if not isinstance(contact_points, list):
            contact_points = [contact_points]
        for cp in contact_points:
            if isinstance(cp, dict):
                cp_email = cp.get("email", "")
                if cp_email and "@" in str(cp_email):
                    results.append({
                        "email": str(cp_email).strip(),
                        "name": item.get("name", ""),
                        "title": cp.get("contactType", item.get("jobTitle", "")),
                        "context": json.dumps(cp)[:500],
                    })

        # Recurse into member/employee/author
        for field in ("member", "employee", "author", "founder"):
            sub_items = item.get(field, [])
            if not isinstance(sub_items, list):
                sub_items = [sub_items]
            for sub in sub_items:
                if isinstance(sub, dict):
                    sub_email = sub.get("email", "")
                    if sub_email and "@" in str(sub_email):
                        results.append({
                            "email": str(sub_email).strip(),
                            "name": sub.get("name", ""),
                            "title": sub.get("jobTitle", ""),
                            "context": json.dumps(sub)[:500],
                        })

    return results


def extract_emails_from_meta_tags(html: str, source_url: str = "") -> list[dict]:
    """Extract emails from HTML meta tags."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    email_pattern = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

    # Check meta tags
    for meta in soup.find_all("meta"):
        content = meta.get("content", "")
        name = meta.get("name", "") + meta.get("property", "")

        if "email" in name.lower() or "contact" in name.lower():
            for match in email_pattern.finditer(content):
                email = match.group().lower()
                if email not in seen:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.STRUCTURED_DATA,
                        "source_url": source_url,
                        "html_context": f"meta[{name}]: {content[:200]}",
                    })

    return results


def extract_emails_from_javascript(html: str, source_url: str = "") -> list[dict]:
    """Extract emails from inline JavaScript and JSON data."""
    results = []
    seen = set()
    email_pattern = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all("script"):
        if script.string:
            for match in email_pattern.finditer(script.string):
                email = match.group().lower()
                if email not in seen:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.JAVASCRIPT,
                        "source_url": source_url,
                        "html_context": "",
                    })

    return results
