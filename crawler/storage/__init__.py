"""
PostgreSQL storage — reads input companies and writes discovered
contacts (emails, phones, social links) to the database.

Replaces the old Google Sheets integration entirely.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from config import settings
from models.schemas import CompanyResult, ExtractedEmail, ExtractedPhone, ExtractedSocial

# Database operations
from storage.db import (
    init_database,
    write_results_batch,
    get_completed_websites,
    get_all_companies,
    get_stats,
    upsert_company_result,
)

# CSV writer (still available for local file exports)
from storage.csv_writer import (
    save_results_csv,
    save_results_csv_latest,
    save_results_json,
    save_all_outputs,
)

logger = logging.getLogger(__name__)

# Initialize database on import
try:
    init_database()
except Exception as e:
    logger.warning(f"Database initialization deferred: {e}")


def read_input_companies(table_name: str = "company_details") -> list[dict]:
    """
    Read companies from PostgreSQL database or a CSV file.
    
    Supports multiple input formats:
    1. PostgreSQL company_details table
    2. CSV file in input/ directory
    """
    # Try PostgreSQL first
    try:
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SELECT company_name, website_url, snippet, location FROM company_details ORDER BY company_id")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if rows:
            companies = []
            for row in rows:
                company_name = row[0] or ""
                website = row[1] or ""
                if website:
                    companies.append({
                        "company_name": company_name or website,
                        "website": website.strip(),
                        "snippet": row[2] or "",
                        "location": row[3] or "",
                        "extra_columns": {},
                    })
            if companies:
                logger.info(f"Read {len(companies)} companies from PostgreSQL")
                return companies
    except Exception as e:
        logger.debug(f"PostgreSQL read failed, trying CSV: {e}")

    # Fallback: read from CSV file
    return _read_input_from_csv()


def _read_input_from_csv() -> list[dict]:
    """Read companies from CSV files in the input/ directory."""
    import csv
    from pathlib import Path

    input_dir = settings.input_dir
    if not input_dir.exists():
        logger.warning(f"Input directory {input_dir} does not exist")
        return []

    companies = []
    for csv_file in input_dir.glob("*.csv"):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    lower_row = {k.lower().strip(): v for k, v in row.items()}
                    website = str(lower_row.get("website", "")).strip()
                    company_name = str(lower_row.get("company_name", lower_row.get("title", lower_row.get("domain", "")))).strip()
                    if not company_name and website:
                        from urllib.parse import urlparse
                        parsed = urlparse(website)
                        company_name = parsed.netloc.replace("www.", "")
                    if website:
                        companies.append({
                            "company_name": company_name or website,
                            "website": website,
                            "extra_columns": {k: v for k, v in row.items()
                                              if k.lower() not in ("company_name", "website", "title", "domain")},
                        })
            logger.info(f"Read {len(companies)} companies from CSV files")
        except Exception as e:
            logger.error(f"Failed to read CSV {csv_file}: {e}")

    return companies


def write_results(
    results: list[CompanyResult],
    **kwargs,
):
    """
    Write contact discovery results to PostgreSQL database.
    
    Each result creates:
    - A row in company_details (upserted by website_url)
    - A row in contact with emails, phones, social links
    
    NULL values for missing data. Handles failures gracefully.
    """
    if not results:
        return True

    try:
        count = write_results_batch(results)
        logger.info(f"Wrote {count}/{len(results)} results to PostgreSQL database")
        return True
    except Exception as e:
        logger.error(f"Failed to write results to database: {e}")
        return False


def get_all_sheet_names() -> list[str]:
    """Get table names from the database."""
    try:
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'public'
        """)
        tables = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return tables
    except Exception as e:
        logger.error(f"Failed to list tables: {e}")
        return []


def get_spreadsheet():
    """Compatibility stub — returns database stats instead of spreadsheet."""
    return get_stats()
