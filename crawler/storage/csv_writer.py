"""
CSV Writer — saves email crawler results to CSV files.
Preserves data between runs so you don't lose gathered information.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from models.schemas import CompanyResult


# CSV headers matching the Google Sheets format
CSV_HEADERS = [
    "Company Name", "Website", "Normalized URL", "Domain",
    "Status", "Pages Crawled", "Pages Discovered",
    "Emails Found", "Emails Rejected",
    "Best Email", "Best Contact Name", "Best Contact Role", "Best Score",
    "Email 1", "Email 1 Name", "Email 1 Role", "Email 1 Score", "Email 1 Source", "Email 1 Method", "Email 1 Page Type",
    "Email 2", "Email 2 Name", "Email 2 Role", "Email 2 Score", "Email 2 Source", "Email 2 Method", "Email 2 Page Type",
    "Email 3", "Email 3 Name", "Email 3 Role", "Email 3 Score", "Email 3 Source", "Email 3 Method", "Email 3 Page Type",
    "Email 4", "Email 4 Name", "Email 4 Role", "Email 4 Score", "Email 4 Source", "Email 4 Method", "Email 4 Page Type",
    "Email 5", "Email 5 Name", "Email 5 Role", "Email 5 Score", "Email 5 Source", "Email 5 Method", "Email 5 Page Type",
    "Duration (s)", "Error Type", "Error Message", "All Emails (JSON)",
]


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use in filenames."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def _result_to_row(result: CompanyResult) -> list:
    """Convert a CompanyResult to a CSV row."""
    sorted_emails = sorted(result.emails, key=lambda e: e.confidence_score, reverse=True)
    
    row = [
        result.company_name,
        result.original_website,
        result.normalized_url,
        result.root_domain,
        result.status.value,
        result.pages_crawled,
        result.pages_discovered,
        result.emails_found,
        result.emails_rejected,
        result.best_contact_email,
        result.best_contact_name,
        result.best_contact_role,
        result.best_contact_score,
    ]
    
    # Flatten top 5 emails
    for i in range(5):
        if i < len(sorted_emails):
            e = sorted_emails[i]
            row.extend([
                e.email,
                e.person_name,
                e.role or e.job_title,
                e.confidence_score,
                e.source_url,
                e.extraction_method.value if hasattr(e.extraction_method, 'value') else e.extraction_method,
                e.source_page_type.value if hasattr(e.source_page_type, 'value') else e.source_page_type,
            ])
        else:
            row.extend(["", "", "", "", "", "", ""])
    
    row.extend([
        result.duration_seconds or 0,
        result.error_type or "",
        result.error_message or "",
        json.dumps([{
            "email": e.email,
            "name": e.person_name,
            "role": e.role or e.job_title,
            "score": e.confidence_score,
            "source": e.source_url,
            "method": e.extraction_method.value if hasattr(e.extraction_method, 'value') else e.extraction_method,
            "page_type": e.source_page_type.value if hasattr(e.source_page_type, 'value') else e.source_page_type,
            "nearby_text": e.nearby_text[:200],
        } for e in sorted_emails], ensure_ascii=False),
    ])
    
    return row


def save_results_csv(
    results: list[CompanyResult],
    output_dir: Path,
    prefix: str = "email_crawl",
    location: str = "",
) -> Path:
    """
    Save email crawler results to a timestamped CSV file.
    
    Args:
        results: List of CompanyResult objects
        output_dir: Directory to save the CSV file
        prefix: Filename prefix (default: "email_crawl")
        location: Optional location string for filename
    
    Returns:
        Path to the saved CSV file
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    loc_slug = _sanitize_filename(location) if location else ""
    
    # Build filename
    parts = [prefix]
    if loc_slug:
        parts.append(loc_slug)
    parts.append(timestamp)
    filename = "_".join(parts) + ".csv"
    
    csv_path = output_dir / filename
    
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        
        for result in results:
            row = _result_to_row(result)
            writer.writerow(row)
    
    print(f"  CSV saved: {csv_path}")
    return csv_path


def save_results_csv_latest(
    results: list[CompanyResult],
    output_dir: Path,
    location: str = "",
) -> Path:
    """
    Save results to a "latest" CSV file (overwrites previous).
    Useful for quick access without timestamps.
    
    Args:
        results: List of CompanyResult objects
        output_dir: Directory to save the CSV file
        location: Optional location string for filename
    
    Returns:
        Path to the saved CSV file
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    filename = "latest_emails.csv"
    csv_path = output_dir / filename
    
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        
        for result in results:
            row = _result_to_row(result)
            writer.writerow(row)
    
    print(f"  Latest CSV saved: {csv_path}")
    return csv_path


def save_results_json(
    results: list[CompanyResult],
    output_dir: Path,
    prefix: str = "email_crawl",
    location: str = "",
) -> Path:
    """
    Save results to a timestamped JSON file.
    
    Args:
        results: List of CompanyResult objects
        output_dir: Directory to save the JSON file
        prefix: Filename prefix
        location: Optional location string for filename
    
    Returns:
        Path to the saved JSON file
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    loc_slug = _sanitize_filename(location) if location else ""
    
    parts = [prefix]
    if loc_slug:
        parts.append(loc_slug)
    parts.append(timestamp)
    filename = "_".join(parts) + ".json"
    
    json_path = output_dir / filename
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            [r.model_dump() for r in results],
            f,
            indent=2,
            default=str,
            ensure_ascii=False,
        )
    
    print(f"  JSON saved: {json_path}")
    return json_path


def save_all_outputs(
    results: list[CompanyResult],
    output_dir: Path,
    prefix: str = "email_crawl",
    location: str = "",
) -> dict[str, str]:
    """
    Save results to both CSV and JSON files.
    
    Returns:
        Dict with paths to saved files: {"csv": str, "json": str, "latest_csv": str}
    """
    csv_path = save_results_csv(results, output_dir, prefix, location)
    json_path = save_results_json(results, output_dir, prefix, location)
    latest_csv_path = save_results_csv_latest(results, output_dir, location)
    
    return {
        "csv": str(csv_path),
        "json": str(json_path),
        "latest_csv": str(latest_csv_path),
    }
