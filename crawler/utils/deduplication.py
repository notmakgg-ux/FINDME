"""
Deduplication — normalize and deduplicate emails across pages and companies.
"""

from models.schemas import ExtractedEmail


def normalize_email(email: str) -> str:
    """Normalize email for deduplication."""
    return email.lower().strip()


def deduplicate_emails(emails: list[ExtractedEmail]) -> list[ExtractedEmail]:
    """
    Deduplicate emails by normalized address.
    When the same email appears on multiple pages, merge the best source
    and keep the highest-scoring version.
    """
    seen: dict[str, ExtractedEmail] = {}

    for email_obj in emails:
        key = normalize_email(email_obj.email)

        if key in seen:
            existing = seen[key]
            # Merge source URLs
            if email_obj.source_url and email_obj.source_url not in existing.all_source_urls:
                existing.all_source_urls.append(email_obj.source_url)
            # Keep higher-scoring version
            if email_obj.confidence_score > existing.confidence_score:
                # Copy enriched fields from the better-scoring version
                existing.email = email_obj.email
                existing.source_url = email_obj.source_url
                existing.source_page_type = email_obj.source_page_type
                existing.extraction_method = email_obj.extraction_method
                existing.nearby_text = email_obj.nearby_text
                existing.html_context = email_obj.html_context
                existing.person_name = email_obj.person_name
                existing.job_title = email_obj.job_title
                existing.role = email_obj.role
                existing.email_type = email_obj.email_type
                existing.confidence_score = email_obj.confidence_score
                existing.confidence_level = email_obj.confidence_level
                existing.score_reasons = email_obj.score_reasons
            # If same score, prefer mailto method
            elif email_obj.confidence_score == existing.confidence_score:
                if email_obj.extraction_method == "mailto" and existing.extraction_method != "mailto":
                    existing.email = email_obj.email
                    existing.source_url = email_obj.source_url
                    existing.source_page_type = email_obj.source_page_type
                    existing.extraction_method = email_obj.extraction_method
                    existing.nearby_text = email_obj.nearby_text
                    existing.person_name = email_obj.person_name
                    existing.job_title = email_obj.job_title
                    existing.role = email_obj.role
        else:
            # First occurrence — set best_source_url
            email_obj.best_source_url = email_obj.source_url
            email_obj.all_source_urls = [email_obj.source_url] if email_obj.source_url else []
            seen[key] = email_obj

    return list(seen.values())


def deduplicate_by_domain(emails: list[ExtractedEmail], root_domain: str) -> list[ExtractedEmail]:
    """Additional deduplication: keep best email per unique local-part pattern."""
    # Group by local part
    local_parts: dict[str, list[ExtractedEmail]] = {}
    for e in emails:
        local = e.email.split("@")[0]
        if local not in local_parts:
            local_parts[local] = []
        local_parts[local].append(e)

    result = []
    for local, group in local_parts.items():
        # Keep the highest-scoring from each group
        best = max(group, key=lambda x: x.confidence_score)
        result.append(best)

    return result
