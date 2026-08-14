#!/usr/bin/env python3
"""渊行账户与模拟持仓服务（2026-08-14）。
手机号+密码注册登录（PBKDF2 哈希），模拟开仓/平仓，SQLite 持久化。
端点（与 worker/auth-positions.js 同协议）：
  POST /signup  {phone,password,name}
  POST /login   {phone,password} -> {token,name}
  GET  /me      Authorization: Bearer <token>
  POST /positions/open  {symbol,side,price,lots}
  POST /positions/close {id,price}
运行：python scripts/account_server.py [--port 8790]
存储：D:\\Kimi\\网站维护\\accounts.db
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB = Path(r"D:\Kimi\网站维护\accounts.db")
ORIGIN = "https://wangziquan-del.github.io"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

db = sqlite3.connect(DB, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS users(
  phone TEXT PRIMARY KEY, name TEXT, salt TEXT, hash TEXT, created_at TEXT)""")
db.execute("""CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, phone TEXT, exp INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS positions(
  id TEXT PRIMARY KEY, phone TEXT, symbol TEXT, side TEXT, price REAL, lots INTEGER,
  open INTEGER, opened_at TEXT, close_price REAL, closed_at TEXT, pnl REAL)""")
db.commit()


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()


def valid_phone(p: str) -> bool:
    return bool(re.fullmatch(r"1[3-9]\d{9}", p or ""))


def auth_phone(headers) -> str | None:
    auth = headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return None
    row = db.execute("SELECT phone, exp FROM tokens WHERE token=?", (token,)).fetchone()
    if not row or row[1] < int(time.time()):
        return None
    return row[0]


def positions_of(phone: str) -> list[dict]:
    rows = db.execute(
        "SELECT id,symbol,side,price,lots,open,opened_at,close_price,closed_at,pnl FROM positions WHERE phone=? ORDER BY opened_at DESC",
        (phone,)).fetchall()
    return [dict(zip(("id", "symbol", "side", "price", "lots", "open", "opened_at", "close_price", "closed_at", "pnl"), r)) for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "YAFCO-Account/1.0"

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")

    def send_json(self, body, status=200):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json({"ok": True, "ts": int(time.time())})
        phone = auth_phone(self.headers)
        if not phone:
            return self.send_json({"error": "未登录或登录已过期"}, 401)
        if path == "/me":
            name = db.execute("SELECT name FROM users WHERE phone=?", (phone,)).fetchone()
            return self.send_json({"name": name[0] if name else phone, "phone": phone,
                                   "positions": positions_of(phone)})
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/signup":
            b = self.body()
            phone, password, name = str(b.get("phone", "")), str(b.get("password", "")), str(b.get("name", ""))
            if not valid_phone(phone):
                return self.send_json({"error": "手机号格式不正确"}, 400)
            if len(password) < 6:
                return self.send_json({"error": "密码至少 6 位"}, 400)
            if db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
                return self.send_json({"error": "该手机号已注册，请直接登录"}, 409)
            salt = secrets.token_hex(16)
            name = name[:24] or f"用户{phone[-4:]}"
            db.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                       (phone, name, salt, hash_pw(password, salt), datetime.now().isoformat()))
            db.commit()
            return self.send_json({"ok": True, "name": name})
        if path == "/login":
            b = self.body()
            phone, password = str(b.get("phone", "")), str(b.get("password", ""))
            row = db.execute("SELECT salt, hash, name FROM users WHERE phone=?", (phone,)).fetchone()
            if not row or hash_pw(password, row[0]) != row[1]:
                return self.send_json({"error": "手机号或密码不正确"}, 401)
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO tokens VALUES(?,?,?)", (token, phone, int(time.time()) + 30 * 86400))
            db.commit()
            return self.send_json({"ok": True, "token": token, "name": row[2], "phone": phone})
        phone = auth_phone(self.headers)
        if not phone:
            return self.send_json({"error": "未登录或登录已过期"}, 401)
        if path == "/positions/open":
            b = self.body()
            symbol, side = str(b.get("symbol", "")).upper(), str(b.get("side", ""))
            price, lots = float(b.get("price") or 0), int(b.get("lots") or 1)
            if not re.fullmatch(r"[A-Z]{1,3}", symbol) or side not in ("long", "short") or price <= 0:
                return self.send_json({"error": "参数不正确"}, 400)
            open_count = db.execute("SELECT COUNT(*) FROM positions WHERE phone=? AND open=1", (phone,)).fetchone()[0]
            if open_count >= 20:
                return self.send_json({"error": "持仓上限 20 笔"}, 400)
            pid = secrets.token_hex(8)
            db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,1,?,NULL,NULL,NULL)",
                       (pid, phone, symbol, side, price, max(1, min(lots, 100)), datetime.now().isoformat()))
            db.commit()
            return self.send_json({"ok": True, "positions": positions_of(phone)})
        if path == "/positions/close":
            b = self.body()
            pid, price = str(b.get("id", "")), float(b.get("price") or 0)
            row = db.execute("SELECT side, price, lots FROM positions WHERE id=? AND phone=? AND open=1", (pid, phone)).fetchone()
            if not row:
                return self.send_json({"error": "持仓不存在"}, 404)
            pnl = (1 if row[0] == "long" else -1) * (price - row[1]) * row[2]
            db.execute("UPDATE positions SET open=0, close_price=?, closed_at=?, pnl=? WHERE id=?",
                       (price, datetime.now().isoformat(), pnl, pid))
            db.commit()
            return self.send_json({"ok": True, "positions": positions_of(phone)})
        return self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = 8790
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print(f"渊行账户服务: http://127.0.0.1:{port}  数据库 {DB}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
