"""
Batch Runner — processes companies in batches of 50, saves after each batch.
Supports resume: skips companies already written to the results sheet.

Usage:
    python batch_runner.py
    python batch_runner.py --batch-size 25
    python batch_runner.py --dry-run
"""

import asyncio
import json
import sys
import os
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from config import settings
from storage import read_input_companies, get_spreadsheet, get_all_sheet_names, save_all_outputs
from pipeline import process_companies
from models.schemas import CompanyResult, CrawlStatus

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_gspread_client():
    creds = Credentials.from_service_account_file(
        settings.google_sa_key_path, scopes=SCOPES
    )
    return gspread.authorize(creds)


def _build_result_row(result: CompanyResult) -> list:
    """Build a single result row matching the Results sheet schema."""
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
                e.email, e.person_name, e.role or e.job_title,
                e.confidence_score, e.source_url,
                e.extraction_method, e.source_page_type.value,
            ])
        else:
            row.extend(["", "", "", "", "", "", ""])
    row.extend([
        result.duration_seconds or 0,
        result.error_type or "",
        result.error_message or "",
        json.dumps([{
            "email": e.email, "name": e.person_name,
            "role": e.role or e.job_title, "score": e.confidence_score,
            "source": e.source_url, "method": e.extraction_method,
            "page_type": e.source_page_type.value,
            "nearby_text": e.nearby_text[:200],
        } for e in sorted_emails], ensure_ascii=False),
    ])
    return row


RESULTS_HEADERS = [
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


def _format_sheet_header(ws: gspread.Worksheet):
    """Apply bold header, freeze row, and column widths."""
    try:
        ws.format("1:1", {
            "textFormat": {
                "bold": True,
                "fontSize": 10,
                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
        })
        ws.freeze(rows=1)
        # Set key column widths
        widths = {"A": 300, "B": 250, "D": 200, "E": 120, "J": 250, "K": 200, "L": 160, "M": 80}
        for col, px in widths.items():
            ws.update_columns(col, {"pixelSize": px})
    except Exception:
        pass


def _color_status_cells(ws: gspread.Worksheet, row_num: int, status: str):
    """Color the Status cell based on result."""
    colors = {
        "completed": {"red": 0.85, "green": 0.92, "blue": 0.83},
        "no_email_found": {"red": 1.0, "green": 0.95, "blue": 0.8},
        "failed": {"red": 0.95, "green": 0.8, "blue": 0.8},
    }
    bg = colors.get(status)
    if bg:
        try:
            ws.format(f"E{row_num}", {"backgroundColor": bg})
        except Exception:
            pass


def _ensure_results_sheet(client, results_sheet: str) -> gspread.Worksheet:
    """Create the results sheet if it doesn't exist, with human-friendly headers."""
    ss = client.open_by_key(settings.google_spreadsheet_id)
    try:
        ws = ss.worksheet(results_sheet)
        existing = ws.row_values(1)
        if not existing or existing[0] != "Company Name":
            ws.clear()
            ws.update(range_name="A1", values=[RESULTS_HEADERS])
            _format_sheet_header(ws)
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=results_sheet, rows=200, cols=len(RESULTS_HEADERS))
        ws.update(range_name="A1", values=[RESULTS_HEADERS])
        _format_sheet_header(ws)
        return ws


def _get_done_websites(client, results_sheet: str) -> set:
    """Get set of websites already processed (from results sheet)."""
    try:
        ss = client.open_by_key(settings.google_spreadsheet_id)
        ws = ss.worksheet(results_sheet)
        rows = ws.get_all_records()
        return {
            row.get("original_website", "").strip()
            for row in rows
            if row.get("status") in ("completed", "no_email_found")
        }
    except Exception:
        return set()


def _append_results(client, results_sheet: str, results: list[CompanyResult]):
    """Append batch results to the Google Sheet with formatting."""
    ws = _ensure_results_sheet(client, results_sheet)
    rows = [_build_result_row(r) for r in results]
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        # Color status cells for newly appended rows
        total_before = len(ws.get_all_values()) - len(rows)
        for idx, result in enumerate(results, start=total_before + 1):
            _color_status_cells(ws, idx, result.status.value)
    return len(rows)


def _write_summary_file(all_results: list[CompanyResult], output_dir: Path):
    """Write aggregate summary to output/summary.txt."""
    total = len(all_results)
    with_emails = sum(1 for r in all_results if r.emails_found > 0)
    without_emails = total - with_emails
    total_emails = sum(r.emails_found for r in all_results)
    total_pages = sum(r.pages_crawled for r in all_results)
    all_scores = [e.confidence_score for r in all_results for e in r.emails]
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

    top = sorted(all_results, key=lambda r: r.emails_found, reverse=True)[:20]
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

    for r in sorted(all_results, key=lambda r: r.best_contact_score, reverse=True):
        if r.best_contact_email:
            lines.append(
                f"  {r.company_name[:45]:<45} {r.best_contact_email:<35} {r.best_contact_score:<6} {r.best_contact_role}"
            )

    lines.append("")
    lines.append("=" * 60)

    summary_path = output_dir / "summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Summary written to {summary_path}")


async def run_batch(companies: list[dict], concurrency: int = 50) -> list[CompanyResult]:
    """Run the pipeline on a batch of companies."""
    return await process_companies(
        companies,
        concurrency=concurrency,
        progress_callback=lambda cur, tot, msg: print(f"    [{cur}/{tot}] {msg}"),
    )


