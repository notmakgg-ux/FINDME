# Running the FINDME Pipeline Long-Term (100 Locations)

## Overview

The pipeline runs in two phases per location:
1. **URL Finder** — searches DuckDuckGo for company websites in a location
2. **Email Crawler** — visits each found website and extracts emails, phones, social links

Locations run **sequentially** — one finishes (both phases complete), then the next starts. Results go into a PostgreSQL database (`company_details` + `contact` tables).

---

## Project Structure

```
C:\TP\URLFinder\                    # project root
├── locations.txt                   # YOUR 100 locations go here
├── batch_run.log                   # pipeline output log (created automatically)
├── batch_run.err.log               # pipeline error log (created automatically)
├── crawler\
│   ├── run_pipeline_cli.py         # pipeline runner (start here)
│   ├── storage\db.py               # DB connection + queue operations
│   └── ...
├── findme\url_finder\
│   └── default_queries.yaml        # search queries + config
├── monitor.py                      # web dashboard (port 8010)
├── .env                            # DB credentials + settings (DO NOT SHARE)
└── check_db_status.py              # utility to verify DB state
```

---

## 1. Where to Put Your 100 Locations

**File:** `locations.txt` (in project root `C:\TP\URLFinder\`)

Format: one location per line. Lines starting with `#` are comments (ignored).

```
# Real estate locations to scrape
Atlanta, Georgia , USA
Miami, Florida, USA
Chicago, Illinois, USA
...
```

**Rules:**
- Use the format: `City, State , USA` (note the space before the comma is tolerated, but `City, State, USA` works too)
- Each line = one pipeline run (URL Finder + Email Crawler)
- The queue processes them **in file order** (top to bottom)

---

## 2. Where to Change Search Queries

**File:** `findme\url_finder\default_queries.yaml`

This YAML file controls what the URL Finder searches for. Key sections:

```yaml
LOCATION: "Atlanta, Georgia"          # placeholder; overridden per-location at runtime

MIN_UNIQUE_RESULTS: 500               # target unique websites per location
MAX_RETRIES: 3                        # retries if under MIN_UNIQUE_RESULTS

SEARCH_QUERIES:                       # <-- edit this list
  - "real estate company {location}"
  - "real estate agency {location}"
  - "realty firm {location}"
  # ... ~80 queries total, each gets {location} substituted

SEARCH_ENGINES:
  - duckduckgo                        # only engine currently used

MAX_RESULTS_PER_QUERY: 25             # results per query per engine
REQUEST_DELAY: 2                      # seconds between requests (slows down scraping)
MIN_SCORE: 1

EXCLUDE_DOMAINS:                      # domains filtered out of results
  - google.com
  - facebook.com
  - youtube.com
  - ...
```

**To customize queries:**
- Add/remove lines under `SEARCH_QUERIES:` — each `- "query {location}"` line is one search
- `{location}` is replaced with the actual location at runtime (e.g., `"real estate company Chicago, Illinois, USA"`)
- Adjust `MIN_UNIQUE_RESULTS` to change how many websites to find per location (default 500)
- Adjust `REQUEST_DELAY` to slow down (higher = slower but less likely to get rate-limited)
- Adjust `MAX_RESULTS_PER_QUERY` to get more/fewer results per query

---

## 3. Where to Change Runtime Settings (.env)

**File:** `.env` (in project root, alongside `locations.txt`)

This holds database credentials and concurrency settings. Key vars:

```
DB_HOST=...
DB_PORT=...
DB_NAME=findme
DB_USER=...
DB_PASSWORD=...

MAX_CONCURRENT_REQUESTS=5             # email crawler thread pool size
```

**Do not change DB credentials unless you know the new values.** The email crawler concurrency (`MAX_CONCURRENT_REQUESTS`) controls how many websites are crawled in parallel per location — higher = faster but more load.

---

## 4. How to Start the Pipeline (Full 100 Locations)

**From the project root (`C:\TP\URLFinder\`):**

```bash
# Using the hermes venv Python (has all dependencies including dotenv):
C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500
```

**What happens:**
1. Reads all 100 locations from `locations.txt`
2. Skips any already queued/running/completed (idempotent — safe to re-run)
3. Enqueues the rest into `pipeline_queue`
4. Runs them **inline** (one at a time) since no external queue runner is active
5. Writes all output to `batch_run.log` (and errors to `batch_run.err.log`)

**Flags you can add:**
- `--min-results 300` — lower target (faster per location, fewer websites)
- `--min-results 1000` — higher target (slower, more websites)
- `--skip-crawler` — only run URL Finder, skip Email Crawler
- `--clear` — **WARNING:** deletes ALL existing data before starting (use only if you want a fresh start)
- `--queue-only` — enqueue and exit immediately (doesn't run inline; useful if you have a separate queue runner)

**Default:** `--min-results 500` is used if you don't specify.

---

## 5. How to Start the Monitor (Web Dashboard)

The monitor shows a live status page at `http://localhost:8010` with:
- Current phase (URL Finder / Email Crawler)
- Current location being processed
- DB counts per location (completed/pending/failed)
- Recent log tail
- **Queue page at `/queue`** — shows the live queue AND has a form to add new locations on the fly

**From the project root:**

```bash
C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -u monitor.py --web --port 8010
```

The monitor:
- Does NOT touch the pipeline or DB
- Reads `batch_run.log` and the DB to build the status page
- Auto-refreshes every 60 seconds (browser meta-refresh)
- You can add locations via the `/queue` page form while the pipeline runs

**To run both together** (pipeline + monitor), start them as two separate processes:

```bash
# Terminal 1 — pipeline (runs for hours/days):
start "FINDME Pipeline" /B "C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" -u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500

# Terminal 2 — monitor (runs as long as you want to watch):
start "FINDME Monitor" /B "C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" -u monitor.py --web --port 8010
```

Or use the provided PowerShell launcher scripts:
- `launch_pipeline.ps1` — launches the pipeline
- `launch_monitor.ps1` — launches the monitor on port 8010

---

## 6. How to Run Unattended (Background, Long-Term)

Since this is Windows, the simplest robust approach:

**Option A — `start /B` (no new window, runs in background):**

```bash
start "FINDME Pipeline" /B "C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" -u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500
```

The pipeline writes to `batch_run.log` so you can check progress anytime:
```bash
type batch_run.log | findstr /c:"PHASE" /c:"LOCATION:" /c:"COMPLETE" /c:"ERROR"
```

**Option B — Run as a scheduled task (most robust for very long runs):**

1. Open Task Scheduler
2. Create Basic Task → name it "FINDME Pipeline"
3. Trigger: "When I log on" (or specific time)
4. Action: Start a program
   - Program: `C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`
   - Arguments: `-u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500`
   - Start in: `C:\TP\URLFinder`
5. Check "Run whether user is logged on or not" + "Run with highest privileges" if needed

This way the pipeline survives logoff/reboots (if you set "Run whether user is logged on or not").

**To check if it's still running:**
```bash
tasklist /FI "IMAGENAME eq python.exe" /FO TABLE
```
Look for `run_pipeline_cli.py` in the command line.

**To stop the pipeline gracefully:**
Find the PID and kill it:
```bash
taskkill /F /PID <pid>
```
Or via PowerShell:
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run_pipeline_cli.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## 7. How to Check Progress While It Runs

**Via the log file:**
```bash
type batch_run.log | findstr /c:"LOCATION:" /c:"PHASE 1" /c:"PHASE 2" /c:"COMPLETE"
```

**Via the DB (quick stats):**
```bash
cd crawler && python ../check_db_status.py
```
Shows: DB connection OK/failed, queue items (running/queued), and company_details counts.

**Via the web dashboard (if monitor is running):**
Open `http://localhost:8010` in a browser — shows live phase, location, DB counts, and recent log.

**Via the queue page (add locations on the fly):**
Open `http://localhost:8010/queue` — shows the queue table and has an "Add new locations" textarea. Submit comma-separated or one-per-line locations to append them to the queue without stopping the pipeline.

---

## 8. How to Add More Locations Mid-Run

You have three options:

**Option A — Edit `locations.txt` and re-run the pipeline CLI:**
```bash
C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500
```
The CLI is idempotent — it skips locations already queued/running/completed. New ones get appended to the queue and run after the current batch.

**Option B — Use the `/queue` web form (if monitor is running):**
Go to `http://localhost:8010/queue`, type new locations in the textarea, click "Add to queue". They get appended after the currently-running location.

**Option C — Direct DB insert (advanced):**
```python
from storage.db import enqueue_locations_batch
enqueue_locations_batch(
    locations=["New City, State, USA"],
    enable_url_finder=True,
    enable_email_crawler=True,
    min_results=500,
)
```
This adds to both the JSON fallback queue and the PostgreSQL `pipeline_queue` table.

---

## 9. What to Expect Time-Wise (Rough Estimate)

Per location, the pipeline does:
1. **URL Finder:** ~80 queries × 25 results each, `REQUEST_DELAY=2s` between requests → roughly 3-6 minutes per location (varies with network/DuckDuckGo rate limits)
2. **Email Crawler:** visits each found website (up to `MIN_UNIQUE_RESULTS`, default 500) → potentially 10-30+ minutes per location depending on concurrency and site response times

For 100 locations at default settings, expect **many hours to a few days** of continuous running. The pipeline writes progress to `batch_run.log` and updates the DB throughout, so you can check partial results at any time.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Pipeline won't start, `ModuleNotFoundError` | You're using the wrong Python. Use the hermes venv one: `C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`. The plain `uv` Python lacks `dotenv`. |
| Port 8010 already in use (monitor won't start) | A previous monitor is still running. Kill it: `taskkill /F /PID <pid>` or `Get-Process python | Where-Object {$_.CommandLine -like '*monitor.py*'} | Stop-Process -Force` |
| No entries appearing in DB | Check `batch_run.err.log` for errors. Verify DB connection with `check_db_status.py`. Pipeline may still be in URL Finder phase (nothing in `company_details` until URLs are found). |
| Pipeline seems stuck | Check `batch_run.log` for the last lines. Email Crawler can appear stuck during slow site responses — look for "Progress:" lines. If truly frozen, kill and restart; the queue preserves progress (completed locations won't re-run). |
| Want to restart from scratch | Use `--clear` flag (WARNING: deletes all `company_details` + `contact` data). |

---

## 11. Quick Reference Card

```bash
# Project root: C:\TP\URLFinder\

# 1. Edit your 100 locations:
notepad locations.txt

# 2. Edit search queries (optional):
notepad findme\url_finder\default_queries.yaml

# 3. Start pipeline (hermes venv Python):
C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -u crawler\run_pipeline_cli.py --locations locations.txt --min-results 500

# 4. Start monitor (separate process):
C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -u monitor.py --web --port 8010
# Then open http://localhost:8010

# 5. Check progress:
type batch_run.log | findstr /c:"LOCATION:" /c:"COMPLETE"
cd crawler && python ../check_db_status.py

# 6. Add locations mid-run (via web):
# http://localhost:8010/queue

# 7. Stop pipeline:
taskkill /F /PID <pid_of_run_pipeline_cli.py>
```

---

## Files Summary

| File | Purpose | Edit to change |
|---|---|---|
| `locations.txt` | List of 100 locations to scrape | Add/remove locations here |
| `findme\url_finder\default_queries.yaml` | Search queries + scraping config | Change `SEARCH_QUERIES`, `MIN_UNIQUE_RESULTS`, `REQUEST_DELAY` |
| `.env` | DB credentials + concurrency | Change `MAX_CONCURRENT_REQUESTS` (don't touch DB creds unless needed) |
| `crawler\run_pipeline_cli.py` | Pipeline runner | CLI flags: `--min-results`, `--skip-crawler`, `--clear`, `--queue-only` |
| `monitor.py` | Web dashboard + queue manager | Port (`--port`), refresh interval (edit `PAGE_REFRESH_SECONDS`) |
| `batch_run.log` | Pipeline output (created at runtime) | Read-only; check for progress/errors |
| `check_db_status.py` | DB status utility | Run to verify DB connection + queue + company_details counts |
