"""
PostgreSQL database storage for the contact scraper.

Two tables:
    company_details: company info (PK: company_id)
    contact: email/phone/social data (PK: contact_id, FK: company_id)

NULL values for missing data, handles failures gracefully.
"""

import logging
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _get_connection():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )


def init_database():
    """
    Create the database and tables if they don't exist.
    Safe to call multiple times (uses IF NOT EXISTS).
    """
    try:
        # First, try to connect to the target database
        conn = _get_connection()
    except psycopg2.OperationalError:
        # Database doesn't exist — connect to 'postgres' and create it
        logger.info(f"Database '{settings.db_name}' not found, creating it...")
        try:
            conn = psycopg2.connect(
                host=settings.db_host,
                port=settings.db_port,
                dbname="postgres",
                user=settings.db_user,
                password=settings.db_password,
            )
            conn.autocommit = True
            cur = conn.cursor()
            # Use safe identifier creation
            cur.execute(sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(settings.db_name)
            ))
            cur.close()
            conn.close()
            logger.info(f"Database '{settings.db_name}' created successfully")
            conn = _get_connection()
        except Exception as e:
            logger.error(f"Failed to create database: {e}")
            raise

    cur = conn.cursor()

    # --- Create company_details table ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS company_details (
            company_id      SERIAL PRIMARY KEY,
            company_name    TEXT,
            website_url     TEXT UNIQUE,
            snippet         TEXT,
            location        TEXT,
            contact_info    TEXT,
            normalized_url  TEXT,
            root_domain     TEXT,
            status          TEXT DEFAULT 'pending',
            pages_crawled   INTEGER DEFAULT 0,
            pages_discovered INTEGER DEFAULT 0,
            duration_seconds FLOAT,
            error_type      TEXT,
            error_message   TEXT,
            started_at      TIMESTAMP,
            completed_at    TIMESTAMP,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)

    # --- Create contact table ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contact (
            contact_id      SERIAL PRIMARY KEY,
            company_id      INTEGER REFERENCES company_details(company_id) ON DELETE CASCADE,
            email1          TEXT,
            email2          TEXT,
            email3          TEXT,
            email4          TEXT,
            email5          TEXT,
            phone1          TEXT,
            phone2          TEXT,
            phone3          TEXT,
            instagram_link  TEXT,
            facebook_link   TEXT,
            linkedin_link   TEXT,
            best_contact_name   TEXT,
            best_contact_role   TEXT,
            best_contact_score  INTEGER,
            pages_crawled   INTEGER,
            emails_found    INTEGER DEFAULT 0,
            phones_found    INTEGER DEFAULT 0,
            social_links_found INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'pending',
            all_emails_json JSONB,
            all_phones_json JSONB,
            all_social_json JSONB,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)

    # --- Create indexes ---
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_company_website
        ON company_details(website_url)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_contact_company
        ON contact(company_id)
    """)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database tables initialized successfully")


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def upsert_company_result(result) -> int:
    """
    Insert or update a company result and its contacts.
    Returns the company_id.

    Uses the CompanyResult model from schemas.py.
    NULL values for missing data.
    """
    conn = _get_connection()
    cur = conn.cursor()

    try:
        # --- Upsert company_details ---
        cur.execute("""
            INSERT INTO company_details (
                company_name, website_url, normalized_url, root_domain,
                status, pages_crawled, pages_discovered,
                duration_seconds, error_type, error_message,
                started_at, completed_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            ON CONFLICT (website_url) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                normalized_url = EXCLUDED.normalized_url,
                root_domain = EXCLUDED.root_domain,
                status = EXCLUDED.status,
                pages_crawled = EXCLUDED.pages_crawled,
                pages_discovered = EXCLUDED.pages_discovered,
                duration_seconds = EXCLUDED.duration_seconds,
                error_type = EXCLUDED.error_type,
                error_message = EXCLUDED.error_message,
                started_at = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at
            RETURNING company_id
        """, (
            result.company_name,
            result.original_website,
            result.normalized_url or None,
            result.root_domain or None,
            result.status.value if hasattr(result.status, 'value') else result.status,
            result.pages_crawled,
            result.pages_discovered,
            result.duration_seconds,
            result.error_type or None,
            result.error_message or None,
            result.started_at,
            result.completed_at,
        ))

        company_id = cur.fetchone()[0]

        # --- Extract top 5 emails, 3 phones, social links ---
        sorted_emails = sorted(result.emails, key=lambda e: e.confidence_score, reverse=True)

        email1 = sorted_emails[0].email if len(sorted_emails) > 0 else None
        email2 = sorted_emails[1].email if len(sorted_emails) > 1 else None
        email3 = sorted_emails[2].email if len(sorted_emails) > 2 else None
        email4 = sorted_emails[3].email if len(sorted_emails) > 3 else None
        email5 = sorted_emails[4].email if len(sorted_emails) > 4 else None

        phones = result.phones if result.phones else []
        phone1 = phones[0].phone if len(phones) > 0 else None
        phone2 = phones[1].phone if len(phones) > 1 else None
        phone3 = phones[2].phone if len(phones) > 2 else None

        social = result.social_links if result.social_links else None
        linkedin = result.linkedin_url or None
        facebook = result.facebook_url or None
        instagram = result.instagram_url or None

        # If no direct fields, search social_links list
        if not linkedin and social:
            for s in social:
                if hasattr(s, 'platform') and s.platform == "linkedin":
                    linkedin = s.url
                    break
        if not facebook and social:
            for s in social:
                if hasattr(s, 'platform') and s.platform == "facebook":
                    facebook = s.url
                    break
        if not instagram and social:
            for s in social:
                if hasattr(s, 'platform') and s.platform == "instagram":
                    instagram = s.url
                    break

        # --- Insert contact row ---
        cur.execute("""
            INSERT INTO contact (
                company_id,
                email1, email2, email3, email4, email5,
                phone1, phone2, phone3,
                instagram_link, facebook_link, linkedin_link,
                best_contact_name, best_contact_role, best_contact_score,
                pages_crawled, emails_found, phones_found, social_links_found,
                status,
                all_emails_json, all_phones_json, all_social_json
            ) VALUES (
                %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s,
                %s, %s, %s
            )
        """, (
            company_id,
            email1, email2, email3, email4, email5,
            phone1, phone2, phone3,
            instagram, facebook, linkedin,
            result.best_contact_name or None,
            result.best_contact_role or None,
            result.best_contact_score,
            result.pages_crawled,
            result.emails_found,
            result.phones_found,
            result.social_links_found,
            result.status.value if hasattr(result.status, 'value') else result.status,
            # JSON blobs for full data
            psycopg2.extras.Json([
                {
                    "email": e.email,
                    "name": e.person_name or None,
                    "role": e.role or e.job_title or None,
                    "score": e.confidence_score,
                    "source_url": e.source_url,
                    "extraction_method": getattr(e.extraction_method, 'value', str(e.extraction_method)),
                    "page_type": getattr(e.source_page_type, 'value', str(e.source_page_type)),
                }
                for e in sorted_emails
            ]) if sorted_emails else None,
            psycopg2.extras.Json([
                {
                    "phone": p.phone,
                    "raw_phone": p.raw_phone or None,
                    "country_code": p.country_code or None,
                    "is_fax": p.is_fax,
                    "is_mobile": p.is_mobile,
                }
                for p in phones
            ]) if phones else None,
            psycopg2.extras.Json([
                {
                    "platform": s.platform,
                    "url": s.url,
                    "profile_name": s.profile_name or None,
                    "is_company_page": s.is_company_page,
                    "is_personal_profile": s.is_personal_profile,
                }
                for s in social
            ]) if social else None,
        ))

        conn.commit()
        return company_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to upsert company result: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def write_results_batch(results: list) -> int:
    """
    Write a batch of CompanyResult objects to PostgreSQL.
    Returns the number of rows inserted/updated.
    """
    count = 0
    for result in results:
        try:
            upsert_company_result(result)
            count += 1
        except Exception as e:
            logger.error(f"Failed to write result for {result.company_name}: {e}")
    return count


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_completed_websites() -> set:
    """Get set of website URLs that are already completed (for resume mode)."""
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT website_url FROM company_details
            WHERE status = 'completed'
        """)
        return {row[0].strip() for row in cur.fetchall()}
    except Exception as e:
        logger.error(f"Failed to get completed websites: {e}")
        return set()
    finally:
        cur.close()
        conn.close()


