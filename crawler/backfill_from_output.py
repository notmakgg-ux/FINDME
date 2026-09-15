"""
Backfill contacts from output JSON
==================================
Rebuilds company_details + contact rows in PostgreSQL from a saved
email-crawler JSON file (output/email_crawl_*.json). The JSON files are
written with `CompanyResult.model_dump()`, so field names match the
Pydantic model 1:1 and can be reconstructed directly.

Idempotent: uses upsert_company_result() which does ON CONFLICT UPDATE on
company_details and DELETE + INSERT on contact — safe to run repeatedly.

Usage:
    python backfill_from_output.py [--json output/email_crawl_....json]
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

import os

# Windows console fix — allow UTF-8 output on cp1252 consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

# Add crawler dir to path so `storage`, `models`, `config` import correctly
_crawler_dir = str(Path(__file__).parent)
if _crawler_dir not in sys.path:
    sys.path.insert(0, _crawler_dir)

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from models.schemas import CompanyResult
from storage.db import upsert_company_result


def find_default_json(output_dir: Path) -> Path | None:
    """Pick the most recent email_crawl_*.json in the output dir."""
    candidates = sorted(output_dir.glob("email_crawl_*.json"))
    if not candidates:
        return None
    # Timestamps are embedded in filenames as YYYYMMDD_HHMMSS — sort works lexically
    return candidates[-1]


def _entry_rank(entry: dict) -> tuple:
    """Higher = better candidate when duplicate website_urls collide."""
    status = entry.get("status", "")
    completed = 1 if status == "completed" else 0
    emails = entry.get("emails_found", 0) or 0
    phones = entry.get("phones_found", 0) or 0
    score = entry.get("best_contact_score", 0) or 0
    return (completed, emails, phones, score)


def main():
    parser = argparse.ArgumentParser(description="Backfill contacts from output JSON")
    parser.add_argument(
        "--json", type=str, default=None,
        help="Path to the email_crawl_*.json file (default: newest in output/)",
    )
    args = parser.parse_args()

    output_dir = Path(__file__).parent / "output"
    json_path = Path(args.json) if args.json else find_default_json(output_dir)

    if not json_path or not json_path.exists():
        print(f"No JSON file found. Looked in: {output_dir}")
        sys.exit(1)

    print(f"\n=== CONTACT BACKFILL ===")
    print(f"  Source: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        print("  ERROR: expected a JSON list of company results")
        sys.exit(1)

    print(f"  Entries: {len(raw)}")

    # --- Dedupe by website_url (the DB unique key) ---
    # The crawler can record the same website under multiple entries
    # (URL Finder dedupes by domain, not exact URL). Keep the best entry
    # per URL so upsert_company_result's DELETE+INSERT doesn't wipe data.
    unique: dict[str, dict] = {}
    dropped = 0
    for entry in raw:
        url = (entry.get("original_website") or "").strip()
        if not url:
            unique.setdefault(f"__no_url_{len(unique)}", entry)
            continue
        cur = unique.get(url)
        if cur is None or _entry_rank(entry) > _entry_rank(cur):
            if cur is not None:
                dropped += 1
            unique[url] = entry
        else:
            dropped += 1

    entries = list(unique.values())
    print(f"  Unique websites after dedupe: {len(entries)} (dropped {dropped} duplicate entries)")

    ok = 0
    failed = 0
    errors: list[str] = []

    for entry in entries:
        company_name = entry.get("company_name", "?")
        try:
            # JSON was saved via model_dump() → reconstruct directly
            result = CompanyResult.model_validate(entry)
            upsert_company_result(result)
            ok += 1
        except Exception as e:
            failed += 1
            errors.append(f"{company_name}: {e}")

    print(f"\n  Upserted:   {ok}/{len(entries)}")
    print(f"  Failed:     {failed}")
    if errors:
        print("\n  Errors (first 10):")
        for err in errors[:10]:
            print(f"    - {err}")

    # --- Guarantee 1:1 company↔contact ---
    # Any company_details row without a contact row (e.g. results that never
    # made it into the JSON) still gets an empty placeholder contact row, so
    # every company is represented exactly once — matching the live run.
    from storage.db import _get_connection
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO contact (company_id, status, emails_found, phones_found, social_links_found)
            SELECT cd.company_id, COALESCE(cd.status, 'pending'), 0, 0, 0
            FROM company_details cd
            WHERE NOT EXISTS (SELECT 1 FROM contact c WHERE c.company_id = cd.company_id)
        """)
        placeholders = cur.rowcount
        conn.commit()
        print(f"  Placeholder contact rows created for companies missing one: {placeholders}")
    except Exception as e:
        conn.rollback()
        print(f"  WARNING: could not create placeholder rows: {e}")
    finally:
        cur.close()
        conn.close()

    print("\n=== BACKFILL COMPLETE ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception:
        traceback.print_exc()
        sys.exit(1)