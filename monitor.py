"""
Pipeline Monitor — watches the live batch pipeline and exposes a light,
plain-HTML status page on http://localhost:8010 plus console/popup alerts.

Two modes (pick one via --web or --poll, default --web):
  --web   Start an HTTP server on port 8010. The / page is a self-refreshing
          plain HTML status page (no JS frameworks, no dark mode, no dashboard
          styling). A /poll JSON endpoint is available for live-ish updates.
          Polling is driven by the browser (meta refresh), not server push.
  --poll  Original polling-only mode: no HTTP server, just console + popup
          alerts every POLL_INTERVAL_SECONDS.

Does NOT touch the pipeline, the DB, the queue, or the log.
Runs from the project root.

Examples:
  python monitor.py --web        # start the status page on :8010
  python monitor.py --web --port 8080
  python monitor.py --poll       # alerts only, no server
"""

from __future__ import annotations

import argparse
import html
import json
import os
import asyncio
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PIPELINE_LOG = Path("batch_run.log")
PIPELINE_CMD_PATTERN = "run_pipeline_cli.py"
CRAWLER_DIR = Path("crawler")

POLL_INTERVAL_SECONDS = 5 * 60

# Controls for launching / pausing the pipeline from the web UI.
# Note: a "hard pause" of the real pipeline process is not possible from the
# outside (Windows SuspendThread is risky), so the monitor enforces pauses by
# running its own inline crawler run with a ControlEvent, and by refusing to
# START the pipeline while a pause is active. The running pipeline itself can
# only be paused cooperatively (not supported for externally-launched runs).
PIPELINE_PYTHON = sys.executable
PAUSE_FILE = Path(".pipeline_paused")

STUCK_LOG_BYTES_MIN = 50
STUCK_DB_COMPLETED_MIN = 1
STUCK_CONSECUTIVE_THRESHOLD = 2
CRASH_CONSECUTIVE_IDLE_THRESHOLD = 3

# --- Watchdog auto-restart -------------------------------------------------
# When enabled, the monitor doesn't just log CRASH/STUCK alerts: it restarts
# the pipeline itself (kill process tree -> requeue stuck 'running' rows ->
# relaunch via _start_pipeline_process).
# All thresholds are WALL-CLOCK based (not poll counts) because the HTTP server
# thread also polls (via _refresh_handler) and shares MonitorState — poll-count
# thresholds would fire within seconds instead of minutes.
WATCHDOG_ENABLED = True
# True silence (pipeline alive but ZERO log growth and no DB completions) for
# this long before the hung process is killed and relaunched.
WATCHDOG_HANG_CONFIRM_SECONDS = 15 * 60
# Pipeline process absent continuously for this long before it is relaunched.
WATCHDOG_DEAD_CONFIRM_SECONDS = 10 * 60
# Minimum seconds between auto-restart attempts, so a pipeline that dies
# immediately after launch can't cause a tight restart loop.
WATCHDOG_MIN_RESTART_INTERVAL = 15 * 60

# Web UI defaults
DEFAULT_HTTP_PORT = 8010
PAGE_REFRESH_SECONDS = 60  # browser meta-refresh for the HTML page


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_message(text: str, monitor_log: Optional[Path] = None) -> None:
    ts = _now_str()
    line = f"[{ts}] {text}"
    print(line)
    if monitor_log is not None:
        try:
            monitor_log.parent.mkdir(parents=True, exist_ok=True)
            with open(monitor_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _find_pipeline_pids() -> list[int]:
    """PIDs of the live pipeline process(es).

    Prefers psutil (exact, matches any interpreter the pipeline runs under,
    e.g. the uv-managed python), and falls back to the historical tasklist
    + wmic parsing if psutil is unavailable.
    """
    try:
        import psutil  # noqa: F401
    except ImportError:
        return _find_pipeline_pids_wmic()
    return sorted(_pipeline_processes().keys())


def _find_pipeline_pids_wmic() -> list[int]:
    """Legacy detection via tasklist + wmic (fallback when psutil missing).

    Only matches python.exe interpreters, so it cannot see a pipeline running
    under the uv-managed python; kept for degraded-mode compatibility only.
    """
    python_pids: list[int] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        ).stdout
    except Exception as exc:
        _log_message(f"WARN: tasklist failed: {exc}")
        return python_pids

    for line in out.splitlines():
        if "python.exe" not in line.lower():
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            python_pids.append(int(parts[1]))
        except ValueError:
            continue

    if not python_pids:
        return []

    pid_set_str = ",".join(str(p) for p in python_pids)
    cmdlines: dict[int, str] = {}
    wmic_ok = False
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"processid={pid_set_str}",
             "get", "processid,commandline"],
            capture_output=True, text=True, errors="replace", timeout=8,
        ).stdout
        for line in out.splitlines():
            s = line.strip().strip('"')
            if not s or "processid" in s.lower() or "commandline" in s.lower():
                continue
            segs = s.split(None, 1)
            if not segs:
                continue
            try:
                pid = int(segs[0])
            except ValueError:
                continue
            cmd = segs[1] if len(segs) > 1 else ""
            cmdlines[pid] = cmd
        wmic_ok = len(cmdlines) > 0
    except Exception as exc:
        _log_message(f"WARN: wmic cmdline query failed: {exc}")

    if not wmic_ok:
        return python_pids

    matched: list[int] = []
    for pid in python_pids:
        if PIPELINE_CMD_PATTERN in cmdlines.get(pid, ""):
            matched.append(pid)
    return matched


def _get_alive_pids_batch() -> set[int]:
    """All live PIDs (psutil preferred, tasklist fallback)."""
    try:
        import psutil
        return {p.pid for p in psutil.process_iter(["pid"])}
    except ImportError:
        pass
    except Exception:
        pass
    alive: set[int] = set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        ).stdout
        for line in out.splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                alive.add(int(parts[1]))
    except Exception:
        pass
    return alive


def _process_alive(pid: int) -> bool:
    return pid in _get_alive_pids_batch()


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _read_recent_log_lines(path: Path, since_size: int, limit: int = 200) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(since_size)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > limit:
            lines = lines[-limit:]
        return "\n".join(lines)
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Pause / Start helpers (web-UI process control)
# ---------------------------------------------------------------------------


def _pause_file_path() -> Path:
    return Path.cwd() / PAUSE_FILE


def _is_paused() -> bool:
    """True while a pause requested from the web UI is active."""
    return _pause_file_path().exists()


def _set_paused(paused: bool) -> None:
    p = _pause_file_path()
    try:
        if paused:
            p.write_text(
                _now_str() + "\n",
                encoding="utf-8",
            )
        else:
            p.unlink(missing_ok=True)
    except Exception as exc:
        _log_message(f"WARN: pause file update failed: {exc}")


def _pipeline_cmdlines() -> dict[int, str]:
    """Map of PID -> command line for every process running the pipeline
    script (any interpreter, e.g. the uv-managed python.exe).

    Uses psutil when available; otherwise falls back to wmic in CSV format,
    which is robust to wmic's 'CommandLine ... ProcessId' column order.
    """
    cmdlines: dict[int, str] = {}
    try:
        import psutil
    except ImportError:
        try:
            out = subprocess.run(
                ["wmic", "process", "get", "processid,commandline", "/FORMAT:CSV"],
                capture_output=True, text=True, errors="replace", timeout=15,
            ).stdout
        except Exception as exc:
            _log_message(f"WARN: wmic cmdline query failed: {exc}")
            return cmdlines
        for line in out.splitlines():
            s = line.strip()
            if not s or PIPELINE_CMD_PATTERN not in s:
                continue
            csv_parts = [p.strip().strip('"') for p in s.split(",")]
            if len(csv_parts) < 3 or not csv_parts[-1].isdigit():
                continue
            try:
                cmdlines[int(csv_parts[-1])] = " ".join(csv_parts[1:-1])
            except ValueError:
                continue
        return cmdlines

    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            info = proc.info
            joined = " ".join(info.get("cmdline") or [])
            if not joined or PIPELINE_CMD_PATTERN not in joined:
                continue
            pid = info["pid"]
            if pid == me:
                continue
            cmdlines[pid] = joined
        except Exception:
            continue
    return cmdlines


def _pipeline_processes() -> dict[int, str]:
    """PIDs whose command line matches the pipeline runner, excluding the
    monitor's own process."""
    me = os.getpid()
    matches = _pipeline_cmdlines()
    matches.pop(me, None)
    return matches


def _start_pipeline_process(min_results: int = 500) -> tuple[bool, str]:
    """Launch run_pipeline_cli.py as a detached background process.

    The spawned process runs in batch/queue mode: it picks up everything
    currently 'queued' in pipeline_queue (plus any --location args given).
    Output is redirected to batch_run.log / batch_run.err.log, exactly like
    launch_pipeline.ps1 does, so the monitor keeps tracking it.
    """
    if _is_paused():
        return False, "Pipeline is PAUSED. Resume it before starting."
    existing = _pipeline_processes()
    if existing:
        pids = ", ".join(str(p) for p in existing)
        return False, f"Pipeline already running (PID {pids})."

    # Self-heal before launch: re-queue any location stuck at 'running' from a
    # previous crashed/killed pipeline run, so the new process always resumes
    # cleanly instead of skipping locations whose rows were left 'running'.
    try:
        reset_count = _reset_stale_running_queue_items()
        if reset_count:
            _log_message(
                f"INFO: pre-start cleanup: {reset_count} stale 'running' "
                "queue item(s) reset to 'queued' before launching the pipeline."
            )
    except Exception:
        pass

    root = Path.cwd()
    script = root / "crawler" / "run_pipeline_cli.py"
    if not script.exists():
        return False, f"Pipeline script not found: {script}"

    log_path = root / PIPELINE_LOG
    err_path = root / "batch_run.err.log"
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x00000008  # DETACHED_PROCESS
        with open(log_path, "ab") as out, open(err_path, "ab") as err:
            proc = subprocess.Popen(
                [
                    PIPELINE_PYTHON, "-u", str(script),
                    "--locations", str(root / "locations.txt"),
                    "--min-results", str(min_results),
                ],
                cwd=str(root),
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
    except Exception as exc:
        _log_message(f"ERROR: failed to start pipeline: {exc}")
        return False, f"Failed to start: {exc}"
    _log_message(f"INFO: pipeline started via web UI (PID {proc.pid})")
    return True, f"Pipeline started (PID {proc.pid}). Output: batch_run.log"


def _kill_pipeline_tree() -> tuple[bool, str]:
    """Force-kill every detected pipeline process (and its children).

    Uses taskkill /T /F so a hung Playwright/HTTP worker under the runner is
    taken down too (taskkill /T walks the child tree, which psutil.terminate()
    alone would not on Windows). Excludes the monitor's own PID defensively.

    Returns (killed_anything, description).
    """
    me = os.getpid()
    pids = [p for p in _find_pipeline_pids() if p != me]
    if not pids:
        return False, "no pipeline process found"

    results: list[str] = []
    killed_any = False
    for pid in pids:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
            )
            ok = completed.returncode == 0
            if ok:
                killed_any = True
            tail = (completed.stdout or completed.stderr or "").strip().splitlines()
            detail = tail[-1] if tail else f"exit {completed.returncode}"
            results.append(f"PID {pid}: {'killed' if ok else 'FAILED'} ({detail})")
        except Exception as exc:
            results.append(f"PID {pid}: ERROR ({exc})")

    # A killed process can linger for a moment; give the OS a beat to reap it
    # so _start_pipeline_process doesn't immediately refuse with "already running".
    for _ in range(10):
        if not [p for p in _find_pipeline_pids() if p != me]:
            break
        time.sleep(0.5)

    return killed_any, "; ".join(results)


_watchdog_lock = threading.Lock()


def _watchdog_auto_restart(
    report: dict[str, Any], state: "MonitorState"
) -> None:
    """Watchdog: restart the pipeline instead of only logging CRASH/STUCK.

    Called from poll_once_with_watchdog(), i.e. from EVERY poll path (main
    loops and the HTTP server's refresh thread).
    Only acts when WATCHDOG_ENABLED, the pipeline is not paused, and the
    cooldown since the last auto-restart has elapsed. Stuck-alive pipelines
    are killed after WATCHDOG_HANG_CONFIRM_SECONDS of true silence, then
    relaunched; dead pipelines are relaunched after
    WATCHDOG_DEAD_CONFIRM_SECONDS. _start_pipeline_process re-stuck 'running'
    queue rows before launching, so the interrupted location resumes.

    Evaluated on EVERY poll (not just alert events): poll_once only reports
    'stuck'/'crash' events once per episode (notified_* latching), so an
    event-gated watchdog would wait forever after the first alert.
    """
    if not WATCHDOG_ENABLED:
        return

    # Non-blocking: if another poll thread is already inside the watchdog
    # (kill/restart in progress), don't double-act — just report it.
    if not _watchdog_lock.acquire(blocking=False):
        state.watchdog_last_action = "skipped (restart already in progress)"
        return
    try:
        _watchdog_auto_restart_locked(report, state)
    finally:
        _watchdog_lock.release()


def _watchdog_auto_restart_locked(
    report: dict[str, Any], state: "MonitorState"
) -> None:
    """Watchdog decision body — caller holds _watchdog_lock."""
    if _is_paused():
        state.watchdog_last_action = "skipped (pipeline paused)"
        return

    now = time.time()
    since_last = now - state.last_watchdog_restart_ts
    if state.last_watchdog_restart_ts and (
        since_last < WATCHDOG_MIN_RESTART_INTERVAL
    ):
        state.watchdog_last_action = (
            f"skipped (cooldown, last restart "
            f"{int(since_last // 60)} min ago)"
        )
        return

    if not report.get("pids_alive"):
        # No live pipeline process (none found, or listed PIDs are dead).
        state.last_silent_since_ts = None
        if state.dead_since_ts is None:
            state.dead_since_ts = now
        dead_secs = now - state.dead_since_ts
        if dead_secs < WATCHDOG_DEAD_CONFIRM_SECONDS:
            state.watchdog_last_action = (
                f"waiting to confirm death "
                f"({int(dead_secs // 60)}/{WATCHDOG_DEAD_CONFIRM_SECONDS // 60} min)"
            )
            return
        reason = f"pipeline process dead for {int(dead_secs // 60)} min"
        if _queue_active_count() == 0:
            state.dead_since_ts = None
            state.watchdog_last_action = "idle — queue empty, not relaunching"
            return
    else:
        # Pipeline alive: hang = continuous true silence (no log growth AND no
        # DB completions) for WATCHDOG_HANG_CONFIRM_SECONDS. last_silent_since_ts
        # is maintained by every poll_once() call, from any thread.
        state.dead_since_ts = None
        silent_secs = 0.0
        if state.last_silent_since_ts is not None:
            silent_secs = now - state.last_silent_since_ts
        if silent_secs < WATCHDOG_HANG_CONFIRM_SECONDS:
            state.watchdog_last_action = (
                f"armed ({int(silent_secs // 60)}/"
                f"{WATCHDOG_HANG_CONFIRM_SECONDS // 60} min silent)"
            )
            return
        reason = (
            f"pipeline hung for {int(silent_secs // 60)} min "
            "(no log growth, no DB completions)"
        )

    loc = state.prev_running_location or report.get("location") or "unknown"
    _log_message(
        f"WATCHDOG: auto-restart triggered — {reason}; "
        f"last location: {loc}"
    )

    killed, kill_desc = _kill_pipeline_tree()
    _log_message(f"WATCHDOG: kill step — {kill_desc}")

    # _start_pipeline_process refuses if a process still matches; retry briefly.
    started = False
    start_msg = ""
    for attempt in range(6):
        started, start_msg = _start_pipeline_process()
        if started:
            break
        if "already running" not in start_msg:
            break
        time.sleep(5)

    state.last_watchdog_restart_ts = time.time()
    state.last_silent_since_ts = None
    state.dead_since_ts = None
    state.consecutive_stuck = 0
    state.consecutive_idle = 0
    if started:
        state.watchdog_restarts += 1
        state.watchdog_last_action = (
            f"RESTARTED ({state.watchdog_restarts} total) after {reason}"
        )
        _log_message(f"WATCHDOG: restart OK — {start_msg}")
    else:
        state.watchdog_last_action = f"RESTART FAILED — {start_msg}"
        _log_message(f"WATCHDOG: restart FAILED — {start_msg}")
    report["event_message"] = (
        f"{report.get('event_message', '')}\n\n"
        f"WATCHDOG: {state.watchdog_last_action}"
    )


