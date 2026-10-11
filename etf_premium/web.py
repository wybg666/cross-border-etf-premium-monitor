"""Loopback-only HTTP server with a fixed, read-only route allowlist."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import core


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/cross-border-etf-premium.html": ("index.html", "text/html; charset=utf-8"),
    "/cross-border-etf-premium.js": ("cross-border-etf-premium.js", "text/javascript; charset=utf-8"),
    "/cross-border-etf-premium.css": ("cross-border-etf-premium.css", "text/css; charset=utf-8"),
}


class PremiumHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class PremiumRequestHandler(BaseHTTPRequestHandler):
    server_version = "ETF-Premium-Monitor/0.1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def _send_bytes(self, status: int, body: bytes, content_type: str, *, allow: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: object, *, allow: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", allow=allow)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # The parser also uses this path for malformed/unsupported requests.
        self.close_connection = True
        self._send_json(code, {"ok": False, "error": HTTPStatus(code).phrase})

    def _valid_host(self) -> bool:
        # Binding to loopback alone does not prevent DNS-rebinding requests.
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        return len(hosts) == 1 and hosts[0].lower() in allowed

    def do_GET(self) -> None:
        if not self._valid_host():
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Host header"})
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid request path"})
            return
        if target.scheme or target.netloc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Absolute URLs are not supported"})
            return
        path = target.path
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "etf-premium-monitor", "dataSource": core.SOURCE_NAME, "apiVersion": "tencent-20261010"})
            return
        if path == "/api/cross-border-etf-premium":
            try:
                query = parse_qs(target.query, max_num_fields=20)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid query"})
                return
            try:
                payload = core.build_cross_border_etf_premium_payload(
                    force_refresh=(query.get("force", [""])[0] == "1" or query.get("refresh", [""])[0] == "1"),
                    allow_stale=False,
                )
                self._send_json(HTTPStatus.OK, payload)
            except Exception:
                # No low-level exception/URL/configuration details in HTTP responses.
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "行情暂不可用，请稍后重试"})
            return
        static = STATIC_ROUTES.get(path)
        if static is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        filename, content_type = static
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Static asset not found"})
            return
        self._send_bytes(HTTPStatus.OK, body, content_type)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "Method not allowed"}, allow="GET")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_HEAD = _method_not_allowed


def create_server(port: int = DEFAULT_PORT) -> PremiumHTTPServer:
    """Create an unstarted loopback server. Use port=0 for an OS-selected port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    return PremiumHTTPServer((HOST, port), PremiumRequestHandler)
