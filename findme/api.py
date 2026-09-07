"""
FINDME API
==========
Unified API for the FINDME Real Estate Discovery Pipeline.

Modules:
  - URL Finder: Searches DuckDuckGo for company websites
  - Email Crawler: Crawls found websites to discover email contacts

Run with:
    python -m uvicorn findme.api:app --host 0.0.0.0 --port 8001

Swagger UI:
    http://localhost:8001/docs
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import json
import os
import sys
import uuid
import time
import traceback
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

# Add module paths BEFORE any imports
_findme_dir = str(Path(__file__).parent)
_url_finder_dir = str(Path(__file__).parent / "url_finder")
_crawler_dir = str(Path(__file__).parent.parent / "crawler")
for _p in [_findme_dir, _url_finder_dir, _crawler_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- URL Finder imports ---
from url_finder.scraper import RealEstateScraper
from url_finder.scraper_config import Config as ScraperConfig

# Google Sheets for URL Finder
try:
    from url_finder.sheets import push_results_to_sheet, list_sheets as uf_list_sheets, create_or_get_spreadsheet
    SHEETS_AVAILABLE = True
except Exception:
    SHEETS_AVAILABLE = False

# --- Email Crawler imports (from crawler/ directory) ---
from config import settings as ec_settings
from storage import (
    read_input_companies as ec_read_input,
    write_results as ec_write_results,
    get_completed_websites as ec_get_completed,
    get_stats as ec_get_stats,
)
from pipeline import process_companies as ec_process_companies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FINDME API",
    description=(
        "FINDME - Real Estate Discovery Pipeline\n\n"
        "**Module 1: URL Finder** - Searches DuckDuckGo "
        "to find real estate company websites for any location.\n\n"
        "**Module 2: Email Crawler** - Crawls discovered websites to extract, "
        "classify, validate, and score email contacts.\n\n"
        "## Pipeline Flow\n"
        "1. POST `/scrape` with a location -> finds 500+ company websites\n"
        "2. If `email_crawler: true` (default), automatically starts email discovery\n"
        "3. Results write to Google Sheets as `{location}` and `{location} Results` tabs\n"
        "4. Summary saved to `output/summary.txt`\n\n"
        "## Quick Start\n"
        "POST `/scrape` with `{\"location\": \"Atlanta, Georgia\"}` - that's it!\n\n"
        "## Multi-City Mode\n"
        "POST `/scrape` with a `cities` array to search each city individually:\n"
        '`{\"location\": \"Victoria, Australia\", `'
        '`\"cities\": [\"Geelong\", \"Ballarat\", \"Bendigo\"], `'
        '`\"queries\": [\"real estate agency {city} {location}\"]}`\n'
        "Queries with `{city}` expand across all cities automatically."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Output directory
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# In-memory stores
UF_RUNS: dict[str, dict[str, Any]] = {}  # URL Finder runs
EC_RUNS: dict[str, dict[str, Any]] = {}  # Email Crawler runs


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    """Start a URL scrape + optional email crawl pipeline."""
    location: str = Field(
        ...,
        description="Region/state/country context, e.g. 'Victoria, Australia' or 'Georgia, USA'",
        examples=["Victoria, Australia"],
        min_length=2,
        max_length=200,
    )
    queries: list[str] | None = Field(
        default=None,
        description="Custom search queries. Supports two modes:\n"
                   "1. **Placeholder mode**: Use {location} for full location or {city} for city name (requires 'cities' field).\n"
                   "   Example: \"real estate agency {city} {location}\"\n"
                   "2. **Pre-formed mode**: Provide fully formed queries without placeholders.\n"
                   "   Example: \"Geelong real estate agents\", \"Ballarat property agency\"",
    )
    cities: list[str] | None = Field(
        default=None,
        description="List of cities/towns to search within the location. Each query with {city} expands across all cities.",
        examples=[["Geelong", "Ballarat", "Bendigo"]],
    )
    pre_formed_queries: bool = Field(
        default=False,
        description="Set to True if queries are fully formed without {location} or {city} placeholders. "
                   "When True, queries are used as-is without any placeholder replacement.",
    )
    engines: list[str] = Field(
        default=["duckduckgo"],
        description="Search engines: duckduckgo",
    )
    max_results_per_query: int = Field(default=25, ge=1, le=100)
    min_score: int = Field(default=1, ge=-10, le=50)
    request_delay: float = Field(default=2.0, ge=0, le=30)
    min_unique_results: int = Field(default=500, ge=10, le=10000)
    max_retries: int = Field(default=3, ge=0, le=10)
    email_crawler: bool = Field(
        default=True,
        description="Automatically run email crawler on found websites after URL scraping completes",
    )
    email_concurrency: int = Field(default=50, ge=1, le=100, description="Email crawler concurrency")
    email_max_pages: int = Field(default=20, ge=5, le=200, description="Max pages to crawl per company website")
    push_to_sheets: bool = Field(default=True, description="Write results to Google Sheets")


class PipelineStatus(BaseModel):
    """Status of a full pipeline run (URL scrape + email crawl)."""
    run_id: str
    location: str
    status: str
    started_at: str
    completed_at: str | None = None
    # URL Finder stats
    url_finder_status: str = "pending"
    url_finder_results: int = 0
    url_finder_duration: float | None = None
    # Email Crawler stats
    email_crawler_enabled: bool = True
    email_crawler_status: str = "not_started"
    email_crawler_results: int = 0
    email_crawler_emails: int = 0
    email_crawler_duration: float | None = None
    # Overall
    total_duration: float | None = None
    sheets_tab: str | None = None
    error: str | None = None


class PipelineResponse(BaseModel):
    """Response after starting a pipeline run."""
    run_id: str
    status: str
    message: str
    location: str
    email_crawler: bool
    poll_url: str


class WebsiteResult(BaseModel):
    """A single found company website."""
    website: str
    domain: str
    title: str
    snippet: str
    source_query: str
    source_engine: str
    location: str
    score: int
    found_at: str


# ---------------------------------------------------------------------------
# Email Crawler helper
# ---------------------------------------------------------------------------

def _run_email_crawler(location: str, results_sheet: str, companies: list[dict],
                       concurrency: int, max_pages: int, ec_run: dict):
    """Run the email crawler in the current thread (called from URL Finder completion)."""
    try:
        ec_run["status"] = "running"
        print(f"\n[Email Crawler] Starting for {len(companies)} companies from '{results_sheet}'")

        # Update settings
        ec_settings.max_total_pages_per_domain = max_pages

        # Import here to avoid circular imports
        from email_crawler.pipeline import process_companies

        start = time.time()
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                process_companies(
                    companies,
                    concurrency=concurrency,
                    progress_callback=lambda cur, tot, msg: print(f"  [EC {cur}/{tot}] {msg}"),
                )
            )
        finally:
            loop.close()
        duration = round(time.time() - start, 2)

        # Write to PostgreSQL
        try:
            ec_write_results(results)
            print(f"[Email Crawler] Wrote {len(results)} results to PostgreSQL database")
        except Exception as e:
            print(f"[Email Crawler] Error writing to database: {e}")

        # Write summary.txt
        _write_summary_file(results)

        total_emails = sum(r.emails_found for r in results)
        companies_with_emails = sum(1 for r in results if r.emails_found > 0)

        ec_run["status"] = "completed"
        ec_run["completed_at"] = datetime.now().isoformat()
        ec_run["duration_seconds"] = duration
        ec_run["total_results"] = len(results)
        ec_run["emails_found"] = total_emails
        ec_run["companies_with_emails"] = companies_with_emails
        ec_run["results"] = results

        print(f"[Email Crawler] Done: {len(results)} companies, {total_emails} emails in {duration}s")

    except Exception as e:
        ec_run["status"] = "failed"
        ec_run["error"] = str(e)
        print(f"[Email Crawler] Error: {e}")
        traceback.print_exc()


def _write_summary_file(results: list):
    """Write summary.txt to output/."""
    total = len(results)
    with_emails = sum(1 for r in results if r.emails_found > 0)
    total_emails = sum(r.emails_found for r in results)
    total_pages = sum(r.pages_crawled for r in results)
    all_scores = [e.confidence_score for r in results for e in r.emails]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0

    lines = [
        "=" * 60,
        "  FINDME - EMAIL CRAWLER SUMMARY",
        "=" * 60,
        "",
        f"  Total Companies Processed:    {total}",
        f"  Companies With Emails:        {with_emails}",
        f"  Companies Without Emails:     {total - with_emails}",
        f"  Total Emails Discovered:      {total_emails}",
        f"  Average Confidence Score:     {avg_score:.1f}",
        f"  Total Pages Crawled:          {total_pages}",
        f"  Generated At:                 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=" * 60,
        "  TOP COMPANIES BY EMAILS FOUND",
        "=" * 60,
        "",
    ]

    top = sorted(results, key=lambda r: r.emails_found, reverse=True)[:20]
    for i, r in enumerate(top, 1):
        lines.append(f"  {i:2d}. {r.company_name[:50]}")
        lines.append(f"      Website: {r.original_website}")
        lines.append(f"      Emails: {r.emails_found}  |  Best: {r.best_contact_email}  |  Score: {r.best_contact_score}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("  ALL BEST CONTACTS")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  {'Company':<45} {'Best Email':<35} {'Score':<6} {'Role'}")
    lines.append(f"  {'-'*45} {'-'*35} {'-'*6} {'-'*20}")

    for r in sorted(results, key=lambda r: r.best_contact_score, reverse=True):
        if r.best_contact_email:
            lines.append(
                f"  {r.company_name[:45]:<45} {r.best_contact_email:<35} {r.best_contact_score:<6} {r.best_contact_role}"
            )

    lines.append("")
    lines.append("=" * 60)

    summary_path = OUTPUT_DIR / "summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[Summary] Written to {summary_path}")


# ---------------------------------------------------------------------------
# Background task - full pipeline
# ---------------------------------------------------------------------------

def _run_pipeline_sync(run_id: str, request: ScrapeRequest):
    """Run the full pipeline: URL scrape -> (optional) email crawl."""
    run = UF_RUNS[run_id]
    run["status"] = "running"
    pipeline_start = time.time()

    try:
        # ================================================================
        # PHASE 1: URL Finder
        # ================================================================
        run["url_finder_status"] = "running"
        print(f"\n{'='*60}")
        print(f"  FINDME PIPELINE - {request.location}")
        print(f"{'='*60}")
        print(f"  Phase 1: URL Finder")
        print(f"  Email Crawler: {'ON' if request.email_crawler else 'OFF'}")
        print(f"{'='*60}\n")

        # Build queries - support three modes:
        # 1. Pre-formed queries: use as-is (no placeholders)
        # 2. City-expanded mode: each query with {city} runs per city
        # 3. Simple mode: {location} replacement only
        if request.pre_formed_queries and request.queries:
            # Pre-formed mode: queries are already complete, use as-is
            search_queries = request.queries
            print(f"  Using {len(search_queries)} pre-formed queries (no placeholder replacement)")
        elif request.cities:
            # City-expanded mode: each query with {city} runs per city
            base_queries = request.queries or [
                "real estate agency {city} {location}",
                "real estate company {city} {location}",
                "real estate agents {city} {location}",
                "realtor {city} {location}",
                "real estate broker {city} {location}",
                "estate agents {city} {location}",
                "independent real estate agency {city} {location}",
                "boutique real estate agency {city} {location}",
                "local real estate agency {city} {location}",
                "property management {city} {location}",
                "property management company {city} {location}",
                "property management agency {city} {location}",
                "property managers {city} {location}",
                "residential property management {city} {location}",
                "rental property management {city} {location}",
                "rental management company {city} {location}",
                "investment property management {city} {location}",
                "real estate management {city} {location}",
                "residential real estate {city} {location}",
                "residential real estate agency {city} {location}",
                "houses for sale real estate agency {city} {location}",
                "property sales agency {city} {location}",
                "property selling agents {city} {location}",
                "home selling agents {city} {location}",
                "sell my house real estate agent {city} {location}",
                "sell my property real estate agent {city} {location}",
                "commercial real estate {city} {location}",
                "commercial real estate agency {city} {location}",
                "commercial property agents {city} {location}",
                "commercial property management {city} {location}",
                "commercial property leasing {city} {location}",
                "commercial property sales {city} {location}",
                "rental agency {city} {location}",
                "property leasing agency {city} {location}",
                "residential leasing agents {city} {location}",
                "property rentals agency {city} {location}",
                "buyers agent {city} {location}",
                "buyers advocate {city} {location}",
                "property buyers agent {city} {location}",
                "rural real estate {city} {location}",
                "farm real estate agents {city} {location}",
                "acreage real estate agency {city} {location}",
                "land for sale real estate agency {city} {location}",
                "real estate developer {city} {location}",
                "property developer {city} {location}",
                "property development company {city} {location}",
                "property valuation {city} {location}",
                "property appraisal {city} {location}",
                "real estate consulting {city} {location}",
            ]
            # Expand: each query × each city
            search_queries = []
            for city in request.cities:
                for q in base_queries:
                    search_queries.append(
                        q.replace("{city}", city).replace("{location}", request.location)
                    )
            print(f"  Expanded {len(base_queries)} queries × {len(request.cities)} cities = {len(search_queries)} total queries")
        else:
            # Simple mode: {location} replacement only
            raw_queries = request.queries or [
                "real estate company {location}", "realtor {location}",
                "property dealer {location}", "real estate agency {location}",
                "real estate brokerage {location}", "property management company {location}",
                "real estate agents {location}", "home buying company {location}",
                "realty company {location}", "commercial real estate {location}",
                "residential real estate {location}", "property investment {location}",
                "real estate developers {location}", "landlord company {location}",
                "rental property management {location}", "house selling company {location}",
                "real estate consultants {location}", "property appraisal {location}",
                "real estate investors {location}", "mortgage company {location}",
                "home inspection company {location}", "real estate law firm {location}",
                "title company {location}", "escrow company {location}",
                "moving company {location}", "estate agents {location}",
                "property services {location}", "residential real estate {location}",
                "property sales agency {location}", "house sales agents {location}",
                "commercial property agency {location}", "commercial property company {location}",
                "commercial leasing agents {location}", "property management agency {location}",
                "property managers {location}", "rental property management {location}",
                "property investment company {location}", "luxury real estate {location}",
                "real estate developer {location}", "property developer {location}",
                "apartment rental agency {location}", "residential leasing agents {location}",
                "property valuation company {location}", "real estate consulting firm {location}",
                "property consultants {location}", "real estate advisory {location}",
                "local real estate agents {location}", "best real estate agents {location}",
                "independent real estate agency {location}", "family owned real estate agency {location}",
                "boutique real estate agency {location}", "property specialists {location}",
                "new homes {location}", "new developments {location}",
            ]
            search_queries = [
                q.replace("{location}", request.location) for q in raw_queries
            ]

        config_data = {
            "LOCATION": request.location,
            "SEARCH_ENGINES": request.engines,
            "MAX_RESULTS_PER_QUERY": request.max_results_per_query,
            "MIN_SCORE": request.min_score,
            "REQUEST_DELAY": request.request_delay,
            "SEARCH_QUERIES": search_queries,
            "MIN_UNIQUE_RESULTS": request.min_unique_results,
            "MAX_RETRIES": request.max_retries,
        }

        tmp_path = OUTPUT_DIR / f"_config_{run_id}.yaml"
        import yaml
        with open(tmp_path, "w") as f:
            yaml.dump(config_data, f)

        config = ScraperConfig(tmp_path)
        scraper = RealEstateScraper(config)

        start = time.time()
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scraper.run())
        finally:
            loop.close()
        uf_duration = round(time.time() - start, 2)

        tmp_path.unlink(missing_ok=True)

        # Save output files
        loc_slug = request.location.lower().replace(" ", "_").replace(",", "").replace(".", "")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        csv_path = OUTPUT_DIR / f"{loc_slug}_{timestamp}.csv"
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("website,domain,title,source_query,source_engine,location,score\n")
            for r in results:
                t = r.get("title", "").replace('"', '""')
                s = r.get("source_query", "").replace('"', '""')
                f.write(f'"{r["website"]}","{r["domain"]}","{t}","{s}","{r["source_engine"]}","{r["location"]}",{r["score"]}\n')

        json_path = OUTPUT_DIR / f"{loc_slug}_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)

        # Push to Google Sheets
        sheets_tab = request.location.strip()
        if request.push_to_sheets and SHEETS_AVAILABLE:
            try:
                push_results_to_sheet(location=request.location, results=results)
                print(f"[Sheets] Pushed {len(results)} URLs to '{sheets_tab}' tab")
            except Exception as e:
                print(f"[Sheets] Error: {e}")

        run["url_finder_status"] = "completed"
        run["url_finder_results"] = len(results)
        run["url_finder_duration"] = uf_duration
        run["sheets_tab"] = sheets_tab

        print(f"\n  Phase 1 complete: {len(results)} websites found in {uf_duration}s")

        # ================================================================
        # PHASE 2: Email Crawler (if enabled)
        # ================================================================
        if request.email_crawler and results:
            # Auto-derive results sheet name
            loc = request.location.strip()
            for suffix in [", Australia", ", USA", ", US"]:
                if loc.endswith(suffix):
                    loc = loc[:-len(suffix)].strip()
                    break
            ec_results_sheet = f"{loc} Results"

            # Read companies from the sheet we just wrote to
            print(f"\n  Phase 2: Email Crawler -> '{ec_results_sheet}'")
            companies = ec_read_input(sheets_tab)

            if companies:
                ec_run_id = f"ec_{run_id}"
                ec_run = {
                    "status": "pending",
                    "started_at": datetime.now().isoformat(),
                    "completed_at": None,
                    "total_results": 0,
                    "emails_found": 0,
                    "duration_seconds": None,
                    "results": [],
                    "error": None,
                }
                EC_RUNS[ec_run_id] = ec_run

                run["email_crawler_status"] = "running"

                _run_email_crawler(
                    location=request.location,
                    results_sheet=ec_results_sheet,
                    companies=companies,
                    concurrency=request.email_concurrency,
                    max_pages=request.email_max_pages,
                    ec_run=ec_run,
                )

                run["email_crawler_status"] = ec_run["status"]
                run["email_crawler_results"] = ec_run.get("total_results", 0)
                run["email_crawler_emails"] = ec_run.get("emails_found", 0)
                run["email_crawler_duration"] = ec_run.get("duration_seconds")
            else:
                run["email_crawler_status"] = "skipped"
                print("  No companies found in sheet for email crawling")

        # ================================================================
        # Done
        # ================================================================
        total_duration = round(time.time() - pipeline_start, 2)

        run["status"] = "completed"
        run["completed_at"] = datetime.now().isoformat()
        run["total_duration"] = total_duration
        run["files"] = {"csv": str(csv_path), "json": str(json_path)}

        print(f"\n{'='*60}")
        print(f"  PIPELINE COMPLETE")
        print(f"  URL Finder: {len(results)} websites in {uf_duration}s")
        if request.email_crawler:
            print(f"  Email Crawler: {run['email_crawler_emails']} emails in {run['email_crawler_duration'] or 0}s")
        print(f"  Total time: {total_duration}s")
        print(f"{'='*60}\n")

    except Exception as e:
        run["status"] = "failed"
        run["completed_at"] = datetime.now().isoformat()
        run["error"] = str(e)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Routes - Pipeline
# ---------------------------------------------------------------------------

@app.get("/health", summary="Health check", tags=["System"])
async def health():
    return {
        "service": "FINDME",
        "status": "healthy",
        "version": "1.0.0",
        "modules": ["url_finder", "email_crawler"],
        "engines": ["duckduckgo"],
        "timestamp": datetime.now().isoformat(),
        "total_runs": len(UF_RUNS),
    }


@app.post("/scrape", response_model=PipelineResponse, summary="Run full pipeline", tags=["Pipeline"], status_code=202)
async def start_pipeline(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Start the FINDME pipeline: URL Finder -> Email Crawler.

    1. Searches queries across DuckDuckGo
    2. If `cities` is provided, queries with `{city}` expand across all cities
    3. If `pre_formed_queries: true`, queries are used as-is (no placeholders)
    4. Retries up to `max_retries` times if under `min_unique_results`
    5. Pushes URLs to Google Sheets as `{location}` tab
    6. If `email_crawler: true` (default), automatically crawls all found websites
    7. Email results write to `{location} Results` tab
    8. Summary saved to output/summary.txt

    **Multi-City Example:**
    ```json
    {
      "location": "Victoria, Australia",
      "cities": ["Geelong", "Ballarat", "Bendigo"],
      "queries": ["real estate agency {city} {location}"],
      "email_crawler": true,
      "email_concurrency": 10
    }
    ```

    **Pre-Formed Queries Example:**
    ```json
    {
      "location": "Regional Victoria, Australia",
      "pre_formed_queries": true,
      "queries": [
        "real estate company Regional Victoria",
        "Geelong real estate agents",
        "Ballarat property agency",
        "Bendigo real estate company"
      ],
      "email_crawler": true,
      "email_concurrency": 10
    }
    ```
    """
    run_id = str(uuid.uuid4())[:8]

    UF_RUNS[run_id] = {
        "run_id": run_id,
        "location": request.location,
        "status": "pending",
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "url_finder_status": "pending",
        "url_finder_results": 0,
        "url_finder_duration": None,
        "email_crawler_enabled": request.email_crawler,
        "email_crawler_status": "not_started",
        "email_crawler_results": 0,
        "email_crawler_emails": 0,
        "email_crawler_duration": None,
        "total_duration": None,
        "sheets_tab": None,
        "error": None,
        "files": {},
    }

    asyncio.get_event_loop().run_in_executor(None, _run_pipeline_sync, run_id, request)

    msg = f"Pipeline started for '{request.location}'"
    if request.email_crawler:
        msg += " - URL Finder -> Email Crawler"
    else:
        msg += " - URL Finder only"

    return PipelineResponse(
        run_id=run_id,
        status="pending",
        message=msg,
        location=request.location,
        email_crawler=request.email_crawler,
        poll_url=f"/status/{run_id}",
    )


