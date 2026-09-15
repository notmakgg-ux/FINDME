# FINDME — Real Estate Contact Discovery Pipeline

FINDME is a self-healing web-scraping pipeline that finds real-estate company websites in
any U.S. location and harvests their contact details (emails, phones, social profiles)
into PostgreSQL — with a live web dashboard and a watchdog that automatically restarts
hung or crashed runs.

##  Demo

**[▶ Watch the demo video](https://player.cloudinary.com/embed/?cloud_name=uywjarsz&public_id=screen-capture)**

<details>
<summary>Embed the video elsewhere (click to expand)</summary>

```html
<iframe
  src="https://player.cloudinary.com/embed/?cloud_name=uywjarsz&public_id=screen-capture"
  width="960" height="540"
  allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
  frameborder="0" allowfullscreen>
</iframe>
```

</details>

---
**For further, more detailed instructions, check out [INSTRUCTIONS](Instructions.md).**

## How it works

For each location, the pipeline runs **two phases sequentially** — a location only
finishes (and the next one starts) when both phases complete:

```
locations.txt ──► pipeline_queue (PostgreSQL, with JSON fallback)
                        │
                        ▼
             ┌─────────────────────┐        ┌────────────────────────┐
             │ Phase 1: URL Finder │──────► │ Phase 2: Email Crawler │
             └─────────────────────┘        └────────────────────────┘
                                                     │
                                                     ▼
                                      PostgreSQL: company_details + contact
```

1. **URL Finder** — runs ~80 DuckDuckGo search queries per location (each query is a
   template with `{location}` substituted), dedupes domains against an exclusion list,
   and keeps searching until it reaches the target number of unique websites
   (`MIN_UNIQUE_RESULTS`, default **500**, with retries).
2. **Email Crawler** — visits every found website with a concurrent thread pool and
   extracts emails, phone numbers, and social links.

**Reliability built in:**

- The queue lives in PostgreSQL (`pipeline_queue`) with a JSON fallback file — both stay
  in sync, and queue IDs are realigned after inserts so the fallback stays trustworthy.
- **Idempotent restarts** — locations already `queued`/`running`/`completed` are never
  re-enqueued, so re-running the CLI is always safe.
- **Self-heal** — a location stuck in `running` (e.g. after a crash) is automatically
  reset to `queued` on the next start.
- **Watchdog** (in the monitor) — detects a dead or hung pipeline, kills the stuck
  process tree, requeues the interrupted location, and relaunches automatically.

## What you can do

| Capability | Where |
|---|---|
| Run a batch of locations end-to-end | CLI (`crawler/run_pipeline_cli.py`) |
| Watch live progress (phase, location, DB counts, log tail) | Dashboard at `http://localhost:8010` |
| Add new locations **while the pipeline is running** | `/queue` page form, CLI re-run, or DB insert |
| Start / pause / resume / stop the pipeline | Dashboard buttons (`/pipeline/start`, `/pipeline/pause`, `/pipeline/resume`) |
| Delete queued locations | `/queue` page (completed rows are protected — they power dedup) |
| Inspect pending companies for the current location | `/crawler/pending` endpoint |
| Auto-recover from hangs/crashes | Watchdog (armed when the monitor runs) |
| Run fully unattended (days-long batches) | `start /B` or Windows Task Scheduler |
| Check DB state from the terminal | `check_db_status.py` |

## Requirements

- **Python 3.10+** (developed/tested on Windows) with the project's dependencies —
  including **Playwright** (used by the email crawler) and `python-dotenv`.
- **PostgreSQL** reachable with the credentials in `.env` (database `findme`,
  tables: `company_details`, `contact`, `pipeline_queue`).
-  The bundled launcher scripts (`launch_pipeline.ps1`, `launch_monitor.ps1`) and the
  ops doc hardcode one machine's venv path — edit them to point at your own Python.

## Setup

### 1. Configure `.env` (project root)

```ini
DB_HOST=...
DB_PORT=...
DB_NAME=findme
DB_USER=...
DB_PASSWORD=...

MAX_CONCURRENT_REQUESTS=5    # email crawler thread pool size (higher = faster, more load)
```

### 2. Add your locations — `locations.txt` (project root)

One location per line, processed top to bottom. `#` lines are comments.

```
# Real estate locations to scrape
Atlanta, Georgia, USA
Miami, Florida, USA
Chicago, Illinois, USA
```

### 3. (Optional) Tune the search — `findme/url_finder/default_queries.yaml`

| Key | Meaning | Default |
|---|---|---|
| `SEARCH_QUERIES` | Query templates; `{location}` is substituted at runtime | ~80 real-estate queries |
| `MIN_UNIQUE_RESULTS` | Target unique websites per location | `500` |
| `MAX_RESULTS_PER_QUERY` | Results fetched per query | `25` |
| `REQUEST_DELAY` | Seconds between search requests (higher = gentler) | `2` |
| `MAX_RETRIES` | Retries if the target isn't reached | `3` |
| `SEARCH_ENGINES` | Engines used | `duckduckgo` |
| `EXCLUDE_DOMAINS` | Domains filtered out of results | google/facebook/etc. |

## Quick start

```bash
# Terminal 1 — start the pipeline (batch mode over locations.txt):
python -u crawler/run_pipeline_cli.py --locations locations.txt --min-results 500

# Terminal 2 — start the dashboard:
python -u monitor.py --web --port 8010

# Then open http://localhost:8010
```

Or use the provided PowerShell launchers (run from the project root):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File launch_pipeline.ps1   # pipeline in background
powershell -NoProfile -ExecutionPolicy Bypass -File launch_monitor.ps1    # dashboard on :8010
```

Both launchers redirect output to `batch_run.log` / `batch_run.monitor.log`
(errors to the matching `.err.log`).

## CLI reference — `crawler/run_pipeline_cli.py`

| Flag | Effect |
|---|---|
| `--locations FILE` | Batch mode: enqueue every location in the file (one per line) |
| `--location "City, State, USA"` | Single-location mode; repeat the flag for several |
| `--min-results N` | Target unique websites per location (default `500`) |
| `--concurrency N` | Email crawler threads (default: `.env` `MAX_CONCURRENT_REQUESTS`) |
| `--skip-crawler` | Run URL Finder only |
| `--queue-only` | Enqueue everything and exit — don't run inline |
| `--clear` | **Deletes all existing DB data before starting** |
| `--skip-clear` | Deprecated — preserving data is now the default |

## The web dashboard (`monitor.py`, port 8010)

| Page / endpoint | What it gives you |
|---|---|
| `GET /` | Live status: current phase + location, per-location DB counts (completed/pending/failed), recent log tail, process PIDs, and the watchdog state. Auto-refreshes every 60s. |
| `GET /poll` | The same status as JSON — handy for scripts or external monitors. |
| `GET /log` | Full pipeline log in the browser. |
| `GET /queue` | Queue table (queued, running **and** completed rows) + a form to add locations (one per line or comma-separated). |
| `POST /queue/add` | Add locations without stopping the pipeline. |
| `POST /queue/delete` | Remove a queued row (completed rows can't be deleted — completion history is what powers dedup). |
| `POST /pipeline/start` | Launch the pipeline process (self-heals stale `running` rows first). |
| `POST /pipeline/pause` | Pause: creates the `.pipeline_paused` sentinel — the queue stops advancing. |
| `POST /pipeline/resume` | Remove the pause sentinel; processing continues. |

The monitor reads the log and the database — it never writes crawl data — and also
heartbeats to `batch_run.monitor.log` (alerting on CRASH/STUCK when watchdog actions fire).

## The watchdog (auto-restart)

Armed whenever the monitor is running. All thresholds are **wall-clock**, so they're
immune to how often anything polls:

| Situation | Detection | Action |
|---|---|---|
| **Hung** — process alive but no log growth *and* no DB completions for 15 min | silence timestamps | `taskkill /F /T` the whole tree → reset stuck `running` rows to `queued` → relaunch (the interrupted location resumes) |
| **Dead** — pipeline process gone for 10 min | process discovery | relaunch — but only if the queue still has work, so a *finished* run isn't relaunched forever |

Safety rails: a **15-minute cooldown** between restarts, a non-blocking lock so the two
poll threads can never double-restart, and a paused pipeline is never touched.

Knobs at the top of `monitor.py`:

```python
WATCHDOG_ENABLED = True
WATCHDOG_HANG_CONFIRM_SECONDS  = 15 * 60
WATCHDOG_DEAD_CONFIRM_SECONDS  = 10 * 60
WATCHDOG_MIN_RESTART_INTERVAL  = 15 * 60
```

Offline state-machine tests: `python test_watchdog.py` (queue-handler UI tests: `python test_queue_handlers.py`).

## Adding locations mid-run

1. **Web form** — open `http://localhost:8010/queue`, paste locations, submit. They're
   appended behind the current one. (Dedup means duplicates are silently skipped.)
2. **Re-run the CLI** with a bigger `locations.txt` — it's idempotent; only the new
   locations get enqueued.
3. **Direct insert** (advanced):

```python
from storage.db import enqueue_locations_batch
enqueue_locations_batch(
    locations=["New City, State, USA"],
    enable_url_finder=True,
    enable_email_crawler=True,
    min_results=500,
)
```

## Checking progress

```bash
# Log highlights
type batch_run.log | findstr /c:"LOCATION:" /c:"PHASE" /c:"COMPLETE" /c:"ERROR"

# DB snapshot (connection, queue state, company_details counts)
cd crawler && python ../check_db_status.py

# Or just open the dashboard / poll JSON
curl http://localhost:8010/poll
```

## Stopping & running unattended

```bash
# Find and stop the pipeline
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run_pipeline_cli.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

For very long runs, register a **Task Scheduler** task ("When I log on" → start the
venv's `python.exe` with arguments `-u crawler\run_pipeline_cli.py --locations
locations.txt --min-results 500`, *Start in* = project root). Progress survives logoffs
and reboots; killing the process is safe — completed locations never re-run.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` on startup | Wrong Python — use one with the project deps (the hardcoded launcher paths assume a specific venv; edit to yours). |
| Port 8010 already in use | A monitor is still running — kill it first (`taskkill /F /PID <pid>`), or use `--port`. |
| Nothing in the DB | Check `batch_run.err.log`; run `check_db_status.py`. Empty `company_details` during URL Finder phase is normal. |
| Looks stuck | The watchdog restarts it within ~15 min if the monitor is running. Manually: kill the process and re-run the CLI — the queue resumes where it left off. |
| Fresh start needed | `--clear` ( wipes `company_details` + `contact`). |

## Project structure

```
├── locations.txt                      # your locations (one per line)
├── batch_run.log / .err.log           # pipeline output (runtime)
├── batch_run.monitor.log              # monitor heartbeat (runtime)
├── crawler/
│   ├── run_pipeline_cli.py            # pipeline runner (start here)
│   ├── input/                         # seed CSVs
│   ├── backfill_from_output.py        # re-ingest results from output files
│   ├── storage/db.py                  # DB connection + queue ops (dedup, self-heal)
│   └── storage/queue.json             # JSON fallback queue (runtime)
├── findme/url_finder/
│   ├── default_queries.yaml           # search queries + scraping config
│   └── engines/playwright_engine.py   # Playwright browser engine
├── monitor.py                         # web dashboard + watchdog (port 8010)
├── launch_pipeline.ps1 / launch_monitor.ps1
├── check_db_status.py / crawler/check_db_state.py
├── test_watchdog.py / test_queue_handlers.py   # offline tests
├── RUN_PIPELINE_LONGTERM.md           # long-run ops guide
└── .env                               # credentials (never commit)
```

---





**Timing expectations:** URL Finder ≈ 3–6 min per location; Email Crawler ≈ 10–30+ min
depending on concurrency and site response times. A 100-location batch takes hours to a
few days — results land in the DB continuously, so partial data is usable at any time.


**For further, more detailed instructions, check out [INSTRUCTIONS](Instructions.md).**
