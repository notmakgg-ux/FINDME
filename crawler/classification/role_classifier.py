"""
Role classification for extracted email contacts.
Determines if a contact is a decision-maker, broker, agent, or general contact.
"""

import re
from typing import Optional

from config import TIER_1_ROLES, TIER_2_ROLES, TIER_3_ROLES, LOW_PRIORITY_ROLES


def classify_person_role(name: str, title: str, email: str = "") -> dict:
    """
    Classify a person's role from their name, title, and email.

    Returns:
        {
            "role": "managing broker",
            "tier": 1,
            "role_label": "Decision Maker",
            "is_decision_maker": True,
            "is_broker_agent": False,
            "is_general_contact": False,
            "is_low_priority": False,
        }
    """
    result = {
        "role": "",
        "tier": 0,
        "role_label": "Unknown",
        "is_decision_maker": False,
        "is_broker_agent": False,
        "is_general_contact": False,
        "is_low_priority": False,
    }

    # Combine all text to search
    search_text = f"{title} {name} {email}".lower()

    # Check Low Priority first (to reject)
    for role in LOW_PRIORITY_ROLES:
        if role in search_text:
            result["role"] = role
            result["tier"] = -1
            result["role_label"] = "Low Priority"
            result["is_low_priority"] = True
            return result

    # Check Tier 1: Decision makers
    for role in TIER_1_ROLES:
        if role in search_text:
            result["role"] = role
            result["tier"] = 1
            result["role_label"] = "Decision Maker"
            result["is_decision_maker"] = True
            return result

    # Check Tier 2: Senior relevant
    for role in TIER_2_ROLES:
        if role in search_text:
            result["role"] = role
            result["tier"] = 2
            result["role_label"] = "Broker/Agent"
            result["is_broker_agent"] = True
            return result

    # Check Tier 3: General company contacts
    for role in TIER_3_ROLES:
        if role in search_text:
            result["role"] = role
            result["tier"] = 3
            result["role_label"] = "General Contact"
            result["is_general_contact"] = True
            return result

    # Check title for any title-like pattern (contains common job words)
    title_lower = title.lower()
    title_patterns = [
        (r'\b(ceo|president|principal|founder|owner|partner|director|vp|vice.president)\b', 1, "Decision Maker"),
        (r'\b(broker|realtor|agent|sales)\b', 2, "Broker/Agent"),
        (r'\b(manager|lead|head|supervisor)\b', 2, "Senior Contact"),
    ]
    for pattern, tier, label in title_patterns:
        if re.search(pattern, title_lower):
            result["role"] = title
            result["tier"] = tier
            result["role_label"] = label
            if tier == 1:
                result["is_decision_maker"] = True
            elif tier == 2:
                result["is_broker_agent"] = True
            return result

    # If we have a non-empty title, classify it as unknown but usable
    if title:
        result["role"] = title
        result["tier"] = 0
        result["role_label"] = "Other"
        return result

    # Check email prefix for generic patterns
    if email:
        local = email.split("@")[0]
        if local in ("info", "hello", "contact", "office", "admin", "sales", "support"):
            result["role"] = local
            result["tier"] = 3
            result["role_label"] = "General Contact"
            result["is_general_contact"] = True
            return result

    return result


def get_tier_label(tier: int) -> str:
    """Get human-readable tier label."""
    labels = {
        -1: "Low Priority (Reject)",
        0: "Unknown",
        1: "Decision Maker",
        2: "Broker/Agent",
        3: "General Contact",
    }
    return labels.get(tier, "Unknown")
