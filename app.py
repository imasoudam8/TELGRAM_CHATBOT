"""Anonymous Telegram Matching & Chat Bot - Developer CLI & Health Server.

This script serves as the persistent local development runtime entrypoint.
Because the bot interfaces exclusively via the Telegram Bot API (aiogram),
this entrypoint provides a lightweight HTTP health and diagnostic server
on port 3000 to satisfy environment health checks while documenting the
Telegram-native architecture.
"""

import http.server
import json
import os
import signal
import sys
from http import HTTPStatus

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anonymous Telegram Matching Bot - Backend</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      margin: 0;
      padding: 2rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: light-dark(#f8fafc, #0d1117);
      color: light-dark(#0f172a, #e6edf3);
    }
    .card {
      max-width: 720px;
      margin: 2rem auto;
      background: light-dark(#ffffff, #161b22);
      border: 1px solid light-dark(#e2e8f0, #30363d);
      border-radius: 8px;
      padding: 2rem;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    h1 { margin-top: 0; font-size: 1.4rem; color: #38bdf8; }
    .badge {
      display: inline-block;
      padding: 0.25rem 0.6rem;
      border-radius: 4px;
      font-size: 0.8rem;
      font-weight: 600;
      background: #238636;
      color: #ffffff;
      margin-bottom: 1rem;
    }
    pre {
      background: light-dark(#f1f5f9, #090d13);
      border: 1px solid light-dark(#cbd5e1, #21262d);
      border-radius: 6px;
      padding: 1rem;
      overflow-x: auto;
      font-size: 0.9rem;
      line-height: 1.5;
    }
    .note {
      padding: 0.75rem 1rem;
      border-left: 4px solid #38bdf8;
      background: light-dark(#f0f9ff, #0e1e2f);
      margin: 1rem 0;
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">BACKEND ACTIVE &middot; PORT 3000</div>
    <h1>Anonymous Telegram Matching &amp; Chat Bot</h1>
    <div class="note">
      <strong>Telegram-Native Architecture:</strong> User interactions, matchmaking,
      and administration occur exclusively inside Telegram via the Telegram Bot API.
      This server provides health check status and developer diagnostics.
    </div>
    <pre>
Engine:       Python 3.12+ (aiogram 3.31+)
Database:     PostgreSQL 16 (SQLAlchemy 2.x Async &amp; asyncpg)
Migrations:   Alembic 1.14+
Cache &amp; FSM:  Redis 7
Interface:    Telegram Bot API (No Web Frontend)

Operational Commands:
  - Run migrations:      alembic upgrade head
  - Start bot polling:   python -m src.app.main
  - Dependency check:    python -m src.app.infrastructure.healthcheck
  - Run test suite:      pytest -v
  - Ruff code check:     ruff check . &amp;&amp; ruff format --check .
    </pre>
  </div>
</body>
</html>
"""


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server with explicit address reuse enabled."""

    allow_reuse_address = True


class HealthAndDiagnosticHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler supporting GET, HEAD, and JSON health endpoints."""

    def do_HEAD(self) -> None:
        """Respond to HEAD requests with HTTP 200 OK."""
        encoded_body = HTML_TEMPLATE.encode("utf-8")
        self._send_headers(content_type="text/html; charset=utf-8", content_len=len(encoded_body))

    def do_GET(self) -> None:
        """Respond to GET requests with HTML console or JSON health check."""
        if self.path in ("/health", "/healthz", "/api/health"):
            payload = json.dumps({"status": "healthy", "service": "telegram-matching-bot"}).encode(
                "utf-8"
            )
            self._send_headers(content_type="application/json", content_len=len(payload))
            self.wfile.write(payload)
            return

        body = HTML_TEMPLATE.encode("utf-8")
        self._send_headers(content_type="text/html; charset=utf-8", content_len=len(body))
        self.wfile.write(body)

    def _send_headers(self, content_type: str, content_len: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_len))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Suppress noisy request logging to keep stdout clean."""
        pass


def _handle_sigterm(_signum: int, _frame: object) -> None:
    """Exit cleanly on SIGTERM so the listening socket closes immediately."""
    sys.exit(0)


def main() -> None:
    """Run persistent HTTP diagnostic server on configured PORT."""
    signal.signal(signal.SIGTERM, _handle_sigterm)
    port = int(os.environ.get("PORT") or "3000")
    host = os.environ.get("HOST") or "0.0.0.0"

    server = ReusableThreadingHTTPServer((host, port), HealthAndDiagnosticHandler)
    server.daemon_threads = True
    print(f"Anonymous Telegram Matching Bot dev server listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("Dev server shutting down gracefully.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