def main():
    parser = argparse.ArgumentParser(description="Batch email crawler for Melbourne companies")
    parser.add_argument("--batch-size", type=int, default=50, help="Companies per batch (default: 50)")
    parser.add_argument("--concurrency", type=int, default=50, help="Max concurrent crawls per batch")
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages per domain")
    parser.add_argument("--input-sheet", type=str, default="Melbourne, Victoria, Australia")
    parser.add_argument("--results-sheet", type=str, default=None,
                        help="Results sheet name. Auto-derived from input-sheet if not set.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip already-processed companies")
    args = parser.parse_args()

    # Auto-derive results sheet name: "{location} Results"
    if args.results_sheet is None:
        loc = args.input_sheet.strip()
        for suffix in [", Australia", ", USA", ", US"]:
            if loc.endswith(suffix):
                loc = loc[:-len(suffix)].strip()
                break
        args.results_sheet = f"{loc} Results"

    print("=" * 70)
    print("  EMAIL CRAWLER — BATCH RUNNER")
    print("=" * 70)
    print(f"  Input sheet:    {args.input_sheet}")
    print(f"  Results sheet:  {args.results_sheet}")
    print(f"  Batch size:     {args.batch_size}")
    print(f"  Concurrency:    {args.concurrency}")
    print(f"  Max pages:      {args.max_pages}")
    print(f"  Resume:         {args.resume}")
    print("=" * 70)

    # Read all companies
    companies = read_input_companies(args.input_sheet)
    if not companies:
        print("ERROR: No companies found in input sheet!")
        return

    print(f"\n  Total companies loaded: {len(companies)}")

    # Filter already-done companies
    client = _get_gspread_client()
    if args.resume:
        done = _get_done_websites(client, args.results_sheet)
        before = len(companies)
        companies = [c for c in companies if c["website"].strip() not in done]
        skipped = before - len(companies)
        if skipped:
            print(f"  Skipped {skipped} already-processed companies")

    if not companies:
        print("\n  All companies already processed! Nothing to do.")
        return

    print(f"  Remaining to process: {len(companies)}")

    if args.dry_run:
        batches = [companies[i:i + args.batch_size] for i in range(0, len(companies), args.batch_size)]
        for idx, batch in enumerate(batches):
            print(f"\n  Batch {idx + 1} ({len(batch)} companies):")
            for c in batch[:5]:
                print(f"    - {c['company_name'][:50]}")
            if len(batch) > 5:
                print(f"    ... and {len(batch) - 5} more")
        print(f"\n  Total batches: {len(batches)}")
        return

    # Split into batches
    batches = [companies[i:i + args.batch_size] for i in range(0, len(companies), args.batch_size)]
    total_batches = len(batches)

    all_results = []
    total_start = time.time()

    for batch_idx, batch in enumerate(batches):
        batch_num = batch_idx + 1
        print(f"\n{'=' * 70}")
        print(f"  BATCH {batch_num}/{total_batches} — {len(batch)} companies")
        print(f"{'=' * 70}")

        batch_start = time.time()

        # Run the pipeline
        results = asyncio.run(run_batch(batch, concurrency=args.concurrency))
        all_results.extend(results)

        batch_duration = round(time.time() - batch_start, 1)
        batch_emails = sum(r.emails_found for r in results)
        batch_with = sum(1 for r in results if r.emails_found > 0)

        print(f"\n  Batch {batch_num} done in {batch_duration}s")
        print(f"    Emails found: {batch_emails}")
        print(f"    Companies with emails: {batch_with}/{len(batch)}")

        # Append to Google Sheets
        print(f"  Writing to Google Sheets ({args.results_sheet})...")
        try:
            written = _append_results(client, args.results_sheet, results)
            print(f"  Written {written} rows to '{args.results_sheet}'")
        except Exception as e:
            print(f"  ERROR writing to sheets: {e}")

        # Estimate remaining
        elapsed = time.time() - total_start
        if batch_idx < total_batches - 1:
            avg_per_batch = elapsed / (batch_idx + 1)
            remaining_batches = total_batches - batch_idx - 1
            eta_seconds = avg_per_batch * remaining_batches
            eta_min = eta_seconds / 60
            print(f"  Elapsed: {elapsed/60:.1f} min | ETA: ~{eta_min:.0f} min remaining")

    # Write summary and CSV/JSON to output directory
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    _write_summary_file(all_results, output_dir)
    
    # Save CSV and JSON (preserved between runs)
    output_files = save_all_outputs(all_results, output_dir, prefix="email_crawl_batch", location=args.input_sheet)
    print(f"  CSV: {output_files['csv']}")
    print(f"  JSON: {output_files['json']}")
    print(f"  Latest CSV: {output_files['latest_csv']}")

    total_duration = round(time.time() - total_start, 1)
    total_emails = sum(r.emails_found for r in all_results)
    total_with = sum(1 for r in all_results if r.emails_found > 0)

    print(f"\n{'=' * 70}")
    print(f"  COMPLETE")
    print(f"  Total companies: {len(all_results)}")
    print(f"  Companies with emails: {total_with}")
    print(f"  Total emails found: {total_emails}")
    print(f"  Total time: {total_duration / 60:.1f} min")
    print(f"  Results in: {args.results_sheet}")
    print(f"  CSV file: {output_files['csv']}")
    print(f"  Latest CSV: {output_files['latest_csv']}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
