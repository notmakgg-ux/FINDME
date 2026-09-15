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

    # --- Create pipeline_runs table ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id              TEXT PRIMARY KEY,
            location            TEXT,
            status              TEXT DEFAULT 'pending',
            enable_url_finder   BOOLEAN DEFAULT TRUE,
            enable_email_crawler BOOLEAN DEFAULT TRUE,
            min_results         INTEGER,
            queries             JSONB,
            cities              JSONB,
            started_at          TIMESTAMP,
            completed_at        TIMESTAMP,
            duration_seconds    FLOAT,
            url_finder_results  INTEGER DEFAULT 0,
            email_crawler_results INTEGER DEFAULT 0,
            companies_processed INTEGER DEFAULT 0,
            emails_found        INTEGER DEFAULT 0,
            phones_found        INTEGER DEFAULT 0,
            social_found        INTEGER DEFAULT 0,
            errors_count        INTEGER DEFAULT 0,
            error_message       TEXT,
            files               JSONB,
            created_at          TIMESTAMP DEFAULT NOW()
        )
    """)

    # --- Create pipeline_queue table ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_queue (
            queue_id            SERIAL PRIMARY KEY,
            location            TEXT NOT NULL,
            enable_url_finder   BOOLEAN DEFAULT TRUE,
            enable_email_crawler BOOLEAN DEFAULT TRUE,
            min_results         INTEGER DEFAULT 300,
            queries             JSONB,
            cities              JSONB,
            scheduled_at        TIMESTAMP,
            priority            INTEGER DEFAULT 0,
            status              TEXT DEFAULT 'queued',
            created_at          TIMESTAMP DEFAULT NOW()
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
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_runs_created
        ON pipeline_runs(created_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_status_priority
        ON pipeline_queue(status, priority ASC, created_at ASC)
    """)

    # --- Set sequences to start from at least 1000 (safe for existing data) ---
    cur.execute("""
        SELECT setval('company_details_company_id_seq',
            GREATEST(1000, COALESCE((SELECT MAX(company_id) FROM company_details), 0) + 1))
    """)
    cur.execute("""
        SELECT setval('contact_contact_id_seq',
            GREATEST(1000, COALESCE((SELECT MAX(contact_id) FROM contact), 0) + 1))
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

        # --- Replace any existing contact row for this company (idempotent save) ---
        # Guarantees exactly one contact row per company even on re-runs/retries
        cur.execute("DELETE FROM contact WHERE company_id = %s", (company_id,))

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


# ---------------------------------------------------------------------------
# Pipeline Runs & Queue Operations (with JSON fallback)
# ---------------------------------------------------------------------------

import json
from pathlib import Path

_STORAGE_DIR = Path(__file__).parent
_RUNS_FILE = _STORAGE_DIR / "runs_history.json"
_QUEUE_FILE = _STORAGE_DIR / "queue.json"


def _read_json_file(path: Path) -> list:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _write_json_file(path: Path, data: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to write fallback file {path}: {e}")


def upsert_pipeline_run(run_data: dict):
    """Save or update a pipeline run record in the database (and JSON fallback)."""
    run_id = run_data.get("run_id")
    if not run_id:
        return

    # 1. Update JSON fallback first
    runs = _read_json_file(_RUNS_FILE)
    existing_idx = next((i for i, r in enumerate(runs) if r.get("run_id") == run_id), None)
    clean_data = dict(run_data)
    if existing_idx is not None:
        runs[existing_idx].update(clean_data)
    else:
        runs.insert(0, clean_data)
    _write_json_file(_RUNS_FILE, runs[:100])

    # 2. Try PostgreSQL
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pipeline_runs (
                run_id, location, status, enable_url_finder, enable_email_crawler,
                min_results, queries, cities, started_at, completed_at,
                duration_seconds, url_finder_results, email_crawler_results,
                companies_processed, emails_found, phones_found, social_found,
                errors_count, error_message, files
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                completed_at = EXCLUDED.completed_at,
                duration_seconds = EXCLUDED.duration_seconds,
                url_finder_results = EXCLUDED.url_finder_results,
                email_crawler_results = EXCLUDED.email_crawler_results,
                companies_processed = EXCLUDED.companies_processed,
                emails_found = EXCLUDED.emails_found,
                phones_found = EXCLUDED.phones_found,
                social_found = EXCLUDED.social_found,
                errors_count = EXCLUDED.errors_count,
                error_message = EXCLUDED.error_message,
                files = EXCLUDED.files
        """, (
            run_id,
            run_data.get("location"),
            run_data.get("status", "pending"),
            run_data.get("enable_url_finder", True),
            run_data.get("enable_email_crawler", True),
            run_data.get("min_results"),
            json.dumps(run_data.get("queries")) if run_data.get("queries") is not None else None,
            json.dumps(run_data.get("cities")) if run_data.get("cities") is not None else None,
            run_data.get("started_at"),
            run_data.get("completed_at"),
            run_data.get("duration_seconds"),
            run_data.get("url_finder_results", 0),
            run_data.get("email_crawler_results", 0),
            run_data.get("companies_processed", 0),
            run_data.get("emails_found", 0),
            run_data.get("phones_found", 0),
            run_data.get("social_found", 0),
            run_data.get("errors_count", 0),
            run_data.get("error_message"),
            json.dumps(run_data.get("files")) if run_data.get("files") is not None else None,
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug(f"DB upsert_pipeline_run: {e}")


def get_pipeline_runs(limit: int = 50) -> list[dict]:
    """Retrieve persisted pipeline runs."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                run_id, location, status, enable_url_finder, enable_email_crawler,
                min_results, queries, cities, started_at, completed_at,
                duration_seconds, url_finder_results, email_crawler_results,
                companies_processed, emails_found, phones_found, social_found,
                errors_count, error_message, files, created_at
            FROM pipeline_runs
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(zip(columns, r)) for r in rows]
    except Exception as e:
        logger.debug(f"DB get_pipeline_runs failed, reading JSON fallback: {e}")
        return _read_json_file(_RUNS_FILE)[:limit]


def get_pipeline_run(run_id: str) -> Optional[dict]:
    """Retrieve a single pipeline run by id."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                run_id, location, status, enable_url_finder, enable_email_crawler,
                min_results, queries, cities, started_at, completed_at,
                duration_seconds, url_finder_results, email_crawler_results,
                companies_processed, emails_found, phones_found, social_found,
                errors_count, error_message, files, created_at
            FROM pipeline_runs
            WHERE run_id = %s
        """, (run_id,))
        row = cur.fetchone()
        if row:
            columns = [desc[0] for desc in cur.description]
            cur.close()
            conn.close()
            return dict(zip(columns, row))
        cur.close()
        conn.close()
    except Exception:
        pass

    for r in _read_json_file(_RUNS_FILE):
        if r.get("run_id") == run_id:
            return r
    return None


def enqueue_run(item: dict) -> int:
    """Add a single run to the pipeline queue."""
    location = item.get("location", "").strip()
    if not location:
        raise ValueError("Location is required")

    # JSON fallback
    queue = _read_json_file(_QUEUE_FILE)
    next_id = max([q.get("queue_id", 0) for q in queue] or [0]) + 1
    new_item = {
        "queue_id": next_id,
        "location": location,
        "enable_url_finder": item.get("enable_url_finder", True),
        "enable_email_crawler": item.get("enable_email_crawler", True),
        "min_results": item.get("min_results", 300),
        "queries": item.get("queries"),
        "cities": item.get("cities"),
        "scheduled_at": item.get("scheduled_at"),
        "priority": len(queue),
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    queue.append(new_item)
    _write_json_file(_QUEUE_FILE, queue)

    # PostgreSQL
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pipeline_queue (
                location, enable_url_finder, enable_email_crawler,
                min_results, queries, cities, scheduled_at, priority, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING queue_id
        """, (
            location,
            new_item["enable_url_finder"],
            new_item["enable_email_crawler"],
            new_item["min_results"],
            json.dumps(new_item["queries"]) if new_item["queries"] is not None else None,
            json.dumps(new_item["cities"]) if new_item["cities"] is not None else None,
            new_item["scheduled_at"],
            new_item["priority"],
            "queued",
        ))
        qid = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return qid
    except Exception as e:
        logger.debug(f"DB enqueue_run: {e}")
        return next_id


def _get_location_queue_status(locations: list[str]) -> dict[str, str]:
    """Map lowercase location -> pipeline_queue status for the given locations.

    Reads ALL rows (not just active ones) so callers can dedup against
    completed runs too. Falls back to the JSON queue file if the DB is
    unreachable (the JSON file may not contain completed rows, so the DB is
    authoritative when available).
    """
    wanted = {str(loc).strip().lower() for loc in locations if loc and str(loc).strip()}
    status_map: dict[str, str] = {}
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SELECT location, status FROM pipeline_queue")
        for loc, status in cur.fetchall():
            key = str(loc).strip().lower()
            if key in wanted:
                status_map[key] = status
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug(f"_get_location_queue_status fell back to JSON queue: {e}")
        for q in _read_json_file(_QUEUE_FILE):
            key = str(q.get("location", "")).strip().lower()
            if key in wanted:
                status_map[key] = q.get("status", "")
    return status_map


def requeue_running_locations() -> list[int]:
    """Reset pipeline_queue rows stuck at status='running' back to 'queued'.

    Call when no pipeline process is alive (fresh start / after a crash) so
    the runner picks those locations up again instead of skipping them.
    Returns the queue_ids that were requeued.
    """
    # JSON fallback queue first
    requeued: list[int] = []
    queue = _read_json_file(_QUEUE_FILE)
    changed = False
    for q in queue:
        if q.get("status") == "running":
            q["status"] = "queued"
            requeued.append(q.get("queue_id"))
            changed = True
    if changed:
        _write_json_file(_QUEUE_FILE, queue)
    # PostgreSQL is authoritative when reachable
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SELECT queue_id FROM pipeline_queue WHERE status = 'running'")
        requeued = [row[0] for row in cur.fetchall()]
        if requeued:
            cur.execute("UPDATE pipeline_queue SET status = 'queued' WHERE status = 'running'")
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"requeue_running_locations failed: {e}")
    return requeued


