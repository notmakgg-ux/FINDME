"""
Confidence scoring engine.
Builds transparent, explainable scores for each extracted email.
"""

from config import settings
from models.schemas import ExtractedEmail, ConfidenceLevel, ExtractionMethod, PageType


def calculate_confidence(email: ExtractedEmail) -> ExtractedEmail:
    """
    Calculate confidence score for an extracted email.
    Updates the email object in-place with score, level, and reasons.
    Returns the updated email.
    """
    score = 0
    reasons = []

    # --- Extraction method scoring ---
    if email.extraction_method == ExtractionMethod.MAILTO:
        score += settings.score_mailto
        reasons.append("Found in mailto link")
    elif email.extraction_method == ExtractionMethod.JSON_LD:
        score += 25
        reasons.append("Found in structured data (JSON-LD)")
    elif email.extraction_method == ExtractionMethod.STRUCTURED_DATA:
        score += 20
        reasons.append("Found in structured data")
    elif email.extraction_method == ExtractionMethod.HTML_ATTRIBUTE:
        score += 15
        reasons.append("Found in HTML attribute")
    elif email.extraction_method == ExtractionMethod.OBFUSCATED:
        score += 10
        reasons.append("Found as obfuscated email")
    elif email.extraction_method == ExtractionMethod.HTML_ENTITY:
        score += 10
        reasons.append("Found as HTML entity")
    elif email.extraction_method == ExtractionMethod.JAVASCRIPT:
        score += 10
        reasons.append("Found in JavaScript")

    # --- Page type scoring ---
    if email.source_page_type == PageType.CONTACT:
        score += settings.score_contact_page
        reasons.append("Found on contact page")
    elif email.source_page_type == PageType.TEAM:
        score += settings.score_team_page
        reasons.append("Found on team page")
    elif email.source_page_type == PageType.AGENT_PROFILE:
        score += settings.score_agent_page
        reasons.append("Found on individual agent page")
    elif email.source_page_type == PageType.AGENT_DIRECTORY:
        score += 20
        reasons.append("Found on agent directory page")
    elif email.source_page_type == PageType.HOMEPAGE:
        score += 10
        reasons.append("Found on homepage")
    elif email.source_page_type == PageType.ABOUT:
        score += 10
        reasons.append("Found on about page")

    # --- Validation scoring ---
    if email.syntax_valid:
        score += 5
        reasons.append("Syntax valid")
    else:
        score -= 20
        reasons.append("Syntax invalid")

    if email.mx_valid:
        score += settings.score_mx_valid
        reasons.append("Valid MX records")
    else:
        score -= 5
        reasons.append("No MX records")

    if email.domain_match:
        score += settings.score_domain_match
        reasons.append("Domain matches company website")
    else:
        score += settings.penalty_different_domain
        reasons.append("Domain does not match company website")

    # --- Person/role detection scoring ---
    if email.person_name:
        score += settings.score_person_detected
        reasons.append(f"Person name detected: {email.person_name}")

    # Check role tier
    role_lower = (email.role or "").lower()
    tier_1_keywords = ["owner", "founder", "ceo", "president", "principal",
                       "partner", "managing broker", "broker owner", "director", "vp"]
    tier_2_keywords = ["broker", "realtor", "agent", "team leader", "sales manager"]
    tier_3_keywords = ["info", "hello", "contact", "office", "admin", "sales"]
    low_keywords = ["noreply", "no-reply", "privacy", "legal", "careers", "support"]

    is_tier1 = any(kw in role_lower for kw in tier_1_keywords)
    is_tier2 = any(kw in role_lower for kw in tier_2_keywords)
    is_tier3 = any(kw in role_lower for kw in tier_3_keywords)
    is_low = any(kw in role_lower for kw in low_keywords)

    if is_tier1:
        score += settings.score_decision_maker
        reasons.append("Decision-maker role detected")
    elif is_tier2:
        score += settings.score_relevant_role
        reasons.append("Relevant broker/agent role detected")
    elif is_tier3:
        score += 5
        reasons.append("General company contact")
    elif is_low:
        score += settings.penalty_low_priority
        reasons.append("Low-priority email type")

    # --- Disposable domain ---
    if email.is_disposable:
        score += settings.penalty_disposable
        reasons.append("Disposable email domain")

    # --- Clamp score ---
    score = max(0, min(100, score))

    # --- Determine level ---
    if score >= settings.confidence_excellent:
        level = ConfidenceLevel.EXCELLENT
    elif score >= settings.confidence_high:
        level = ConfidenceLevel.HIGH
    elif score >= settings.confidence_medium:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW

    # Update the email object
    email.confidence_score = score
    email.confidence_level = level
    email.score_reasons = reasons

    return email


def score_all(emails: list[ExtractedEmail]) -> list[ExtractedEmail]:
    """Score all emails in a list."""
    return [calculate_confidence(e) for e in emails]
