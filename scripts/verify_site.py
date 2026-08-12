#!/usr/bin/env python3
"""Fail CI when a published site is incomplete or exposes a secret."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "_site"
market = json.loads((SITE / "data" / "market.json").read_text(encoding="utf-8"))
products = market.get("products") or []
assert market.get("total_supported") == 77, market.get("total_supported")
assert market.get("total_published") == 77, market.get("total_published")
assert len(products) == 77, len(products)
assert len({item["symbol"] for item in products}) == 77
for item in products:
    path = SITE / "data" / "symbols" / f"{item['symbol']}.json"
    assert path.is_file(), path
    detail = json.loads(path.read_text(encoding="utf-8"))
    assert set(detail["frameworks"]) == {"ari", "chan", "macd", "gann"}
    assert detail["decision"].get("confidence") in {"完整多周期", "降级观察"}
fundamentals = json.loads((SITE / "data" / "fundamentals.json").read_text(encoding="utf-8"))
focus = fundamentals.get("products") or []
assert fundamentals.get("coverage") == {"total": 77, "covered": 8}
assert len(focus) == 77
covered = [item for item in focus if item.get("covered")]
assert {item["symbol"] for item in covered} == {"AL", "CU", "PB", "ZN", "NI", "SN", "LC", "SI"}
assert all(len(item.get("metrics") or []) == 3 for item in covered)
fundamental_raw = json.dumps(fundamentals, ensure_ascii=False)
assert '"score"' not in fundamental_raw
assert '"consistency"' not in fundamental_raw
assert "fundamentalCard" in (SITE / "index.html").read_text(encoding="utf-8")
for path in SITE.rglob("*"):
    if path.is_file() and path.suffix.lower() in {".html", ".js", ".css", ".json", ".md", ".txt"}:
        assert "wk_" not in path.read_text(encoding="utf-8", errors="ignore"), path
assert (SITE / "assets" / "abyss-voyage-cover.png").stat().st_size > 100_000
print("site verification: 77/77 technical, 8 concise fundamentals, cover and secret scan OK")
