import socket
import threading
import time
import json

HTML_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>test</title>
<body style="background:#fff;color:#111;padding:24px;font-family:Segoe UI,sans-serif">
<h1>Smoke test</h1><p>If you see this the socket layer works.</p>
</body></html>""".encode("utf-8")

POLL_JSON = json.dumps({"time": "ok", "pids_alive": True, "pids": [99],
                        "stage": "url_finder", "location": "Atlanta",
                        "details": "test", "event": "ok"}).encode("utf-8")


def handle(conn):
    try:
        data = conn.recv(8192).decode("utf-8", errors="replace")
    except Exception:
        conn.close()
        return
    if not data:
        conn.close()
        return
    request_line = data.splitlines()[0]
    parts = request_line.split()
    if len(parts) < 2:
        conn.close()
        return
    path = parts[1]
    if path in ("/", "/index.html"):
        body = HTML_PAGE
        ct = b"text/html; charset=utf-8"
    elif path == "/poll":
        body = POLL_JSON
        ct = b"application/json; charset=utf-8"
    else:
        body = b"Not Found\n"
        ct = b"text/plain; charset=utf-8"
    resp = (b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: " + ct + b"\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n")
    conn.sendall(resp + body)
    conn.close()


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 8010))
    s.listen(5)
    print("SMOKE TEST listening on :8010")
    while True:
        try:
            conn, addr = s.accept()
        except socket.timeout:
            continue
        t = threading.Thread(target=handle, args=(conn,), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
