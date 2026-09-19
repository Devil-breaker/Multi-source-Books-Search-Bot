#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local Vercel dev server -- simulates Vercel's Python runtime for testing.
Starts a local HTTP server on port 3000 and mimics the Vercel request/response format.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, errors="replace")
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, errors="replace")

from http.server import HTTPServer, BaseHTTPRequestHandler

# Import the bot
from src.bot import get_bot, TELEGRAM_BOT_TOKEN


class VercelRequest:
    """Mimics the Vercel Python request object."""
    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path.split("?")[0].rstrip("/")
        self.query = path.split("?")[1] if "?" in path else ""
        self.headers = headers  # lowercase keys
        self.body = body  # bytes


class VercelResponse:
    """Mimics the Vercel Python response object."""
    def __init__(self):
        self.status = 200
        self._headers = {}
        self._body = ""

    def json(self, data):
        self._headers["Content-Type"] = "application/json"
        self._body = json.dumps(data, ensure_ascii=False)
        return self

    def send(self, body, status=200):
        self.status = status
        self._body = str(body)
        return self


def normalize_response(response):
    """Convert dict or VercelResponse to (status, headers_dict, body_bytes)."""
    if isinstance(response, dict):
        status = response.get("statusCode", 200)
        body = response.get("body", "")
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        return status, response.get("headers", {}), body
    else:
        status = response.status
        body = getattr(response, "_body", "") or ""
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        headers = getattr(response, "_headers", {}) or {}
        return status, headers, body


class VercelHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests the way Vercel Python runtime does."""

    def _parse_headers(self):
        headers = {}
        for key, val in self.headers.items():
            headers[key.lower()] = val
        return headers

    def _send_response(self, response):
        """Send HTTP response. Handles both dict and VercelResponse."""
        status, headers, body = normalize_response(response)
        self.send_response(status)
        for key, val in headers.items():
            self.send_header(key, val)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Telegram-Bot-Api-Secret-Token, X-Vercel-Cron")
        self.end_headers()

    def do_GET(self):
        req = VercelRequest("GET", self.path, self._parse_headers(), b"")
        self._route(req)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        req = VercelRequest("POST", self.path, self._parse_headers(), body)
        self._route(req)

    def _route(self, req):
        """Route request to the appropriate handler."""
        path = req.path

        if path == "/api/webhook":
            self._handle_webhook(req)
        elif path == "/api/cron":
            self._handle_cron(req)
        elif path in ("/", ""):
            self._send_response({"statusCode": 200, "body": json.dumps(
                {"status": "ok", "service": "vercel-bot", "version": "1.0.0"}, ensure_ascii=False
            )})
        elif path in ("/api/health", "/health"):
            self._send_response({"statusCode": 200, "body": json.dumps(
                {"status": "ok", "message": "Bot is running"}, ensure_ascii=False
            )})
        else:
            self._send_response({"statusCode": 404, "body": json.dumps(
                {"error": f"Not Found: {path}"}, ensure_ascii=False
            )})

    def _handle_webhook(self, req):
        """Handle Telegram webhook -- calls api/webhook.py handler."""
        from api.webhook import handler
        result = handler(req)
        self._send_response(result)

    def _handle_cron(self, req):
        """Handle cron -- calls api/cron.py handler."""
        from api.cron import handler
        result = handler(req)
        self._send_response(result)

    def log_message(self, fmt, *args):
        print(f"  [{self.log_date_time_string()}] {fmt % args}")


def main():
    port = 3000
    print("=" * 50)
    print("Vercel Bot - Local Test Server")
    print("=" * 50)

    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN not set in .env")
        print()

    # Initialize bot (loads env + creates singleton)
    bot = get_bot()
    preview = TELEGRAM_BOT_TOKEN[:12] if TELEGRAM_BOT_TOKEN else "NO TOKEN"
    print(f"Bot initialized: {preview}...")

    server = HTTPServer(("localhost", port), VercelHandler)
    print(f"Server running at http://localhost:{port}")
    print()
    print("  Endpoints:")
    print(f"    GET  http://localhost:{port}/             -> health check")
    print(f"    POST http://localhost:{port}/api/webhook -> Telegram webhook")
    print(f"    POST http://localhost:{port}/api/cron    -> cron (X-Vercel-Cron header)")
    print()
    print("  NOTE: Telegram won't send updates to localhost.")
    print("        Expose with: npx ngrok http 3000")
    print()
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()