# ---------------------------------------------------------------------------
# Stage detection (same logic as before, reused)
# ---------------------------------------------------------------------------


def _detect_stage_from_log(recent_log: str) -> dict:
    result: dict[str, str] = {
        "phase": "unknown",
        "location": "",
        "progress": "",
        "last_company": "",
        "details": "",
    }

    url_finder_hits: list[int] = []
    email_crawler_hits: list[int] = []

    if "PHASE 1: URL FINDER" in recent_log:
        url_finder_hits.append(recent_log.rfind("PHASE 1: URL FINDER"))
    if "run_url_finder" in recent_log:
        url_finder_hits.append(recent_log.rfind("run_url_finder"))

    query_prog_re = re.compile(r"^\s*\[(\d+)/(\d+)\]\s+(.+)$", re.MULTILINE)
    for m in query_prog_re.finditer(recent_log):
        url_finder_hits.append(m.start())
        if int(m.group(1)) > 0 and not result["last_company"]:
            result["last_company"] = (
                f"query {m.group(1)}/{m.group(2)}: {m.group(3).strip()}"
            )

    result_counter_re = re.compile(r"-> \+?\d+ new \(\d+ total\)")
    for m in result_counter_re.finditer(recent_log):
        url_finder_hits.append(m.start())

    uf_result_re = re.compile(
        r"(DuckDuckGo (?:failed after 3 attempts|found).*"
        r"|(found|inserted) .+ URL Finder)"
    )
    for m in uf_result_re.finditer(recent_log):
        url_finder_hits.append(m.start())

    url_live_re = re.compile(r"\[URL LIVE #\d+\]\s+(.+?)\s+->")
    for um in url_live_re.finditer(recent_log):
        url_finder_hits.append(um.start())
        if not result["last_company"]:
            result["last_company"] = um.group(1)

    if "PHASE 2: EMAIL CRAWLER" in recent_log:
        email_crawler_hits.append(recent_log.rfind("PHASE 2: EMAIL CRAWLER"))
    if "EMAIL CRAWLER" in recent_log:
        email_crawler_hits.append(recent_log.rfind("EMAIL CRAWLER"))

    progress_re = re.compile(
        r"Progress:\s+(\d+)/(\d+)\s+\([\d]+\)%\s+[\s\S]{0,3}\s+Processed\s+(.+)$"
    )
    for m in progress_re.finditer(recent_log):
        email_crawler_hits.append(m.start())
        result["progress"] = f"{m.group(1)}/{m.group(2)} ({m.group(3)}%)"
        result["last_company"] = m.group(4).strip()

    crawled_re = re.compile(r"\[(.+?)\]\s+Crawled\s+(\d+)\s+pages\s+via\s+HTTP")
    for cm in crawled_re.finditer(recent_log):
        email_crawler_hits.append(cm.start())
        if not result["last_company"]:
            result["last_company"] = cm.group(1)

    extraction_re = re.compile(
        r"pipeline:\s+\[([^\]]+)\]\s+(HTTP extraction|Enriching|Validating|After dedup)"
    )
    for em in extraction_re.finditer(recent_log):
        email_crawler_hits.append(em.start())
        if not result["last_company"]:
            result["last_company"] = em.group(1)

    latest_url = max(url_finder_hits) if url_finder_hits else -1
    latest_ec = max(email_crawler_hits) if email_crawler_hits else -1
    if latest_url > latest_ec and latest_url >= 0:
        result["phase"] = "url_finder"
    elif latest_ec > latest_url and latest_ec >= 0:
        result["phase"] = "email_crawler"

    running_pat = re.compile(
        r"(?:RUNNING|Location|Running)\s*[:=]\s*(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = running_pat.search(recent_log)
    if m:
        cand = m.group(1).strip().strip('"' + "'").strip()
        if re.search(r"[A-Z][a-z]+,\s+[A-Z][a-z]+", cand):
            result["location"] = cand
            return result

    city_pat = re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),"
        r"\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
        r"(?:,\s*([A-Z][A-Z]+))?\b"
    )
    exclude_cities = {
        "inc", "corp", "llc", "ltd", "group", "company", "co",
        "management", "services", "development", "solutions",
        "investments", "capital", "ventures", "partners",
        "holdings", "brokerage", "agents", "officers", "advisors",
        "consultants", "consulting", "commercial", "residential",
        "property", "realty", "real", "estate", "homes", "home",
        "markets", "strategies", "treasure", "island", "city",
        "town", "county", "village", "landing", "park", "square",
        "place", "point", "view", "hill", "ranch", "farm",
        "gardens", "plaza", "center", "club",
    }
    for m in reversed(list(city_pat.finditer(recent_log))):
        city = m.group(1).strip()
        state = m.group(2).strip()
        country = m.group(3)
        if city.lower() in exclude_cities:
            continue
        if not re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?$", state):
            continue
        if len(state) < 3 or len(state) > 30:
            continue
        loc = f"{city}, {state}"
        if country:
            loc += f", {country}"
        result["location"] = loc
        return result

    loc = result["location"] or "the current location"
    if result["phase"] == "url_finder":
        qtext = result["last_company"] or "searching queries"
        result["details"] = (
            f"URL Finder is searching DuckDuckGo for company websites in {loc}. "
            f"Current query: {qtext}."
        )
    elif result["phase"] == "email_crawler":
        parts = [f"Email Crawler is extracting contacts from companies in {loc}."]
        if result["progress"]:
            parts.append(f"Progress: {result['progress']} companies processed.")
        if result["last_company"]:
            parts.append(f"Current company: {result['last_company']}.")
        result["details"] = " ".join(parts)
    else:
        result["details"] = "Stage could not be determined from recent log lines."
    return result


def _detect_stage_from_db(
    running_location: str, all_locations: list[str]
) -> dict:
    result: dict[str, Any] = {
        "current_location": running_location or "",
        "completed": 0,
        "pending": 0,
        "failed": 0,
        "total": 0,
        "next_locations": [],
    }
    if not running_location:
        return result
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, COUNT(*) FROM company_details "
            "WHERE location = %s GROUP BY status",
            (running_location,),
        )
        for status, count in cur.fetchall():
            if status == "completed":
                result["completed"] = count
            elif status == "pending":
                result["pending"] = count
            elif status == "failed":
                result["failed"] = count
        cur.execute(
            "SELECT COUNT(*) FROM company_details WHERE location = %s",
            (running_location,),
        )
        result["total"] = cur.fetchone()[0]
        cur.execute(
            "SELECT location FROM pipeline_queue "
            "WHERE status = 'queued' ORDER BY priority ASC"
        )
        result["next_locations"] = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as exc:
        _log_message(f"WARN: DB stage query failed: {exc}")
    return result


def _detect_current_location_from_queue() -> Optional[str]:
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT location FROM pipeline_queue "
            "WHERE status = 'running' ORDER BY priority ASC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            return row[0].strip()
    except Exception as exc:
        _log_message(f"WARN: queue read failed: {exc}")
    return None


def _detect_all_locations() -> list[str]:
    """All locations to show on the status page.

    Merges locations.txt with locations known to the database (pipeline_queue
    and company_details) so that locations added via the web /queue form —
    which are not in locations.txt — also appear in the per-location table.
    """
    locs: list[str] = []
    seen: set[str] = set()

    def _add(loc: str) -> None:
        s = (loc or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            locs.append(s)

    p = Path("locations.txt")
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    _add(s)
        except Exception:
            pass

    # Also include locations from the DB (queue entries + crawled companies).
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT location FROM pipeline_queue")
        for row in cur.fetchall():
            _add(row[0])
        cur.execute(
            "SELECT DISTINCT location FROM company_details WHERE location IS NOT NULL"
        )
        for row in cur.fetchall():
            _add(row[0])
        cur.close()
        conn.close()
    except Exception:
        pass

    return locs


def _count_completions_for_locations(locations: list[str]) -> dict[str, int]:
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        loc_tuple = tuple(locations)
        cur.execute(
            "SELECT location, COUNT(*) FROM company_details "
            "WHERE status = 'completed' AND location IN %s GROUP BY location",
            (loc_tuple,),
        )
        result = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()
        conn.close()
        return result
    except Exception:
        return {}


def _count_completions_for_location(location: str) -> int:
    return _count_completions_for_locations([location]).get(location, -1)


def _count_all_pending() -> int:
    """Total number of companies with status='pending' across ALL locations."""
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM company_details WHERE status = 'pending' "
            "AND website_url IS NOT NULL"
        )
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as exc:
        _log_message(f"WARN: pending count query failed: {exc}")
        return 0


def _get_pipeline_running_locations() -> set[str]:
    """Locations the external pipeline is working on right now.

    Two sources:
    1. pipeline_queue rows with status='running' — authoritative while the
       pipeline's queue-runner loop is between locations, but can go stale
       if the pipeline crashed mid-location (no 'completed' write ever came).
    2. batch_run.log — the runner prints '  RUNNING: <location>' when it picks
       up a location, so the newest marker is the live one even if queue rows
       are stale.
    """
    locs: set[str] = set()
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT location FROM pipeline_queue WHERE status = 'running'"
        )
        for row in cur.fetchall():
            if row[0] and str(row[0]).strip():
                locs.add(str(row[0]).strip())
        cur.close()
        conn.close()
    except Exception as exc:
        _log_message(f"WARN: running-locations queue query failed: {exc}")

    try:
        text = _read_recent_log_lines(PIPELINE_LOG, 0, limit=2000)
        marker_re = re.compile(r"^\s{2}RUNNING:\s*(.+)$", re.MULTILINE)
        markers = marker_re.findall(text)
        if markers:
            locs.add(markers[-1].strip())
    except Exception:
        pass
    return locs


