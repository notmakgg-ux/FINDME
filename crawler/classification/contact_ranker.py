"""
Contact ranking — rank all discovered emails for a company
and select the best contact for outreach.
"""

from models.schemas import ExtractedEmail, ConfidenceLevel


def rank_contacts(emails: list[ExtractedEmail]) -> list[ExtractedEmail]:
    """
    Rank all emails for a company. Returns sorted list (best first).
    Also sets is_best_contact on the top result.
    """
    if not emails:
        return []

    # Sort by: confidence_score desc, then tier (lower = better), then extraction method
    def sort_key(e: ExtractedEmail):
        # Higher score = better
        # Lower tier = better (tier 1 = decision maker)
        tier = _get_tier(e)
        method_priority = {
            "mailto": 0,
            "json_ld": 1,
            "structured_data": 1,
            "html_attribute": 2,
            "regex": 3,
            "obfuscated": 3,
            "html_entity": 3,
            "javascript": 4,
            "cloudflare": 3,
        }
        method_rank = method_priority.get(e.extraction_method, 5)
        return (-e.confidence_score, tier, method_rank)

    sorted_emails = sorted(emails, key=sort_key)

    # Mark best contact
    for i, email in enumerate(sorted_emails):
        if i == 0 and email.confidence_score > 0:
            # Only mark as best if it's reasonably confident
            pass  # is_best_contact is set in CompanyResult, not here

    return sorted_emails


def _get_tier(email: ExtractedEmail) -> int:
    """Get the tier for ranking purposes."""
    role_lower = (email.role or "").lower()

    tier_1_keywords = ["owner", "founder", "ceo", "president", "principal", "partner",
                       "managing broker", "broker owner", "director", "vp"]
    tier_2_keywords = ["broker", "realtor", "agent", "team leader", "sales manager"]
    tier_3_keywords = ["info", "hello", "contact", "office", "admin", "sales"]
    low_keywords = ["noreply", "no-reply", "privacy", "legal", "careers", "support"]

    for kw in low_keywords:
        if kw in role_lower:
            return -1
    for kw in tier_1_keywords:
        if kw in role_lower:
            return 1
    for kw in tier_2_keywords:
        if kw in role_lower:
            return 2
    for kw in tier_3_keywords:
        if kw in role_lower:
            return 3

    return 0


def select_best_contact(emails: list[ExtractedEmail]) -> dict:
    """
    Select the single best contact from a company's email list.
    Returns best_contact_email, best_contact_name, best_contact_role, best_contact_score.
    """
    if not emails:
        return {
            "best_contact_email": "",
            "best_contact_name": "",
            "best_contact_role": "",
            "best_contact_score": 0,
        }

    ranked = rank_contacts(emails)
    best = ranked[0]

    return {
        "best_contact_email": best.email,
        "best_contact_name": best.person_name,
        "best_contact_role": best.role or best.job_title,
        "best_contact_score": best.confidence_score,
    }
