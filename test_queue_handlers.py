"""Reproduce /queue and /queue/add handler failures outside the server."""
import sys, traceback
from pathlib import Path

ROOT = Path(r"C:\TP\URLFinder")
CRAWLER_DIR = ROOT / "crawler"
sys.path.insert(0, str(CRAWLER_DIR))
sys.path.insert(0, str(ROOT))

print("interpreter:", sys.executable)

# --- Hypothesis 1: _queue_page raises (template .format vs JS braces) ---
try:
    import monitor
    page = monitor._queue_page({}, [])
    print("1. _queue_page OK, length:", len(page))
except Exception:
    print("1. _queue_page FAILED:")
    traceback.print_exc()

# --- Hypothesis 2: /queue/add response tuple has no .encode ---
status_line = "HTTP/1.1 200 OK\r\n"
content_type = "Content-Type: application/json; charset=utf-8\r\n"
response = (status_line, content_type, "Content-Length: 5\r\n", "Connection: close\r\n", "\r\n")
try:
    response.encode("utf-8")
    print("2. response.encode OK")
except Exception as exc:
    print(f"2. response.encode FAILED: {type(exc).__name__}: {exc}")

# --- Hypothesis 3: parser fragments comma locations ---
try:
    parts = monitor._parse_queue_input("Testtown, Teststate, USA")
    print("3. _parse_queue_input('Testtown, Teststate, USA') ->", parts)
except Exception:
    print("3. _parse_queue_input FAILED:")
    traceback.print_exc()