def get_all_companies() -> list[dict]:
    """Get all companies with their contacts."""
    conn = _get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT
                cd.company_id, cd.company_name, cd.website_url, cd.snippet,
                cd.location, cd.contact_info, cd.normalized_url, cd.root_domain,
                cd.status, cd.pages_crawled, cd.pages_discovered,
                cd.duration_seconds, cd.error_type, cd.error_message,
                c.contact_id,
                c.email1, c.email2, c.email3, c.email4, c.email5,
                c.phone1, c.phone2, c.phone3,
                c.instagram_link, c.facebook_link, c.linkedin_link,
                c.best_contact_name, c.best_contact_role, c.best_contact_score,
                c.emails_found, c.phones_found, c.social_links_found
            FROM company_details cd
            LEFT JOIN contact c ON cd.company_id = c.company_id
            ORDER BY cd.company_id
        """)
        return cur.fetchall()
    except Exception as e:
        logger.error(f"Failed to get companies: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def get_stats() -> dict:
    """Get summary statistics from the database."""
    conn = _get_connection()
    cur = conn.cursor()
    try:
        stats = {}

        cur.execute("SELECT COUNT(*) FROM company_details")
        stats["total_companies"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM company_details WHERE status = 'completed'")
        stats["completed"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM company_details WHERE status = 'failed'")
        stats["failed"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM contact WHERE email1 IS NOT NULL")
        stats["companies_with_emails"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM contact WHERE phone1 IS NOT NULL")
        stats["companies_with_phones"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM contact WHERE linkedin_link IS NOT NULL OR facebook_link IS NOT NULL OR instagram_link IS NOT NULL")
        stats["companies_with_social"] = cur.fetchone()[0]

        return stats
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return {}
    finally:
        cur.close()
        conn.close()
