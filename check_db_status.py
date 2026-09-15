import sys, pathlib

PROJECT_DIR = pathlib.Path.cwd().parent
if PROJECT_DIR.name != 'URLFinder':
    PROJECT_DIR = pathlib.Path.cwd()
CRAWLER_DIR = PROJECT_DIR / 'crawler'
sys.path.insert(0, str(CRAWLER_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from storage.db import _get_connection, get_queue_items, get_stats

print('=== DB connection test ===')
try:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 AS test")
    row = cur.fetchone()
    print('DB connection OK:', row)
    cur.close()
    conn.close()
except Exception as e:
    print('DB connection FAILED:', e)

print()
print('=== Queue entries (pipeline_queue) ===')
try:
    items = get_queue_items()
    print('Queue items found:', len(items))
    for it in items:
        print(f"  queue_id={it['queue_id']} | {it['location']} | status={it['status']} | priority={it.get('priority','')}")
except Exception as e:
    print('Queue read FAILED:', e)

print()
print('=== company_details counts ===')
try:
    stats = get_stats()
    print('Stats keys:', list(stats.keys()) if stats else 'none')
    for loc, counts in (stats or {}).items():
        print(f"  {loc}: {counts}")
except Exception as e:
    print('Stats read FAILED:', e)
