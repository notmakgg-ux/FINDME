"""
Decode obfuscated email addresses.
Supports common obfuscation patterns used on real estate websites.
"""

import re

from models.schemas import ExtractionMethod


# Patterns for obfuscated emails
OBFUSCATION_PATTERNS = [
    # [at] and [dot] variations
    re.compile(r'(\w[\w._%+-]*)\s*[\[\(]\s*at\s*[\]\)]\s*(\w[\w.-]*)\s*[\[\(]\s*dot\s*[\]\)]\s*(\w{2,})', re.I),
    # (at) and (dot) without brackets
    re.compile(r'(\w[\w._%+-]*)\s*\(at\)\s*(\w[\w.-]*)\s*\(dot\)\s*(\w{2,})', re.I),
    # AT and DOT uppercase
    re.compile(r'(\w[\w._%+-]*)\s+AT\s+(\w[\w.-]*)\s+DOT\s+(\w{2,})', re.I),
    # {at} and {dot}
    re.compile(r'(\w[\w._%+-]*)\s*\{at\}\s*(\w[\w.-]*)\s*\{dot\}\s*(\w{2,})', re.I),
    # &#64; (HTML entity for @)
    re.compile(r'(\w[\w._%+-]+)&#64;([\w.-]+)\.(\w{2,})', re.I),
    # &#x40; (hex HTML entity for @)
    re.compile(r'(\w[\w._%+-]+)&#x40;([\w.-]+)\.(\w{2,})', re.I),
    # Cloudflare data-cfemail (JS encoded)
    re.compile(r'data-cfemail\s*=\s*"([a-fA-F0-9]+)"'),
]


def decode_cloudflare_email(encoded: str) -> str:
    """Decode Cloudflare email protection (data-cfemail)."""
    try:
        r = int(encoded[:2], 16)
        decoded = ""
        for i in range(2, len(encoded), 2):
            decoded += chr(int(encoded[i:i+2], 16) ^ r)
        return decoded.lower()
    except (ValueError, IndexError):
        return ""


def decode_html_entities(text: str) -> str:
    """Decode HTML entities in text."""
    import html
    return html.unescape(text)


def extract_obfuscated_emails(text: str, source_url: str = "") -> list[dict]:
    """
    Extract obfuscated email addresses from text/HTML.

    Returns list of dicts with:
        email, extraction_method, source_url, html_context
    """
    results = []
    seen = set()

    # First decode HTML entities
    decoded_text = decode_html_entities(text)

    # Check each pattern
    for pattern in OBFUSCATION_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()

            if len(groups) == 3:
                # Standard obfuscation: user [at] domain [dot] tld
                email = f"{groups[0]}@{groups[1]}.{groups[2]}"
            elif len(groups) == 2 and groups[1] == "":
                # Cloudflare data-cfemail
                email = decode_cloudflare_email(groups[0])
            else:
                continue

            email = email.lower().strip()
            if not email or "@" not in email:
                continue

            if email in seen:
                continue
            seen.add(email)

            # Get surrounding context
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 100)
            context = text[start:end].strip()

            results.append({
                "email": email,
                "extraction_method": ExtractionMethod.OBFUSCATED,
                "source_url": source_url,
                "html_context": context[:500],
            })

    # Also check for decoded entities
    for pattern in [re.compile(r'(\w[\w._%+-]+)&#64;([\w.-]+)\.(\w{2,})', re.I),
                     re.compile(r'(\w[\w._%+-]+)&#x40;([\w.-]+)\.(\w{2,})', re.I)]:
        for match in pattern.finditer(decoded_text):
            groups = match.groups()
            if len(groups) == 3:
                email = f"{groups[0]}@{groups[1]}.{groups[2]}".lower()
                if email not in seen and "@" in email:
                    seen.add(email)
                    results.append({
                        "email": email,
                        "extraction_method": ExtractionMethod.HTML_ENTITY,
                        "source_url": source_url,
                        "html_context": "",
                    })

    return results
