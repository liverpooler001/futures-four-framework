#!/usr/bin/env python3
"""Build all-products static snapshots for GitHub Pages."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
from build_fundamentals import build as build_fundamentals  # noqa: E402

DATA_DIR = ROOT / "data"
SYMBOL_DIR = DATA_DIR / "symbols"
SITE_DIR = ROOT / "_site"


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temp.replace(path)


def summary_of(detail: dict[str, Any], stale: bool = False) -> dict[str, Any]:
    product = detail["product"]
    quote = detail["quote"]
    frameworks = detail["frameworks"]
    decision = detail["decision"]
    return {
        "symbol": product["product"],
        "name": product["name"],
        "sector": product.get("sector") or "其他",
        "exchange": product.get("exch_cn") or product.get("exch") or "—",
        "contract": str(quote.get("symbol") or "").upper(),
        "last": quote.get("last"),
        "change_pct": quote.get("change_pct"),
        "quote_time": quote.get("time"),
        "updated_at": detail.get("updated_at"),
        "analysis_updated_at": detail.get("analysis_updated_at") or detail.get("updated_at"),
        "score": decision["score"],
        "bias": decision["bias"],
        "tone": decision["tone"],
        "ari": frameworks["ari"]["environment"],
        "ari_score": frameworks["ari"]["score"],
        "chan": frameworks["chan"]["signal"],
        "chan_score": frameworks["chan"]["score"],
        "macd": frameworks["macd"]["summary"],
        "macd_score": frameworks["macd"]["score"],
        "gann": frameworks["gann"]["line_relation"],
        "gann_score": frameworks["gann"]["score"],
        "data_mode": detail.get("decision", {}).get("confidence", "完整多周期"),
        "data_note": detail.get("decision", {}).get("quality_note", ""),
        "support": decision["support"],
        "resistance": decision["resistance"],
        "long_trigger": decision["long"]["trigger"],
        "short_trigger": decision["short"]["trigger"],
        "left_long": decision.get("left_long"),
        "left_short": decision.get("left_short"),
        "stale": stale,
    }


def load_previous(symbol: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = SYMBOL_DIR / f"{symbol}.json"
    if not path.exists():
        return None, None
    try:
        detail = json.loads(path.read_text(encoding="utf-8"))
        return detail, summary_of(detail, stale=True)
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None, None


def build_one(
    symbol: str,
    product: dict[str, Any],
    quote: dict[str, Any] | None,
    force_daily_proxy: bool = False,
    attempts: int = 2,
) -> tuple[str, dict[str, Any] | None, str | None]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            detail = engine.build_dashboard(
                symbol,
                product_override=product,
                quote_override=quote,
                force_daily_proxy=force_daily_proxy,
            )
            detail["static_snapshot"] = True
            detail["snapshot_status"] = "fresh"
            raw = json.dumps(detail, ensure_ascii=False, allow_nan=False)
            if "wk_" in raw:
                raise RuntimeError("security check: API key appeared in snapshot")
            atomic_json(SYMBOL_DIR / f"{symbol}.json", detail)
            return symbol, summary_of(detail), None
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(1.2 * (attempt + 1))
    previous, previous_summary = load_previous(symbol)
    if previous is not None and previous_summary is not None:
        previous["snapshot_status"] = "stale"
        previous["snapshot_error"] = str(last_error)[:240]
        atomic_json(SYMBOL_DIR / f"{symbol}.json", previous)
        return symbol, previous_summary, f"沿用旧快照：{last_error}"
    return symbol, None, str(last_error)


def materialize_site() -> None:
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)
    for name in ("index.html", "app.js", "auth.js", "styles.css", "mobile.css", "manifest.webmanifest", "sw.js", "styles.journal.css", "robots.txt", "404.html", ".nojekyll"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, SITE_DIR / name)
    shutil.copytree(ROOT / "assets", SITE_DIR / "assets")
    shutil.copytree(ROOT / "data", SITE_DIR / "data")


def security_scan(directory: Path) -> None:
    text_ext = {".html", ".js", ".css", ".json", ".md", ".txt", ".xml"}
    offenders = []
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in text_ext:
            try:
                if "wk_" in path.read_text(encoding="utf-8", errors="ignore"):
                    offenders.append(str(path.relative_to(directory)))
            except OSError:
                continue
    if offenders:
        raise SystemExit(f"security scan failed; API key pattern in: {offenders}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", help="comma-separated subset")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--daily-proxy",
        action="store_true",
        help="skip unreliable minute endpoints and publish labelled daily proxies",
    )
    parser.add_argument("--no-site", action="store_true")
    parser.add_argument("--skip-fundamentals", action="store_true", help="reuse existing fundamentals during intraday strategy rebuilds")
    args = parser.parse_args()

    products = engine.get_products()
    product_map = {str(product["product"]).upper(): product for product in products}
    codes = list(product_map)
    if args.symbols:
        wanted = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
        codes = [code for code in codes if code in wanted]
    started = time.time()
    summaries: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    quote_map: dict[str, dict[str, Any]] = {}
    try:
        for quote in engine.get_quote(",".join(codes), ttl=5):
            code = str(quote.get("product") or "").upper()
            if not code:
                code = "".join(
                    char
                    for char in str(quote.get("symbol") or "")
                    if char.isalpha()
                ).upper()
            if code:
                quote_map[code] = quote
    except engine.DashboardError as exc:
        print(
            f"[quotes] batch request unavailable, using per-product requests: {exc}",
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {
            pool.submit(
                build_one,
                code,
                product_map[code],
                quote_map.get(code),
                args.daily_proxy,
            ): code
            for code in codes
        }
        for future in as_completed(futures):
            code, summary, error = future.result()
            if summary:
                summaries[code] = summary
                label = "STALE" if summary.get("stale") else "OK"
            else:
                label = "FAILED"
            if error:
                failures[code] = error
            print(f"[{len(summaries):02d}/{len(codes):02d}] {code:<4} {label} {error or ''}", flush=True)

    # Preserve out-of-scope summaries in subset builds.
    old_market = DATA_DIR / "market.json"
    if args.symbols and old_market.exists():
        try:
            for item in json.loads(old_market.read_text(encoding="utf-8")).get("products", []):
                summaries.setdefault(item["symbol"], item)
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    ordered = sorted(summaries.values(), key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
    fresh = sum(not item.get("stale") for item in ordered)
    market_updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    market = {
        "updated_at": market_updated_at,
        "analysis_updated_at": market_updated_at,
        "source": "知几·观｜GitHub Actions 静态快照",
        "total_supported": len(products),
        "total_published": len(ordered),
        "fresh": fresh,
        "stale": len(ordered) - fresh,
        "failures": failures,
        "build_seconds": round(time.time() - started, 1),
        "products": ordered,
        "method": "Ari 30% + 缠论 25% + MACD 25% + 江恩 20%；方向看日线与60分钟，15分钟仅作入场确认",
    }
    atomic_json(DATA_DIR / "market.json", market)
    if not args.skip_fundamentals:
        build_fundamentals(max(1, min(args.workers, 8)))
    if not args.no_site:
        materialize_site()
        security_scan(SITE_DIR)
    security_scan(DATA_DIR)
    print(json.dumps({key: market[key] for key in ("updated_at", "total_supported", "total_published", "fresh", "stale", "build_seconds")}, ensure_ascii=False))
    if len(ordered) < len(products):
        raise SystemExit(f"only published {len(ordered)}/{len(products)} products")


if __name__ == "__main__":
    main()
