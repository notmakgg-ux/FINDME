"""
Real Estate Scraper API
=======================
Expose the multi-engine scraper as a REST API with Swagger UI.

Run with:
    python -m uvicorn src.api:app --reload --host 0.0.0.0 --port 8000

Swagger UI:
    http://localhost:8000/docs
"""

import asyncio
import json
import os
import uuid
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field

from src.config import Config
from src.scraper import RealEstateScraper

# Google Sheets (optional)
try:
    from src.sheets import push_results_to_sheet, list_sheets, create_or_get_spreadsheet
    SHEETS_AVAILABLE = True
except Exception:
    SHEETS_AVAILABLE = False


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Real Estate Company Scraper API",
    description=(
        "Searches DuckDuckGo for real estate company websites "
        "to find real estate, realtor, and property dealer company websites "
        "for any location worldwide.\n\n"
        "## Quick Start\n"
        "1. POST `/scrape` with a location\n"
        "2. GET `/results/{run_id}` to retrieve the results\n"
        "3. GET `/results` to see all past runs"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# In-memory store for run results (reset on restart)
RUNS: dict[str, dict[str, Any]] = {}

# Output directory
OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    """Request body for starting a scrape run. All fields are optional except location."""
    location: str = Field(
        ...,
        description="US city and state, e.g. 'Atlanta, Georgia' or 'Miami, FL'",
        examples=["Atlanta, Georgia"],
        min_length=2,
        max_length=200,
    )
    queries: list[str] | None = Field(
        default=None,
        description="Custom search queries. Use {location} as placeholder. If null, uses default real estate queries.",
        examples=[["coffee shop {location}", "restaurant {location}", "gym {location}"]],
    )
    engines: list[str] = Field(
        default=["duckduckgo"],
        description="Search engines to use. Options: duckduckgo",
        examples=[["duckduckgo"]],
    )
    max_results_per_query: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Max search results to fetch per query per engine",
    )
    min_score: int = Field(
        default=1,
        ge=-10,
        le=50,
        description="Minimum relevance score to keep a result",
    )
    request_delay: float = Field(
        default=2.0,
        ge=0,
        le=30,
        description="Delay in seconds between search requests",
    )
    exclude_domains: list[str] | None = Field(
        default=None,
        description="Additional domains to exclude. Merged with built-in junk list.",
        examples=[["example.com", "test.com"]],
    )
    min_unique_results: int = Field(
        default=500,
        ge=10,
        le=10000,
        description="Target number of unique results. Scraper retries up to 3 times if under this.",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max retry rounds if under min_unique_results.",
    )
    push_to_sheets: bool = Field(
        default=False,
        description="Push results to Google Sheets (requires GOOGLE_SA_KEY_PATH)",
    )


class RunStatus(BaseModel):
    """Status of a scrape run."""
    run_id: str
    location: str
    status: str  # "pending", "running", "completed", "failed"
    started_at: str
    completed_at: str | None = None
    total_results: int | None = None
    engines_used: list[str] = []
    duration_seconds: float | None = None
    error: str | None = None


class ScrapeResponse(BaseModel):
    """Response after starting a scrape run."""
    run_id: str
    status: str
    message: str
    location: str
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


class RunResult(BaseModel):
    """Full result of a completed scrape run."""
    run_id: str
    location: str
    status: str
    started_at: str
    completed_at: str | None
    duration_seconds: float | None
    engines_used: list[str]
    engine_stats: dict[str, int]
    total_results: int
    results: list[WebsiteResult]


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