@app.get("/status/{run_id}", response_model=PipelineStatus, summary="Check pipeline status", tags=["Pipeline"])
async def get_status(run_id: str):
    if run_id not in UF_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    d = UF_RUNS[run_id]
    return PipelineStatus(**{k: v for k, v in d.items() if k in PipelineStatus.model_fields})


@app.get("/results/{run_id}", summary="Get URL Finder results", tags=["Results"])
async def get_results(run_id: str):
    if run_id not in UF_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    d = UF_RUNS[run_id]
    return {
        "run_id": run_id,
        "location": d["location"],
        "status": d["status"],
        "url_finder": {
            "status": d.get("url_finder_status"),
            "total_results": d.get("url_finder_results", 0),
            "duration_seconds": d.get("url_finder_duration"),
        },
        "email_crawler": {
            "enabled": d.get("email_crawler_enabled", False),
            "status": d.get("email_crawler_status"),
            "total_results": d.get("email_crawler_results", 0),
            "emails_found": d.get("email_crawler_emails", 0),
            "duration_seconds": d.get("email_crawler_duration"),
        },
        "sheets_tab": d.get("sheets_tab"),
        "files": d.get("files", {}),
    }


@app.get("/results/{run_id}/download", summary="Download URL results as CSV", tags=["Results"])
async def download_results(run_id: str):
    if run_id not in UF_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    d = UF_RUNS[run_id]
    csv_path = d.get("files", {}).get("csv")
    if not csv_path or not Path(csv_path).exists():
        raise HTTPException(status_code=404, detail="CSV file not found")
    return FileResponse(csv_path, media_type="text/csv",
                        filename=f"findme_{d['location'].replace(' ', '_')}.csv")


