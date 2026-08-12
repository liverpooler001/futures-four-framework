#!/usr/bin/env python3
"""Fast quote-only refresh for the static GitHub Pages site."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import engine
from build_all import DATA_DIR, atomic_json, materialize_site, security_scan


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    started = time.perf_counter()
    market_path = DATA_DIR / "market.json"
    market = read_json(market_path)
    products = market.get("products", [])
    symbols = [item["product"] for item in products if item.get("product")]

    quotes = engine.get_quote(",".join(symbols))
    quote_map = {str(item.get("product", "")).upper(): item for item in quotes}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    fresh = 0

    for item in products:
        symbol = str(item.get("product", "")).upper()
        quote = quote_map.get(symbol)
        if not quote:
            item["stale"] = True
            continue

        fresh += 1
        item.update({
            "contract": quote.get("symbol") or item.get("contract"),
            "last": quote.get("last"),
            "change_pct": quote.get("change_pct"),
            "quote_time": quote.get("time") or now,
            "updated_at": now,
            "stale": False,
        })

        detail_path = DATA_DIR / "symbols" / f"{symbol}.json"
        if not detail_path.exists():
            continue
        detail = read_json(detail_path)
        detail["quote"] = quote
        detail["updated_at"] = now
        detail["snapshot_status"] = "fresh"
        detail_market = detail.setdefault("market", {})
        detail_market["open_interest"] = quote.get("open_interest")
        detail_market["volume"] = quote.get("volume")
        atomic_json(detail_path, detail)

    market["products"] = products
    market["updated_at"] = now
    market["source"] = "zhiji guan quote / GitHub Actions fast refresh"
    market["fresh_count"] = fresh
    market["stale_count"] = len(products) - fresh
    market["build_seconds"] = round(time.perf_counter() - started, 2)
    atomic_json(market_path, market)

    security_scan(DATA_DIR)
    materialize_site()
    print(f"Fast quote refresh: {fresh}/{len(products)} fresh at {now}")


if __name__ == "__main__":
    main()