def _norm_loc(s: str) -> str:
    """Normalize a location string for comparison (case/punct/spacing)."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_last_stale_reset_ts: float = 0.0


def _reset_stale_running_queue_items(min_interval_seconds: float = 0) -> int:
    """Re-queue pipeline_queue rows stuck at status='running' while no pipeline
    process is alive.

    The queue runner marks a row 'running' when it starts a location and
    'completed' when that location finishes. If the pipeline crashes or is
    killed mid-location, no 'completed' write ever happens and the row stays
    'running' FOREVER — and because the runner only picks rows with
    status='queued', such zombie rows would never be processed again.

    ONLY acts when no pipeline process is detected, so legitimately-running
    rows are never touched. min_interval_seconds throttles periodic calls
    (0 = always run, used at startup). Returns rows reset.
    """
    global _last_stale_reset_ts
    if min_interval_seconds > 0:
        now = time.time()
        if now - _last_stale_reset_ts < min_interval_seconds:
            return 0
        _last_stale_reset_ts = now
    if _pipeline_processes():
        return 0  # pipeline alive — its 'running' row is legitimate
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT queue_id, location FROM pipeline_queue WHERE status = 'running'"
        )
        rows = cur.fetchall()
        if not rows:
            cur.close()
            conn.close()
            return 0
        for qid, loc in rows:
            cur.execute(
                "UPDATE pipeline_queue SET status = 'queued' WHERE queue_id = %s",
                (qid,),
            )
            _log_message(
                f"ALERT: queue item {qid} ('{loc}') was stuck at 'running' "
                "with no pipeline process alive — reset to 'queued' so it can rerun."
            )
        conn.commit()
        cur.close()
        conn.close()
        return len(rows)
    except Exception as exc:
        _log_message(f"WARN: stale running-queue reset failed: {exc}")
        return 0


def _queue_active_count() -> int:
    """Number of pipeline_queue rows with status 'queued' or 'running'.

    Used by the watchdog to avoid relaunching the pipeline when there is no
    work left (all locations completed): without this, a finished run would
    be relaunched forever (start -> nothing queued -> exit -> relaunch).
    """
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM pipeline_queue "
            "WHERE status IN ('queued', 'running')"
        )
        (count,) = cur.fetchone()
        cur.close()
        conn.close()
        return int(count or 0)
    except Exception as exc:
        # If we can't ask the queue, assume there IS work (relaunch is the
        # safe default — the runner exits on its own if there isn't).
        _log_message(f"WARN: queue active-count query failed: {exc}")
        return 1


def _select_pending_companies(skip_locations: Optional[set[str]] = None) -> list[dict]:
    """Fetch every company with status='pending' from company_details, all locations.

    Rows are ordered by location so the log groups by location. The dict shape
    matches what pipeline.process_companies expects (company_name, website).

    skip_locations: location strings to EXCLUDE (normalized comparison) — used
    to keep the inline crawler away from the location the pipeline is working
    on right now, so they never process the same companies simultaneously.
    """
    companies: list[dict] = []
    skip_norm = {_norm_loc(s) for s in (skip_locations or set())}
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT company_name, website_url, location FROM company_details "
            "WHERE status = 'pending' AND website_url IS NOT NULL "
            "ORDER BY location, company_id"
        )
        for name, website, loc in cur.fetchall():
            if loc and _norm_loc(loc) in skip_norm:
                continue
            companies.append({
                "company_name": (name or website or "").strip(),
                "website": (website or "").strip(),
                "location": loc or "",
                "extra_columns": {},
            })
        cur.close()
        conn.close()
    except Exception as exc:
        _log_message(f"WARN: pending companies query failed: {exc}")
    return companies


def _run_crawler_only(control: "ControlEvent | None" = None) -> dict:
    """Run the Email Crawler (Phase 2 only) on every pending company, all locations.

    Runs inline in a background thread (started by the /crawler/pending POST
    handler). Each finished company is written straight back to PostgreSQL via
    upsert_company_result, so partial progress survives a monitor restart.

    `control` (optional) enables cooperative Pause/Resume/Stop from the web UI.
    """
    started = time.time()
    sys.path.insert(0, str(CRAWLER_DIR))
    sys.path.insert(0, str(CRAWLER_DIR.parent))
    # Note: crawler/config.py loads the project .env itself on import.

    try:
        from config import settings
        from pipeline import process_companies
        from storage.db import upsert_company_result
        from utils.control import ControlEvent
    except Exception as exc:
        _log_message(f"ERROR: crawler-only run could not import crawler modules: {exc}")
        return {"ok": False, "error": f"import failed: {exc}", "processed": 0}

    running_locs = _get_pipeline_running_locations()
    companies = _select_pending_companies(skip_locations=running_locs)
    if not companies:
        if running_locs:
            _log_message(
                "INFO: crawler-only run: no pending companies OUTSIDE the "
                f"pipeline's current location(s) {sorted(running_locs)} — nothing to do"
            )
        else:
            _log_message("INFO: crawler-only run: no pending companies found — nothing to do")
        return {
            "ok": True,
            "processed": 0,
            "total": 0,
            "skipped_running": sorted(running_locs),
        }

    skipped_note = (
        f" (skipping pipeline's current location(s): {', '.join(sorted(running_locs))})"
        if running_locs else ""
    )
    _log_message(
        f"INFO: crawler-only run starting: {len(companies)} pending companies "
        f"across all locations{skipped_note} (Phase 2 only)"
    )
    print("\n" + "=" * 60)
    print("  PHASE 2: EMAIL CRAWLER (monitor: pending-only, all locations)")
    print("=" * 60)
    print(f"  Pending companies to process: {len(companies)}")
    if running_locs:
        print(f"  Skipping (pipeline working on): {', '.join(sorted(running_locs))}")

    saved_count = [0]
    failed_saves: list = []

    def _realtime_save(result) -> None:
        try:
            upsert_company_result(result)
            saved_count[0] += 1
            if saved_count[0] <= 3 or saved_count[0] % 10 == 0:
                print(
                    f"\n  [DB LIVE #{saved_count[0]}] {result.company_name}: "
                    f"{result.emails_found} emails, {result.phones_found} phones, "
                    f"{result.social_links_found} social"
                )
        except Exception as e:
            failed_saves.append(result)
            print(f"\n  [DB SAVE ERROR] {result.company_name}: {e}")

    def _progress(current: int, total: int, message: str) -> None:
        print(f"\r  Progress: {current}/{total} — {message}", end="", flush=True)
        if current >= total:
            print()

    if control is not None:
        control.start()

    if control is None:
        control = ControlEvent()
        control.start()

    try:
        results = asyncio.run(
            process_companies(
                companies,
                concurrency=settings.max_concurrent_requests,
                progress_callback=_progress,
                result_callback=_realtime_save,
                control=control,
            )
        )
    except Exception as exc:
        _log_message(f"ERROR: crawler-only run crashed: {exc}")
        return {"ok": False, "error": str(exc), "processed": saved_count[0]}

    if control is not None and control.is_stop_requested():
        control.mark_stopped()
        return {
            "ok": True,
            "stopped": True,
            "processed": len(results),
            "failed": sum(1 for r in results if r.status.value == "failed"),
            "emails": sum(r.emails_found for r in results),
            "elapsed_seconds": round(time.time() - started, 1),
        }

    # Retry any transient DB failures so no company's data is lost (same as
    # run_pipeline_cli.run_email_crawler).
    retried = 0
    if failed_saves:
        print(f"\n  [RETRY] Re-saving {len(failed_saves)} companies that failed on first write...")
        for r in failed_saves:
            try:
                upsert_company_result(r)
                retried += 1
            except Exception as e:
                print(f"  [RETRY FAILED] {r.company_name}: {e}")

    elapsed = time.time() - started
    total_emails = sum(r.emails_found for r in results)
    total_phones = sum(r.phones_found for r in results)
    total_social = sum(r.social_links_found for r in results)
    companies_failed = sum(1 for r in results if r.status.value == "failed")

    print("\n" + "=" * 60)
    print("         CRAWLER-ONLY RUN COMPLETE (pending, all locations)")
    print("=" * 60)
    print(f"  Companies processed:     {len(results)}")
    print(f"  Companies failed:        {companies_failed}")
    print(f"  Total emails found:      {total_emails}")
    print(f"  Total phones found:      {total_phones}")
    print(f"  Total social links:      {total_social}")
    print(f"  Time elapsed:            {elapsed:.1f}s")
    print("=" * 60)

    _log_message(
        f"INFO: crawler-only run finished: processed={len(results)}, "
        f"failed={companies_failed}, emails={total_emails}, "
        f"elapsed={elapsed:.0f}s"
    )
    return {
        "ok": True,
        "processed": len(results),
        "failed": companies_failed,
        "emails": total_emails,
        "elapsed_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# MonitorState (used by both --poll and --web)
# ---------------------------------------------------------------------------


class MonitorState:
    def __init__(self) -> None:
        self.prev_pids: list[int] = []
        self.prev_log_size: int = 0
        self.prev_completed: dict[str, int] = {}
        self.prev_running_location: Optional[str] = None
        self.consecutive_stuck: int = 0
        self.consecutive_idle: int = 0
        self.notified_crash: bool = False
        self.notified_stuck: bool = False
        self.notified_finish: bool = False
        self.last_stage_desc: str = ""
        self.last_reported_location: Optional[str] = None
        # Watchdog auto-restart bookkeeping (wall-clock based; poll paths may
        # run at any frequency — server thread AND main loop share this state)
        self.last_silent_since_ts: Optional[float] = None
        self.dead_since_ts: Optional[float] = None
        self.last_watchdog_restart_ts: float = 0.0
        self.watchdog_restarts: int = 0
        self.watchdog_last_action: str = "armed — will restart pipeline on hang/crash"


def poll_once(state: MonitorState, monitor_log: Optional[Path]) -> dict:
    # Self-healing: while we're polling anyway, re-queue any location stuck at
    # 'running' if no pipeline process is alive (throttled to once a minute).
    try:
        _reset_stale_running_queue_items(min_interval_seconds=60)
    except Exception:
        pass

    now = _now_str()
    report: dict[str, Any] = {
        "time": now,
        "pids_alive": False,
        "pids": [],
        "stage": "unknown",
        "location": "",
        "details": "",
        "event": "ok",
        "event_message": "",
    }

    pids = _find_pipeline_pids()
    alive_set = _get_alive_pids_batch()
    pids_alive = all(p in alive_set for p in pids) if pids else False
    report["pids"] = list(pids)
    report["pids_alive"] = pids_alive

    log_size = _log_size(PIPELINE_LOG)
    recent_log = _read_recent_log_lines(PIPELINE_LOG, state.prev_log_size)
    log_grew = log_size > state.prev_log_size
    log_growth_bytes = max(0, log_size - state.prev_log_size)

    stage_info = _detect_stage_from_log(recent_log)
    running_location = _detect_current_location_from_queue()
    db_info = _detect_stage_from_db(running_location, _detect_all_locations())

    if not stage_info["location"] and db_info["current_location"]:
        stage_info["location"] = db_info["current_location"]

    stage_desc_parts: list[str] = []
    if stage_info["phase"] != "unknown":
        stage_desc_parts.append(f"PHASE: {stage_info['phase'].upper()}")
    if stage_info["location"]:
        stage_desc_parts.append(f"LOCATION: {stage_info['location']}")
    if stage_info["progress"]:
        stage_desc_parts.append(f"PROGRESS: {stage_info['progress']}")
    if db_info["current_location"]:
        total_clause = (
            f" (of {db_info['total']} total)" if db_info["total"] else ""
        )
        stage_desc_parts.append(
            f"DB: {db_info['completed']} completed, "
            f"{db_info['pending']} pending, "
            f"{db_info['failed']} failed{total_clause}"
        )
    if db_info["next_locations"]:
        nxt = db_info["next_locations"][:3]
        tail = " ..." if len(db_info["next_locations"]) > 3 else ""
        stage_desc_parts.append(f"NEXT UP: {', '.join(nxt)}{tail}")
    stage_desc = " | ".join(stage_desc_parts) if stage_desc_parts else "Stage unknown"

    report["stage"] = stage_info["phase"]
    report["location"] = stage_info["location"] or db_info["current_location"] or ""
    report["details"] = stage_desc

    # Always include a fresh log tail in the report so the web UI shows recent lines.
    report["log_tail"] = _read_recent_log_lines(
        PIPELINE_LOG, state.prev_log_size, limit=120
    )

    # crash
    if state.prev_pids and not pids and not state.notified_crash:
        report["event"] = "crash"
        report["event_message"] = (
            "PIPELINE CRASHED/DIED\n\n"
            "The pipeline process is no longer running.\n"
            f"Previous PIDs: {state.prev_pids}\n"
            f"Location being processed: {state.prev_running_location or 'unknown'}\n\n"
            "Check batch_run.log for the last entries before it stopped.\n"
            "Restart with: python crawler/run_pipeline_cli.py "
            "--locations locations.txt --min-results 500"
        )
        # popup notifications disabled per user request
        # _pop_notification("FINDME Pipeline — CRASHED", report["event_message"])
        _log_message("ALERT: pipeline crashed/died")
        state.notified_crash = True
        state.consecutive_idle = 0
        state.consecutive_stuck = 0
        return report

    if pids and state.notified_crash:
        _log_message("INFO: pipeline process reappeared after crash alert")
        state.notified_crash = False

    # progress / stuck / hung
    progress_made = False
    if pids_alive:
        current_completed = db_info["completed"]
        prev_completed = state.prev_completed.get(stage_info["location"], -1)
        progress_made = (
            (log_grew and log_growth_bytes >= STUCK_LOG_BYTES_MIN)
            or (current_completed > prev_completed and prev_completed >= 0)
        )
        if progress_made:
            state.consecutive_stuck = 0
            state.consecutive_idle = 0
            state.last_silent_since_ts = None  # wall-clock silence window reset
        else:
            state.consecutive_idle += 1
            state.consecutive_stuck += 1
            if state.last_silent_since_ts is None:
                state.last_silent_since_ts = time.time()  # silence window start

        if (
            state.consecutive_stuck >= STUCK_CONSECUTIVE_THRESHOLD
            and not state.notified_stuck
        ):
            report["event"] = "stuck"
            report["event_message"] = (
                "PIPELINE MAY BE STUCK\n\n"
                f"The pipeline process is alive but hasn't made visible progress "
                f"for {state.consecutive_stuck * POLL_INTERVAL_SECONDS // 60} minutes.\n\n"
                f"Current location: {stage_info['location'] or 'unknown'}\n"
                f"Stage: {stage_info['phase']}\n"
                f"DB completed: {db_info['completed']}\n"
                f"DB pending: {db_info['pending']}\n\n"
                "This can happen during slow email validation or network waits. "
                "If it persists, check batch_run.log for errors."
            )
            # popup notifications disabled per user request
            # _pop_notification(
            #     "FINDME Pipeline — MAY BE STUCK", report["event_message"]
            # )
            _log_message("ALERT: pipeline may be stuck")
            state.notified_stuck = True
            return report

        if (
            state.consecutive_idle >= CRASH_CONSECUTIVE_IDLE_THRESHOLD
            and log_growth_bytes == 0
            and (current_completed <= prev_completed or prev_completed < 0)
            and not state.notified_stuck
        ):
            report["event"] = "stuck"
            report["event_message"] = (
                "PIPELINE APPEARS HUNG\n\n"
                "The pipeline process is alive but has produced NO log output "
                "and NO DB progress for "
                f"{state.consecutive_idle * POLL_INTERVAL_SECONDS // 60} minutes.\n\n"
                f"Current location: {stage_info['location'] or 'unknown'}\n"
                f"Stage: {stage_info['phase']}\n\n"
                "This likely means the process is blocked or frozen. "
                "Check batch_run.log and consider restarting."
            )
            # popup notifications disabled per user request
            # _pop_notification(
            #     "FINDME Pipeline — APPEARS HUNG", report["event_message"]
            # )
            _log_message("ALERT: pipeline appears hung")
            state.notified_stuck = True
            return report
    else:
        if not state.notified_crash:
            report["event"] = "crash"
            report["event_message"] = (
                "PIPELINE NOT RUNNING\n\n"
                "No pipeline process found. It may have crashed or been stopped.\n"
                f"Last known location: {state.prev_running_location or 'unknown'}\n\n"
                "Restart with: python crawler/run_pipeline_cli.py "
                "--locations locations.txt --min-results 500"
            )
            # popup notifications disabled per user request
            # _pop_notification(
            #     "FINDME Pipeline — NOT RUNNING", report["event_message"]
            # )
            _log_message(
                "ALERT: pipeline not running (no crash transition caught)"
            )
            state.notified_crash = True
            return report

    # finish
    all_locs = _detect_all_locations()
    if all_locs:
        completed_counts = _count_completions_for_locations(all_locs)
        try:
            sys.path.insert(0, str(CRAWLER_DIR))
            from storage.db import get_queue_items

            active_queue = get_queue_items()
            active_locs = {
                item.get("location", "").strip()
                for item in active_queue
                if item.get("status") in ("queued", "running")
            }
        except Exception:
            active_locs = set()
        done_locs = [
            loc for loc in all_locs
            if completed_counts.get(loc, 0) > 0 and loc not in active_locs
        ]
        all_done = len(done_locs) == len(all_locs) and not active_locs
        if all_done and not state.notified_finish:
            report["event"] = "finish"
            msg = (
                "PIPELINE FINISHED SUCCESSFULLY\n\n"
                f"All {len(all_locs)} locations have been processed.\n\n"
            )
            for loc in all_locs:
                c = _count_completions_for_location(loc)
                msg += f"  - {loc}: {completed_counts.get(loc, 0)} companies\n"
            try:
                sys.path.insert(0, str(CRAWLER_DIR))
                from storage.db import _get_connection

                conn = _get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT status, COUNT(*) FROM company_details "
                    "WHERE status IN ('completed', 'failed', 'no_contacts_found') "
                    "AND location IN %s GROUP BY status",
                    (tuple(all_locs),),
                )
                total_comp = total_fail = total_no_contact = 0
                for status, count in cur.fetchall():
                    if status == "completed":
                        total_comp = count
                    elif status == "failed":
                        total_fail = count
                    elif status == "no_contacts_found":
                        total_no_contact = count
                cur.close()
                conn.close()
            except Exception:
                total_comp = total_fail = total_no_contact = 0
            msg += (
                f"\nTotal completed: {total_comp}\n"
                f"Total failed: {total_fail}\n"
                f"Total no contacts: {total_no_contact}\n\n"
                "Log: batch_run.log\n"
                "Results are in the PostgreSQL database (findme)."
            )
            report["event_message"] = msg
            # popup notifications disabled per user request
            # _pop_notification(
            #     "FINDME Pipeline — FINISHED", report["event_message"]
            # )
            _log_message("ALERT: pipeline finished successfully")
            state.notified_finish = True
            return report

    # Always refresh the log tail on every poll so / and /poll show fresh content.
    report["log_tail"] = _read_recent_log_lines(
        PIPELINE_LOG, state.prev_log_size, limit=120
    )

    # stage change / heartbeat
    if stage_desc != state.last_stage_desc:
        report["event"] = "stage_change"
        report["event_message"] = stage_desc
        _log_message(f"INFO: stage update — {stage_desc}")
        state.last_stage_desc = stage_desc
        if stage_info["location"] and stage_info["location"] != state.last_reported_location:
            state.last_reported_location = stage_info["location"]
            # popup notifications disabled per user request
            # _pop_notification(
            #     "FINDME Pipeline — still running",
            #     f"{stage_desc}\n\nMonitoring continues every 5 minutes.",
            # )

    if progress_made and state.notified_stuck:
        _log_message("INFO: progress resumed after stuck alert")
        state.notified_stuck = False
        state.consecutive_stuck = 0

    state.prev_pids = list(pids)
    state.prev_log_size = log_size
    state.prev_running_location = (
        stage_info["location"] or db_info["current_location"]
    )
    if db_info["current_location"]:
        state.prev_completed[db_info["current_location"]] = db_info["completed"]

    return report


def poll_once_with_watchdog(
    state: MonitorState, monitor_log: Optional[Path]
) -> dict:
    """poll_once + watchdog auto-restart evaluation.

    ALL poll paths use this (main loops and the HTTP server's refresh), so the
    watchdog is evaluated on every observation regardless of which thread
    polls — poll_once only latches crash/stuck ALERTS once per episode, but
    the watchdog decides from wall-clock silence/death windows instead.
    """
    report = poll_once(state, monitor_log)
    try:
        _watchdog_auto_restart(report, state)
    except Exception as exc:
        _log_message(f"WARN: watchdog auto-restart error: {exc}")
    report["watchdog"] = state.watchdog_last_action
    return report


def _pop_notification(title: str, message: str) -> None:
    """Show a Windows MessageBox popup.

    Disabled per user request — this is now a no-op. All alerts go to the
    console log and the web UI only.
    """
    pass


# ---------------------------------------------------------------------------
# Plain-HTML status page
# ---------------------------------------------------------------------------

_STATUS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<script>var TS = {ts};</script>
<title>Pipeline Monitor — {title}</title>
<style>
  :root {{
    --bg: #F8FAFC;
    --surface: #FFFFFF;
    --surface-secondary: #F1F5F9;
    --border: #E2E8F0;
    --text: #0F172A;
    --text-secondary: #475569;
    --text-muted: #64748B;
    --primary: #2563EB;
    --primary-hover: #1D4ED8;
    --success: #16A34A;
    --warning: #D97706;
    --danger: #DC2626;
    --neutral: #64748B;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    padding: 24px 24px 40px;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--primary); text-decoration: none; transition: color 0.15s ease; }}
  a:hover {{ color: var(--primary-hover); text-decoration: underline; }}
  code {{
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    background: var(--surface-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    color: var(--text-secondary);
  }}
  .container {{
    max-width: 1120px;
    margin: 0 auto;
  }}
  .page-header {{
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: 10px 16px;
    margin-bottom: 20px;
  }}
  .page-header h1 {{
    font-size: 25px;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin: 0 0 2px 0;
    color: var(--text);
  }}
  .page-header .tagline {{
    font-size: 13px;
    color: var(--text-muted);
    margin: 0;
  }}
  .header-links {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px 8px;
    font-size: 12.5px;
    font-weight: 500;
  }}
  .header-links .sep {{ color: var(--border); }}
  .header-links a {{
    padding: 5px 10px;
    border-radius: 8px;
    transition: background-color 0.15s ease, color 0.15s ease;
  }}
  .header-links a:hover {{
    background: var(--surface-secondary);
    text-decoration: none;
  }}
  .refresh-note {{
    width: 100%;
    font-size: 12px;
    color: var(--text-muted);
    margin: 0;
  }}
  .sub {{
    font-size: 13px;
    color: var(--text-muted);
    margin: -12px 0 20px 0;
    max-width: 860px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }}
  .card h2 {{
    font-size: 12px;
    font-weight: 600;
    margin: 0 0 12px 0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
  }}
  th, td {{
    text-align: left;
    padding: 7px 10px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }}
  th {{
    color: var(--text-muted);
    font-weight: 500;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.05em;
  }}
  tr:last-child td, tr:last-child th {{ border-bottom: none; }}
  .kv th, .kv td {{ padding: 7px 0; }}
  .kv th {{
    width: 110px;
    color: var(--text-muted);
    font-weight: 500;
    vertical-align: top;
  }}
  .kv td {{ color: var(--text); font-size: 13.5px; }}
  .status-alive {{
    color: var(--success);
    font-weight: 600;
  }}
  .status-dead {{
    color: var(--danger);
    font-weight: 600;
  }}
  .status-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 7px;
    vertical-align: middle;
    background: var(--neutral);
  }}
  .dot-success {{ background: var(--success); }}
  .dot-danger {{ background: var(--danger); }}
  .dot-warning {{ background: var(--warning); }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
  }}
  .badge-running {{ background: #d4edda; color: #155724; }}
  .badge-queued {{ background: #fff3cd; color: #856404; }}
  .badge-completed {{ background: #d1ecf1; color: #0c5460; }}
  .badge-unknown {{ background: #eeeeee; color: #555555; }}
  .log {{
    background: var(--surface-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 320px;
    overflow-y: auto;
  }}
  .log .empty {{
    color: var(--text-muted);
    font-style: italic;
  }}
  .footer {{
    font-size: 11.5px;
    color: var(--text-muted);
    margin-top: 22px;
    border-top: 1px solid var(--border);
    padding-top: 12px;
  }}  .event-banner {{
    border: 1px solid #FECACA;
    border-left: 4px solid var(--danger);
    background: #FEF2F2;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 14px;
    font-size: 13px;
    color: var(--text-secondary);
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }}
  .event-banner strong {{ color: var(--danger); }}
  .event-banner.ok {{
    border-color: #BBF7D0;
    border-left-color: var(--success);
    background: #F0FDF4;
  }}
  .event-banner.ok strong {{ color: var(--success); }}
  .event-banner.pause {{
    border-color: #FDE68A;
    border-left-color: var(--warning);
    background: #FFFBEB;
  }}
  .event-banner.pause strong {{ color: var(--warning); }}
  .event-banner .banner-head {{
    display: flex;
    align-items: center;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 2px;
  }}
  .event-banner .banner-sub {{
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 2px;
  }}
  .event-banner .banner-time {{
    font-size: 11.5px;
    color: var(--text-muted);
  }}
  .btn {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 500;
    font-family: inherit;
    line-height: 1.2;
    border-radius: 8px;
    border: 1px solid transparent;
    cursor: pointer;
    margin-right: 8px;
    transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease, opacity 0.15s ease;
  }}
  .btn:disabled {{
    background: var(--surface-secondary);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
    opacity: 0.7;
  }}
  .btn-primary {{
    background: var(--primary);
    border-color: var(--primary);
    color: #ffffff;
  }}
  .btn-primary:hover:not(:disabled) {{ background: var(--primary-hover); border-color: var(--primary-hover); }}
  .btn-primary:active:not(:disabled) {{ background: #1E40AF; border-color: #1E40AF; }}
  .btn-warn {{
    background: var(--warning);
    border-color: var(--warning);
    color: #ffffff;
  }}
  .btn-warn:hover:not(:disabled) {{ background: #B45309; border-color: #B45309; }}
  .btn-warn:active:not(:disabled) {{ background: #92400E; border-color: #92400E; }}
  .btn-outline {{
    background: var(--surface);
    border-color: var(--border);
    color: var(--primary);
  }}
  .btn-outline:hover:not(:disabled) {{ background: #EFF6FF; border-color: #BFDBFE; }}
  .btn-outline:active:not(:disabled) {{ background: #DBEAFE; }}
  .btn-danger {{
    background: var(--surface);
    border-color: #FECACA;
    color: var(--danger);
  }}
  .btn-danger:hover:not(:disabled) {{ background: #FEF2F2; border-color: #FCA5A5; }}
  .btn-danger:active:not(:disabled) {{ background: #FEE2E2; }}
  .btn-sm {{
    padding: 4px 10px;
    font-size: 12px;
    border-radius: 6px;
  }}
  .controls-row {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 4px;
  }}
  .controls-form button {{
    margin-top: 0;
  }}
  .controls-form .result {{
    width: 100%;
    margin-top: 10px;
    font-size: 13px;
  }}
  .controls-form .result.ok {{ color: var(--success); }}
  .controls-form .result.bad {{ color: var(--danger); }}
  .controls-form .hint {{
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 12px;
    border-top: 1px solid var(--border);
    padding-top: 10px;
    max-width: 860px;
  }}
  td.actions {{ text-align: center; }}
</style>
<style>
  /* ---- Company Details by Location table (rich) ---- */
  .loc-card {{ position: relative; }}
  .table-scroll {{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .loc-table {{ min-width: 900px; }}
  .loc-table th, .loc-table td {{ padding: 9px 10px; }}
  .loc-table th {{
    text-align: right;
    border-bottom: 1px solid var(--border);
    background: var(--surface-secondary);
  }}
  .loc-table th:first-child {{ text-align: left; }}
  .loc-table .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .loc-table .loc-cell {{ font-weight: 600; color: var(--text); }}
  .loc-table tbody tr:hover td, .loc-table tr:hover td {{ background: #F8FAFC; }}
  .loc-table .totals-row td {{
    border-top: 2px solid var(--border);
    border-bottom: none;
    background: var(--surface-secondary);
    font-weight: 600;
  }}
  .loc-table .totals-row .count {{ font-weight: 600; }}
  .count {{ display: inline-block; min-width: 2.2em; }}
  .completed-count {{ color: var(--success); font-weight: 600; }}
  .pending-count {{ color: var(--warning); }}
  .failed-count {{ color: var(--danger); }}
  .muted-count {{ color: var(--neutral); }}
  .total-count {{ color: var(--primary); font-weight: 600; }}
  .mini-bar {{
    display: block;
    height: 4px;
    width: 64px;
    margin-left: auto;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
  }}
  .mini-fill {{ display: block; height: 100%; background: var(--success); border-radius: 2px; }}
  .last-cell {{ color: var(--text-muted); font-size: 12px; white-space: nowrap; }}
  .loc-legend {{
    display: flex;
    gap: 14px;
    align-items: center;
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 10px;
    flex-wrap: wrap;
  }}
  .loc-legend .dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 4px;
    vertical-align: middle;
  }}
  .dot-completed {{ background: var(--success); }}
  .dot-pending {{ background: var(--warning); }}
  .dot-failed {{ background: var(--danger); }}
  .dot-muted {{ background: var(--neutral); }}
  .legend-hint {{ color: var(--text-muted); font-style: italic; }}
  /* ---- Hover preview popup ---- */
  .hover-popup {{
    display: none;
    position: absolute;
    z-index: 100;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.14);
    padding: 12px 14px;
    max-width: 640px;
    max-height: 420px;
    overflow: auto;
    font-size: 12px;
  }}
  .hover-popup h3 {{
    margin: 0 0 8px 0;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border);
    padding-bottom: 6px;
  }}
  .hover-popup table {{ font-size: 12px; }}
  .hover-popup th, .hover-popup td {{
    padding: 4px 8px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .hover-popup th {{ color: var(--text-muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .popup-status {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }}
  .popup-status.completed {{ background: #DCFCE7; color: #15803D; }}
  .popup-status.pending {{ background: #FEF3C7; color: #B45309; }}
  .popup-status.failed {{ background: #FEE2E2; color: #B91C1C; }}
  .popup-status.no_contacts_found {{ background: var(--surface-secondary); color: var(--text-secondary); }}
  .popup-empty {{ color: var(--text-muted); font-style: italic; padding: 6px 0; }}
</style>
<style>
  .queue-form {{
    margin-top: 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    background: var(--surface);
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }}
  .queue-form h2 {{
    font-size: 12px;
    font-weight: 600;
    margin: 0 0 10px 0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
  }}
  .queue-form label {{
    font-size: 12px;
    color: var(--text-secondary);
    display: block;
    margin-bottom: 6px;
  }}
  .queue-form textarea {{
    width: 100%;
    min-height: 90px;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 13px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    resize: vertical;
    box-sizing: border-box;
    background: var(--surface);
    color: var(--text);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }}
  .queue-form textarea:focus {{
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }}
  .queue-form button {{
    margin-top: 10px;
    padding: 8px 16px;
    background: var(--primary);
    color: #ffffff;
    border: 1px solid var(--primary);
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background-color 0.15s ease;
  }}
  .queue-form button:hover {{ background: var(--primary-hover); border-color: var(--primary-hover); }}
  .queue-form button:disabled {{
    background: var(--surface-secondary);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
    opacity: 0.7;
  }}
  .queue-form .hint {{
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
  }}
  .queue-form .result {{
    margin-top: 10px;
    font-size: 13px;
  }}
  .queue-form .result.ok {{ color: var(--success); }}
  .queue-form .result.bad {{ color: var(--danger); }}
  .queue-table td.first {{
    color: var(--text-muted);
    text-align: center;
  }}
  .crawler-form {{
    margin-top: 0;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    background: var(--surface);
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }}
  .crawler-form h2 {{
    font-size: 12px;
    font-weight: 600;
    margin: 0 0 10px 0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
  }}
  .crawler-form .hint {{
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 10px;
    border-top: 1px solid var(--border);
    padding-top: 10px;
    max-width: 860px;
  }}
  .crawler-form button {{
    margin-top: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 16px;
    background: var(--primary);
    color: #ffffff;
    border: 1px solid var(--primary);
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background-color 0.15s ease;
  }}
  .crawler-form button:hover {{ background: var(--primary-hover); border-color: var(--primary-hover); }}
  .crawler-form button:disabled {{
    background: var(--surface-secondary);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
    opacity: 0.7;
  }}
  .crawler-form .result {{
    margin-top: 10px;
    font-size: 13px;
  }}
  .crawler-form .result.ok {{ color: var(--success); }}
  .crawler-form .result.bad {{ color: var(--danger); }}
  .crawler-form .result.busy {{ color: var(--warning); }}
</style>
</head>
<body>
<div class="container">

<div class="page-header">
  <div>
    <h1>Pipeline Monitor</h1>
    <p class="tagline">Real-time pipeline operations</p>
  </div>
  <div class="header-links">
    <a href="/poll">/poll (JSON)</a><span class="sep">|</span><a href="/log">/log (tail)</a><span class="sep">|</span><a href="/queue">/queue (manage locations)</a>
  </div>
  <p class="refresh-note">Watching <code>batch_run.log</code> &bull; auto-refreshes every {refresh} seconds</p>
</div>

<div class="card controls-form" id="controls-form">
  <h2>Pipeline Controls</h2>
  <form id="pipeline-controls-form">
    <div class="controls-row">
    <button type="button" class="btn btn-primary" id="start_btn" onclick="pipelineStart()">Start pipeline</button>
    <button type="button" class="btn btn-warn" id="pause_btn" onclick="pipelinePause()">Pause</button>
    <button type="button" class="btn btn-outline" id="resume_btn" onclick="pipelineResume()">Resume</button>
    <div class="result" id="controls_result" style="display:none;"></div>
    </div>
    <div class="hint">
      <strong>Start</strong> launches the pipeline (all queued locations, sequential runs) as a detached background process writing to <code>batch_run.log</code>.
      <strong>Pause</strong> holds the monitor-run crawler and blocks new starts &mdash; it cannot hard-freeze an external pipeline process; it resumes cleanly afterwards.
      <strong>Resume</strong> lifts the pause. Deleting a queued location is done per-row in the queue table below or on the <a href="/queue">/queue</a> page.
    </div>
  </form>
</div>

{text_block}

<div class="card crawler-form" id="crawler-form">
  <h2>Crawler-only run</h2>
  <form id="crawler-pending-form">
    <button type="submit" id="crawler_pending_btn">Crawl pending now &mdash; all locations</button>
    <div class="hint">Runs the Email Crawler (Phase 2 only, no URL Finder) on every company with status = &lsquo;pending&rsquo; across <strong>all locations</strong> &mdash; automatically <strong>skipping the location the pipeline is currently processing</strong> so they never overlap. Writes results to PostgreSQL as each one finishes. Output is appended to <code>batch_run.log</code>, so the monitor keeps tracking it.</div>
    <div class="result" id="crawler_result" style="display:none;"></div>
  </form>
  <script>
    // ---- Pipeline controls (Start / Pause / Resume) ----
    function _controlsResult(msg, cls) {{
      var el = document.getElementById('controls_result');
      el.className = 'result ' + (cls || '');
      el.style.display = 'block';
      el.textContent = msg;
    }}
    function _postJson(url, body, cb) {{
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      if (body !== null && body !== undefined) {{
        xhr.setRequestHeader('Content-Type', 'application/json');
      }}
      xhr.onreadystatechange = function() {{
        if (xhr.readyState === 4) {{
          var data = {{}};
          try {{ data = JSON.parse(xhr.responseText); }} catch (err) {{}}
          cb(xhr.status, data);
        }}
      }};
      xhr.send(body === null || body === undefined ? '' : body);
    }}
    function pipelineStart() {{
      var btn = document.getElementById('start_btn');
      btn.disabled = true; btn.textContent = 'Starting...';
      _postJson('/pipeline/start', JSON.stringify({{}}), function(status, data) {{
        btn.disabled = false; btn.textContent = 'Start pipeline';
        _controlsResult(
          (data && data.message) ? data.message : (data && data.error) || 'done',
          status < 400 ? 'ok' : 'bad'
        );
      }});
    }}
    function pipelinePause() {{
      _postJson('/pipeline/pause', JSON.stringify({{}}), function(status, data) {{
        _controlsResult(
          (data && data.message) || 'Paused.',
          status < 400 ? 'ok' : 'bad'
        );
        refreshControlsState();
      }});
    }}
    function pipelineResume() {{
      _postJson('/pipeline/resume', JSON.stringify({{}}), function(status, data) {{
        _controlsResult(
          (data && data.message) || 'Resumed.',
          status < 400 ? 'ok' : 'bad'
        );
        refreshControlsState();
      }});
    }}
    function refreshControlsState() {{
      var p = new XMLHttpRequest();
      p.open('GET', '/poll', true);
      p.onreadystatechange = function() {{
        if (p.readyState === 4 && p.status === 200) {{
          var d = JSON.parse(p.responseText);
          var paused = !!(d.pipeline_controls && d.pipeline_controls.paused);
          var running = !!(d.pipeline_controls && d.pipeline_controls.pipeline_running);
          document.getElementById('pause_btn').disabled = paused;
          document.getElementById('resume_btn').disabled = !paused;
          document.getElementById('start_btn').disabled = paused || running;
        }}
      }};
      p.send();
    }}
    refreshControlsState();

    // Belt-and-braces: force a reload even if the browser ignores the
    // meta refresh tag above.
    setTimeout(function() {{ window.location.reload(); }}, {refresh} * 1000);

    // ---- Hover preview popups for the Company Details table ----
    // Hovering the "Completed" cell fetches the top 10 contact rows for that
    // location; hovering "Total" fetches the top 10 company rows. Results are
    // cached per (kind, location) until the next full page reload.
    var _previewCache = {{}};
    var _popup = document.getElementById('hover-popup');
    var _popupHideTimer = null;
    function _escHtml(s) {{
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }}
    function _statusBadge(st) {{
      var s = String(st || 'pending');
      var label = s === 'no_contacts_found' ? 'no contacts' : s;
      return '<span class="popup-status ' + _escHtml(s) + '">' + _escHtml(label) + '</span>';
    }}
    function _contactsTable(rows) {{
      if (!rows || !rows.length) return '<div class="popup-empty">No contact rows found.</div>';
      var h = '<table><tr><th>Company</th><th>Contact</th><th>Email</th><th>Phone</th>';
      h += '<th>Found</th><th>Status</th></tr>';
      for (var i = 0; i < rows.length; i++) {{
        var r = rows[i];
        var found = (r.emails_found || 0) + 'e / ' + (r.phones_found || 0) + 'p';
        var email = r.email1 || r.email2 || '';
        h += '<tr>'
          + '<td title="' + _escHtml(r.website_url) + '">' + _escHtml(r.company_name) + '</td>'
          + '<td>' + _escHtml(r.contact_name || '&mdash;') + '</td>'
          + '<td>' + _escHtml(email || '&mdash;') + '</td>'
          + '<td>' + _escHtml(r.phone1 || '&mdash;') + '</td>'
          + '<td>' + _escHtml(found) + '</td>'
          + '<td>' + _statusBadge(r.status) + '</td>'
          + '</tr>';
      }}
      return h + '</table>';
    }}
    function _companiesTable(rows) {{
      if (!rows || !rows.length) return '<div class="popup-empty">No companies found.</div>';
      var h = '<table><tr><th>Company</th><th>Website</th><th>Status</th><th>Pages</th><th>Completed</th></tr>';
      for (var i = 0; i < rows.length; i++) {{
        var r = rows[i];
        h += '<tr>'
          + '<td>' + _escHtml(r.company_name) + '</td>'
          + '<td title="' + _escHtml(r.website_url) + '">' + _escHtml(r.website_url || '&mdash;') + '</td>'
          + '<td>' + _statusBadge(r.status) + '</td>'
          + '<td>' + _escHtml(r.pages_crawled == null ? 0 : r.pages_crawled) + '</td>'
          + '<td>' + _escHtml(r.completed_at || '&mdash;') + '</td>'
          + '</tr>';
      }}
      return h + '</table>';
    }}
    function _renderPreview(kind, loc, data) {{
      var title = (kind === 'contacts' ? 'Top 10 contacts &mdash; ' : 'Top 10 companies &mdash; ') + _escHtml(loc);
      var body = kind === 'contacts' ? _contactsTable(data.rows) : _companiesTable(data.rows);
      _popup.innerHTML = '<h3>' + title + '</h3>' + body;
    }}
    function _fetchPreview(kind, loc) {{
      var key = kind + '|' + loc;
      var cached = _previewCache[key];
      if (cached) {{ _renderPreview(kind, loc, cached); return; }}
      _popup.innerHTML = '<h3>Loading&hellip;</h3>';
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/preview/' + kind + '?location=' + encodeURIComponent(loc), true);
      xhr.onreadystatechange = function() {{
        if (xhr.readyState === 4) {{
          var data = {{ rows: [] }};
          try {{ data = JSON.parse(xhr.responseText); }} catch (err) {{}}
          if (xhr.status < 400 && data && data.ok) {{
            _previewCache[key] = data;
            if (_popup.style.display === 'block') _renderPreview(kind, loc, data);
          }} else {{
            _popup.innerHTML = '<h3>Error</h3><div class="popup-empty">'
              + _escHtml((data && data.error) || ('HTTP ' + xhr.status)) + '</div>';
          }}
        }}
      }};
      xhr.send();
    }}
    window.showPreview = function(ev, kind, loc) {{
      if (!_popup) return;
      if (_popupHideTimer) {{ clearTimeout(_popupHideTimer); _popupHideTimer = null; }}
      // Position near the cursor, clamped to the viewport.
      var x = ev.clientX + 14, y = ev.clientY + 12;
      var maxW = 640, maxH = 420;
      if (x + maxW > window.innerWidth - 8) x = window.innerWidth - maxW - 8;
      if (y + 200 > window.innerHeight - 8) y = Math.max(8, window.innerHeight - 260);
      _popup.style.left = x + 'px';
      _popup.style.top = y + 'px';
      _popup.style.display = 'block';
      _fetchPreview(kind, loc);
    }};
    window.hidePreview = function() {{
      if (_popupHideTimer) clearTimeout(_popupHideTimer);
      _popupHideTimer = setTimeout(function() {{
        if (_popup) _popup.style.display = 'none';
      }}, 250);
    }};
    // Keep the popup open while the pointer is over it.
    if (_popup) {{
      _popup.addEventListener('mouseenter', function() {{
        if (_popupHideTimer) {{ clearTimeout(_popupHideTimer); _popupHideTimer = null; }}
      }});
      _popup.addEventListener('mouseleave', hidePreview);
    }}

    // ---- Delete a queued location (status page queue table) ----
    function deleteQueueItem(qid, btn) {{
      if (!confirm('Delete queue item #' + qid + ' from the queue?')) return;
      btn.disabled = true;
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/queue/delete', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.onreadystatechange = function() {{
        if (xhr.readyState === 4) {{
          var data = {{}};
          try {{ data = JSON.parse(xhr.responseText); }} catch (err) {{}}
          if (xhr.status < 400 && data.ok) {{
            var row = btn.closest ? btn.closest('tr') : null;
            if (row) row.parentNode.removeChild(row);
            _controlsResult('Deleted queue item #' + qid + '.', 'ok');
          }} else {{
            btn.disabled = false;
            _controlsResult('Delete failed: ' + (data.error || ('HTTP ' + xhr.status)), 'bad');
          }}
        }}
      }};
      xhr.send(JSON.stringify({{queue_id: qid}}));
    }}
  </script>

  <script>
    (function() {{
      var form = document.getElementById('crawler-pending-form');
      var btn = document.getElementById('crawler_pending_btn');
      var result = document.getElementById('crawler_result');
      var pollTimer = null;
      function setBusyUI() {{
        btn.disabled = true;
        btn.textContent = 'Crawling pending companies — running...';
        result.className = 'result busy';
        result.style.display = 'block';
        result.textContent = 'Crawler-only run in progress... this page polls every 10s; watch the Recent Log below for live progress.';
      }}
      function setDoneUI(msg, ok) {{
        if (pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
        btn.disabled = false;
        btn.textContent = 'Crawl pending now — all locations';
        result.className = 'result ' + (ok ? 'ok' : 'bad');
        result.style.display = 'block';
        result.textContent = msg;
      }}
      form.addEventListener('submit', function(e) {{
        e.preventDefault();
        btn.disabled = true;
        btn.textContent = 'Starting...';
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/crawler/pending', true);
        xhr.onreadystatechange = function() {{
          if (xhr.readyState === 4) {{
            var data = {{}};
            try {{ data = JSON.parse(xhr.responseText); }} catch (err) {{}}
            if (xhr.status >= 400 || !data.ok) {{
              setDoneUI('Could not start: ' + (data.error || 'unknown error'), false);
              return;
            }}
            if (data.running) {{
              setBusyUI();
              pollTimer = setInterval(function() {{
                var p = new XMLHttpRequest();
                p.open('GET', '/poll', true);
                p.onreadystatechange = function() {{
                  if (p.readyState === 4 && p.status === 200) {{
                    var d = JSON.parse(p.responseText);
                    if (!d.crawler_only || !d.crawler_only.running) {{
                      setDoneUI(
                        'Crawler-only run finished. ' + (d.crawler_only.summary || ''),
                        true
                      );
                    }}
                  }}
                }};
                p.send();
              }}, 10000);
            }} else {{
              setDoneUI('Nothing to do: ' + (data.message || 'no pending companies.'), true);
            }}
          }}
          }};
        xhr.send('');
      }});
    }})();
  </script>
</div>

<div class="footer">
  Pipeline Monitor &bull; local status page &bull; {now_str}
</div>

</div>
</body>
</html>
"""

_QUEUE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>Pipeline Queue — FINDME Monitor</title>
<style>
  :root {
    --bg: #F8FAFC;
    --surface: #FFFFFF;
    --surface-secondary: #F1F5F9;
    --border: #E2E8F0;
    --text: #0F172A;
    --text-secondary: #475569;
    --text-muted: #64748B;
    --primary: #2563EB;
    --primary-hover: #1D4ED8;
    --success: #16A34A;
    --warning: #D97706;
    --danger: #DC2626;
    --neutral: #64748B;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    padding: 24px 24px 40px;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--primary); text-decoration: none; transition: color 0.15s ease; }
  a:hover { color: var(--primary-hover); text-decoration: underline; }
  code {
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    background: var(--surface-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    color: var(--text-secondary);
  }
  .container {
    max-width: 1120px;
    margin: 0 auto;
  }
  .page-header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: 10px 16px;
    margin-bottom: 20px;
  }
  .page-header h1 {
    font-size: 25px;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin: 0 0 2px 0;
    color: var(--text);
  }
  .page-header .tagline {
    font-size: 13px;
    color: var(--text-muted);
    margin: 0;
  }
  .header-links {
    font-size: 12.5px;
    font-weight: 500;
  }
  .page-header .refresh-note {
    width: 100%;
    font-size: 12px;
    color: var(--text-muted);
    margin: 0;
  }
  h2 {
    font-size: 12px;
    font-weight: 600;
    margin: 0 0 10px 0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
  }
  .sub {
    font-size: 13px;
    color: var(--text-muted);
    margin: 0 0 20px 0;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }
  table {
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
  }
  th, td {
    text-align: left;
    padding: 7px 10px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  th {
    color: var(--text-muted);
    font-weight: 500;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.05em;
  }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: #F8FAFC; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .badge-running { background: #DCFCE7; color: #15803D; }
  .badge-queued { background: #FEF3C7; color: #B45309; }
  .badge-completed { background: var(--surface-secondary); color: var(--text-secondary); }
  .badge-unknown { background: var(--surface-secondary); color: var(--text-muted); }
  .log {
    background: var(--surface-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 320px;
    overflow-y: auto;
  }
  .log .empty {
    color: var(--text-muted);
    font-style: italic;
  }
  .footer {
    font-size: 11.5px;
    color: var(--text-muted);
    margin-top: 22px;
    border-top: 1px solid var(--border);
    padding-top: 12px;
  }
  .row.running td {
    background: #FEF2F2;
  }
  .row.running td:first-child {
    border-left: 4px solid var(--danger);
  }
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 500;
    font-family: inherit;
    line-height: 1.2;
    border-radius: 8px;
    border: 1px solid transparent;
    cursor: pointer;
    margin-right: 8px;
    transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease, opacity 0.15s ease;
  }
  .btn:disabled {
    background: var(--surface-secondary);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
    opacity: 0.7;
  }
  .btn-primary { background: var(--primary); border-color: var(--primary); color: #ffffff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); border-color: var(--primary-hover); }
  .btn-primary:active:not(:disabled) { background: #1E40AF; border-color: #1E40AF; }
  .btn-danger { background: var(--surface); border-color: #FECACA; color: var(--danger); }
  .btn-danger:hover:not(:disabled) { background: #FEF2F2; border-color: #FCA5A5; }
  .btn-danger:active:not(:disabled) { background: #FEE2E2; }
  .btn-sm { padding: 4px 10px; font-size: 12px; border-radius: 6px; }
  td.actions { text-align: center; }
  .queue-form {
    margin-top: 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    background: var(--surface);
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }
  .queue-form h2 {
    font-size: 12px;
    font-weight: 600;
    margin: 0 0 10px 0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
  }
  .queue-form label {
    font-size: 12px;
    color: var(--text-secondary);
    display: block;
    margin-bottom: 6px;
  }
  .queue-form textarea {
    width: 100%;
    min-height: 110px;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 13px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    resize: vertical;
    box-sizing: border-box;
    background: var(--surface);
    color: var(--text);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .queue-form textarea:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }
  .queue-form button {
    margin-top: 10px;
    padding: 8px 16px;
    background: var(--primary);
    color: #ffffff;
    border: 1px solid var(--primary);
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background-color 0.15s ease;
  }
  .queue-form button:hover {
    background: var(--primary-hover);
    border-color: var(--primary-hover);
  }
  .queue-form button:disabled {
    background: var(--surface-secondary);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
    opacity: 0.7;
  }
  .queue-form .hint {
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
  }
  .queue-form .result {
    margin-top: 12px;
    font-size: 13px;
  }
  .queue-form .result.ok {
    color: var(--success);
  }
  .queue-form .result.bad {
    color: var(--danger);
  }
  .table-scroll {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
</style>
</head>
<body>
<div class="container">

<div class="page-header">
  <div>
    <h1>Pipeline Queue</h1>
    <p class="tagline">Manage pipeline locations</p>
  </div>
  <div class="header-links"><a href="/">&larr; Back to status page</a></div>
  <p class="refresh-note">Live view of <code>pipeline_queue</code> &bull; auto-refreshes every {refresh} seconds</p>
</div>
<p class="sub">Add new locations at the bottom &mdash; they are appended after any currently-running location; the order of already-queued items is preserved.</p>

<div class="card">
  <h2>Summary</h2>
  <table>
    <tr><th>Total locations tracked</th><td>{total_locs}</td></tr>
    <tr><th>Completed (company_details)</th><td>{completed_count}</td></tr>
    <tr><th>Currently running</th><td><span class="badge badge-running">{running_loc}</span></td></tr>
  </table>
</div>

<div class="card">
  <h2>Queue (pipeline_queue)</h2>
  <div class="table-scroll">
  <table>
    <tr><th>Priority</th><th>Queue ID</th><th>Status</th><th>Location</th><th>Actions</th></tr>
{q_rows}
  </table>
  </div>
</div>

<div class="queue-form">
  <h2>Add new locations</h2>
  <form method="POST" action="/queue/add">
    <label for="new_locations">One location per line, or comma-separated. Examples:<br>
      <code>Detroit, Michigan , USA</code><br>
      <code>Nashville, Tennessee, USA</code><br>
    </label>
    <textarea id="new_locations" name="locations" placeholder="Detroit, Michigan , USA\nCharlotte, North Carolina, USA">{queue_text}</textarea>
    <div class="hint">New items are appended to the queue. The currently-running location is not disturbed, and existing queued order is preserved. Use the <strong>Delete</strong> button in the table above to remove a queued location.</div>
    <button type="submit" id="add_btn">Add to queue</button>
    <div class="result" id="result" style="display:none;"></div>
  </form>
  <script>
    // ---- Delete a queued location ----
    function deleteQueueItem(qid, btn) {
      if (!confirm('Delete queue item #' + qid + ' from the queue?')) return;
      btn.disabled = true;
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/queue/delete', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.onreadystatechange = function() {
        if (xhr.readyState === 4) {
          var data = {};
          try { data = JSON.parse(xhr.responseText); } catch (err) {}
          if (xhr.status < 400 && data.ok) {
            var row = btn.closest ? btn.closest('tr') : null;
            if (row) row.parentNode.removeChild(row);
          } else {
            btn.disabled = false;
            alert('Delete failed: ' + (data.error || ('HTTP ' + xhr.status)));
          }
        }
      };
      xhr.send(JSON.stringify({queue_id: qid}));
    }
  </script>
  <script>
    (function() {
      var form = document.querySelector('form');
      var btn = document.getElementById('add_btn');
      var result = document.getElementById('result');
      form.addEventListener('submit', function(e) {
        e.preventDefault();
        var text = document.getElementById('new_locations').value;
        btn.disabled = true;
        btn.textContent = 'Adding…';
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/queue/add', true);
        xhr.setRequestHeader('Content-Type', 'text/plain;charset=utf-8');
        xhr.onreadystatechange = function() {
          if (xhr.readyState === 4) {
            btn.disabled = false;
            btn.textContent = 'Add to queue';
            if (xhr.status >= 400) {
              var err = JSON.parse(xhr.responseText);
              result.className = 'result bad';
              result.style.display = 'block';
              result.textContent = 'Could not add locations: ' + (err.error || 'unknown error');
              return;
            }
            var data = JSON.parse(xhr.responseText);
            if (data.ok) {
              result.className = 'result ok';
              result.style.display = 'block';
              result.textContent = 'Added ' + data.added + ' location(s) to the queue: ' + data.locations.join(', ');
              // Simplest reliable UI refresh: reload the page so the server
              // renders a fresh queue table (short delay so the result
              // message stays visible first).
              setTimeout(function() { window.location.reload(); }, 800);
            } else {
              result.className = 'result bad';
              result.style.display = 'block';
              result.textContent = 'Unexpected server response.';
            }
          }
        };
        xhr.send(text);
      });
    })();
  </script>
</div>

<div class="footer">
  Pipeline Queue &bull; local status page &bull; {now_str}
</div>

</div>
</body>
</html>
"""


def _fetch_top_companies(location: str, limit: int = 10) -> list[dict[str, Any]]:
    """Top N most recent companies for a location (for the Total hover popup)."""
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT company_id, company_name, website_url, status, "
            "COALESCE(pages_crawled, 0), "
            "to_char(completed_at, 'YYYY-MM-DD HH24:MI') "
            "FROM company_details WHERE location = %s "
            "ORDER BY completed_at DESC NULLS LAST, company_id DESC LIMIT %s",
            (location, limit),
        )
        rows = [
            {
                "company_id": r[0],
                "company_name": r[1] or "(unnamed)",
                "website_url": r[2] or "",
                "status": r[3] or "pending",
                "pages_crawled": r[4],
                "completed_at": r[5] or "",
            }
            for r in cur.fetchall()
        ]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _fetch_top_contacts(location: str, limit: int = 10) -> list[dict[str, Any]]:
    """Top N recent companies-with-contacts for a location (Completed hover popup).

    'Contacts' rows = companies that have at least one email or phone recorded.
    """
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT c.company_id, cd.company_name, cd.website_url, "
            "cd.status, cd.location, "
            "COALESCE(NULLIF(c.best_contact_name, ''), '') , "
            "COALESCE(NULLIF(c.email1, ''), '') , "
            "COALESCE(NULLIF(c.email2, ''), '') , "
            "COALESCE(NULLIF(c.phone1, ''), '') , "
            "COALESCE(c.emails_found, 0), COALESCE(c.phones_found, 0), "
            "COALESCE(c.social_links_found, 0), "
            "to_char(cd.completed_at, 'YYYY-MM-DD HH24:MI') "
            "FROM contact c "
            "JOIN company_details cd ON cd.company_id = c.company_id "
            "WHERE cd.location = %s "
            "AND (COALESCE(c.emails_found, 0) > 0 OR COALESCE(c.phones_found, 0) > 0 "
            "     OR COALESCE(NULLIF(c.email1, ''), '') <> '' "
            "     OR COALESCE(NULLIF(c.phone1, ''), '') <> '') "
            "ORDER BY cd.completed_at DESC NULLS LAST, c.company_id DESC LIMIT %s",
            (location, limit),
        )
        rows = [
            {
                "company_id": r[0],
                "company_name": r[1] or "(unnamed)",
                "website_url": r[2] or "",
                "status": r[3] or "pending",
                "location": r[4] or location,
                "contact_name": r[5],
                "email1": r[6],
                "email2": r[7],
                "phone1": r[8],
                "emails_found": r[9],
                "phones_found": r[10],
                "social_found": r[11],
                "completed_at": r[12] or "",
            }
            for r in cur.fetchall()
            if (r[4] or "") == location
        ]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _fetch_location_table_data(all_locations: list[str]) -> dict[str, dict[str, Any]]:
    """Per-location: status counts, emails/phones totals, last completion time."""
    data: dict[str, dict[str, Any]] = {}
    if not all_locations:
        return data
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        loc_tuple = tuple(all_locations)
        # Status counts per location
        cur.execute(
            "SELECT location, status, COUNT(*) FROM company_details "
            "WHERE location IN %s GROUP BY location, status",
            (loc_tuple,),
        )
        for loc, status, count in cur.fetchall():
            d = data.setdefault(
                loc,
                {"completed": 0, "pending": 0, "failed": 0,
                 "no_contacts_found": 0, "emails": 0, "phones": 0,
                 "last_completion": ""},
            )
            d[status] = d.get(status, 0) + count
        # Emails/phones totals + last completion per location (join contact)
        cur.execute(
            "SELECT cd.location, "
            "COALESCE(SUM(COALESCE(c.emails_found, 0)), 0), "
            "COALESCE(SUM(COALESCE(c.phones_found, 0)), 0), "
            "to_char(MAX(cd.completed_at), 'YYYY-MM-DD HH24:MI') "
            "FROM company_details cd LEFT JOIN contact c ON c.company_id = cd.company_id "
            "WHERE cd.location IN %s GROUP BY cd.location",
            (loc_tuple,),
        )
        for loc, emails, phones, last_ts in cur.fetchall():
            d = data.setdefault(
                loc,
                {"completed": 0, "pending": 0, "failed": 0,
                 "no_contacts_found": 0, "emails": 0, "phones": 0,
                 "last_completion": ""},
            )
            d["emails"] = int(emails or 0)
            d["phones"] = int(phones or 0)
            d["last_completion"] = last_ts or ""
        cur.close()
        conn.close()
    except Exception:
        pass
    return data


def _fetch_top_companies_all(limit: int = 10) -> list[dict[str, Any]]:
    """Top N most recent companies across ALL locations (Total hover popup)."""
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT company_id, company_name, website_url, status, location, "
            "COALESCE(pages_crawled, 0), "
            "to_char(completed_at, 'YYYY-MM-DD HH24:MI') "
            "FROM company_details "
            "ORDER BY completed_at DESC NULLS LAST, company_id DESC LIMIT %s",
            (limit,),
        )
        rows = [
            {
                "company_id": r[0],
                "company_name": r[1] or "(unnamed)",
                "website_url": r[2] or "",
                "status": r[3] or "pending",
                "location": r[4] or "",
                "pages_crawled": r[5],
                "completed_at": r[6] or "",
            }
            for r in cur.fetchall()
        ]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _fetch_top_contacts_all(limit: int = 10) -> list[dict[str, Any]]:
    """Top N recent companies-with-contacts across ALL locations (Completed hover)."""
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT c.company_id, cd.company_name, cd.website_url, "
            "cd.status, cd.location, "
            "COALESCE(NULLIF(c.best_contact_name, ''), ''), "
            "COALESCE(NULLIF(c.email1, ''), ''), "
            "COALESCE(NULLIF(c.email2, ''), ''), "
            "COALESCE(NULLIF(c.phone1, ''), ''), "
            "COALESCE(c.emails_found, 0), COALESCE(c.phones_found, 0), "
            "COALESCE(c.social_links_found, 0), "
            "to_char(cd.completed_at, 'YYYY-MM-DD HH24:MI') "
            "FROM contact c "
            "JOIN company_details cd ON cd.company_id = c.company_id "
            "WHERE (COALESCE(c.emails_found, 0) > 0 OR COALESCE(c.phones_found, 0) > 0 "
            "     OR COALESCE(NULLIF(c.email1, ''), '') <> '' "
            "     OR COALESCE(NULLIF(c.phone1, ''), '') <> '') "
            "ORDER BY cd.completed_at DESC NULLS LAST, c.company_id DESC LIMIT %s",
            (limit,),
        )
        rows = [
            {
                "company_id": r[0],
                "company_name": r[1] or "(unnamed)",
                "website_url": r[2] or "",
                "status": r[3] or "pending",
                "location": r[4] or "",
                "contact_name": r[5],
                "email1": r[6],
                "email2": r[7],
                "phone1": r[8],
                "emails_found": r[9],
                "phones_found": r[10],
                "social_found": r[11],
                "completed_at": r[12] or "",
            }
            for r in cur.fetchall()
        ]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _status_block_html(report: dict[str, Any],
                        db_counts: dict[str, dict[str, int]],
                        all_locations: list[str]) -> str:
    """Return the inner HTML block that varies with the current state."""
    blocks: list[str] = []

    # Event banner (crash/stuck/finish -> red-tinted; otherwise green-tinted ok)
    # Event banner (crash/stuck/finish -> red-tinted; otherwise green-tinted ok)
    ev = report.get("event", "ok")
    ev_msg = report.get("event_message", "")
    if _is_paused():
        blocks.append(
            '<div class="event-banner pause">'
            '<div class="banner-head"><span class="status-dot dot-warning"></span>PAUSED</div>'
            '<div class="banner-sub">Pipeline controls are paused. Press <em>Resume</em> to continue. '
            'An already-running pipeline process is not affected until it exits.</div>'
            '</div>'
        )
    if ev in ("crash", "stuck", "finish"):
        blocks.append(
            f'<div class="event-banner"><strong>{html.escape(ev.upper())}</strong><br>'
            f'{html.escape(ev_msg)}</div>'
        )
    else:
        last_check = html.escape(str(report.get("time", "")))
        blocks.append(
            '<div class="event-banner ok">'
            '<div class="banner-head"><span class="status-dot dot-success"></span>OK</div>'
            '<div class="banner-sub">Pipeline is being monitored</div>'
            f'<div class="banner-time">Last check: {last_check}</div>'
            '</div>'
        )

    # Header summary
    pids_alive = report.get("pids_alive", False)
    pid_str = ", ".join(str(p) for p in report.get("pids", [])) or "none"
    alive_cls = "status-alive" if pids_alive else "status-dead"
    alive_text = "ALIVE" if pids_alive else "DEAD"
    watchdog_row = ""
    if report.get("watchdog"):
        watchdog_row = (
            f'<tr><th>Watchdog</th><td>{html.escape(str(report["watchdog"]))}</td></tr>'
        )
    blocks.append(
        f'<div class="card">'
        f'<h2>Process</h2>'
        f'<table class="kv">'
        f'<tr><th>Status</th><td><span class="{alive_cls}">'
        f'<span class="status-dot {"dot-success" if pids_alive else "dot-danger"}"></span>{alive_text}</span></td></tr>'
        f'<tr><th>PIDs</th><td>{html.escape(pid_str)}</td></tr>'
        f'<tr><th>Last check</th><td>{html.escape(str(report.get("time", "")))}</td></tr>'
        f'{watchdog_row}'
        f'</table></div>'
    )

    # Stage
    blocks.append(
        f'<div class="card">'
        f'<h2>Current Stage</h2>'
        f'<table class="kv">'
        f'<tr><th>Phase</th><td><strong>{html.escape(str(report.get("stage", "unknown")).upper())}</strong></td></tr>'
        f'<tr><th>Location</th><td><strong>{html.escape(str(report.get("location", "")) or "—")}</strong></td></tr>'
        f'<tr><th>Details</th><td>{html.escape(str(report.get("details", "")) or "—")}</td></tr>'
        f'</table></div>'
    )

    # Per-location DB counts — rich table with TOTAL row and hover popups.
    # Hovering "Completed" shows the top 10 contact rows for that location;
    # hovering "Total" shows the top 10 company rows. Popups are lazy-loaded
    # from /preview endpoints and cached per location for the page lifetime.
    if all_locations:
        loc_data = _fetch_location_table_data(all_locations)
        grand = {"completed": 0, "pending": 0, "failed": 0,
                 "no_contacts_found": 0, "total": 0, "emails": 0, "phones": 0}
        rows = ""
        for loc in all_locations:
            c = db_counts.get(loc, {})
            extra = loc_data.get(loc, {})
            comp = int(c.get("completed", 0))
            pend = int(c.get("pending", 0))
            fail = int(c.get("failed", 0))
            no_c = int(c.get("no_contacts_found", 0))
            total = comp + pend + fail + no_c
            emails = int(extra.get("emails", 0))
            phones = int(extra.get("phones", 0))
            done_pct = (100.0 * comp / total) if total else 0.0
            last_ts = str(extra.get("last_completion", "") or "")
            vals = {"completed": comp, "pending": pend, "failed": fail,
                    "no_contacts_found": no_c, "total": total,
                    "emails": emails, "phones": phones}
            for k, v in vals.items():
                grand[k] += v
            esc_loc = html.escape(loc)
            attr_loc = html.escape(loc, quote=True)
            rows += (
                f'<tr>'
                f'<td class="loc-cell">{esc_loc}</td>'
                f'<td class="num hover-cell" '
                f'onmouseenter="showPreview(event, \'contacts\', \'{attr_loc}\')" '
                f'onmouseleave="hidePreview()">'
                f'<span class="count completed-count">{comp}</span>'
                f'<span class="mini-bar"><span class="mini-fill" '
                f'style="width:{done_pct:.0f}%"></span></span>'
                f'</td>'
                f'<td class="num"><span class="count pending-count">{pend}</span></td>'
                f'<td class="num"><span class="count failed-count">{fail}</span></td>'
                f'<td class="num"><span class="count muted-count">{no_c}</span></td>'
                f'<td class="num emails-cell">{emails}</td>'
                f'<td class="num phones-cell">{phones}</td>'
                f'<td class="num hover-cell" '
                f'onmouseenter="showPreview(event, \'companies\', \'{attr_loc}\')" '
                f'onmouseleave="hidePreview()">'
                f'<span class="count total-count">{total}</span>'
                f'</td>'
                f'<td class="last-cell">{html.escape(last_ts) if last_ts else "&mdash;"}</td>'
                f'</tr>'
            )
        totals_row = (
            f'<tr class="totals-row">'
            f'<td>TOTAL</td>'
            f'<td class="num"><span class="count completed-count">{grand["completed"]}</span></td>'
            f'<td class="num"><span class="count pending-count">{grand["pending"]}</span></td>'
            f'<td class="num"><span class="count failed-count">{grand["failed"]}</span></td>'
            f'<td class="num"><span class="count muted-count">{grand["no_contacts_found"]}</span></td>'
            f'<td class="num emails-cell">{grand["emails"]}</td>'
            f'<td class="num phones-cell">{grand["phones"]}</td>'
            f'<td class="num"><span class="count total-count">{grand["total"]}</span></td>'
            f'<td></td>'
            f'</tr>'
        )
        blocks.append(
            f'<div class="card loc-card">'
            f'<h2>Company Details by Location (PostgreSQL)</h2>'
            f'<div class="loc-legend">'
            f'<span><span class="dot dot-completed"></span>Completed</span>'
            f'<span><span class="dot dot-pending"></span>Pending</span>'
            f'<span><span class="dot dot-failed"></span>Failed</span>'
            f'<span><span class="dot dot-muted"></span>No Contacts</span>'
            f'<span class="legend-hint">& hover counts for a top-10 preview</span>'
            f'</div>'
            f'<div class="table-scroll">'
            f'<table class="loc-table">'
            f'<tr>'
            f'<th>Location</th>'
            f'<th>Completed</th>'
            f'<th>Pending</th>'
            f'<th>Failed</th>'
            f'<th>No Contacts</th>'
            f'<th>Emails</th>'
            f'<th>Phones</th>'
            f'<th>Total</th>'
            f'<th>Last Activity</th>'
            f'</tr>'
            f'{rows}'
            f'{totals_row}'
            f'</table>'
            f'</div>'
            f'<div id="hover-popup" class="hover-popup"></div>'
            f'</div>'
        )

    # Active queue
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import get_queue_items

        q = get_queue_items()
        q_rows = ""
        if q:
            for item in q:
                loc = html.escape(str(item.get("location", "")))
                st = item.get("status", "unknown")
                pri = item.get("priority", "")
                qid = item.get("queue_id", "")
                badge_cls = {
                    "running": "badge-running",
                    "queued": "badge-queued",
                    "completed": "badge-completed",
                }.get(st, "badge-unknown")
                if st == "running":
                    del_btn = (
                        f'<button class="btn btn-danger btn-sm" disabled '
                        f'title="Cannot delete the running location">Delete</button>'
                    )
                else:
                    del_btn = (
                        f'<button class="btn btn-danger btn-sm" '
                        f'onclick="deleteQueueItem({int(qid)}, this)">Delete</button>'
                    )
                q_rows += (
                    f'<tr>'
                    f'<td>{pri if pri != "" else "—"}</td>'
                    f'<td><span class="badge {badge_cls}">{html.escape(str(st))}</span></td>'
                    f'<td>{loc}</td>'
                    f'<td class="actions">{del_btn}</td>'
                    f'</tr>'
                )
        else:
            q_rows = '<tr><td colspan="4" style="color:#888888;">(empty)</td></tr>'
        blocks.append(
            f'<div class="card">'
            f'<h2>Pipeline Queue (pipeline_queue)</h2>'
            f'<div class="table-scroll">'
            f'<table>'
            f'<tr><th>Priority</th><th>Status</th><th>Location</th><th>Actions</th></tr>'
            f'{q_rows}'
            f'</table>'
            f'</div></div>'
        )
    except Exception as exc:
        blocks.append(
            f'<div class="card">'
            f'<h2>Pipeline Queue</h2>'
            f'<p style="color:#b00020;">Could not read queue: {html.escape(str(exc))}</p></div>'
        )

    # Log tail
    log_text = report.get("log_tail", "")
    if log_text:
        blocks.append(
            f'<div class="card">'
            f'<h2>Recent Log (last 200 lines since last check)</h2>'
            f'<div class="log">{html.escape(log_text)}</div></div>'
        )
    else:
        blocks.append(
            f'<div class="card">'
            f'<h2>Recent Log</h2>'
            f'<div class="log"><span class="empty">(no new log lines since last check)</span></div></div>'
        )

    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Crawler-only run ("Crawl pending now" button)
# ---------------------------------------------------------------------------


class _CrawlerRunStdout:
    """File-like object that tees written text into a real file.

    The email crawler prints its live progress with print(); tee-ing stdout
    into batch_run.log keeps the monitor's log tail, stage detection and
    crash alerts working while a crawler-only run is active.
    """

    def __init__(self, real_out, log_path: Path) -> None:
        self._real_out = real_out
        self._log_path = log_path

    def write(self, text: str) -> int:
        try:
            self._real_out.write(text)
            self._real_out.flush()
        except Exception:
            pass
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        try:
            self._real_out.flush()
        except Exception:
            pass

    def isatty(self) -> bool:  # pragma: no cover - trivial
        return False


def _crawler_only_worker(handler: "_PipelineStatusHandler", log_path: Path) -> None:
    """Background-thread body for the crawler-only run."""
    real_stdout = sys.stdout
    sys.stdout = _CrawlerRunStdout(real_stdout, log_path)
    summary = ""
    error = ""
    try:
        result = _run_crawler_only(control=handler.crawler_control)
        if result.get("ok"):
            if result.get("stopped"):
                summary = (
                    f"Stopped after {result.get('processed', 0)} pending companies"
                    + (f" ({result.get('failed', 0)} failed)" if result.get("failed") else "")
                    + f" in {result.get('elapsed_seconds', 0):.0f}s."
                )
            else:
                summary = (
                    f"Processed {result.get('processed', 0)} pending companies"
                    + (f" ({result.get('failed', 0)} failed)" if result.get("failed") else "")
                    + f" in {result.get('elapsed_seconds', 0):.0f}s."
                )
        else:
            error = str(result.get("error", "unknown error"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log_message(f"ERROR: crawler-only run crashed: {error}")
    finally:
        sys.stdout = real_stdout
        handler.finish_crawler_run(summary=summary, error=error)
        if error:
            _log_message(f"ALERT: crawler-only run failed: {error}")
        else:
            _log_message(f"INFO: crawler-only run finished. {summary}")


def _build_status_page(report: dict[str, Any],
                       db_counts: dict[str, dict[str, int]],
                       all_locations: list[str],
                       now_str: str) -> str:
    inner = _status_block_html(report, db_counts, all_locations)
    try:
        ev = report.get("event", "ok")
        ev_repr = ev.upper()
    except Exception:
        ev_repr = "OK"
    return _STATUS_HTML_TEMPLATE.format(
        refresh=PAGE_REFRESH_SECONDS,
        title="Pipeline Monitor",
        text_block=inner,
        now_str=now_str,
        ts=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# HTTP server (single-threaded, simple)
# ---------------------------------------------------------------------------


class _PipelineStatusHandler:
    """Tiny HTTP handler that serves the status page + /poll + /log."""

    def __init__(self, state: MonitorState, monitor_log: Optional[Path]) -> None:
        self.state = state
        self.monitor_log = monitor_log
        self._db_counts: dict[str, dict[str, int]] = {}
        self._all_locations: list[str] = []
        self._report: dict[str, Any] = {}
        self._lock = threading.Lock()
        # Crawler-only background run (started via the web UI button).
        self._crawler_run_lock = threading.Lock()
        self._crawler_running = False
        self._crawler_started_at: Optional[float] = None
        self._crawler_summary: str = ""
        self._crawler_last_error: str = ""
        # Shared ControlEvent for cooperative Pause/Resume of the inline run.
        from utils.control import ControlEvent as _CE
        self.crawler_control = _CE()

    # ---- global pause (web-UI requested) ---------------------------------

    def is_paused(self) -> bool:
        return _is_paused()

    def set_paused(self, paused: bool) -> None:
        _set_paused(paused)
        if paused:
            try:
                self.crawler_control.request_pause()
            except Exception:
                pass
            _log_message("ALERT: PAUSE requested via web UI")
        else:
            try:
                self.crawler_control.resume()
            except Exception:
                pass
            _log_message("INFO: RESUME via web UI — pause lifted")

    def request_crawler_stop(self) -> None:
        try:
            self.crawler_control.request_stop()
        except Exception:
            pass

    def try_start_crawler_run(self) -> tuple[bool, str]:
        """Atomically claim the single crawler-only run slot."""
        with self._crawler_run_lock:
            if self._crawler_running:
                return False, "A crawler-only run is already in progress."
            self._crawler_running = True
            self._crawler_started_at = time.time()
            self._crawler_summary = ""
            self._crawler_last_error = ""
            # Fresh control event for this run (unless a pause is active —
            # then the run starts already paused).
            try:
                self.crawler_control.mark_stopped()
            except Exception:
                pass
            if _is_paused():
                try:
                    self.crawler_control.request_pause()
                except Exception:
                    pass
            else:
                try:
                    self.crawler_control.start()
                except Exception:
                    pass
            return True, ""

    def finish_crawler_run(self, summary: str = "", error: str = "") -> None:
        with self._crawler_run_lock:
            self._crawler_running = False
            self._crawler_started_at = None
            self._crawler_summary = summary
            self._crawler_last_error = error
        try:
            self.crawler_control.mark_stopped()
        except Exception:
            pass

    def update(self, report: dict[str, Any]) -> None:
        with self._lock:
            self._report = report
            all_locs = _detect_all_locations()
            self._all_locations = all_locs
            counts = _count_completions_for_locations(all_locs)
            per_loc: dict[str, dict[str, int]] = {}
            for loc in all_locs:
                per_loc[loc] = {}
                # fill from the global counts
                per_loc[loc]["completed"] = counts.get(loc, 0)
                try:
                    sys.path.insert(0, str(CRAWLER_DIR))
                    from storage.db import _get_connection
                    conn = _get_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT status, COUNT(*) FROM company_details "
                        "WHERE location = %s GROUP BY status",
                        (loc,),
                    )
                    for status, count in cur.fetchall():
                        per_loc[loc][status] = count
                    cur.close()
                    conn.close()
                except Exception:
                    pass
            self._db_counts = per_loc

    def handle(self, conn: Any) -> None:
        try:
            data = conn.recv(8192).decode("utf-8", errors="replace")
        except Exception:
            conn.close()
            return
        if not data:
            conn.close()
            return

        request_line = data.splitlines()[0] if data.splitlines() else ""
        parts = request_line.split()
        if len(parts) < 2:
            conn.close()
            return
        method, path, _ = parts

        with self._lock:
            report = dict(self._report)
            db_counts = dict(self._db_counts)
            all_locations = list(self._all_locations)

        if path == "/" or path == "/index.html":
            body = _build_status_page(report, db_counts, all_locations, _now_str())
            content = body.encode("utf-8")
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(content)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("utf-8") + content)

        elif path == "/poll":
            import json
            payload = {
                "time": report.get("time", _now_str()),
                "pids_alive": report.get("pids_alive", False),
                "pids": report.get("pids", []),
                "stage": report.get("stage", "unknown"),
                "location": report.get("location", ""),
                "details": report.get("details", ""),
                "event": report.get("event", "ok"),
                "watchdog": report.get("watchdog", ""),
                "db_counts": db_counts,
                "all_locations": all_locations,
                "log_tail": report.get("log_tail", ""),
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            content = body.encode("utf-8")
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(content)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("utf-8") + content)

        elif path == "/log":
            log_text = _read_recent_log_lines(PIPELINE_LOG, 0, limit=500)
            content = log_text.encode("utf-8")
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(content)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("utf-8") + content)

        else:
            body = (
                "Not Found\n\n"
                f"Path: {path}\n"
                "Available: / (status page), /poll (JSON), /log (raw tail)\n"
            )
            content = body.encode("utf-8")
            response = (
                "HTTP/1.1 404 Not Found\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(content)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("utf-8") + content)

        try:
            conn.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        try:
            conn.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _run_http_server(state: MonitorState,
                     monitor_log: Optional[Path],
                     port: int,
                     server_handler: _PipelineStatusHandler) -> None:
    import socket as _socket

    server_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server_sock.settimeout(1.0)
    try:
        server_sock.bind(("127.0.0.1", port))
    except OSError as exc:
        # sys.exit() in a thread only kills the thread — use os._exit so a
        # second monitor instance can never linger as a "zombie poller".
        _log_message(f"ERROR: cannot bind port {port}: {exc} — another monitor is probably already running. Exiting.")
        sys.stdout.flush()
        os._exit(1)
    server_sock.listen(5)
    handler = server_handler

    _log_message(f"HTTP status page listening on http://127.0.0.1:{port}/")
    _log_message(f"  /            — plain HTML status page (auto-refresh every {PAGE_REFRESH_SECONDS}s)")
    _log_message(f"  /poll        — JSON status endpoint")
    _log_message(f"  /log         — raw batch_run.log tail")

    _log_message("DEBUG: server thread about to enter accept loop")

    if monitor_log is not None:
        try:
            monitor_log.parent.mkdir(parents=True, exist_ok=True)
            with open(monitor_log, "a", encoding="utf-8") as f:
                f.write(
                    f"[{_now_str()}] HTTP status page listening on "
                    f"http://127.0.0.1:{port}/\n"
                )
        except Exception:
            pass

    # Local helper for the accept loop: refresh the handler's in-memory
    # report from a fresh poll_once (using the handler's own state object),
    # so even before the main polling loop's first cycle completes, /poll and /
    # return real data instead of the init-time stub.
    def _refresh_handler() -> None:
        try:
            r = poll_once_with_watchdog(handler.state, handler.monitor_log)
            handler.update(r)
        except Exception:
            pass

    # Kick off one refresh right away so an immediate connection isn't served
    # stale init data. Then keep refreshing on each accept-timeout.
    try:
        _refresh_handler()
    except Exception as exc:
        _log_message(f"WARN: initial handler refresh failed: {exc}")


    while True:
        try:
            conn, addr = server_sock.accept()
        except _socket.timeout:
            _refresh_handler()
            continue
        except Exception as exc:
            _log_message(f"WARN: accept failed: {exc}")
            continue
        t = threading.Thread(
            target=_serve_one_connection, args=(handler, conn),
            daemon=True,
        )
        t.start()


def _serve_one_connection(handler: _PipelineStatusHandler, conn: Any) -> None:
    """Serve a single HTTP connection. Reads the request, sends the response,
    then closes. Must not raise — all exceptions are caught."""
    try:
        data = conn.recv(8192).decode("utf-8", errors="replace")
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return

    if not data:
        try:
            conn.close()
        except Exception:
            pass
        return

    request_line = data.splitlines()[0] if data.splitlines() else ""
    parts = request_line.split()
    if len(parts) < 2:
        try:
            conn.close()
        except Exception:
            pass
        return

    method, path, _ = parts

    with handler._lock:
        report = dict(handler._report)
        db_counts = dict(handler._db_counts)
        all_locations = list(handler._all_locations)

    if path == "/" or path == "/index.html":
        body = _build_status_page(report, db_counts, all_locations, _now_str())
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/poll":
        import json as _json
        with handler._crawler_run_lock:
            crawler_running = handler._crawler_running
            crawler_summary = handler._crawler_summary
            crawler_error = handler._crawler_last_error
            crawler_started_at = handler._crawler_started_at
        crawler_only_payload = {
            "running": crawler_running,
            "summary": crawler_summary,
            "error": crawler_error,
            "started_at": (
                datetime.fromtimestamp(crawler_started_at).strftime("%Y-%m-%d %H:%M:%S")
                if crawler_started_at else None
            ),
        }
        pipeline_pids = list(_pipeline_processes().keys())
        pipeline_controls = {
            "paused": handler.is_paused(),
            "pipeline_running": bool(pipeline_pids),
            "pids": pipeline_pids,
            "inline_crawler_running": crawler_running,
        }
        payload = {
            "time": report.get("time", _now_str()),
            "pids_alive": report.get("pids_alive", False),
            "pids": report.get("pids", []),
            "stage": report.get("stage", "unknown"),
            "location": report.get("location", ""),
            "details": report.get("details", ""),
            "event": report.get("event", "ok"),
            "watchdog": report.get("watchdog", ""),
            "db_counts": db_counts,
            "all_locations": all_locations,
            "log_tail": report.get("log_tail", ""),
            "crawler_only": crawler_only_payload,
            "pipeline_controls": pipeline_controls,
        }
        body = _json.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/log":
        log_text = _read_recent_log_lines(PIPELINE_LOG, 0, limit=500)
        content = log_text.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/crawler/pending" and method.upper() == "POST":
        import json as _json_c
        started, start_error = handler.try_start_crawler_run()
        if not started:
            payload = {
                "ok": False,
                "running": True,
                "error": "A crawler-only run is already in progress.",
            }
            status_line = "HTTP/1.1 409 Conflict\r\n"
        else:
            running_locs = _get_pipeline_running_locations()
            companies = _select_pending_companies(skip_locations=running_locs)
            pending_count = len(companies)
            if pending_count <= 0:
                handler.finish_crawler_run()
                if running_locs:
                    skipped = ", ".join(sorted(running_locs))
                    payload = {
                        "ok": True,
                        "running": False,
                        "message": (
                            f"Nothing to crawl: all pending companies belong to the "
                            f"pipeline's current location(s): {skipped}. "
                            "They will be handled by the pipeline itself."
                        ),
                    }
                else:
                    payload = {
                        "ok": True,
                        "running": False,
                        "message": "No companies with status='pending' found — nothing to crawl.",
                    }
                status_line = "HTTP/1.1 200 OK\r\n"
            else:
                skipped_note = (
                    f" (skipping pipeline's current location(s): {', '.join(sorted(running_locs))})"
                    if running_locs else ""
                )
                payload = {
                    "ok": True,
                    "running": True,
                    "pending": pending_count,
                    "skipped_running_locations": sorted(running_locs),
                    "message": (
                        f"Crawler-only run started for {pending_count} pending "
                        f"companies across all locations{skipped_note}."
                    ),
                }
                status_line = "HTTP/1.1 200 OK\r\n"
                threading.Thread(
                    target=_crawler_only_worker,
                    args=(handler, Path.cwd() / PIPELINE_LOG),
                    daemon=True,
                ).start()
                _log_message(
                    f"INFO: crawler-only run requested via web UI "
                    f"({pending_count} pending companies, all locations{skipped_note})"
                )
        body = _json_c.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            status_line
            + "Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(content)}\r\n"
            + "Connection: close\r\n"
            + "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/pipeline/start" and method.upper() == "POST":
        ok, message = _start_pipeline_process(min_results=500)
        payload = {
            "ok": ok,
            "message": message if ok else None,
            "error": None if ok else message,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            ("HTTP/1.1 200 OK\r\n" if ok else "HTTP/1.1 409 Conflict\r\n")
            + "Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(content)}\r\n"
            + "Connection: close\r\n"
            + "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path.startswith("/preview/") and method.upper() == "GET":
        # Lazy top-10 previews for the Company Details table hover popups:
        #   /preview/companies?location=<loc>  -> top 10 company rows
        #   /preview/contacts?location=<loc>   -> top 10 contact rows
        # An empty/missing location returns the top 10 across all locations.
        import json as _json
        from urllib.parse import urlsplit, parse_qs as _pqs
        kind = path[len("/preview/"):].split("?")[0].strip("/").lower()
        qs = _pqs(urlsplit(path).query)
        location = (qs.get("location", [""])[0] or "").strip()
        try:
            if kind == "companies":
                rows = (
                    _fetch_top_companies(location, limit=10)
                    if location else _fetch_top_companies_all(limit=10)
                )
            elif kind == "contacts":
                rows = (
                    _fetch_top_contacts(location, limit=10)
                    if location else _fetch_top_contacts_all(limit=10)
                )
            else:
                raise ValueError(f"Unknown preview kind: {kind}")
            payload = {
                "ok": True,
                "kind": kind,
                "location": location,
                "count": len(rows),
                "rows": rows,
            }
            status_line = "HTTP/1.1 200 OK\r\n"
        except Exception as exc:
            payload = {"ok": False, "error": str(exc), "rows": []}
            status_line = "HTTP/1.1 500 Internal Server Error\r\n"
        body = _json.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            status_line
            + "Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(content)}\r\n"
            + "Connection: close\r\n"
            + "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/pipeline/pause" and method.upper() == "POST":
        handler.set_paused(True)
        payload = {
            "ok": True,
            "paused": True,
            "message": (
                "PAUSED: inline crawler will hold at the next company boundary, "
                "new pipeline starts are blocked, and queued locations will not be picked up. "
                "A running pipeline process finishes its current company first; use Resume to lift the pause."
            ),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/pipeline/resume" and method.upper() == "POST":
        handler.set_paused(False)
        payload = {
            "ok": True,
            "paused": False,
            "message": "RESUMED: pause lifted. You can Start the pipeline again and the inline crawler continues.",
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/queue/delete" and method.upper() == "POST":
        import json as _json_del
        payload: dict = {"ok": False, "error": "unknown"}
        status_line = "HTTP/1.1 400 Bad Request\r\n"
        try:
            raw_body = _read_post_body(data)
            qid = None
            try:
                qid = int(_json_del.loads(raw_body or "{}").get("queue_id"))
            except Exception:
                qid = None
            if qid is None:
                payload = {"ok": False, "error": "Missing or invalid queue_id"}
            else:
                from storage.db import get_queue_items as _gqi, delete_queue_item as _dq
                item = next(
                    (i for i in _gqi() if int(i.get("queue_id", -1)) == qid),
                    None,
                )
                if item is None:
                    payload = {"ok": False, "error": f"Queue item {qid} not found"}
                    status_line = "HTTP/1.1 404 Not Found\r\n"
                elif item.get("status") == "running":
                    payload = {"ok": False, "error": "Cannot delete the RUNNING location — pause or let it finish first."}
                    status_line = "HTTP/1.1 409 Conflict\r\n"
                else:
                    removed = _dq(qid)
                    ok_flag = bool(removed)
                    payload = {
                        "ok": ok_flag,
                        "deleted": qid,
                        "location": item.get("location", ""),
                    }
                    if not ok_flag:
                        payload["error"] = "delete_queue_item returned False"
                    status_line = "HTTP/1.1 200 OK\r\n"
                    _log_message(f"INFO: queue item {qid} ({item.get('location', '')}) deleted via web UI")
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
            status_line = "HTTP/1.1 500 Internal Server Error\r\n"
        body = _json_del.dumps(payload, ensure_ascii=False, indent=2)
        content = body.encode("utf-8")
        response = (
            status_line
            + "Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(content)}\r\n"
            + "Connection: close\r\n"
            + "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/queue" and method.upper() == "GET":
        body = _queue_page(db_counts, all_locations)
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    elif path == "/queue/add" and method.upper() == "POST":
        try:
            raw_body = _read_post_body(data)
            from storage.db import enqueue_locations_batch as _enq_batch
            locations = _parse_queue_input(raw_body)
            if not locations:
                raise ValueError("No locations provided")
            added_ids, skipped = _enq_batch(
                locations=locations,
                enable_url_finder=True,
                enable_email_crawler=True,
                min_results=300,
            )
            import json as _json_q
            body = _json_q.dumps(
                {
                    "ok": True,
                    "added": len(added_ids),
                    "ids": [int(x) for x in added_ids],
                    "locations": [l for l in locations if l not in skipped],
                    "skipped": skipped,
                },
                ensure_ascii=False,
                indent=2,)
            status_line = "HTTP/1.1 200 OK\r\n"
            content_type = "Content-Type: application/json; charset=utf-8\r\n"
        except Exception as exc:
            import json as _json_q_e
            body = _json_q_e.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
            status_line = "HTTP/1.1 400 Bad Request\r\n"
            content_type = "Content-Type: application/json; charset=utf-8\r\n"
        content = body.encode("utf-8")
        response = (
            status_line
            + content_type
            + f"Content-Length: {len(content)}\r\n"
            + "Connection: close\r\n"
            + "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    else:
        body = (
            "Not Found\n\n"
            f"Path: {path}\n"
            "Available: / (status page), /poll (JSON), /log (raw tail), "
            "/queue (add locations), /crawler/pending (POST: crawl pending)\n"
        )
        content = body.encode("utf-8")
        response = (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode("utf-8") + content)
        except Exception:
            pass

    try:
        conn.shutdown(socket.SHUT_WR)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _queue_page(
    db_counts: dict[str, dict[str, int]],
    all_locations: list[str],
    submitted_text: str = "",
) -> str:
    """HTML page that shows the live queue and exposes an 'Add locations' form.

    submitted_text is the body the user most recently POSTed to /queue/add;
    it is echoed back into the textarea so they can adjust and resubmit.
    """
    # Pull ALL queue rows from the DB (including completed ones, so the page
    # reflects history and duplicate submissions are visible), with a safe
    # fallback to the active-queue view if the DB is unavailable.
    try:
        sys.path.insert(0, str(CRAWLER_DIR))
        from storage.db import _get_connection

        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT queue_id, location, status, priority FROM pipeline_queue "
            "ORDER BY priority ASC, created_at ASC"
        )
        q = [
            dict(zip(("queue_id", "location", "status", "priority"), row))
            for row in cur.fetchall()
        ]
        cur.close()
        conn.close()
    except Exception as exc:
        q = []
        _log_message(f"WARN: queue page could not read queue: {exc}")
        try:
            from storage.db import get_queue_items

            q = get_queue_items()
        except Exception:
            pass

    running_loc = ""
    for item in q:
        if item.get("status") == "running":
            running_loc = item.get("location", "").strip()
            break

    rows = ""
    for idx, item in enumerate(q):
        loc = html.escape(str(item.get("location", "")))
        st = item.get("status", "unknown")
        pri = item.get("priority", "")
        qid = item.get("queue_id", "")
        badge_cls = {
            "running": "badge-running",
            "queued": "badge-queued",
            "completed": "badge-completed",
        }.get(st, "badge-unknown")
        running_mark = " running" if st == "running" else ""
        if st == "running":
            del_btn = (
                f'<button class="btn btn-danger btn-sm" disabled '
                f'title="Cannot delete the running location">Delete</button>'
            )
        elif st == "completed":
            del_btn = (
                f'<button class="btn btn-danger btn-sm" disabled '
                f'title="Completed locations cannot be deleted (their history prevents accidental re-crawling)">Delete</button>'
            )
        else:
            del_btn = (
                f'<button class="btn btn-danger btn-sm" '
                f'onclick="deleteQueueItem({int(qid)}, this)">Delete</button>'
            )
        rows += (
            f'<tr class="row{running_mark}">'
            f'<td>{pri if pri != "" else "—"}</td>'
            f'<td>{qid}</td>'
            f'<td><span class="badge {badge_cls}">{html.escape(str(st))}</span></td>'
            f'<td>{loc}</td>'
            f'<td class="actions">{del_btn}</td>'
            f'</tr>'
        )

    if not rows:
        rows = '<tr><td colspan="5" style="color:#888888;">(empty)</td></tr>'

    total_locs = len(all_locations)
    completed_count = sum(int(db_counts.get(loc, {}).get("completed", 0)) for loc in all_locations)

    # NOTE: We deliberately avoid str.format() here — the page's embedded
    # JavaScript contains literal braces that would break .format(). Plain
    # token substitution is safe.
    page = _QUEUE_PAGE_HTML
    for token, value in {
        "{refresh}": str(PAGE_REFRESH_SECONDS),
        "{total_locs}": str(total_locs),
        "{completed_count}": str(completed_count),
        "{running_loc}": html.escape(running_loc or "—"),
        "{q_rows}": rows,
        "{queue_text}": html.escape(submitted_text),
        "{now_str}": _now_str(),
    }.items():
        page = page.replace(token, value)
    return page


def _read_post_body(raw_request: str) -> str:
    """Extract the POST body from a raw HTTP request string."""
    idx = raw_request.find("\r\n\r\n")
    if idx == -1:
        idx = raw_request.find("\n\n")
    if idx == -1:
        return ""
    sep_len = 4 if raw_request[idx:idx + 4] == "\r\n\r\n" else 2
    return raw_request[idx + sep_len:]


def _parse_queue_input(raw_body: str) -> list[str]:
    """Parse the POST body into a list of location strings.

    Accepts either a raw text body (one location per line) or a form-encoded
    body such as 'locations=Detroit%2C+Michigan+%2C+USA'. Locations contain
    commas (e.g. 'Detroit, Michigan, USA'), so the value is never split on
    commas — only on newlines.
    """
    body = raw_body.strip()
    if not body:
        return []

    # Form-encoded submission from the HTML form: extract the 'locations'
    # field value and URL-decode it.
    if body.startswith("locations=") or "\nlocations=" in body or "&locations=" in body:
        from urllib.parse import parse_qs
        try:
            fields = parse_qs(body, keep_blank_values=True)
            values = fields.get("locations", [])
        except Exception:
            values = []
        if values:
            body = "\n".join(values)

    parts: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            parts.append(s)
    return parts


def _web_main(port: int, monitor_log: Optional[Path]) -> None:
    root = Path.cwd()
    log_path = root / PIPELINE_LOG
    if not log_path.exists():
        _log_message(f"ERROR: pipeline log not found at {log_path}")
        _log_message("Start the pipeline first, then run this monitor.")
        sys.exit(1)

    _log_message("FINDME Pipeline Monitor (web mode) starting")
    _log_message(f"  Log watched: {log_path}")
    if monitor_log is not None:
        _log_message(f"  Monitor log: {monitor_log}")
    _log_message(f"  Status page port: {port}")
    _log_message(f"  Page refresh: every {PAGE_REFRESH_SECONDS}s")
    _log_message(f"  Poll interval: {POLL_INTERVAL_SECONDS}s ({POLL_INTERVAL_SECONDS // 60} min)")
    _log_message("")

    state = MonitorState()
    state.prev_log_size = _log_size(log_path)
    state.prev_pids = _find_pipeline_pids()
    # If no pipeline is alive, re-queue any location stuck at 'running'
    # (crashed mid-run) so the runner can pick it up again.
    reset_count = _reset_stale_running_queue_items()
    if reset_count:
        _log_message(f"INFO: {reset_count} stale 'running' queue item(s) reset to 'queued'")

    state.prev_running_location = _detect_current_location_from_queue()
    if state.prev_running_location:
        state.prev_completed[state.prev_running_location] = (
            _count_completions_for_location(state.prev_running_location)
        )
    state.last_stage_desc = "_initial_"

    _log_message(f"Initial snapshot: PIDs={state.prev_pids}, log_size={state.prev_log_size}")
    _log_message(f"Initial running location: {state.prev_running_location}")
    _log_message("")

    # Start the HTTP server in a daemon thread so the main thread can poll.
    server_handler = _PipelineStatusHandler(state, monitor_log)
    server_thread = threading.Thread(
        target=_run_http_server,
        args=(state, monitor_log, port, server_handler),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.2)  # give the server thread a moment to bind

    # Main polling loop — runs in-process, updates the handler, prints heartbeat.
    try:
        while True:
            report = poll_once_with_watchdog(state, monitor_log)

            # Update the handler that serves /poll and / so the web UI stays current.
            try:
                server_handler.update(report)
            except Exception as exc:
                _log_message(f"WARN: failed to update server handler: {exc}")

            alive_str = "ALIVE" if report["pids_alive"] else "DEAD"
            pid_str = ", ".join(str(p) for p in report["pids"]) or "none"
            event_str = {
                "ok": "OK",
                "none": "ok",
                "crash": "CRASH",
                "stuck": "STUCK",
                "finish": "FINISH",
                "stage_change": "HEARTBEAT",
            }.get(report["event"], report["event"])

            heartbeat = (
                f"[{report['time']}] pid={pid_str} ({alive_str}) | "
                f"event={event_str} | {report['details']}"
            )
            _log_message(heartbeat)
            if report["event"] in ("crash", "stuck", "finish"):
                _log_message(f"  MESSAGE: {report['event_message']}")

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        _log_message("Monitor stopped by user.")
    except Exception as exc:
        _log_message(f"Monitor error: {exc}")
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FINDME Pipeline Monitor — status page + alerts"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--web",
        action="store_true",
        help="Start HTTP status page on port 8010 (default if no mode given).",
    )
    group.add_argument(
        "--poll",
        action="store_true",
        help="Polling-only mode: no HTTP server, console + popup alerts only.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Port for the status page (default: {DEFAULT_HTTP_PORT}).",
    )
    args = parser.parse_args()

    root = Path.cwd()
    log_path = root / PIPELINE_LOG
    monitor_log = root / f"monitor_{_today_str()}.log"

    if not log_path.exists():
        _log_message(f"ERROR: pipeline log not found at {log_path}")
        _log_message("Start the pipeline first, then run this monitor.")
        sys.exit(1)

    mode = "web" if args.web or not args.poll else "poll"
    _log_message(f"FINDME Pipeline Monitor starting (mode={mode})")
    _log_message(f"  Log watched: {log_path}")
    _log_message(f"  Monitor log: {monitor_log}")
    if mode == "web":
        _log_message(f"  Status page: http://127.0.0.1:{args.port}/")
        _log_message(f"  Page refresh: every {PAGE_REFRESH_SECONDS}s")
    _log_message(f"  Poll interval: {POLL_INTERVAL_SECONDS}s ({POLL_INTERVAL_SECONDS // 60} min)")
    _log_message("")

    state = MonitorState()
    state.prev_log_size = _log_size(log_path)
    state.prev_pids = _find_pipeline_pids()
    # If no pipeline is alive, re-queue any location stuck at 'running'
    # (crashed mid-run) so the runner can pick it up again.
    reset_count = _reset_stale_running_queue_items()
    if reset_count:
        _log_message(f"INFO: {reset_count} stale 'running' queue item(s) reset to 'queued'")

    state.prev_running_location = _detect_current_location_from_queue()
    if state.prev_running_location:
        state.prev_completed[state.prev_running_location] = (
            _count_completions_for_location(state.prev_running_location)
        )
    state.last_stage_desc = "_initial_"

    _log_message(f"Initial snapshot: PIDs={state.prev_pids}, log_size={state.prev_log_size}")
    _log_message(f"Initial running location: {state.prev_running_location}")
    _log_message("")

    if mode == "web":
        _web_main(args.port, monitor_log)
        return

    # --poll mode: original behavior
    try:
        while True:
            report = poll_once_with_watchdog(state, monitor_log)

            alive_str = "ALIVE" if report["pids_alive"] else "DEAD"
            pid_str = ", ".join(str(p) for p in report["pids"]) or "none"
            event_str = {
                "ok": "OK",
                "none": "ok",
                "crash": "CRASH",
                "stuck": "STUCK",
                "finish": "FINISH",
                "stage_change": "HEARTBEAT",
            }.get(report["event"], report["event"])

            heartbeat = (
                f"[{report['time']}] pid={pid_str} ({alive_str}) | "
                f"event={event_str} | {report['details']}"
            )
            _log_message(heartbeat)
            if report["event"] in ("crash", "stuck", "finish"):
                _log_message(f"  MESSAGE: {report['event_message']}")

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        _log_message("Monitor stopped by user.")
    except Exception as exc:
        _log_message(f"Monitor error: {exc}")
        raise


if __name__ == "__main__":
    main()
