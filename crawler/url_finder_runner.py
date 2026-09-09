"""
Shared URL Finder runner
========================
Single implementation of "search for real estate company websites for a
location and insert them into company_details".

Used by:
    - crawler/run_pipeline_cli.py  (CLI pipeline runner)
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Path to the shared default search queries
_DEFAULT_QUERIES_PATH = (
    Path(__file__).parent.parent / "findme" / "url_finder" / "default_queries.yaml"
)

# Fallback queries if default_queries.yaml is missing
_FALLBACK_QUERIES = [
    "real estate company {location}",
    "realtor {location}",
    "property management company {location}",
    "real estate agency {location}",
    "property dealer {location}",
    "real estate brokerage {location}",
    "real estate agents {location}",
    "home buying company {location}",
    "commercial real estate {location}",
    "residential real estate {location}",
    "property investment {location}",
    "real estate developers {location}",
    "rental property management {location}",
    "real estate consultants {location}",
    "property appraisal {location}",
]

# Default query templates for multi-city mode (each {city} × location expands)
_CITY_DEFAULT_TEMPLATES = [
    "real estate agency {city} {location}",
    "real estate company {city} {location}",
    "real estate agents {city} {location}",
    "realtor {city} {location}",
    "real estate broker {city} {location}",
    "property management {city} {location}",
    "property management company {city} {location}",
    "property managers {city} {location}",
    "residential real estate {city} {location}",
    "commercial real estate {city} {location}",
    "real estate developer {city} {location}",
    "property developer {city} {location}",
    "buyers agent {city} {location}",
    "property valuation {city} {location}",
    "property appraisal {city} {location}",
    "real estate consulting {city} {location}",
]


def load_search_queries(location: str) -> list[str]:
    """Load default search queries with {location} replaced."""
    raw_queries = []
    if _DEFAULT_QUERIES_PATH.exists():
        with open(_DEFAULT_QUERIES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            raw_queries = data.get("SEARCH_QUERIES", [])

    if not raw_queries:
        logger.warning("default_queries.yaml not found, using fallback queries")
        raw_queries = _FALLBACK_QUERIES

    return [q.replace("{location}", location) for q in raw_queries]


def build_search_queries(
    location: str,
    queries: list[str] | None = None,
    cities: list[str] | None = None,
    pre_formed_queries: bool = False,
) -> list[str]:
    """
    Build the list of search queries for a location.

    Three modes:
    1. pre_formed_queries=True  → queries used as-is (no placeholder replacement)
    2. cities provided          → each query with {city} expands across all cities
    3. default                  → {location} replacement only (from default_queries.yaml)
    """
    if pre_formed_queries and queries:
        return list(queries)

    if cities:
        base = queries or _CITY_DEFAULT_TEMPLATES
        expanded = []
        for city in cities:
            for q in base:
                expanded.append(q.replace("{city}", city).replace("{location}", location))
        return expanded

    if queries:
        return [q.replace("{location}", location) for q in queries]

    return load_search_queries(location)


def run_url_finder(
    location: str,
    min_results: int,
    on_new_result=None,
    search_queries: list[str] | None = None,
    control=None,
) -> list[dict]:
    """
    Run the URL Finder scraper synchronously and return found website dicts.

    Args:
        location: Location string, e.g. "Miami, Florida, USA"
        min_results: Minimum unique results target
        on_new_result: Optional callback(record_dict) invoked with each NEW
                       unique URL as it is found (for real-time DB saves).
        search_queries: Optional explicit query list (defaults to
                        default_queries.yaml with {location} replaced).
        control: Optional ControlEvent for cooperative cancellation/pause.

    Returns:
        List of result dicts: website, domain, title, snippet, source_query,
        source_engine, location, score, found_at.
    """
    from config import settings

    if search_queries is None:
        search_queries = load_search_queries(location)

    config_data = {
        "LOCATION": location,
        "SEARCH_ENGINES": ["playwright"],   # Playwright-only (Bug #5 fix)
        "MAX_RESULTS_PER_QUERY": 25,
        "MIN_SCORE": settings.min_score,
        "REQUEST_DELAY": settings.request_delay,
        "SEARCH_QUERIES": search_queries,
        "MIN_UNIQUE_RESULTS": min_results,
        "MAX_RETRIES": settings.max_retries_scraper,
    }

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".yaml")
    tmp_path = Path(tmp_path_str)
    with os.fdopen(tmp_fd, "w") as f:
        yaml.dump(config_data, f)

    try:
        # Import from findme/url_finder (added to sys.path by callers)
        from scraper_config import Config as ScraperConfig
        from scraper import RealEstateScraper

        config = ScraperConfig(str(tmp_path))
        scraper = RealEstateScraper(config, on_new_result=on_new_result, control=control)

        # FIX #3: scraper.run() is now synchronous — no nested event loop needed.
        return scraper.run()
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def insert_company_now(record: dict, location: str):
    """Insert a single URL Finder result into company_details immediately."""
    from storage.db import _get_connection

    website = record.get("website", "")
    if not website:
        return
    snippet = record.get("snippet", "")

    company_name = _extract_company_name(record.get("title", ""), record.get("domain", ""))

    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO company_details (company_name, website_url, snippet, location, status)
            VALUES (%s, %s, %s, %s, 'pending')
            ON CONFLICT (website_url) DO NOTHING
        """, (company_name, website, snippet[:500] if snippet else "", location))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def save_url_finder_to_db(results: list[dict], location: str) -> int:
    """Insert URL Finder results into company_details (idempotent bulk save)."""
    from storage.db import _get_connection

    conn = _get_connection()
    cur = conn.cursor()
    try:
        inserted = 0
        for r in results:
            website = r.get("website", "")
            if not website:
                continue

            company_name = _extract_company_name(r.get("title", ""), r.get("domain", ""))

            cur.execute("""
                INSERT INTO company_details (company_name, website_url, snippet, location, status)
                VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (website_url) DO NOTHING
            """, (company_name, website, r.get("snippet", "")[:500], location))
            if cur.rowcount > 0:
                inserted += 1

        conn.commit()
        logger.info(f"Inserted {inserted}/{len(results)} URL Finder results into company_details")
        return inserted
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save URL Finder results to DB: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def save_url_finder_outputs(results: list[dict], output_dir, location: str) -> dict:
    """Write URL Finder results to timestamped CSV + JSON files. Returns paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    loc_slug = location.lower().replace(" ", "_").replace(",", "").replace(".", "")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"url_finder_{loc_slug}_{timestamp}"

    csv_path = output_dir / f"{prefix}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("website,domain,title,source_query,source_engine,location,score\n")
        for r in results:
            t = r.get("title", "").replace('"', '""')
            s = r.get("source_query", "").replace('"', '""')
            f.write(
                f'"{r["website"]}","{r.get("domain", "")}","{t}","{s}",'
                f'"{r.get("source_engine", "")}","{r.get("location", "")}",{r.get("score", 0)}\n'
            )

    json_path = output_dir / f"{prefix}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    logger.info(f"Saved URL Finder outputs: {csv_path}, {json_path}")
    return {"csv": str(csv_path), "json": str(json_path)}


def _extract_company_name(title: str, domain: str) -> str:
    """Derive a company name from a search-result title or fall back to domain."""
    company_name = title.split(" - ")[0].split(" | ")[0].strip() if title else domain
    if not company_name or len(company_name) < 2:
        company_name = domain
    return company_name