def enqueue_locations_batch(
    locations: list[str],
    enable_url_finder: bool = True,
    enable_email_crawler: bool = True,
    min_results: int = 300,
    queries: Optional[list[str]] = None,
    cities: Optional[list[str]] = None,
) -> tuple[list[int], list[str]]:
    """Bulk-queue one full pipeline run per location (URL Finder + Email Crawler).

    Each location becomes its own queue item. The QueueManager runs them
    sequentially in priority/created order — when one finishes, the next starts.

    Idempotent: locations already present in pipeline_queue with status
    'queued', 'running', or 'completed' are skipped, so re-submitting the
    same list (e.g. after a restart) never creates duplicate rows.

    Returns (queue_ids_created, skipped_locations).
    """
    if not locations:
        return [], []

    existing_status = _get_location_queue_status(locations)
    skipped: list[str] = [
        loc for loc in locations
        if existing_status.get(str(loc).strip().lower()) in ("queued", "running", "completed")
    ]
    locations = [
        loc for loc in locations
        if existing_status.get(str(loc).strip().lower()) not in ("queued", "running", "completed")
    ]
    if not locations:
        return [], skipped

    added: list[int] = []
    base_queue = _read_json_file(_QUEUE_FILE)
    base_next_id = max([q.get("queue_id", 0) for q in base_queue] or [0])

    for idx, loc in enumerate(locations):
        loc = loc.strip()
        if not loc:
            continue

        next_id = base_next_id + idx + 1
        new_item = {
            "queue_id": next_id,
            "location": loc,
            "enable_url_finder": enable_url_finder,
            "enable_email_crawler": enable_email_crawler,
            "min_results": min_results,
            "queries": queries,
            "cities": cities,
            "scheduled_at": None,
            "priority": len(base_queue) + idx,
            "status": "queued",
            "created_at": datetime.now().isoformat(),
        }
        base_queue.append(new_item)
        added.append(next_id)

    _write_json_file(_QUEUE_FILE, base_queue)

    # PostgreSQL batch insert
    try:
        conn = _get_connection()
        cur = conn.cursor()
        db_ids: list[int] = []
        for qid, loc in zip(added, [l.strip() for l in locations if l.strip()]):
            cur.execute("""
                INSERT INTO pipeline_queue (
                    location, enable_url_finder, enable_email_crawler,
                    min_results, queries, cities, scheduled_at, priority, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING queue_id
            """, (
                loc,
                enable_url_finder,
                enable_email_crawler,
                min_results,
                json.dumps(queries) if queries is not None else None,
                json.dumps(cities) if cities is not None else None,
                None,
                len(base_queue) - len(added) + added.index(qid),
                "queued",
            ))
            db_ids.append(cur.fetchone()[0])
        conn.commit()
        cur.close()
        conn.close()

        # Realign the JSON fallback file's ids with the DB-assigned ids so
        # status updates (which key on queue_id) keep matching if the DB ever
        # becomes unreachable and the JSON file takes over.
        id_map = dict(zip(added, db_ids))
        json_queue = _read_json_file(_QUEUE_FILE)
        other_ids = {q.get("queue_id") for q in json_queue} - set(added)
        if not (set(db_ids) & other_ids):
            for q in json_queue:
                if q.get("queue_id") in id_map:
                    q["queue_id"] = id_map[q["queue_id"]]
            _write_json_file(_QUEUE_FILE, json_queue)
        added = db_ids
    except Exception as e:
        logger.debug(f"DB enqueue_locations_batch: {e}")

    return added, skipped


