"""CLI for real estate company lead scraper."""

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import get_config
from src.scraper import RealEstateScraper


def _sanitize_filename(location: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", location).strip("_").lower()


def save_results(results: list[dict[str, Any]], output_dir: str, location: str) -> None:
    """Save results to CSV, TXT, and JSON."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    loc_slug = _sanitize_filename(location)

    # --- CSV ---
    csv_path = out / f"websites_{loc_slug}_{ts}.csv"
    fields = ["website", "domain", "title", "source_query", "score", "location", "found_at"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"  CSV saved: {csv_path}")

    # --- TXT (just URLs, one per line) ---
    txt_path = out / f"websites_{loc_slug}_{ts}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(r["website"] + "\n")
    print(f"  TXT saved: {txt_path}")

    # --- JSON ---
    json_path = out / f"websites_{loc_slug}_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"  JSON saved: {json_path}")

    # --- Also save "latest" copies for easy access ---
    latest_csv = out / "latest_websites.csv"
    with open(latest_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    latest_txt = out / "latest_websites.txt"
    with open(latest_txt, "w", encoding="utf-8") as f:
        for r in results:
            f.write(r["website"] + "\n")


def print_results(results: list[dict[str, Any]]) -> None:
    """Print results table."""
    print(f"\n{'=' * 90}")
    print(f"  FOUND {len(results)} REAL ESTATE COMPANY WEBSITES")
    print(f"{'=' * 90}\n")

    print(f"  {'#':<5} {'WEBSITE':<45} {'TITLE':<25} {'SCORE'}")
    print(f"  {'-' * 5} {'-' * 45} {'-' * 25} {'-' * 5}")

    for i, r in enumerate(results, 1):
        try:
            website = r["website"][:43]
            title = r.get("title", "")[:23]
            # Sanitize non-ASCII for console output
            title = title.encode("ascii", errors="replace").decode("ascii")
            score = r.get("score", 0)
            print(f"  {i:<5} {website:<45} {title:<25} {score}")
        except Exception:
            print(f"  {i:<5} {r.get('website', 'N/A')[:43]}")

    print(f"\n  Total unique websites: {len(results)}")
    print(f"{'=' * 90}\n")


async def run(location: str | None = None) -> None:
    """Main run flow."""
    config = get_config()

    # Override location if provided via CLI
    if location:
        config._config["LOCATION"] = location

    scraper = RealEstateScraper(config)
    results = await scraper.run()

    if results:
        print_results(results)
        save_results(results, "output", config.location)
    else:
        print("\n  No websites found. Try a different location or check network.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape real estate company websites by location"
    )
    parser.add_argument(
        "--location", "-l",
        help='Override the location from config.yaml (e.g., "Los Angeles, CA")',
    )
    args = parser.parse_args()

    asyncio.run(run(location=args.location))


if __name__ == "__main__":
    main()
