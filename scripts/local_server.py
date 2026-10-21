"""Local mode: the kiosk page and the same API, with in-memory adapters (no AWS, no Docker).

    uv run python scripts/local_server.py [--params config/params.example.json] [--port 8080]
        [--retention-seconds N]

Binds to 127.0.0.1 only. A random kiosk token is generated per run and handed to the page through
config.json (local mode only). Download links are HMAC-signed local URLs with the same properties as
presigned URLs (bound to one object, bearer, expiry checked at request time). Expiry is driven by a
background sweep that plays the role of the Step Functions Wait state. Audit events go to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiosk.settings import Settings  # noqa: E402
from kiosk.wiring import LocalStack  # noqa: E402

WEB = ROOT / "web"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
ROUTES = (
    ("GET", ("notice",), "notice"),
    ("POST", ("sessions",), "create"),
    ("POST", ("sessions", None, "photo"), "photo"),
    ("GET", ("sessions", None), "status"),
    ("DELETE", ("sessions", None), "delete"),
)


def make_handler(stack: LocalStack, token: str, port: int):
    svc = stack.service
    token_digest = hashlib.sha256(token.encode()).digest()

    class Handler(BaseHTTPRequestHandler):
        server_version = "kiosk-local"

        def log_message(self, fmt: str, *args) -> None:  # no request lines (they would carry paths)
            return

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode())

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path.startswith("/api/"):
                return self._api(method, url.path[len("/api/"):].strip("/").split("/"))
            if method != "GET":
                return self._json(405, {"error": "method_not_allowed"})
            if url.path == "/share":
                status, data = stack.objects.resolve_share(url.query)
                return self._send(status, data or b"", "image/png" if data else "text/plain")
            if url.path == "/config.json":
                return self._json(200, {"apiUrl": "/api", "region": "local", "kioskId": svc.s.kiosk_id,
                                        "localMode": True, "localKioskToken": token})
            name = "index.html" if url.path in ("", "/") else url.path.lstrip("/")
            path = (WEB / name).resolve()
            if not path.is_file() or WEB.resolve() not in path.parents:
                return self._json(404, {"error": "not_found"})
            self._send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

        def _api(self, method: str, parts: list[str]) -> None:
            supplied = self.headers.get("X-Kiosk-Token", "")
            if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), token_digest):
                svc.audit.emit("kiosk_auth_denied", stage="auth", reason_code="token_mismatch")
                return self._json(401, {"message": "Unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > svc.s.max_upload_bytes * 2 + 4096:
                return self._json(413, {"error": "body_too_large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._json(400, {"error": "body_not_json"})
            secret = self.headers.get("X-Session-Secret", "")
            for m, pattern, name in ROUTES:
                if m != method or len(pattern) != len(parts) or any(p is not None and p != q
                                                                    for p, q in zip(pattern, parts, strict=True)):
                    continue
                sid = parts[1] if len(parts) > 1 else ""
                if name == "notice":
                    result = svc.notice()
                elif name == "create":
                    result = svc.create_session(body)
                elif name == "photo":
                    result = svc.submit_photo(sid, secret, body)
                elif name == "status":
                    result = svc.status(sid, secret)
                else:
                    result = svc.delete(sid, secret)
                return self._json(*result)
            self._json(404, {"error": "route_not_found"})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default=str(ROOT / "config" / "params.example.json"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--retention-seconds", type=int)
    args = ap.parse_args()
    params = json.loads(Path(args.params).read_text())
    if args.retention_seconds:
        params["session"]["retention_seconds"] = args.retention_seconds
        params["session"]["share_link_ttl_seconds"] = min(params["session"]["share_link_ttl_seconds"],
                                                          args.retention_seconds)
    settings = Settings.from_params(params, region="local")
    base = f"http://127.0.0.1:{args.port}/share"
    stack = LocalStack(settings, background=True, share_base_url=base, strict_audit=False)
    stack.audit_sink.lines = _PrintingList()
    token = secrets.token_urlsafe(24)

    def sweep() -> None:
        while True:
            stack.run_due_expiries()
            time.sleep(1)

    threading.Thread(target=sweep, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(stack, token, args.port))
    print(f"kiosk (local mode) on http://127.0.0.1:{args.port}/  retention={settings.retention_seconds}s", flush=True)
    server.serve_forever()


class _PrintingList(list):
    def append(self, line: str) -> None:  # audit lines to stdout as they happen
        print(line, flush=True)
        super().append(line)


if __name__ == "__main__":
    main()
