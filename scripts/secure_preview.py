#!/usr/bin/env python3
"""Local preview with server-side timed access control and security headers."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent / "_site"
HASHES = {
    "day": "8473d01df08bbb2e40cd7f0ad5e7c9ba002e3674eb16cbbe261c3c46991d6b9c",
    "night": "dfc1d541e6dbbc1f24d98dde8da2f19bd6fc57565ff43ff04a012a12958966ca",
}
PUBLIC = {
    "/", "/index.html", "/auth.js", "/styles.css", "/mobile.css", "/styles.journal.css", "/sw.js",
    "/manifest.webmanifest", "/robots.txt", "/favicon.ico",
    "/assets/favicon-32.png", "/assets/apple-touch-icon.png",
    "/assets/app-icon-192.png", "/assets/app-icon-512.png",
}
SECRET = secrets.token_bytes(32)
FAILURES: dict[str, list[float]] = {}


def access_window() -> tuple[str, str]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    mode = "maintenance" if now.hour < 8 else "free" if now.hour < 9 else "day" if now.hour < 16 else "night"
    return mode, f"{now:%Y-%m-%d}:{mode}"


def token(slot: str) -> str:
    return f"{slot}.{hmac.new(SECRET, slot.encode(), hashlib.sha256).hexdigest()}"


def valid_cookie(header: str | None, slot: str) -> bool:
    if not header:
        return False
    value = next((part.split("=", 1)[1] for part in header.split(";") if part.strip().startswith("yafco_session=")), "")
    return hmac.compare_digest(value, token(slot))


class Handler(BaseHTTPRequestHandler):
    server_version = "YAFCO-SecurePreview/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def headers_common(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")

    def json(self, status: int, payload: dict[str, object], cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/__auth":
            return self.json(HTTPStatus.NOT_FOUND, {"ok": False})
        mode, slot = access_window()
        if mode == "maintenance":
            return self.json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "维护窗关闭访问"})
        address = self.client_address[0]
        now = time.time()
        recent = [stamp for stamp in FAILURES.get(address, []) if now - stamp < 300]
        FAILURES[address] = recent
        if len(recent) >= 5:
            return self.json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "尝试过多，请稍后再试"})
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1024)
        try:
            password = str(json.loads(self.rfile.read(length)).get("password") or "")
        except Exception:
            password = ""
        digest = hashlib.sha256(password.encode()).hexdigest()
        ok = mode == "free" or hmac.compare_digest(digest, HASHES.get(mode, ""))
        if not ok:
            recent.append(now)
            return self.json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "密码不正确"})
        FAILURES.pop(address, None)
        return self.json(HTTPStatus.OK, {"ok": True, "slot": slot}, f"yafco_session={token(slot)}; HttpOnly; SameSite=Strict; Path=/")

    def do_GET(self) -> None:  # noqa: N802
        raw_path = urlparse(self.path).path
        mode, slot = access_window()
        if raw_path not in PUBLIC and (mode == "maintenance" or (mode != "free" and not valid_cookie(self.headers.get("Cookie"), slot))):
            return self.json(HTTPStatus.SERVICE_UNAVAILABLE if mode == "maintenance" else HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "access denied"})
        relative = "index.html" if raw_path in {"/", "/index.html"} else unquote(raw_path).lstrip("/")
        target = (ROOT / relative).resolve()
        try:
            target.relative_to(ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.headers_common()
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    if not ROOT.is_dir():
        raise SystemExit("_site 不存在，请先构建")
    print(f"YAFCO secure preview: http://127.0.0.1:{args.port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
