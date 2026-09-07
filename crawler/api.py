"""
Email Crawler API
=================
Expose the email discovery pipeline as a REST API with Swagger UI.

Run with:
    python -m uvicorn api:app --reload --host 0.0.0.0 --port 8000

Swagger UI:
    http://localhost:8000/docs
"""

import asyncio
import json
import os
import uuid
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from config import settings
from storage import (
    read_input_companies, write_results,
    get_all_sheet_names, get_spreadsheet,
    save_all_outputs,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Email Crawler API",
    description=(
        "Crawls company websites to discover, extract, classify, validate, "
        "score, and export email contacts.\n\n"
        "## Quick Start\n"
        "1. Ensure your Google Sheet has an 'Input' tab with company_name + website columns\n"
        "2. POST `/crawl` to start email discovery\n"
        "3. Poll `GET /status/{run_id}` to check progress\n"
        "4. GET `/results/{run_id}` for full results\n"
        "5. Results auto-write to Google Sheets + summary.txt in output/\n\n"
        "## How it works\n"
        "- Reads companies from the Input tab\n"
        "- Crawls each website (homepage, contact, team, agents pages)\n"
        "- Extracts emails using 8+ methods (mailto, regex, JSON-LD, etc.)\n"
        "- Classifies contacts by role (Owner > Broker > Agent > General)\n"
        "- Scores confidence with explainable reasons\n"
        "- Writes best contacts to Google Sheets + summary.txt"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# In-memory store for run results
RUNS: dict[str, dict[str, Any]] = {}

# Output directory
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CrawlRequest(BaseModel):
    """Request body for starting an email crawl run."""
    concurrency: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Max concurrent HTTP requests",
    )
    max_pages_per_domain: int = Field(
        default=50,
        ge=5,
        le=200,
        description="Max pages to crawl per company website",
    )
    enable_playwright: bool = Field(
        default=True,
        description="Enable Playwright fallback for JS-heavy pages",
    )
    input_sheet: str = Field(
        default="Input",
        description="Google Sheet tab name to read companies from",
    )
    results_sheet: str | None = Field(
        default=None,
        description="Google Sheet tab name for results. Auto-derived as '{location} Results' if not set.",
    )
    push_to_sheets: bool = Field(
        default=True,
        description="Write results to Google Sheets",
    )
    resume: bool = Field(
        default=False,
        description="Skip companies that already have results",
    )


class RunStatus(BaseModel):
    """Status of a crawl run."""
    run_id: str
    status: str
    started_at: str
    completed_at: str | None = None
    total_companies: int = 0
    processed: int = 0
    emails_found: int = 0
    companies_with_emails: int = 0
    duration_seconds: float | None = None
    error: str | None = None


class CrawlResponse(BaseModel):
    """Response after starting a crawl run."""
    run_id: str
    status: str
    message: str
    poll_url: str


class EmailResult(BaseModel):
    """A single discovered email."""
    email: str
    person_name: str
    job_title: str
    role: str
    confidence_score: int
    confidence_level: str
    source_url: str
    source_page_type: str
    extraction_method: str
    domain_match: bool
    mx_valid: bool
    nearby_text: str


class CompanyResultItem(BaseModel):
    """Result for a single company."""
    company_name: str
    original_website: str
    root_domain: str
    status: str
    pages_crawled: int
    emails_found: int
    best_contact_email: str
    best_contact_name: str
    best_contact_role: str
    best_contact_score: int
    emails: list[EmailResult]


class CrawlResult(BaseModel):
    """Full result of a completed crawl run."""
    run_id: str
    status: str
    started_at: str
    completed_at: str | None
    duration_seconds: float | None
    total_companies: int
    companies_with_emails: int
    total_emails: int
    results: list[CompanyResultItem]


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

def _write_summary_file(results: list, output_dir: Path):
    """Write aggregate summary to output/summary.txt."""
    total = len(results)
    with_emails = sum(1 for r in results if r.emails_found > 0)
    without_emails = total - with_emails
    total_emails = sum(r.emails_found for r in results)
    total_pages = sum(r.pages_crawled for r in results)
    all_scores = [e.confidence_score for r in results for e in r.emails]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0

    lines = [
        "=" * 60,
        "  EMAIL CRAWLER — SUMMARY",
        "=" * 60,
        "",
        f"  Total Companies Processed:    {total}",
        f"  Companies With Emails:        {with_emails}",
        f"  Companies Without Emails:     {without_emails}",
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

    summary_path = output_dir / "summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[Summary] Written to {summary_path}")


def _update_run_progress(run: dict, run_id: str, cur: int, tot: int, msg: str):
    """Update the run dict with progress so status endpoint reflects it."""
    run["processed"] = cur
    run["total_companies"] = tot
    print(f"  [{cur}/{tot}] {msg}")


def _run_crawl_sync(run_id: str, request: CrawlRequest):
    """Run the email crawl pipeline synchronously in a thread."""
    run = RUNS[run_id]
    run["status"] = "running"

    try:
        # Update settings from request
        settings.max_total_pages_per_domain = request.max_pages_per_domain
        settings.enable_playwright_fallback = request.enable_playwright

        # Read companies from Google Sheets
        companies = read_input_companies(request.input_sheet)
        if not companies:
            run["status"] = "failed"
            run["error"] = f"No companies found in '{request.input_sheet}' sheet"
            return

        run["total_companies"] = len(companies)

        # Auto-derive results sheet name if not provided
        results_sheet = request.results_sheet
        if not results_sheet:
            loc = request.input_sheet.strip()
            for suffix in [", Australia", ", USA", ", US"]:
                if loc.endswith(suffix):
                    loc = loc[:-len(suffix)].strip()
                    break
            results_sheet = f"{loc} Results"

        # Resume: skip already-processed companies
        if request.resume:
            try:
                existing_sheets = get_all_sheet_names()
                if results_sheet in existing_sheets:
                    ss = get_spreadsheet()
                    ws = ss.worksheet(results_sheet)
                    existing_rows = ws.get_all_records()
                    done_websites = {
                        row.get("original_website", "").strip()
                        for row in existing_rows
                        if row.get("status") == "completed"
                    }
                    before = len(companies)
                    companies = [
                        c for c in companies
                        if c["website"].strip() not in done_websites
                    ]
                    skipped = before - len(companies)
                    if skipped:
                        print(f"[Resume] Skipped {skipped} already-processed companies")
            except Exception as e:
                print(f"[Resume] Could not check existing results: {e}")

        # Run the pipeline
        from pipeline import process_companies

        start = time.time()
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                process_companies(
                    companies,
                    concurrency=request.concurrency,
                    progress_callback=lambda cur, tot, msg: _update_run_progress(run, run_id, cur, tot, msg),
                )
            )
        finally:
            loop.close()
        duration = round(time.time() - start, 2)

        # Write results to Google Sheets
        sheets_info = {}
        if request.push_to_sheets:
            try:
                write_results(results, sheet_name=results_sheet)
                sheets_info = {
                    "results_sheet": results_sheet,
                    "companies_written": len(results),
                }
                print(f"[Sheets] Wrote {len(results)} results to '{results_sheet}'")
            except Exception as e:
                print(f"[Sheets] Error writing to Google Sheets: {e}")
                sheets_info = {"error": str(e)}

        # Save results to CSV and JSON (preserved between runs)
        output_files = save_all_outputs(results, OUTPUT_DIR, prefix=f"email_crawl_{run_id}")
        print(f"[Files] CSV: {output_files['csv']}")
        print(f"[Files] JSON: {output_files['json']}")
        print(f"[Files] Latest CSV: {output_files['latest_csv']}")

        # Write summary.txt
        total_emails = sum(r.emails_found for r in results)
        companies_with_emails = sum(1 for r in results if r.emails_found > 0)
        _write_summary_file(results, OUTPUT_DIR)

        run["status"] = "completed"
        run["completed_at"] = datetime.now().isoformat()
        run["duration_seconds"] = duration
        run["processed"] = len(results)
        run["emails_found"] = total_emails
        run["companies_with_emails"] = companies_with_emails
        run["results"] = results
        run["sheets"] = sheets_info
        summary_path = OUTPUT_DIR / "summary.txt"
        run["files"] = output_files

    except Exception as e:
        run["status"] = "failed"
        run["completed_at"] = datetime.now().isoformat()
        run["error"] = str(e)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    summary="Health check",
    tags=["System"],
)
async def health():
    """Check if the API is running."""
    return {
        "status": "healthy",
        "service": "email_crawler",
        "timestamp": datetime.now().isoformat(),
        "total_runs": len(RUNS),
        "google_sheets": {
            "spreadsheet_id": settings.google_spreadsheet_id,
            "connected": bool(settings.google_spreadsheet_id),
        },
    }