def _run_scraper_sync(run_id: str, request: ScrapeRequest):
    """Run the scraper synchronously in a thread (avoids blocking the event loop)."""
    import yaml
    run = RUNS[run_id]
    run["status"] = "running"

    try:
        # Use custom queries or default real estate queries
        if request.queries:
            search_queries = request.queries
        else:
            search_queries = [
                "real estate company {location}",
                "realtor {location}",
                "property dealer {location}",
                "real estate agency {location}",
                "real estate brokerage {location}",
                "property management company {location}",
                "real estate agents {location}",
                "home buying company {location}",
                "realty company {location}",
                "commercial real estate {location}",
                "residential real estate {location}",
                "property investment {location}",
                "real estate developers {location}",
                "landlord company {location}",
                "rental property management {location}",
                "house selling company {location}",
                "real estate consultants {location}",
                "property appraisal {location}",
                "real estate investors {location}",
                "mortgage company {location}",
                "home inspection company {location}",
                "real estate law firm {location}",
                "title company {location}",
                "escrow company {location}",
                "moving company {location}",
            ]

        # Build config from request
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

        # Write temp config and load it
        tmp_path = OUTPUT_DIR / f"_config_{run_id}.yaml"
        with open(tmp_path, "w") as f:
            yaml.dump(config_data, f)

        # Merge custom exclude domains
        if request.exclude_domains:
            config_data["EXCLUDE_DOMAINS"] = (
                config_data.get("EXCLUDE_DOMAINS", []) + request.exclude_domains
            )

        config = Config(tmp_path)
        scraper = RealEstateScraper(config)

        # Run the async scraper in a new event loop inside the thread
        start = time.time()
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scraper.run())
        finally:
            loop.close()
        duration = round(time.time() - start, 2)

        # Save results to files
        loc_slug = request.location.lower().replace(" ", "_").replace(",", "").replace(".", "")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # CSV
        csv_path = OUTPUT_DIR / f"api_{loc_slug}_{timestamp}.csv"
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("website,domain,title,source_query,source_engine,location,score\n")
            for r in results:
                title = r.get("title", "").replace('"', '""')
                snippet = r.get("source_query", "").replace('"', '""')
                f.write(f'"{r["website"]}","{r["domain"]}","{title}","{snippet}","{r["source_engine"]}","{r["location"]}",{r["score"]}\n')

        # TXT
        txt_path = OUTPUT_DIR / f"api_{loc_slug}_{timestamp}.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            for r in results:
                f.write(r["website"] + "\n")

        # JSON
        json_path = OUTPUT_DIR / f"api_{loc_slug}_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)

        # Cleanup temp config
        tmp_path.unlink(missing_ok=True)

        # Push to Google Sheets if requested
        sheets_info = {}
        if request.push_to_sheets and SHEETS_AVAILABLE:
            try:
                sheets_info = push_results_to_sheet(
                    location=request.location,
                    results=results,
                )
                print(f"[Sheets] Pushed {len(results)} results to Google Sheets")
            except Exception as e:
                print(f"[Sheets] Error pushing to Google Sheets: {e}")
                sheets_info = {"error": str(e)}
        elif request.push_to_sheets and not SHEETS_AVAILABLE:
            sheets_info = {"error": "gspread not installed"}

        # Store in memory
        run["status"] = "completed"
        run["completed_at"] = datetime.now().isoformat()
        run["duration_seconds"] = duration
        run["total_results"] = len(results)
        run["engines_used"] = request.engines
        run["engine_stats"] = scraper.engine_stats
        run["results"] = results
        run["files"] = {
            "csv": str(csv_path),
            "txt": str(txt_path),
            "json": str(json_path),
        }
        run["sheets"] = sheets_info

    except Exception as e:
        run["status"] = "failed"
        run["completed_at"] = datetime.now().isoformat()
        run["error"] = str(e)
        # Cleanup temp config on error
        tmp_path = OUTPUT_DIR / f"_config_{run_id}.yaml"
        tmp_path.unlink(missing_ok=True)


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
        "timestamp": datetime.now().isoformat(),
        "total_runs": len(RUNS),
        "engines_available": ["duckduckgo"],
        "google_sheets": SHEETS_AVAILABLE,
    }


