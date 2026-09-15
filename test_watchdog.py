"""Offline test for the monitor watchdog auto-restart logic (wall-clock based).

Stubs out process discovery / kill / start so no real pipeline or DB is
touched. Simulates: dead pipeline -> relaunch after confirmation window,
alive-but-silent -> kill+relaunch after hang window, cooldown suppression,
pause suppression, in-progress lock, and report propagation.
"""
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\TP\URLFinder")
sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402

calls = {"kill": 0, "start": 0, "pipeline_alive": False}


def fake_kill():
    calls["kill"] += 1
    calls["pipeline_alive"] = False
    return True, "PID 1: killed (test)"


def fake_start(min_results: int = 500):
    calls["start"] += 1
    calls["pipeline_alive"] = True
    return True, f"Pipeline started (PID 9999). [TEST min-results={min_results}]"


monitor._kill_pipeline_tree = fake_kill
monitor._start_pipeline_process = fake_start
monitor._is_paused = lambda: False
monitor._pipeline_processes = lambda: ({123: "cmd"} if calls["pipeline_alive"] else {})

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f" - {name}")
    if not cond:
        failures.append(name)


MIN = 60


def fresh_state(**kwargs):
    state = monitor.MonitorState()
    for k, v in kwargs.items():
        setattr(state, k, v)
    return state


def report(alive=True, event="ok", pids=(123,)):
    return {"event": event, "pids": list(pids) if alive else [], "location": "Testloc",
            "pids_alive": alive, "event_message": ""}


# --- 1. Dead pipeline: waits for confirmation window, then relaunches -------
calls.update(kill=0, start=0, pipeline_alive=False)
state = fresh_state()
rep = report(alive=False)
monitor._watchdog_auto_restart(rep, state)
check("dead: waits during confirmation window",
      calls["start"] == 0 and "waiting to confirm" in state.watchdog_last_action)
# Simulate 11 minutes already elapsed in the death window.
state.dead_since_ts = time.time() - (monitor.WATCHDOG_DEAD_CONFIRM_SECONDS + MIN)
rep = report(alive=False)
monitor._watchdog_auto_restart(rep, state)
check("dead: relaunches after window",
      calls["start"] == 1 and calls["kill"] == 1 and state.watchdog_restarts == 1)
check("dead: message appended to report",
      "WATCHDOG" in rep.get("event_message", ""))
check("dead: death window cleared", state.dead_since_ts is None)

# --- 2. Alive but silent: kill+relaunch after hang window -------------------
calls.update(kill=0, start=0, pipeline_alive=True)
state = fresh_state(last_silent_since_ts=time.time() - (monitor.WATCHDOG_HANG_CONFIRM_SECONDS + MIN))
rep = report(alive=True, event="ok")  # event is ok — watchdog must still act
monitor._watchdog_auto_restart(rep, state)
check("silent: acts even when event is 'ok'",
      calls["kill"] == 1 and calls["start"] == 1)
check("silent: silence window cleared", state.last_silent_since_ts is None)

# --- 3. Alive and silent but inside window: no action -----------------------
calls.update(kill=0, start=0, pipeline_alive=True)
state = fresh_state(last_silent_since_ts=time.time() - 3 * MIN)
rep = report(alive=True)
monitor._watchdog_auto_restart(rep, state)
check("silent: waits inside window",
      calls["kill"] == 0 and "armed" in state.watchdog_last_action)

# --- 4. Cooldown suppresses a second restart --------------------------------
calls.update(kill=0, start=0, pipeline_alive=True)
state = fresh_state(
    last_silent_since_ts=time.time() - (monitor.WATCHDOG_HANG_CONFIRM_SECONDS + MIN),
    last_watchdog_restart_ts=time.time() - MIN,  # restarted 1 min ago
)
rep = report(alive=True)
monitor._watchdog_auto_restart(rep, state)
check("cooldown suppresses restart", calls["kill"] == 0 and "cooldown" in state.watchdog_last_action)

# --- 5. Paused pipeline is never touched ------------------------------------
calls.update(kill=0, start=0, pipeline_alive=True)
monitor._is_paused = lambda: True
state = fresh_state(last_silent_since_ts=time.time() - (monitor.WATCHDOG_HANG_CONFIRM_SECONDS + MIN))
rep = report(alive=False)
monitor._watchdog_auto_restart(rep, state)
check("paused: no kill/start", calls["kill"] == 0 and calls["start"] == 0)
monitor._is_paused = lambda: False

# --- 6. Lock held elsewhere -> skip, never double-act -----------------------
calls.update(kill=0, start=0, pipeline_alive=True)
state = fresh_state(last_silent_since_ts=time.time() - (monitor.WATCHDOG_HANG_CONFIRM_SECONDS + MIN))
acquired = monitor._watchdog_lock.acquire(blocking=False)
assert acquired
try:
    rep = report(alive=True)
    monitor._watchdog_auto_restart(rep, state)
    check("lock: concurrent watchdog skips", calls["kill"] == 0 and "in progress" in state.watchdog_last_action)
finally:
    monitor._watchdog_lock.release()

# --- 7. poll_once_with_watchdog propagates watchdog status ------------------
calls.update(kill=0, start=0, pipeline_alive=True)
state = fresh_state()
orig_poll = monitor.poll_once
monitor.poll_once = lambda s, m: {"event": "ok", "pids": [123], "pids_alive": True,
                                  "location": "L", "event_message": ""}
try:
    rep = monitor.poll_once_with_watchdog(state, None)
    check("wrapper: report carries watchdog field", "watchdog" in rep)
finally:
    monitor.poll_once = orig_poll

# --- 8. Process reappears mid-death-window -> window resets -----------------
state = fresh_state(dead_since_ts=time.time() - 2 * MIN)
rep = report(alive=True)
monitor._watchdog_auto_restart(rep, state)
check("reappeared: death window resets", state.dead_since_ts is None)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All watchdog tests passed.")
