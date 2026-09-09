"""
Context extraction — detect person names and job titles near extracted emails.
Uses DOM proximity and text analysis.
"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from config import TIER_1_ROLES, TIER_2_ROLES, TIER_3_ROLES, LOW_PRIORITY_ROLES


def extract_person_context(html: str, email: str) -> dict:
    """
    Find person name and job title near the given email in the DOM.

    Returns:
        {
            "person_name": "John Smith",
            "job_title": "Managing Broker",
            "role": "managing broker",
            "tier": 1,
        }
    """
    soup = BeautifulSoup(html, "lxml")
    result = {"person_name": "", "job_title": "", "role": "", "tier": 0}

    # Find the element containing this email
    element = _find_element_with_email(soup, email)
    if element is None:
        return result

    # Walk up to a container (card, list item, section, etc.)
    container = _find_parent_container(element)
    if container is None:
        container = element.parent or element

    # Extract name and title from container
    name = _extract_name(container)
    title = _extract_job_title(container)
    role, tier = classify_role(title)

    result["person_name"] = name
    result["job_title"] = title
    result["role"] = role
    result["tier"] = tier

    return result


def _find_element_with_email(soup: BeautifulSoup, email: str) -> Optional[BeautifulSoup]:
    """Find the DOM element that directly contains the email address."""
    # Check text nodes
    for text_node in soup.find_all(string=re.compile(re.escape(email))):
        parent = text_node.parent
        if parent:
            return parent

    # Check href attributes
    for tag in soup.find_all(href=re.compile(re.escape(email), re.I)):
        return tag

    # Check data attributes
    for tag in soup.find_all(True):
        for attr_val in tag.attrs.values():
            if isinstance(attr_val, str) and email.lower() in attr_val.lower():
                return tag

    return None


def _find_parent_container(element) -> Optional[BeautifulSoup]:
    """Walk up the DOM to find a meaningful container (card, section, etc.)."""
    container_tags = {"div", "section", "article", "li", "tr", "td", "td"}
    max_depth = 5

    current = element
    for _ in range(max_depth):
        current = current.parent
        if current is None:
            break
        if hasattr(current, "name") and current.name in container_tags:
            text = current.get_text(strip=True)
            # Make sure it's a meaningful container (not too small, not too big)
            if 20 < len(text) < 2000:
                return current

    return current


def _extract_name(container) -> str:
    """Extract person name from container using heading/tag heuristics."""
    # Check headings first
    for tag in container.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        name = _clean_name(tag.get_text(strip=True))
        if name:
            return name

    # Check strong, b tags
    for tag in container.find_all(["strong", "b"]):
        name = _clean_name(tag.get_text(strip=True))
        if name:
            return name

    # Check first line of text that looks like a name
    text = container.get_text(separator="\n", strip=True)
    for line in text.split("\n"):
        line = line.strip()
        name = _clean_name(line)
        if name and _looks_like_name(name):
            return name

    return ""


def _clean_name(text: str) -> str:
    """Clean and validate a potential name."""
    if not text:
        return ""

    # Remove email addresses
    text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '', text).strip()

    # Remove phone numbers
    text = re.sub(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '', text).strip()

    # Remove if it's clearly not a name
    if len(text) < 2 or len(text) > 80:
        return ""
    if re.search(r'\d{3,}', text):
        return ""
    if text.lower() in ("the", "a", "an", "and", "or", "but", "our", "we", "you"):
        return ""

    return text


def _looks_like_name(text: str) -> bool:
    """Check if text looks like a person's name."""
    if not text:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 5:
        return False
    # All words should start with uppercase (or be common lowercase words)
    for word in words:
        if word[0].islower() and word.lower() not in ("de", "van", "von", "di", "da", "la", "el"):
            return False
    return True


def _extract_job_title(container) -> str:
    """Extract job title from container."""
    role_keywords = (
        [r.lower() for r in TIER_1_ROLES] +
        [r.lower() for r in TIER_2_ROLES] +
        [r.lower() for r in TIER_3_ROLES]
    )

    text = container.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for line in lines:
        line_lower = line.lower()
        for keyword in role_keywords:
            if keyword in line_lower:
                return line

    # Check italic/span text
    for tag in container.find_all(["em", "i", "span", "p"]):
        tag_text = tag.get_text(strip=True)
        tag_lower = tag_text.lower()
        for keyword in role_keywords:
            if keyword in tag_lower:
                return tag_text

    return ""


def classify_role(title: str) -> tuple[str, int]:
    """
    Classify a job title into a role and tier.
    Returns (role_name, tier_number).
    """
    if not title:
        return "", 0

    title_lower = title.lower()

    # Check Tier 1: Decision makers
    for role in TIER_1_ROLES:
        if role in title_lower:
            return role, 1

    # Check Tier 2: Senior relevant
    for role in TIER_2_ROLES:
        if role in title_lower:
            return role, 2

    # Check Tier 3: General company
    for role in TIER_3_ROLES:
        if role in title_lower:
            return role, 3

    # Check Low priority
    for role in LOW_PRIORITY_ROLES:
        if role in title_lower:
            return role, -1

    return title, 0  # Unknown tier but keep the title
