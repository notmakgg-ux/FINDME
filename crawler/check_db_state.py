"""Check current database state."""
import sys
sys.path.insert(0, ".")

from storage.db import _get_connection

conn = _get_connection()
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM company_details")
print("company_details rows:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM contact")
print("contact rows:", cur.fetchone()[0])
cur.execute("SELECT location, COUNT(*) FROM company_details GROUP BY location")
for row in cur.fetchall():
    print(f"  location={row[0]!r}: {row[1]}")
cur.close()
conn.close()
print("DONE")