@app.post(
    "/crawl",
    response_model=CrawlResponse,
    summary="Start an email crawl run",
    tags=["Crawler"],
    status_code=202,
)
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks):
    """
    Start a new email crawl run.

    Reads companies from the Google Sheets Input tab, crawls their websites
    to discover email contacts, and writes results back to Google Sheets.

    **How to use:**
    1. POST here to start the crawl
    2. You'll get a `run_id` back
    3. Poll `GET /status/{run_id}` to check progress
    4. GET `/results/{run_id}` for full results when complete

    **Google Sheets setup:**
    - The 'Input' tab should have columns: company_name, website
    - Results are written to the 'Results' tab
    - Summary is saved as output/summary.txt (not a sheet)
    """
    run_id = str(uuid.uuid4())[:8]

    RUNS[run_id] = {
        "run_id": run_id,
        "status": "pending",
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "total_companies": 0,
        "processed": 0,
        "emails_found": 0,
        "companies_with_emails": 0,
        "duration_seconds": None,
        "error": None,
        "results": [],
        "sheets": {},
        "files": {},
    }

    # Run in background thread
    asyncio.get_event_loop().run_in_executor(
        None, _run_crawl_sync, run_id, request
    )

    return CrawlResponse(
        run_id=run_id,
        status="pending",
        message=f"Crawl started. Poll GET /status/{run_id} for progress.",
        poll_url=f"/status/{run_id}",
    )


