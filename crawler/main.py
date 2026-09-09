"""
Email Crawler — main entry point.

Reads companies from Google Sheets, crawls websites to find emails,
and writes results back to the same spreadsheet.

Usage:
    python main.py                         # Process all companies from Input sheet
    python main.py --concurrency 30        # Custom concurrency
    python main.py --dry-run               # Show what would be processed
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (one level up from crawler/)
load_dotenv(Path(__file__).parent.parent / ".env")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("email_crawler.log", mode="a"),
    ],
)
logger = logging.getLogger("email_crawler")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Email Crawler — discover email contacts from company websites"
    )
    parser.add_argument(
        "--concurrency", type=int, default=50,
        help="Max concurrent HTTP requests (default: 50)"
    )
    parser.add_argument(
        "--input-sheet", type=str, default=None,
        help="Google Sheet tab name for input companies (default: from .env INPUT_SHEET or 'Input')"
    )
    parser.add_argument(
        "--results-sheet", type=str, default=None,
        help="Google Sheet tab name for results (default: from .env RESULTS_SHEET or 'Results')"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show companies that would be processed without actually crawling"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip companies that already have results in the Results sheet"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    return parser.parse_args()


def get_progress_callback(total: int):
    """Create a progress callback that prints status."""
    last_printed = [0]

    def callback(current, total, message):
        if current > last_printed[0]:
            pct = (current / total) * 100 if total > 0 else 0
            print(f"\r  Progress: {current}/{total} ({pct:.0f}%) — {message}", end="", flush=True)
            last_printed[0] = current
            if current == total:
                print()  # Newline at end

    return callback


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Use settings defaults when CLI args not provided
    from config import settings
    input_sheet = args.input_sheet or settings.input_sheet
    results_sheet = args.results_sheet or settings.results_sheet

    # --- Load input from Google Sheets ---
    print("\n========================================")
    print("    Email Crawler Pipeline")
    print("========================================\n")

    print(f"Reading companies from database...")
    from storage import read_input_companies, write_results, get_all_sheet_names, save_all_outputs

    companies = read_input_companies(input_sheet)
    if not companies:
        print("No companies found in the Input sheet. Make sure it has columns: company_name, website")
        return

    print(f"Found {len(companies)} companies to process\n")

    # Dry run — just show the list
    if args.dry_run:
        print("DRY RUN — companies that would be processed:\n")
        for i, c in enumerate(companies, 1):
            print(f"  {i:3d}. {c['company_name']:40s}  {c['website']}")
        print(f"\nTotal: {len(companies)} companies")
        return

    # --- Resume: skip already-processed companies ---
    if args.resume:
        try:
            from storage import get_completed_websites
            done_websites = get_completed_websites()
            if done_websites:
                before = len(companies)
                companies = [c for c in companies if c["website"].strip() not in done_websites]
                skipped = before - len(companies)
                if skipped:
                    print(f"Resume mode: skipped {skipped} already-processed companies")
        except Exception as e:
            logger.warning(f"Could not check for existing results: {e}")

    # --- Process companies ---
    print(f"Starting crawl with concurrency={args.concurrency}...\n")
    start_time = datetime.now()

    from pipeline import process_companies

    # Batch save callback — writes ALL accumulated results to PostgreSQL after each batch
    def batch_save(results):
        """Write all results to PostgreSQL after each batch."""
        if not results:
            return
        try:
            write_results(results)
            emails = sum(r.emails_found for r in results)
            phones = sum(r.phones_found for r in results)
            social = sum(r.social_links_found for r in results)
            print(f"\n  [DB UPLOAD] {len(results)} companies, {emails} emails, {phones} phones, {social} social — saved to PostgreSQL")
        except Exception as e:
            print(f"\n  [DB UPLOAD ERROR] {e}")

    progress_cb = get_progress_callback(len(companies))
    results = await process_companies(
        companies,
        concurrency=args.concurrency,
        progress_callback=progress_cb,
        batch_callback=batch_save,
    )

    elapsed = (datetime.now() - start_time).total_seconds()

    # --- Final DB save (batch_callback already saved incrementally) ---
    # Skipping redundant write_results(results) to avoid duplicate contact rows

    # --- Save CSV and JSON locally (backup) ---
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_files = save_all_outputs(results, output_dir, prefix="email_crawl", location=input_sheet)

    # --- Print summary ---
    total_emails = sum(r.emails_found for r in results)
    total_phones = sum(r.phones_found for r in results)
    total_social = sum(r.social_links_found for r in results)
    companies_with_contacts = sum(1 for r in results if r.emails_found > 0 or r.phones_found > 0 or r.social_links_found > 0)
    companies_failed = sum(1 for r in results if r.status.value == "failed")

    # Print DB stats
    try:
        from storage import get_stats
        db_stats = get_stats()
    except Exception:
        db_stats = {}

    print("\n========================================")
    print("         PIPELINE COMPLETE")
    print("========================================\n")
    print(f"  Companies processed:    {len(results)}")
    print(f"  Companies with contacts: {companies_with_contacts}")
    print(f"  Companies failed:       {companies_failed}")
    print(f"  Total emails found:     {total_emails}")
    print(f"  Total phones found:     {total_phones}")
    print(f"  Total social links:     {total_social}")
    print(f"  Time elapsed:           {elapsed:.1f}s")
    print(f"\n  Database: {settings.db_host}:{settings.db_port}/{settings.db_name}")
    if db_stats:
        print(f"  DB total companies:     {db_stats.get('total_companies', 0)}")
        print(f"  DB with emails:         {db_stats.get('companies_with_emails', 0)}")
        print(f"  DB with phones:         {db_stats.get('companies_with_phones', 0)}")
        print(f"  DB with social:         {db_stats.get('companies_with_social', 0)}")
    print(f"  CSV backup:             {output_files['csv']}")
    print(f"  JSON backup:            {output_files['json']}\n")

if __name__ == "__main__":
    from config import settings
    asyncio.run(main())