@app.get("/results/{run_id}/summary", summary="Download email summary", tags=["Results"])
async def download_summary(run_id: str):
    summary_path = OUTPUT_DIR / "summary.txt"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Summary not found yet")
    return FileResponse(summary_path, media_type="text/plain", filename="summary.txt")


@app.get("/results", summary="List all pipeline runs", tags=["Results"])
async def list_results():
    runs = []
    for run_id, d in UF_RUNS.items():
        runs.append({
            "run_id": run_id,
            "location": d["location"],
            "status": d["status"],
            "started_at": d["started_at"],
            "completed_at": d.get("completed_at"),
            "url_finder_results": d.get("url_finder_results", 0),
            "email_crawler_emails": d.get("email_crawler_emails", 0),
            "total_duration": d.get("total_duration"),
        })
    runs.sort(key=lambda x: x["started_at"], reverse=True)
    return runs


@app.delete("/results/{run_id}", summary="Delete a run", tags=["Results"])
async def delete_run(run_id: str):
    if run_id not in UF_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    data = UF_RUNS.pop(run_id)
    for fpath in data.get("files", {}).values():
        try:
            Path(fpath).unlink(missing_ok=True)
        except Exception:
            pass
    return {"message": f"Run '{run_id}' deleted"}


# ---------------------------------------------------------------------------
# Routes - Email Crawler (standalone)
# ---------------------------------------------------------------------------