def get_queue_items() -> list[dict]:
    """Get active items in queue ordered by priority."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                queue_id, location, enable_url_finder, enable_email_crawler,
                min_results, queries, cities, scheduled_at, priority, status, created_at
            FROM pipeline_queue
            WHERE status IN ('queued', 'running')
            ORDER BY priority ASC, created_at ASC
        """)
        columns = [desc[0] for desc in cur.description]
        items = [dict(zip(columns, r)) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return items
    except Exception:
        items = [q for q in _read_json_file(_QUEUE_FILE) if q.get("status") in ("queued", "running")]
        items.sort(key=lambda x: (x.get("priority", 0), x.get("created_at", "")))
        return items


def delete_queue_item(queue_id: int) -> bool:
    """Remove an item from the queue."""
    # JSON
    queue = [q for q in _read_json_file(_QUEUE_FILE) if q.get("queue_id") != queue_id]
    _write_json_file(_QUEUE_FILE, queue)

    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM pipeline_queue WHERE queue_id = %s", (queue_id,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return True


def update_queue_item_status(queue_id: int, status: str) -> bool:
    """Update status of a queue item."""
    queue = _read_json_file(_QUEUE_FILE)
    for q in queue:
        if q.get("queue_id") == queue_id:
            q["status"] = status
    _write_json_file(_QUEUE_FILE, queue)

    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE pipeline_queue SET status = %s WHERE queue_id = %s", (status, queue_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return True


def reorder_queue(queue_ids: list[int]) -> bool:
    """Reorder queue items to match the given list of IDs."""
    queue = _read_json_file(_QUEUE_FILE)
    id_map = {q.get("queue_id"): q for q in queue}
    new_queue = []
    for prio, qid in enumerate(queue_ids):
        if qid in id_map:
            id_map[qid]["priority"] = prio
            new_queue.append(id_map[qid])
    # Add any remaining
    for q in queue:
        if q.get("queue_id") not in queue_ids:
            new_queue.append(q)
    _write_json_file(_QUEUE_FILE, new_queue)

    try:
        conn = _get_connection()
        cur = conn.cursor()
        for prio, qid in enumerate(queue_ids):
            cur.execute("UPDATE pipeline_queue SET priority = %s WHERE queue_id = %s", (prio, qid))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return True


def get_failed_companies(location: Optional[str] = None) -> list[dict]:
    """Fetch companies marked as failed."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        query = """
            SELECT company_id, company_name, website_url, location, error_type, error_message
            FROM company_details
            WHERE status = 'failed'
        """
        params = []
        if location and location.strip():
            query += " AND location ILIKE %s"
            params.append(f"%{location.strip()}%")
        query += " ORDER BY company_id DESC LIMIT 500"
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Failed to get failed companies: {e}")
        return []


def reset_failed_companies_to_pending(location: Optional[str] = None) -> int:
    """Reset status of failed companies to pending for re-crawling."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        query = "UPDATE company_details SET status = 'pending', error_message = NULL, error_type = NULL WHERE status = 'failed'"
        params = []
        if location and location.strip():
            query += " AND location ILIKE %s"
            params.append(f"%{location.strip()}%")
        cur.execute(query, params)
        count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Failed to reset failed companies: {e}")
        return 0


def insert_bulk_companies(companies: list[dict], default_location: str = "CSV Upload") -> int:
    """Bulk insert companies into company_details."""
    if not companies:
        return 0
    from utils.urls import normalize_url, get_root_domain
    count = 0
    try:
        conn = _get_connection()
        cur = conn.cursor()
        for comp in companies:
            raw_url = comp.get("website") or comp.get("website_url") or comp.get("url") or ""
            if not raw_url:
                continue
            name = comp.get("company_name") or comp.get("name") or raw_url
            loc = comp.get("location") or default_location
            try:
                norm = normalize_url(raw_url)
                domain = get_root_domain(norm)
            except Exception:
                norm = raw_url
                domain = raw_url

            cur.execute("""
                INSERT INTO company_details (
                    company_name, website_url, normalized_url, root_domain, location, status
                ) VALUES (%s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (website_url) DO NOTHING
            """, (name, raw_url, norm, domain, loc))
            count += cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Bulk insert failed: {e}")
    return count
