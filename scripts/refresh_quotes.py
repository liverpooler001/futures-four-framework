#!/usr/bin/env python3
"""Fast resilient quote refresh for the static GitHub Pages site."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import engine
from build_all import DATA_DIR, atomic_json, materialize_site, security_scan

QUOTE_BATCH_SIZE = 20
MIN_FRESH_RATIO = 0.90
DEPLOYED_DATA = "https://wangziquan-del.github.io/futures-four-framework/data"


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_deployed_json(relative: str) -> dict | None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    try:
        url = f"{DEPLOYED_DATA}/{relative}?t={int(time.time())}"
        request = urllib.request.Request(url, headers={"User-Agent": "yuanxing-refresh/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as exc:  # noqa: BLE001
        print(f"[baseline] {relative}: {exc}", file=sys.stderr)
        return None


def hydrate_latest_strategy_baseline(market: dict) -> dict:
    """Continue from the deployed strategy snapshot instead of the repo's old JSON."""
    remote = fetch_deployed_json("market.json")
    if not remote or len(remote.get("products") or []) != len(market.get("products") or []):
        return market
    if str(remote.get("analysis_updated_at") or "") <= str(market.get("analysis_updated_at") or ""):
        return market
    products = remote.get("products") or []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(fetch_deployed_json, f"symbols/{product_code(item)}.json"): product_code(item)
            for item in products
        }
        hydrated = 0
        for future in as_completed(futures):
            symbol = futures[future]
            detail = future.result()
            if detail and detail.get("analysis_updated_at"):
                atomic_json(DATA_DIR / "symbols" / f"{symbol}.json", detail)
                hydrated += 1
    if hydrated < int(len(products) * MIN_FRESH_RATIO):
        print(f"[baseline] only hydrated {hydrated}/{len(products)} details; using repo baseline", file=sys.stderr)
        return market
    print(f"[baseline] hydrated deployed strategies: {hydrated}/{len(products)}")
    return remote


def product_code(item: dict) -> str:
    """Market summaries use `symbol`; older snapshots may still use `product`."""
    return str(item.get("symbol") or item.get("product") or "").upper()


def quote_code(item: dict) -> str:
    code = str(item.get("product") or "").upper()
    if code:
        return code
    return "".join(char for char in str(item.get("symbol") or "") if char.isalpha()).upper()


def fetch_quotes(symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Use bounded batches, then retry every missing product individually."""
    found: dict[str, dict] = {}
    errors: list[str] = []
    for start in range(0, len(symbols), QUOTE_BATCH_SIZE):
        batch = symbols[start : start + QUOTE_BATCH_SIZE]
        try:
            for quote in engine.get_quote(",".join(batch), ttl=0):
                code = quote_code(quote)
                if code:
                    found[code] = quote
        except Exception as exc:  # noqa: BLE001
            errors.append(f"batch {','.join(batch)}: {exc}")
    for symbol in (item for item in symbols if item not in found):
        try:
            quotes = engine.get_quote(symbol, ttl=0)
            if quotes:
                found[symbol] = quotes[0]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
    return found, errors


def refresh_candidate_status(plan: dict, price: float | None) -> None:
    if price is None:
        return
    for side in ("left_long", "left_short"):
        candidate = plan.get(side)
        if not candidate:
            continue
        low, high = candidate.get("zone") or (None, None)
        stop = candidate.get("stop")
        if low is None or high is None:
            continue
        invalid = price <= stop if side == "left_long" else price >= stop
        candidate["status"] = "已失效" if invalid else ("进入候选区" if low <= price <= high else "等待进入候选区")


def main() -> None:
    started = time.perf_counter()
    market_path = DATA_DIR / "market.json"
    market = hydrate_latest_strategy_baseline(read_json(market_path))
    products = market.get("products", [])
    symbols = [code for item in products if (code := product_code(item))]
    if not symbols:
        raise SystemExit("quote refresh aborted: market.json contains no product symbols")
    quote_map, errors = fetch_quotes(symbols)
    coverage = len(set(symbols) & set(quote_map)) / max(1, len(symbols))
    if coverage < MIN_FRESH_RATIO:
        raise SystemExit(
            f"quote refresh rejected: only {len(quote_map)}/{len(symbols)} fresh; old site remains live"
        )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    fresh = 0

    for item in products:
        symbol = product_code(item)
        quote = quote_map.get(symbol)
        if not quote:
            item["stale"] = True
            item["refresh_error"] = "本轮报价缺失，保留上一笔真值"
            continue

        fresh += 1
        item.update({
            "contract": quote.get("symbol") or item.get("contract"),
            "last": quote.get("last"),
            "change_pct": quote.get("change_pct"),
            "quote_time": quote.get("time") or now,
            "updated_at": now,
            "quote_received_at": now,
            "quote_source": quote.get("source") or "知几·观",
            "stale": False,
            "refresh_error": None,
        })

        detail_path = DATA_DIR / "symbols" / f"{symbol}.json"
        if not detail_path.exists():
            continue
        detail = read_json(detail_path)
        detail.setdefault("analysis_updated_at", detail.get("updated_at"))
        detail["quote"] = quote
        detail["updated_at"] = now
        detail["quote_received_at"] = now
        detail["snapshot_status"] = "fresh"
        refresh_candidate_status(detail.get("decision", {}), quote.get("last"))
        for plan in (detail.get("strategies") or {}).values():
            refresh_candidate_status(plan, quote.get("last"))
        detail_market = detail.setdefault("market", {})
        detail_market["open_interest"] = quote.get("open_interest")
        detail_market["volume"] = quote.get("volume")
        atomic_json(detail_path, detail)

    market["products"] = products
    market["updated_at"] = now
    market["source"] = "zhiji guan quote / GitHub Actions fast refresh"
    market["fresh"] = fresh
    market["stale"] = len(products) - fresh
    market["refresh_errors"] = errors[-20:]
    market["build_seconds"] = round(time.perf_counter() - started, 2)
    atomic_json(market_path, market)

    security_scan(DATA_DIR)
    materialize_site()
    print(f"Fast quote refresh: {fresh}/{len(products)} fresh at {now}")


if __name__ == "__main__":
    main()