@app.post(
    "/scrape",
    response_model=ScrapeResponse,
    summary="Start a scrape run",
    tags=["Scraper"],
    status_code=202,
)
async def start_scrape(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Start a new scrape run for the given location.

    The scraper will search multiple search engines for real estate company
    websites in the specified location. Results are returned asynchronously.

    **How to use:**
    1. POST here with your location
    2. You'll get a `run_id` back
    3. Poll `GET /results/{run_id}` to check status and get results
    4. Or use `GET /results/{run_id}/download` to download the CSV

    **Example locations:**
    - "Atlanta, Georgia"
    - "Miami, FL"
    - "Los Angeles, CA"
    - "Chicago, Illinois"
    """
    run_id = str(uuid.uuid4())[:8]

    RUNS[run_id] = {
        "run_id": run_id,
        "location": request.location,
        "status": "pending",
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "total_results": None,
        "engines_used": request.engines,
        "engine_stats": {},
        "duration_seconds": None,
        "error": None,
        "results": [],
        "files": {},
    }

    # Use a background thread so the scraper's time.sleep() doesn't block the event loop
    asyncio.get_event_loop().run_in_executor(None, _run_scraper_sync, run_id, request)

    return ScrapeResponse(
        run_id=run_id,
        status="pending",
        message=f"Scrape started for '{request.location}'. Poll GET /results/{run_id} for status.",
        location=request.location,
        poll_url=f"/results/{run_id}",
    )


@app.get(
    "/results",
    response_model=list[RunStatus],
    summary="List all scrape runs",
    tags=["Results"],
)
async def list_results():
    """List all past and current scrape runs."""
    runs = []
    for run_id, data in RUNS.items():
        runs.append(RunStatus(
            run_id=run_id,
            location=data["location"],
            status=data["status"],
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            total_results=data.get("total_results"),
            engines_used=data.get("engines_used", []),
            duration_seconds=data.get("duration_seconds"),
            error=data.get("error"),
        ))
    # Sort by started_at descending
    runs.sort(key=lambda x: x.started_at, reverse=True)
    return runs


@app.get(
    "/results/{run_id}",
    response_model=RunResult,
    summary="Get results for a specific run",
    tags=["Results"],
)
async def get_results(run_id: str):
    """
    Get the full results for a specific scrape run.

    - If status is `pending` or `running`, results are not yet available.
    - If status is `completed`, the full list of found websites is returned.
    - If status is `failed`, the error message is included.
    """
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]

    return RunResult(
        run_id=run_id,
        location=data["location"],
        status=data["status"],
        started_at=data["started_at"],
        completed_at=data.get("completed_at"),
        duration_seconds=data.get("duration_seconds"),
        engines_used=data.get("engines_used", []),
        engine_stats=data.get("engine_stats", {}),
        total_results=data.get("total_results", 0),
        results=[WebsiteResult(**r) for r in data.get("results", [])],
    )


@app.get(
    "/results/{run_id}/download",
    summary="Download results as CSV",
    tags=["Results"],
)
async def download_results(run_id: str):
    """Download the results of a completed run as a CSV file."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]
    if data["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Run status is '{data['status']}', not 'completed'")

    csv_path = data.get("files", {}).get("csv")
    if not csv_path or not Path(csv_path).exists():
        raise HTTPException(status_code=404, detail="CSV file not found")

    return FileResponse(
        csv_path,
        media_type="text/csv",
        filename=f"real_estate_{data['location'].replace(' ', '_').replace(',', '')}.csv",
    )


@app.get(
    "/results/{run_id}/websites",
    summary="Get just the website URLs",
    tags=["Results"],
)
async def get_websites(run_id: str):
    """Get a simple list of website URLs from a completed run."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    data = RUNS[run_id]
    if data["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Run status is '{data['status']}', not 'completed'")

    websites = [r["website"] for r in data.get("results", [])]
    return {
        "run_id": run_id,
        "location": data["location"],
        "total": len(websites),
        "websites": websites,
    }


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

    # Delete output files
    for fpath in data.get("files", {}).values():
        try:
            Path(fpath).unlink(missing_ok=True)
        except Exception:
            pass

    return {"message": f"Run '{run_id}' deleted", "location": data["location"]}


@app.get(
    "/sheets",
    summary="List Google Sheets tabs",
    tags=["Google Sheets"],
)
async def get_sheets():
    """List all sheets (tabs) in the Google Spreadsheet. Each tab = one location."""
    if not SHEETS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Google Sheets integration not available. Install gspread.")

    try:
        sheets = list_sheets()
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "not configured")
        return {
            "spreadsheet_id": spreadsheet_id,
            "sheets": sheets,
            "total_locations": len(sheets),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/sheets/create",
    summary="Create a new Google Spreadsheet",
    tags=["Google Sheets"],
)
async def create_sheet():
    """Create a new Google Spreadsheet for storing results."""
    if not SHEETS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Google Sheets integration not available. Install gspread.")

    try:
        spreadsheet_id = create_or_get_spreadsheet()
        return {
            "spreadsheet_id": spreadsheet_id,
            "message": "Spreadsheet created. Share it with your service account email!",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
