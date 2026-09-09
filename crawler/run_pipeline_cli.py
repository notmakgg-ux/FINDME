"""
FINDME CLI Pipeline Runner
==========================
Runs the full pipeline (URL Finder + Email Crawler) for one or more locations.

Single location (runs immediately):
    python run_pipeline_cli.py --location "Miami, Florida, USA" --min-results 500

Batch mode — queue many locations and run them sequentially:
    python run_pipeline_cli.py --locations locations.txt --min-results 500
    python run_pipeline_cli.py --location "A" --location "B" --location "C" --min-results 500

Batch mode enqueues all locations into the pipeline queue. The queue runner
then picks them up one at a time and runs URL Finder + Email Crawler for each,
moving to the next location only when the current one finishes.

The queue runner runs inline by default (no separate process needed). If an
external queue runner is active, it will pick up the queued items instead."""

import argparse
import asyncio
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import os

# Windows console fix — allow emoji/UTF-8 output instead of cp1252 crashes
# line_buffering=True keeps live-log prints visible in real time under `nohup > log`
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
else:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

from dotenv import load_dotenv

# Load .env from project root (one level up from crawler/)
load_dotenv(Path(__file__).parent.parent / ".env")

# Add paths
_crawler_dir = str(Path(__file__).parent)
_findme_dir = str(Path(__file__).parent.parent / "findme")
_url_finder_dir = str(Path(__file__).parent.parent / "findme" / "url_finder")
for _p in [_crawler_dir, _findme_dir, _url_finder_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline_cli.log", mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("findme_cli")

from config import settings


def parse_args():
    parser = argparse.ArgumentParser(
        description="""FINDME CLI Pipeline Runner

Runs the full pipeline (URL Finder + Email Crawler) for one or more locations.

Single location (runs immediately):
    python run_pipeline_cli.py --location "Miami, Florida, USA" --min-results 500

Batch mode — queue many locations, run them sequentially:
    python run_pipeline_cli.py --locations locations.txt --min-results 500
    python run_pipeline_cli.py --location "A" --location "B" --location "C" --min-results 500

Batch mode enqueues all locations into the pipeline queue; the QueueManager
picks them up one at a time and runs URL Finder + Email Crawler for each,
moving to the next only when the current one finishes.""",
    )
    parser.add_argument(
        "--location", action="append", default=None,
        help="Location to search. May be given multiple times: --location A --location B",
    )
    parser.add_argument(
        "--locations", type=str, default=None,
        help="Path to a text file with one location per line (batch mode).",
    )
    parser.add_argument(
        "--min-results", type=int, default=500,
        help="Minimum unique URL Finder results per location (default: 500)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Email Crawler concurrency (default: from .env MAX_CONCURRENT_REQUESTS)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="DELETE all existing database data before starting "
             "(default: preserve old data and append new results)",
    )
    parser.add_argument(
        "--skip-clear", action="store_true",
        help="(deprecated — now the default) Preserve existing database data",
    )
    parser.add_argument(
        "--skip-crawler", action="store_true",
        help="Only run URL Finder, skip Email Crawler",
    )
    parser.add_argument(
        "--queue-only", action="store_true",
        help="Batch mode: enqueue all locations and exit (do not run them inline). "
             "The QueueManager will pick them up sequentially.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def resolve_locations(args) -> list[str]:
    """Resolve the final location list from --location / --locations."""
    locations: list[str] = []

    if args.location:
        locations.extend(args.location)

    if args.locations:
        path = Path(args.locations)
        if not path.exists():
            print(f"\n❌ Locations file not found: {path}")
            sys.exit(1)
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"\n❌ Failed to read locations file {path}: {e}")
            sys.exit(1)
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                locations.append(s)

    if not locations:
        print("\n❌ No locations provided. Use --location, --locations, or both.")
        sys.exit(1)

    return locations


def clear_database():
    """Delete all rows from company_details and contact tables."""
    from storage.db import _get_connection
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM company_details")
        companies = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM contact")
        contacts = cur.fetchone()[0]

        cur.execute("TRUNCATE contact, company_details RESTART IDENTITY CASCADE")
        conn.commit()
        print(f"\n🗑️  Cleared database: {companies} companies, {contacts} contacts deleted")
        logger.info(f"Cleared database: {companies} companies, {contacts} contacts")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to clear database: {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Step 1: URL Finder
# ---------------------------------------------------------------------------
# URL Finder logic lives in url_finder_runner.py
from url_finder_runner import (
    insert_company_now,
    run_url_finder,
    save_url_finder_to_db,
)


# ---------------------------------------------------------------------------
# Step 2: Email Crawler
# ---------------------------------------------------------------------------
def get_progress_callback(total: int):
    """Create a progress callback that prints status."""
    last_printed = [0]

    def callback(current, total_val, message):
        if current > last_printed[0]:
            pct = (current / total_val) * 100 if total_val > 0 else 0
            print(f"\r  Progress: {current}/{total_val} ({pct:.0f}%) — {message}", end="", flush=True)
            last_printed[0] = current
            if current == total_val:
                print()
    return callback


async def run_email_crawler(location: str, concurrency: int):
    """Run the email crawler on all companies in the DB for the given location."""
    from storage import read_input_companies, save_all_outputs
    from pipeline import process_companies

    print(f"\n[EMAIL CRAWLER] Reading companies for '{location}'...")
    companies = read_input_companies(location)

    if not companies:
        print(f"No companies found to crawl for location '{location}'. Run URL Finder first.")
        return []

    print(f"[EMAIL CRAWLER] Found {len(companies)} companies to process")
    start_time = time.time()

    from storage.db import upsert_company_result

    # Real-time per-company save — writes to BOTH tables (company_details + contact)
    # the moment each company finishes processing. Each company is saved exactly once.
    saved_count = [0]
    failed_saves = []
    def realtime_save(result):
        try:
            upsert_company_result(result)
            saved_count[0] += 1
            # Throttle live-log noise: show 1st-3rd, then every 10th
            if saved_count[0] <= 3 or saved_count[0] % 10 == 0:
                emails = result.emails_found
                phones = result.phones_found
                social = result.social_links_found
                print(f"\n  [DB LIVE #{saved_count[0]}] {result.company_name}: {emails} emails, {phones} phones, {social} social")
        except Exception as e:
            failed_saves.append(result)
            print(f"\n  [DB SAVE ERROR] {result.company_name}: {e}")

    progress_cb = get_progress_callback(len(companies))
    results = await process_companies(
        companies,
        concurrency=concurrency,
        progress_callback=progress_cb,
        result_callback=realtime_save,
    )

    elapsed = time.time() - start_time

    # Retry any transient DB failures so no company's data is lost
    if failed_saves:
        print(f"\n  [RETRY] Re-saving {len(failed_saves)} companies that failed on first write...")
        retried = 0
        for r in failed_saves:
            try:
                upsert_company_result(r)
                retried += 1
            except Exception as e:
                print(f"  [RETRY FAILED] {r.company_name}: {e}")
        print(f"  [RETRY] Successfully re-saved {retried}/{len(failed_saves)}")

    # Save CSV/JSON backup
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    try:
        output_files = save_all_outputs(results, output_dir, prefix="email_crawl", location=location)
        print(f"\n  CSV backup: {output_files['csv']}")
        print(f"  JSON backup: {output_files['json']}")
    except Exception as e:
        logger.warning(f"Failed to save outputs: {e}")

    # Summary
    total_emails = sum(r.emails_found for r in results)
    total_phones = sum(r.phones_found for r in results)
    total_social = sum(r.social_links_found for r in results)
    companies_with_contacts = sum(1 for r in results if r.emails_found > 0 or r.phones_found > 0 or r.social_links_found > 0)
    companies_failed = sum(1 for r in results if r.status.value == "failed")

    print("\n" + "=" * 60)
    print("         EMAIL CRAWLER COMPLETE")
    print("=" * 60)
    print(f"  Companies processed:     {len(results)}")
    print(f"  Companies with contacts: {companies_with_contacts}")
    print(f"  Companies failed:        {companies_failed}")
    print(f"  Total emails found:      {total_emails}")
    print(f"  Total phones found:      {total_phones}")
    print(f"  Total social links:      {total_social}")
    print(f"  Time elapsed:            {elapsed:.1f}s")
    print("=" * 60)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()
    locations = resolve_locations(args)
    concurrency = args.concurrency or settings.max_concurrent_requests

    if len(locations) == 1 and not args.queue_only:
        # ---------- Single-location inline run (original behavior) ----------
        location = locations[0]

        print("\n" + "=" * 60)
        print("  🚀 FINDME CLI PIPELINE")
        print("=" * 60)
        print(f"  Location:       {location}")
        print(f"  Min results:    {args.min_results}")
        print(f"  Concurrency:    {concurrency}")
        print("=" * 60)

        # Step 0: Clear database ONLY if explicitly requested (default: append)
        if args.clear:
            clear_database()
        else:
            print("\n[Preserving existing data — appending to current database]")
            print("  (run with --clear to delete all old data first)")

        # Step 1: URL Finder
        print("\n" + "=" * 60)
        print("  PHASE 1: URL FINDER")
        print("=" * 60)
        # Real-time URL saves — each new URL found is inserted into company_details immediately
        uf_saved = [0]
        def realtime_url_save(record):
            try:
                insert_company_now(record, location)
                uf_saved[0] += 1
                # Throttle: show 1st, 5th, 10th, then every 25th to avoid log spam
                if uf_saved[0] <= 3 or uf_saved[0] % 10 == 0:
                    print(f"\n  [URL LIVE #{uf_saved[0]}] {record.get('domain', '')} → company_details")
            except Exception as e:
                print(f"\n  [URL SAVE ERROR] {record.get('domain', '')}: {e}")

        uf_start = time.time()
        results = run_url_finder(location, args.min_results, on_new_result=realtime_url_save)
        uf_elapsed = time.time() - uf_start
        print(f"\n  URL Finder complete: {len(results)} websites found in {uf_elapsed:.1f}s")

        if not results:
            print("No websites found — aborting pipeline.")
            return

        # Safety net — re-save any that were missed (ON CONFLICT DO NOTHING, no duplicates)
        inserted = save_url_finder_to_db(results, location)
        print(f"  Final DB sync: {inserted} new companies added (location: '{location}')")

        # Step 2: Email Crawler
        if args.skip_crawler:
            print("\n[Skipping Email Crawler as requested]")
            return

        print("\n" + "=" * 60)
        print("  PHASE 2: EMAIL CRAWLER")
        print("=" * 60)

        asyncio.run(run_email_crawler(location, concurrency))

        print("\n✅  FULL PIPELINE COMPLETE")

    else:
        # ---------- Batch / queue mode ----------
        print("\n" + "=" * 60)
        print("  🚀 FINDME CLI — BATCH QUEUE MODE")
        print("=" * 60)
        print(f"  Locations:      {len(locations)}")
        print(f"  Min results:    {args.min_results}")
        print(f"  Concurrency:    {concurrency}")
        print(f"  Queue-only:     {args.queue_only}")
        print("=" * 60)

        for loc in locations:
            print(f"  • {loc}")
        print("=" * 60)

        # Step 0: Clear database ONLY if explicitly requested (default: append)
        if args.clear:
            clear_database()

        # Enqueue only locations that are not already queued/completed/running.
        # This makes repeated batch invocations idempotent (no duplicate queue entries).
        from storage.db import enqueue_locations_batch, get_queue_items as _get_all_queue

        existing = _get_all_queue()
        existing_locs = {(q["location"].strip().lower(), q["status"]) for q in existing}
        new_locations = [
            loc for loc in locations
            if (loc.strip().lower(), "queued") not in existing_locs
            and (loc.strip().lower(), "running") not in existing_locs
            and (loc.strip().lower(), "completed") not in existing_locs
        ]

        if not new_locations:
            print("\n✅  All requested locations are already queued, running, or completed. Nothing to enqueue.")
        else:
            queue_ids = enqueue_locations_batch(
                locations=new_locations,
                enable_url_finder=True,
                enable_email_crawler=not args.skip_crawler,
                min_results=args.min_results,
            )
            print(f"\n✅  Enqueued {len(queue_ids)} new pipeline run(s) into the sequential queue.")
            print("   Queue IDs:", ", ".join(str(qid) for qid in queue_ids))
            for loc in new_locations:
                print(f"   • {loc}")

        if args.queue_only:
            print("   (--queue-only) Exiting now — the queue runner will process them sequentially.")
            return

        # If no external queue runner is active, run queued locations inline now.
        # Only run items that are still queued (skip anything already running/completed).
        from storage.db import get_queue_items, update_queue_item_status

        pending = [q for q in get_queue_items() if q.get("status") == "queued"]
        if not pending:
            print("\n✅  Nothing queued to run.")
            return

        print(f"\n   No external queue runner detected — running {len(pending)} queued location(s) inline now.\n")

        for item in pending:
            loc = item["location"]
            print("\n" + "=" * 60)
            print(f"  RUNNING: {loc}")
            print("=" * 60)

            update_queue_item_status(item["queue_id"], "running")

            # URL Finder
            print("\n  PHASE 1: URL FINDER")
            uf_saved = [0]
            def make_url_cb(loc2):
                def cb(record):
                    try:
                        insert_company_now(record, loc2)
                        uf_saved[0] += 1
                        if uf_saved[0] <= 3 or uf_saved[0] % 10 == 0:
                            print(f"\n    [URL LIVE #{uf_saved[0]}] {record.get('domain', '')} → company_details")
                    except Exception as e:
                        print(f"\n    [URL SAVE ERROR] {record.get('domain', '')}: {e}")
                return cb

            uf_start = time.time()
            results = run_url_finder(loc, args.min_results, on_new_result=make_url_cb(loc))
            uf_elapsed = time.time() - uf_start
            print(f"\n  URL Finder complete: {len(results)} websites found in {uf_elapsed:.1f}s")

            if results:
                inserted = save_url_finder_to_db(results, loc)
                print(f"  Final DB sync: {inserted} new companies added (location: '{loc}')")
            else:
                print("  No websites found for this location — skipping Email Crawler.")

            # Email Crawler
            if not args.skip_crawler and results:
                print("\n  PHASE 2: EMAIL CRAWLER")
                asyncio.run(run_email_crawler(loc, concurrency))

            update_queue_item_status(item["queue_id"], "completed")

            print("\n" + "=" * 60)
            print(f"  {loc} COMPLETE — moving to next location...")
            print("=" * 60 + "\n")

        print("\n✅  ALL LOCATIONS COMPLETE")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        print(f"\n❌ Pipeline failed: {e}")
        traceback.print_exc()