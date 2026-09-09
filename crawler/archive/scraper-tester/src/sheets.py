"""
Google Sheets Integration
=========================
Pushes scrape results to a Google Spreadsheet.
Each location gets its own sheet (tab) within the spreadsheet.

Setup:
1. Create a Google Cloud project
2. Enable the Google Sheets API
3. Create a Service Account and download the JSON key
4. Save the JSON key as config/google_service_account.json (or set GOOGLE_SA_KEY_PATH)
5. Share the target spreadsheet with the service account email
"""

import os
from pathlib import Path
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Scopes needed for Sheets + Drive (to create sheets)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Default header row
HEADERS = [
    "Website",
    "Domain",
    "Title",
    "Snippet",
    "Source Query",
    "Source Engine",
    "Location",
    "Score",
    "Found At",
]


def _get_credentials() -> Credentials:
    """Load Google service account credentials from env or file."""
    key_path = os.getenv(
        "GOOGLE_SA_KEY_PATH",
        str(Path(__file__).parent.parent / "config" / "google_service_account.json"),
    )

    if not Path(key_path).exists():
        raise FileNotFoundError(
            f"Google service account key not found at: {key_path}\n"
            "Set GOOGLE_SA_KEY_PATH env var or place the JSON file in config/"
        )

    return Credentials.from_service_account_file(key_path, scopes=SCOPES)


def _get_client() -> gspread.Client:
    """Get an authenticated gspread client."""
    creds = _get_credentials()
    return gspread.authorize(creds)


def _slugify(text: str) -> str:
    """Turn 'Atlanta, Georgia' into 'Atlanta, Georgia' (safe for sheet names)."""
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_or_get_spreadsheet(title: str = "Real Estate Scraper Results") -> str:
    """
    Create a new spreadsheet or return an existing one.
    Returns the spreadsheet ID.
    """
    client = _get_client()

    # Check if spreadsheet already exists in Drive
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")

    if spreadsheet_id:
        try:
            sh = client.open_by_key(spreadsheet_id)
            print(f"[Sheets] Using existing spreadsheet: {sh.url}")
            return spreadsheet_id
        except gspread.SpreadsheetNotFound:
            print("[Sheets] Configured spreadsheet not found, creating new one...")

    # Create new spreadsheet
    sh = client.create(title)
    spreadsheet_id = sh.id
    print(f"[Sheets] Created new spreadsheet: {sh.url}")
    print(f"[Sheets] Spreadsheet ID: {spreadsheet_id}")
    print(f"[Sheets] IMPORTANT: Save this in .env as GOOGLE_SPREADSHEET_ID={spreadsheet_id}")
    print(f"[Sheets] Also share the spreadsheet with your service account email!")

    # Delete the default "Sheet1" if it exists
    try:
        default_sheet = sh.sheet1
        if default_sheet.title == "Sheet1":
            sh.del_worksheet(default_sheet)
    except Exception:
        pass

    return spreadsheet_id


def push_results_to_sheet(
    location: str,
    results: list[dict],
    spreadsheet_id: str | None = None,
) -> dict:
    """
    Push scrape results to a Google Spreadsheet.
    Creates a new sheet (tab) for each unique location.

    Args:
        location: The location name (e.g., "Atlanta, Georgia")
        results: List of result dicts from the scraper
        spreadsheet_id: Override spreadsheet ID (or use env var)

    Returns:
        Dict with sheet info
    """
    if not spreadsheet_id:
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")

    if not spreadsheet_id:
        spreadsheet_id = create_or_get_spreadsheet()

    client = _get_client()
    sh = client.open_by_key(spreadsheet_id)

    # Sheet name = location (truncated to 100 chars for Google Sheets limit)
    sheet_name = location[:100]

    # Check if sheet already exists for this location
    try:
        worksheet = sh.worksheet(sheet_name)
        print(f"[Sheets] Found existing sheet '{sheet_name}', clearing old data...")
        worksheet.clear()
    except gspread.WorksheetNotFound:
        # Create new sheet for this location
        print(f"[Sheets] Creating new sheet '{sheet_name}' for location: {location}")
        worksheet = sh.add_worksheet(title=sheet_name, rows=1000, cols=len(HEADERS))

    # Write headers
    worksheet.update(range_name="A1", values=[HEADERS])

    # Format headers (bold + freeze)
    worksheet.format("A1:I1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
    })
    worksheet.freeze(rows=1)

    # Build rows from results
    rows = []
    for r in results:
        rows.append([
            r.get("website", ""),
            r.get("domain", ""),
            r.get("title", ""),
            r.get("snippet", ""),
            r.get("source_query", ""),
            r.get("source_engine", ""),
            r.get("location", ""),
            r.get("score", 0),
            r.get("found_at", datetime.now().isoformat()),
        ])

    # Write data after headers
    worksheet.update(range_name="A2", values=rows)

    # Auto-resize columns (best effort)
    try:
        worksheet.columns_auto_resize(0, len(HEADERS) - 1)
    except Exception:
        pass

    print(f"[Sheets] Pushed {len(rows)} rows to sheet '{sheet_name}'")
    print(f"[Sheets] Spreadsheet URL: {sh.url}")

    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": sh.url,
        "sheet_name": sheet_name,
        "rows_added": len(rows),
        "total_rows": existing_rows + len(rows) if existing_rows == 1 else existing_rows + len(rows),
    }


def list_sheets(spreadsheet_id: str | None = None) -> list[str]:
    """List all sheet (tab) names in the spreadsheet."""
    if not spreadsheet_id:
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")

    if not spreadsheet_id:
        return []

    client = _get_client()
    sh = client.open_by_key(spreadsheet_id)
    return [ws.title for ws in sh.worksheets()]