@app.get(
    "/status/{run_id}",
    response_model=RunStatus,
    summary="Check crawl status",
    tags=["Crawler"],
)
async def get_status(run_id: str):
    """Check the current status of a crawl run."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]
    return RunStatus(
        run_id=run_id,
        status=data["status"],
        started_at=data["started_at"],
        completed_at=data.get("completed_at"),
        total_companies=data.get("total_companies", 0),
        processed=data.get("processed", 0),
        emails_found=data.get("emails_found", 0),
        companies_with_emails=data.get("companies_with_emails", 0),
        duration_seconds=data.get("duration_seconds"),
        error=data.get("error"),
    )


@app.get(
    "/results/{run_id}",
    response_model=CrawlResult,
    summary="Get crawl results",
    tags=["Results"],
)
async def get_results(run_id: str):
    """
    Get the full results for a completed crawl run.

    Returns all discovered emails with confidence scores, roles,
    extraction methods, and source URLs.
    """
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]

    results = []
    for r in data.get("results", []):
        emails = []
        for e in r.emails:
            emails.append(EmailResult(
                email=e.email,
                person_name=e.person_name,
                job_title=e.job_title,
                role=e.role,
                confidence_score=e.confidence_score,
                confidence_level=e.confidence_level.value,
                source_url=e.source_url,
                source_page_type=e.source_page_type.value,
                extraction_method=e.extraction_method.value,
                domain_match=e.domain_match,
                mx_valid=e.mx_valid,
                nearby_text=e.nearby_text[:200],
            ))
        results.append(CompanyResultItem(
            company_name=r.company_name,
            original_website=r.original_website,
            root_domain=r.root_domain,
            status=r.status.value,
            pages_crawled=r.pages_crawled,
            emails_found=r.emails_found,
            best_contact_email=r.best_contact_email,
            best_contact_name=r.best_contact_name,
            best_contact_role=r.best_contact_role,
            best_contact_score=r.best_contact_score,
            emails=emails,
        ))

    return CrawlResult(
        run_id=run_id,
        status=data["status"],
        started_at=data["started_at"],
        completed_at=data.get("completed_at"),
        duration_seconds=data.get("duration_seconds"),
        total_companies=data.get("total_companies", 0),
        companies_with_emails=data.get("companies_with_emails", 0),
        total_emails=data.get("emails_found", 0),
        results=results,
    )


@app.get(
    "/results/{run_id}/download",
    summary="Download results as JSON",
    tags=["Results"],
)
async def download_results(run_id: str):
    """Download the results of a completed run as a JSON file."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]
    if data["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Run status is '{data['status']}', not 'completed'"
        )

    json_path = data.get("files", {}).get("json")
    if not json_path or not Path(json_path).exists():
        raise HTTPException(status_code=404, detail="Results file not found")

    return FileResponse(
        json_path,
        media_type="application/json",
        filename=f"email_crawl_{run_id}.json",
    )


