#!/usr/bin/env python3
"""Build one contradiction and three key metrics for covered products."""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "fundamentals.json"
CACHE = ROOT / "cache" / "fundamentals"
API = "https://zhiji-ai.xyz/commodity/api"
LIBRARY = "https://wangziquan-del.github.io/metals-framework/"

# metric tuple: id, name, unit, source, why it matters
FOCUS: dict[str, dict[str, Any]] = {
    "CU": {"route": "cu", "contradiction": "海外去库支撑高价，但中国现货买盘偏弱；关键看海外去库能否传导成中国补库。", "metrics": [
        ("a10134435", "LME 铜库存", "吨", "SMM", "海外去库是否延续"),
        ("s20015675", "洋山铜溢价", "美元/吨", "SMM", "中国进口买盘是否回归"),
        ("ID00188319", "上海电解铜库存", "万吨", "Mysteel", "国内是否真正补库")]},
    "AL": {"route": "al", "contradiction": "社库下降，但电解铝产量仍升、铝型材开工回落；关键看去库是需求改善还是阶段性提货。", "metrics": [
        ("ID00188307", "电解铝社会库存", "万吨", "Mysteel", "去库主线是否持续"),
        ("a10124317", "中国电解铝产量", "万吨", "SMM", "供应是否继续抬升"),
        ("a10031808", "铝型材开工率", "%", "SMM", "初端需求能否承接")]},
    "PB": {"route": "pb", "contradiction": "国内库存偏紧而海外库存偏高；关键看再生供应恢复后，内外哪一端主导价格。", "metrics": [
        ("a10134441", "LME 铅库存", "吨", "SMM", "海外压力是否缓解"),
        ("FU00015325", "SHFE 铅库存", "吨", "SHFE", "国内紧张程度"),
        ("a10017000", "再生铅开工率", "%", "SMM", "再生供应是否恢复")]},
    "ZN": {"route": "zn", "contradiction": "LME 低库存与现货升水对着国内高社库；关键看海外挤仓继续还是快速退潮。", "metrics": [
        ("a10134450", "LME 锌库存", "吨", "SMM", "海外可交割货源松紧"),
        ("a10097491", "LME 锌 0-3", "美元/吨", "SMM", "挤仓强度是否衰减"),
        ("ID00188329", "国内锌锭社库", "万吨", "Mysteel", "国内过剩能否消化")]},
    "NI": {"route": "ni", "contradiction": "国内交割品累库而价格已在低位；关键看印尼 NPI 是否真正减产并阻断过剩。", "metrics": [
        ("a10018953", "纯镍社会库存", "吨", "SMM", "国内累库是否减速"),
        ("a10193590", "印尼 NPI 产量", "万镍吨", "SMM", "边际供应是否收缩"),
        ("s20019092", "金川镍升贴水", "元/吨", "SMM", "低价下现货是否转紧")]},
    "SN": {"route": "sn", "contradiction": "国内社库回升、TC 持高，但 ICDX 成交萎缩；关键看矿供应恢复能否转成精锡增量。", "metrics": [
        ("ID01517441", "锡锭社会库存", "吨", "Mysteel", "现货紧张是否延续"),
        ("ID01538256", "云南 40% 锡矿 TC", "元/吨", "Mysteel", "矿端松紧是否拐点"),
        ("FU00082529", "ICDX 锡成交量", "吨", "ICDX", "印尼供应链是否正常化")]},
    "LC": {"route": "li", "contradiction": "周产和样本库存下降，但仓单反而回升；关键看去库是真实消费，还是货源向交割库转移。", "metrics": [
        ("a12715547", "碳酸锂周度产量", "吨", "SMM", "供应出清速度"),
        ("a10172022", "碳酸锂样本总库存", "吨", "SMM", "存量过剩消化速度"),
        ("FU00058102", "广期所碳酸锂仓单", "手", "GFEX", "可交割压力是否下降")]},
    "SI": {"route": "si", "contradiction": "供应和仓单继续增加，但下游原料库存也在回升；关键看后者是主动补库还是被动积压。", "metrics": [
        ("FU00050831", "广期所工业硅仓单", "手", "GFEX", "交割压力是否见顶"),
        ("ID01448337", "中国工业硅产量", "吨", "Mysteel", "供应是否真实收缩"),
        ("a12811428", "下游工业硅原料库存", "万吨", "SMM", "下游是否开始补库")]},
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temp.replace(path)


def api_key() -> str:
    key = os.environ.get("ZHIJI_DATA_KEY") or os.environ.get("ZHIJI_GUAN_KEY") or ""
    if key:
        return key.strip()
    config = load_json(ROOT / "config.local.json", {})
    return str(config.get("data_key") or config.get("guan_key") or "").strip()


def parse_day(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def cadence(points: list[tuple[date, float]]) -> tuple[str, int, int]:
    gaps = [(b[0] - a[0]).days for a, b in zip(points[-13:-1], points[-12:]) if b[0] > a[0]]
    gap = statistics.median(gaps) if gaps else 30
    if gap <= 3:
        return "日", 5, 10
    if gap <= 10:
        return "周", 4, 21
    if gap <= 45:
        return "月", 3, 70
    return "季", 1, 150


def fetch_payload(metric_id: str, key: str) -> dict[str, Any]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{metric_id}.json"
    cached = load_json(path, {})
    if path.exists() and time.time() - path.stat().st_mtime < 1800 and cached.get("points"):
        return cached
    if not key:
        if cached.get("points"):
            return cached
        raise RuntimeError("未配置商品数据 API key")
    query = urllib.parse.urlencode({"id": metric_id, "start": "2023-01-01", "end": date.today().isoformat()})
    request = urllib.request.Request(f"{API}/series?{query}", headers={"X-Data-Key": key, "User-Agent": "YAFCO-Fundamental-Focus/1.0"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("points"):
                raise RuntimeError(str(payload.get("error") or "空序列"))
            write_json(path, payload)
            return payload
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(1.2 * (attempt + 1))
    if cached.get("points"):
        return cached
    raise RuntimeError(str(last_error))


def summarize(metric: tuple[str, str, str, str, str], key: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    metric_id, name, unit, source, why = metric
    try:
        payload = fetch_payload(metric_id, key)
        values: dict[date, float] = {}
        for point in payload.get("points") or []:
            day = parse_day(point.get("date"))
            try:
                value = float(point.get("value"))
            except (TypeError, ValueError):
                continue
            if day and math.isfinite(value):
                values[day] = value
        points = sorted(values.items())
        if len(points) < 2:
            raise RuntimeError(f"仅 {len(points)} 个有效点")
        freq, lookback, max_age = cadence(points)
        lookback = min(lookback, len(points) - 1)
        latest_day, latest = points[-1]
        previous_day, previous_value = points[-1 - lookback]
        recent = [abs(value) for _, value in points[-12:] if value != 0]
        scale = max(abs(previous_value), statistics.median(recent or [1.0]), 1e-9)
        return {
            "id": metric_id, "name": name, "unit": unit, "source": source, "why": why,
            "latest": round(latest, 6), "end": latest_day.isoformat(),
            "change_pct": round((latest - previous_value) / scale * 100, 2),
            "comparison": f"较{lookback}{freq}前",
            "stale": (date.today() - latest_day).days > max_age,
            "status": "ok", "points": len(points), "previous_end": previous_day.isoformat(),
        }
    except Exception as error:  # noqa: BLE001
        if previous and previous.get("latest") is not None:
            fallback = dict(previous)
            fallback.update({"status": "fallback", "error": str(error)[:160]})
            return fallback
        return {
            "id": metric_id, "name": name, "unit": unit, "source": source, "why": why,
            "latest": None, "end": None, "change_pct": None, "comparison": "",
            "stale": True, "status": "failed", "error": str(error)[:160],
        }


def build(workers: int) -> dict[str, Any]:
    market = load_json(ROOT / "data" / "market.json", {})
    products = market.get("products") or []
    if not products:
        raise SystemExit("data/market.json is empty")
    old = load_json(OUTPUT, {})
    old_metrics = {
        item.get("id"): item
        for product in old.get("products") or []
        for item in product.get("metrics") or []
        if item.get("id")
    }
    configs = {item[0]: item for config in FOCUS.values() for item in config["metrics"]}
    results: dict[str, dict[str, Any]] = {}
    key = api_key()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = {
            pool.submit(summarize, item, key, old_metrics.get(metric_id)): metric_id
            for metric_id, item in configs.items()
        }
        for future in as_completed(futures):
            metric_id = futures[future]
            results[metric_id] = future.result()
            print(f"[fundamental] {metric_id}: {results[metric_id]['status']}", flush=True)

    output_products = []
    for product in products:
        symbol = str(product.get("symbol") or "").upper()
        config = FOCUS.get(symbol)
        if not config:
            output_products.append({
                "symbol": symbol, "name": product.get("name"), "covered": False,
                "contradiction": "基本面待建库；当前仅展示四框架技术结构。",
                "metrics": [], "library_url": LIBRARY,
            })
            continue
        output_products.append({
            "symbol": symbol, "name": product.get("name"), "covered": True,
            "contradiction": config["contradiction"],
            "metrics": [results[item[0]] for item in config["metrics"]],
            "library_url": f"{LIBRARY}#/c/{config['route']}",
        })

    payload = {
        "schema_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "知几·料（SMM/Mysteel 镜像）",
        "coverage": {"total": len(output_products), "covered": len(FOCUS)},
        "products": output_products,
    }
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if "wk_" in raw or '"score"' in raw or '"consistency"' in raw:
        raise SystemExit("fundamentals output violated security/minimality rules")
    write_json(OUTPUT, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    payload = build(args.workers)
    print(json.dumps({"updated_at": payload["updated_at"], **payload["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