@app.post("/crawl", summary="Run email crawler standalone", tags=["Email Crawler"], status_code=202)
async def start_email_crawl(
    input_sheet: str = "Melbourne, Victoria, Australia",
    concurrency: int = 50,
    max_pages: int = 20,
):
    """Run the email crawler standalone on an existing sheet tab."""
    # Auto-derive results sheet
    loc = input_sheet.strip()
    for suffix in [", Australia", ", USA", ", US"]:
        if loc.endswith(suffix):
            loc = loc[:-len(suffix)].strip()
            break
    results_sheet = f"{loc} Results"

    companies = ec_read_input(input_sheet)
    if not companies:
        raise HTTPException(status_code=400, detail=f"No companies found in '{input_sheet}'")

    run_id = f"ec_{str(uuid.uuid4())[:8]}"
    ec_run = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "total_results": 0,
        "emails_found": 0,
        "duration_seconds": None,
        "results": [],
        "error": None,
    }
    EC_RUNS[run_id] = ec_run

    asyncio.get_event_loop().run_in_executor(
        None, _run_email_crawler, input_sheet, results_sheet, companies, concurrency, max_pages, ec_run
    )

    return {
        "run_id": run_id,
        "status": "running",
        "input_sheet": input_sheet,
        "results_sheet": results_sheet,
        "companies": len(companies),
        "poll_url": f"/crawl/{run_id}",
    }


@app.get("/crawl/{run_id}", summary="Check email crawl status", tags=["Email Crawler"])
async def get_crawl_status(run_id: str):
    if run_id not in EC_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    d = EC_RUNS[run_id]
    return {
        "run_id": run_id,
        "status": d["status"],
        "total_results": d.get("total_results", 0),
        "emails_found": d.get("emails_found", 0),
        "duration_seconds": d.get("duration_seconds"),
        "error": d.get("error"),
    }


# ---------------------------------------------------------------------------
# Routes - Google Sheets
# ---------------------------------------------------------------------------

@app.get("/sheets", summary="List all sheet tabs", tags=["Google Sheets"])
async def get_sheets():
    try:
        stats = ec_get_stats()
        return {
            "database": ec_settings.db_name,
            "host": ec_settings.db_host,
            "stats": stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
