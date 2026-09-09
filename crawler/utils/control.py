"""
Cooperative run control for a single pipeline run.

Provides a shared event object so both the email pipeline
(crawler/pipeline.py::process_companies) and the URL Finder scraper
(findme/url_finder/scraper.py::RealEstateScraper) can react to stop/pause
requests without process-level kills.

Usage (caller side):
    control = ControlEvent()
    control.start()
    run_pipeline(..., control=control)   # passes it through
    control.request_stop()
    control.request_pause()
    control.resume()
"""

from __future__ import annotations

import asyncio
import threading
import time


class ControlEvent:
    """Cooperative stop + pause for one run."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running = False
        self._paused = False
        self._stop_requested = False

    def start(self) -> None:
        with self._lock:
            self._running = True
            self._stop_requested = False
            self._paused = False

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            self._paused = False

    def request_pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def check(self) -> bool:
        """Return False if the run should stop, True otherwise.

        Paused runs return True (keep waiting) so the caller can sleep
        or busy-wait until resumed or stopped.
        """
        with self._lock:
            if self._stop_requested:
                return False
            return True

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def is_stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def mark_stopped(self) -> None:
        with self._lock:
            self._running = False
            self._stop_requested = False
            self._paused = False

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def wait_sync_if_paused(self, interval: float = 0.2) -> bool:
        """Wait synchronously if paused. Returns False if stop requested, True if resumed."""
        while True:
            with self._lock:
                if self._stop_requested:
                    return False
                if not self._paused:
                    return True
            time.sleep(interval)

    async def wait_async_if_paused(self, interval: float = 0.2) -> bool:
        """Wait asynchronously if paused. Returns False if stop requested, True if resumed."""
        while True:
            with self._lock:
                if self._stop_requested:
                    return False
                if not self._paused:
                    return True
            await asyncio.sleep(interval)