@app.get(
    "/results/{run_id}/summary",
    summary="Download summary as text",
    tags=["Results"],
)
async def download_summary(run_id: str):
    """Download the summary.txt for a completed run."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    summary_path = OUTPUT_DIR / "summary.txt"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Summary file not found")

    return FileResponse(
        summary_path,
        media_type="text/plain",
        filename=f"summary_{run_id}.txt",
    )


@app.get(
    "/results",
    summary="List all crawl runs",
    tags=["Results"],
)
async def list_results():
    """List all past and current crawl runs."""
    runs = []
    for run_id, data in RUNS.items():
        runs.append(RunStatus(
            run_id=run_id,
            status=data["status"],
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            total_companies=data.get("total_companies", 0),
            processed=data.get("processed", 0),
            emails_found=data.get("emails_found", 0),
            companies_with_emails=data.get("companies_with_emails", 0),
            duration_seconds=data.get("duration_seconds"),
            error=data.get("error"),
        ))
    runs.sort(key=lambda x: x.started_at, reverse=True)
    return runs


@app.delete(
    "/results/{run_id}",
    summary="Delete a run",
    tags=["Results"],
)
async def delete_run(run_id: str):
    """Delete a specific run and its output files."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS.pop(run_id)

    for fpath in data.get("files", {}).values():
        try:
            Path(fpath).unlink(missing_ok=True)
        except Exception:
            pass

    return {"message": f"Run '{run_id}' deleted"}


@app.get(
    "/sheets",
    summary="List Google Sheets tabs",
    tags=["Google Sheets"],
)
async def get_sheets():
    """List all sheets (tabs) in the Google Spreadsheet."""
    try:
        sheets = get_all_sheet_names()
        return {
            "spreadsheet_id": settings.google_spreadsheet_id,
            "sheets": sheets,
            "total_sheets": len(sheets),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/sheets/setup-input",
    summary="Create Input sheet from existing data",
    tags=["Google Sheets"],
)
async def setup_input_sheet(
    source_sheet: str = "Seattle, Washington",
    max_rows: int = 50,
):
    """
    Create or update the Input tab from an existing sheet.
    Useful for bootstrapping the Input tab from scraper output.
    """
    try:
        ss = get_spreadsheet()

        # Read source sheet
        ws_source = ss.worksheet(source_sheet)
        source_rows = ws_source.get_all_records()

        # Create or clear Input sheet
        try:
            ws_input = ss.worksheet("Input")
        except Exception:
            ws_input = ss.add_worksheet(title="Input", rows=max_rows + 1, cols=2)

        ws_input.clear()
        ws_input.update(range_name="A1", values=[["company_name", "website"]])

        input_rows = []
        for row in source_rows[:max_rows]:
            name = row.get("Title", row.get("company_name", "Unknown"))
            url = row.get("Website", row.get("website", ""))
            if url:
                input_rows.append([name, url])

        if input_rows:
            ws_input.update(range_name="A2", values=input_rows)

        return {
            "message": f"Created Input sheet with {len(input_rows)} companies from '{source_sheet}'",
            "companies": len(input_rows),
            "source_sheet": source_sheet,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
