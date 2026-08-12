#!/usr/bin/env python3
"""Local futures four-framework decision dashboard.

The browser only talks to this local server. The Zhiji API key is read from
config.local.json or ZHIJI_GUAN_KEY and is never sent to the browser.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"
CACHE_DIR = ROOT / "cache"
API_BASE = "https://zhiji-ai.xyz/guan/api"
ALLOWED_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class DashboardError(RuntimeError):
    pass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def round_tick(value: float, tick: float, mode: str = "nearest") -> float:
    if not tick:
        return round(value, 4)
    ratio = value / tick
    if mode == "up":
        ratio = math.ceil(ratio - 1e-10)
    elif mode == "down":
        ratio = math.floor(ratio + 1e-10)
    else:
        ratio = round(ratio)
    return round(ratio * tick, 6)


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardError(f"本地配置读取失败：{exc}") from exc
    config["guan_key"] = os.environ.get("ZHIJI_GUAN_KEY", "").strip() or str(
        config.get("guan_key", "")
    ).strip()
    if not config["guan_key"]:
        raise DashboardError("缺少知几行情密钥，请配置 config.local.json 的 guan_key。")
    return config


@dataclass
class CacheItem:
    expires_at: float
    value: Any


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, CacheItem] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, ttl: float, builder: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item and item.expires_at > now:
                return item.value
        value = builder()
        with self._lock:
            self._data[key] = CacheItem(now + ttl, value)
        return value


CACHE = TTLCache()


def api_get(path: str, params: dict[str, Any], ttl: float) -> dict[str, Any]:
    config = load_config()
    clean_params = {key: value for key, value in params.items() if value is not None}
    cache_key = path + "?" + urlencode(sorted(clean_params.items()))
    disk_path = CACHE_DIR / f"{hashlib.sha256(cache_key.encode('utf-8')).hexdigest()}.json"

    def fetch() -> dict[str, Any]:
        url = API_BASE + path + "?" + urlencode(clean_params)
        request = Request(
            url,
            headers={
                "X-Guan-Key": config["guan_key"],
                "User-Agent": "YAFCO-Futures-Decision-Dashboard/1.0",
                "Accept": "application/json",
            },
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DashboardError("行情接口返回格式异常")
                # Empty K-line responses are transient upstream failures. Do not
                # overwrite a previously usable cache file with an empty payload.
                if path == "/kline" and not (payload.get("bars") or []):
                    detail = payload.get("error") or "no data"
                    raise DashboardError(f"行情接口暂未返回K线：{detail}")
                CACHE_DIR.mkdir(exist_ok=True)
                disk_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise DashboardError(f"行情接口 HTTP {exc.code}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
        raise DashboardError(f"行情接口暂时不可用：{last_error}")

    try:
        return CACHE.get_or_set(cache_key, ttl, fetch)
    except DashboardError:
        if disk_path.exists():
            try:
                stale = json.loads(disk_path.read_text(encoding="utf-8"))
                if isinstance(stale, dict) and (path != "/kline" or (stale.get("bars") or [])):
                    stale["_dashboard_stale"] = True
                    return stale
            except (OSError, json.JSONDecodeError):
                pass
        raise


def get_products() -> list[dict[str, Any]]:
    products = api_get("/products", {}, 12 * 3600).get("products") or []
    return [item for item in products if isinstance(item, dict) and item.get("product")]


def get_quote(symbols: str, ttl: float = 8) -> list[dict[str, Any]]:
    return api_get("/quote", {"symbols": symbols}, ttl).get("quotes") or []


def normalize_bar(item: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = str(item.get("time") or item.get("date") or item.get("datetime") or "")
    open_ = finite(item.get("open"))
    high = finite(item.get("high"))
    low = finite(item.get("low"))
    close = finite(item.get("close"))
    if not timestamp or None in (open_, high, low, close):
        return None
    return {
        "time": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": finite(item.get("volume"), 0.0),
        "open_interest": finite(item.get("open_interest")),
        "settle": finite(item.get("settle")),
    }


def get_bars(symbol: str, freq: str, limit: int, ttl: float) -> list[dict[str, Any]]:
    raw = api_get(
        "/kline",
        {"symbol": symbol, "freq": freq, "cont": 1, "limit": limit},
        ttl,
    ).get("bars") or []
    bars = [bar for bar in (normalize_bar(item) for item in raw) if bar]
    bars.sort(key=lambda item: item["time"])
    if len(bars) < 30:
        raise DashboardError(f"{symbol} {freq} K线不足（仅 {len(bars)} 根）")
    return bars


def merge_live_daily(
    daily: list[dict[str, Any]], intraday: list[dict[str, Any]], quote: dict[str, Any]
) -> list[dict[str, Any]]:
    if not intraday:
        return daily
    day = intraday[-1]["time"][:10]
    today = [bar for bar in intraday if bar["time"][:10] == day]
    if not today:
        return daily
    live = {
        "time": day,
        "open": today[0]["open"],
        "high": max(bar["high"] for bar in today),
        "low": min(bar["low"] for bar in today),
        "close": finite(quote.get("last"), today[-1]["close"]),
        "volume": sum(bar.get("volume") or 0 for bar in today),
        "open_interest": finite(quote.get("open_interest"), today[-1].get("open_interest")),
        "settle": None,
        "live": True,
    }
    merged = list(daily)
    if merged and merged[-1]["time"][:10] == day:
        merged[-1] = live
    else:
        merged.append(live)
    return merged


def merge_live_weekly(weekly: list[dict[str, Any]], daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not daily:
        return weekly
    current_day = datetime.fromisoformat(daily[-1]["time"][:10]).date()
    monday = current_day - timedelta(days=current_day.weekday())
    week_days = [
        bar
        for bar in daily
        if datetime.fromisoformat(bar["time"][:10]).date() >= monday
    ]
    if not week_days:
        return weekly
    live = {
        "time": current_day.isoformat(),
        "open": week_days[0]["open"],
        "high": max(bar["high"] for bar in week_days),
        "low": min(bar["low"] for bar in week_days),
        "close": week_days[-1]["close"],
        "volume": sum(bar.get("volume") or 0 for bar in week_days),
        "open_interest": week_days[-1].get("open_interest"),
        "settle": None,
        "live": True,
    }
    merged = list(weekly)
    if merged:
        last_date = datetime.fromisoformat(merged[-1]["time"][:10]).date()
        if last_date >= monday:
            merged[-1] = live
        else:
            merged.append(live)
    return merged


def resample_daily_to_weekly(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weeks: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for bar in daily:
        date = datetime.fromisoformat(bar['time'][:10]).date()
        iso = date.isocalendar()
        weeks.setdefault((iso.year, iso.week), []).append(bar)
    output: list[dict[str, Any]] = []
    for group in weeks.values():
        output.append({
            'time': group[-1]['time'][:10],
            'open': group[0]['open'],
            'high': max(bar['high'] for bar in group),
            'low': min(bar['low'] for bar in group),
            'close': group[-1]['close'],
            'volume': sum(bar.get('volume') or 0 for bar in group),
            'open_interest': group[-1].get('open_interest'),
            'settle': group[-1].get('settle'),
            'live': bool(group[-1].get('live')),
        })
    return output


def daily_proxy_frame(daily: list[dict[str, Any]], frame: str) -> list[dict[str, Any]]:
    """Use official daily bars as a clearly labelled intraday proxy."""
    output: list[dict[str, Any]] = []
    for item in daily:
        bar = dict(item)
        bar["proxy_frame"] = frame
        output.append(bar)
    return output


def bars_are_same(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    """Detect the upstream bug that returns daily bars for minute requests."""
    if len(left) < 30 or len(right) < 30:
        return False
    sample_left = left[-30:]
    sample_right = right[-30:]
    return all(
        a["time"] == b["time"]
        and a["open"] == b["open"]
        and a["high"] == b["high"]
        and a["low"] == b["low"]
        and a["close"] == b["close"]
        for a, b in zip(sample_left, sample_right)
    )


def sma(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            output[index] = running / period
    return output


def ema(values: list[float], period: int) -> list[float]:
    factor = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * factor + output[-1] * (1 - factor))
    return output


def macd(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    fast = ema(values, 12)
    slow = ema(values, 26)
    dif = [left - right for left, right in zip(fast, slow)]
    dea = ema(dif, 9)
    hist = [(left - right) * 2 for left, right in zip(dif, dea)]
    return dif, dea, hist


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        result[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def atr_series(bars: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    trs: list[float] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1]["close"] if index else bar["close"]
        trs.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous), abs(bar["low"] - previous)))
    output: list[float | None] = [None] * len(bars)
    if len(trs) < period:
        return output
    average = sum(trs[:period]) / period
    output[period - 1] = average
    for index in range(period, len(trs)):
        average = (average * (period - 1) + trs[index]) / period
        output[index] = average
    return output


def frame_metrics(name: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [bar["close"] for bar in bars]
    ma5, ma10, ma20, ma60 = (sma(closes, period) for period in (5, 10, 20, 60))
    dif, dea, hist = macd(closes)
    rsi = rsi_series(closes)
    atr = atr_series(bars)
    last = len(bars) - 1
    window = bars[-20:]
    range_low = min(bar["low"] for bar in window)
    range_high = max(bar["high"] for bar in window)
    position = (closes[-1] - range_low) / max(range_high - range_low, 1e-9)
    score = 0.0
    if ma20[-1] is not None:
        score += 1.5 if closes[-1] > ma20[-1] else -1.5
    if ma5[-1] is not None and ma20[-1] is not None:
        score += 1.0 if ma5[-1] > ma20[-1] else -1.0
    if len(ma20) > 6 and ma20[-1] is not None and ma20[-6] is not None:
        score += 0.75 if ma20[-1] > ma20[-6] else -0.75
    score += 1.0 if hist[-1] >= 0 else -1.0
    score += 0.5 if hist[-1] >= hist[-2] else -0.5
    if rsi[-1] is not None:
        score += 0.5 if rsi[-1] >= 55 else (-0.5 if rsi[-1] <= 45 else 0)
    if position >= 0.8:
        score += 0.75
    elif position <= 0.2:
        score -= 0.75
    return {
        "name": name,
        "time": bars[-1]["time"],
        "price": closes[-1],
        "ma5": ma5[-1],
        "ma10": ma10[-1],
        "ma20": ma20[-1],
        "ma60": ma60[-1],
        "ma20_slope": None if ma20[-6] is None else ma20[-1] - ma20[-6],
        "dif": dif[-1],
        "dea": dea[-1],
        "hist": hist[-1],
        "hist_prev": hist[-2],
        "rsi": rsi[-1],
        "atr": atr[-1] or (range_high - range_low) / 10,
        "range_low": range_low,
        "range_high": range_high,
        "position": position,
        "score": round(score, 2),
        "macd_state": macd_state(dif, dea, hist),
        "trend": "偏多" if score >= 1.5 else ("偏空" if score <= -1.5 else "震荡"),
    }


def macd_state(dif: list[float], dea: list[float], hist: list[float]) -> str:
    if dif[-1] >= dea[-1]:
        return "零上金叉" if dif[-1] >= 0 else "零下金叉"
    return "零下死叉" if dif[-1] < 0 else "零上死叉"


def find_fractals(bars: list[dict[str, Any]], window: int = 2) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for index in range(window, len(bars) - window):
        group = bars[index - window : index + window + 1]
        bar = bars[index]
        if bar["high"] == max(item["high"] for item in group) and sum(
            item["high"] == bar["high"] for item in group
        ) == 1:
            raw.append({"index": index, "kind": "top", "price": bar["high"], "time": bar["time"]})
        if bar["low"] == min(item["low"] for item in group) and sum(
            item["low"] == bar["low"] for item in group
        ) == 1:
            raw.append({"index": index, "kind": "bottom", "price": bar["low"], "time": bar["time"]})
    raw.sort(key=lambda item: (item["index"], 0 if item["kind"] == "bottom" else 1))
    pivots: list[dict[str, Any]] = []
    for point in raw:
        if not pivots:
            pivots.append(point)
            continue
        previous = pivots[-1]
        if point["kind"] == previous["kind"]:
            more_extreme = point["price"] > previous["price"] if point["kind"] == "top" else point["price"] < previous["price"]
            if more_extreme:
                pivots[-1] = point
        elif point["index"] - previous["index"] >= 3:
            pivots.append(point)
    return pivots


def chan_analysis(bars: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    pivots = find_fractals(bars)
    recent = pivots[-10:]
    zone: dict[str, float] | None = None
    if len(pivots) >= 4:
        for start in range(len(pivots) - 4, -1, -1):
            segment = pivots[start : start + 4]
            stroke_lows = [min(segment[i]["price"], segment[i + 1]["price"]) for i in range(3)]
            stroke_highs = [max(segment[i]["price"], segment[i + 1]["price"]) for i in range(3)]
            lower, upper = max(stroke_lows), min(stroke_highs)
            if lower < upper:
                zone = {"low": lower, "high": upper}
                break
    last_pen = "无有效笔"
    score = 0.0
    divergence = "未见明确背驰"
    if len(pivots) >= 2:
        last_pen = "向上笔" if pivots[-1]["kind"] == "top" else "向下笔"
        score += 1.5 if last_pen == "向上笔" else -1.5
    price = metrics["price"]
    structure = "中枢内"
    if zone:
        if price > zone["high"]:
            structure = "中枢上方"
            score += 1.0
        elif price < zone["low"]:
            structure = "中枢下方"
            score -= 1.0
    else:
        position = metrics["position"]
        structure = "上部结构" if position >= 0.75 else ("下部结构" if position <= 0.25 else "区间内部")
        score += 0.5 if position >= 0.75 else (-0.5 if position <= 0.25 else 0)
    if len(pivots) >= 4:
        same_kind = [point for point in pivots if point["kind"] == pivots[-1]["kind"]]
        if len(same_kind) >= 2:
            previous, current = same_kind[-2], same_kind[-1]
            if current["kind"] == "top" and current["price"] > previous["price"] and metrics["hist"] < metrics["hist_prev"]:
                divergence = "疑似顶背驰"
                score -= 1.0
            elif current["kind"] == "bottom" and current["price"] < previous["price"] and metrics["hist"] > metrics["hist_prev"]:
                divergence = "疑似底背驰"
                score += 1.0
    signal = "三买观察" if structure == "中枢上方" else ("三卖观察" if structure == "中枢下方" else "等待离开中枢")
    return {
        "score": round(score, 2),
        "tone": "up" if score >= 1 else ("down" if score <= -1 else "neutral"),
        "last_pen": last_pen,
        "structure": structure,
        "zone": zone,
        "divergence": divergence,
        "signal": signal,
        "pivots": recent,
        "note": "分型—笔—三笔重叠中枢的程序化简化口径",
    }


def ari_analysis(frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
    daily, hourly, minute = frames["D"], frames["60"], frames["15"]
    score = daily["score"] * 0.6 + hourly["score"] * 0.3 + minute["score"] * 0.1
    above_line = daily["price"] >= daily["ma20"]
    if score >= 1.5 and above_line:
        environment, tone = "GREEN · 多头主场", "up"
    elif score <= -1.5 and not above_line:
        environment, tone = "RED · 空头主场", "down"
    else:
        environment, tone = "YELLOW · 过渡区", "neutral"
    rsi = minute["rsi"] or 50
    if rsi >= 75:
        momentum = "短线过热，等回踩确认"
    elif rsi <= 25:
        momentum = "短线过冷，避免追空"
    elif score >= 1.5:
        momentum = "多头动量扩张"
    elif score <= -1.5:
        momentum = "空头动量扩张"
    else:
        momentum = "动量过渡，等待方向"
    return {
        "score": round(score, 2),
        "tone": tone,
        "environment": environment,
        "line": daily["ma20"],
        "line_relation": "多空线上方" if above_line else "多空线下方",
        "momentum": momentum,
        "rsi15": minute["rsi"],
        "rsi60": hourly["rsi"],
        "rsiD": daily["rsi"],
        "discipline": "顺环境交易；过渡区减仓，短周期过热不追价",
    }


def macd_framework(frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
    weights = {"D": 0.5, "60": 0.3, "15": 0.2}
    score = 0.0
    details = []
    for key in ("D", "60", "15"):
        frame = frames[key]
        part = (1.5 if frame["hist"] >= 0 else -1.5) + (
            0.5 if frame["hist"] >= frame["hist_prev"] else -0.5
        )
        score += part * weights[key]
        details.append(
            {
                "frame": key,
                "state": frame["macd_state"],
                "dif": frame["dif"],
                "dea": frame["dea"],
                "hist": frame["hist"],
                "direction": "增强" if frame["hist"] >= frame["hist_prev"] else "减弱",
            }
        )
    return {
        "score": round(score, 2),
        "tone": "up" if score >= 0.8 else ("down" if score <= -0.8 else "neutral"),
        "summary": "多级别偏多" if score >= 0.8 else ("多级别偏空" if score <= -0.8 else "多级别分歧"),
        "details": details,
    }


def gann_analysis(bars: list[dict[str, Any]], metrics: dict[str, Any], tick: float) -> dict[str, Any]:
    lookback = bars[-120:] if len(bars) >= 120 else bars
    low_index = min(range(len(lookback)), key=lambda i: lookback[i]["low"])
    high_index = max(range(len(lookback)), key=lambda i: lookback[i]["high"])
    low, high = lookback[low_index]["low"], lookback[high_index]["high"]
    direction = "上升摆动" if low_index < high_index else "下降摆动"
    span = max(high - low, tick * 8)
    levels = [round_tick(low + span * step / 8, tick) for step in range(-2, 11)]
    price = metrics["price"]
    support = max((level for level in levels if level <= price), default=levels[0])
    resistance = min((level for level in levels if level >= price), default=levels[-1])
    position = (price - low) / span
    score = clamp((position - 0.5) * 4, -2.0, 2.0)
    if direction == "上升摆动":
        score += 0.5
    else:
        score -= 0.5
    anchor_index = low_index if direction == "上升摆动" else high_index
    opposite_index = high_index if direction == "上升摆动" else low_index
    elapsed = max(abs(opposite_index - anchor_index), 1)
    slope = span / elapsed
    bars_after_anchor = len(lookback) - 1 - anchor_index
    line_1x1 = low + slope * bars_after_anchor if direction == "上升摆动" else high - slope * bars_after_anchor
    line_1x1 = round_tick(line_1x1, tick)
    days_from_anchor = (len(lookback) - 1 - anchor_index)
    next_window = next((value for value in (7, 14, 21, 28, 45, 60, 90, 120) if value > days_from_anchor), None)
    return {
        "score": round(score, 2),
        "tone": "up" if score >= 0.75 else ("down" if score <= -0.75 else "neutral"),
        "direction": direction,
        "swing_low": low,
        "swing_high": high,
        "anchor_time": lookback[anchor_index]["time"],
        "levels": levels,
        "support": support,
        "resistance": resistance,
        "position_eighth": round(position * 8, 2),
        "line_1x1": line_1x1,
        "line_relation": "1×1线上方" if price >= line_1x1 else "1×1线下方",
        "next_time_window": None if next_window is None else f"锚点后第 {next_window} 根日K附近",
        "note": "近120根日K有效摆动区间；八分位与1×1速度线",
    }


def unique_levels(values: list[float | None], price: float, threshold: float) -> list[float]:
    levels = sorted(value for value in values if value is not None and math.isfinite(value))
    clustered: list[float] = []
    for value in levels:
        if not clustered or abs(value - clustered[-1]) > threshold:
            clustered.append(value)
        else:
            clustered[-1] = (clustered[-1] + value) / 2
    return clustered


def decision_plan(
    product: dict[str, Any],
    quote: dict[str, Any],
    frames: dict[str, dict[str, Any]],
    frameworks: dict[str, dict[str, Any]],
    bars: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    price = finite(quote.get("last"), frames["15"]["price"]) or frames["15"]["price"]
    tick = finite(product.get("tick"), 1.0) or 1.0
    atr_d = frames["D"]["atr"]
    atr_60 = frames["60"]["atr"]
    threshold = max(tick * 4, atr_60 * 0.18, price * 0.001)
    chan_d = frameworks["chan"]
    gann = frameworks["gann"]
    pivots = chan_d.get("pivots") or []
    candidates: list[float | None] = [
        frames["D"]["ma5"], frames["D"]["ma20"], frames["D"]["ma60"],
        frames["60"]["ma5"], frames["60"]["ma20"],
        frames["D"]["range_low"], frames["D"]["range_high"],
        frames["60"]["range_low"], frames["60"]["range_high"],
        gann["support"], gann["resistance"], *gann["levels"],
        *(point["price"] for point in pivots),
    ]
    if chan_d.get("zone"):
        candidates.extend([chan_d["zone"]["low"], chan_d["zone"]["high"]])
    levels = unique_levels(candidates, price, threshold)
    supports = [value for value in levels if value < price - tick]
    resistances = [value for value in levels if value > price + tick]
    support1 = supports[-1] if supports else price - atr_d
    support2 = supports[-2] if len(supports) >= 2 else support1 - atr_d * 0.7
    resistance1 = resistances[0] if resistances else price + atr_d
    resistance2 = resistances[1] if len(resistances) >= 2 else resistance1 + atr_d * 0.7
    buffer = max(tick * 2, round_tick(atr_60 * 0.08, tick, "up"))
    pullback_width = max(tick * 2, round_tick(atr_60 * 0.10, tick, "up"))
    long_trigger = round_tick(resistance1 + buffer, tick, "up")
    short_trigger = round_tick(support1 - buffer, tick, "down")
    long_entry_low = round_tick(resistance1 - pullback_width, tick, "down")
    long_entry_high = round_tick(resistance1 + pullback_width, tick, "up")
    short_entry_low = round_tick(support1 - pullback_width, tick, "down")
    short_entry_high = round_tick(support1 + pullback_width, tick, "up")
    long_stop = round_tick(min(support1 - buffer, long_entry_low - atr_60 * 0.55), tick, "down")
    short_stop = round_tick(max(resistance1 + buffer, short_entry_high + atr_60 * 0.55), tick, "up")
    long_target1 = round_tick(max(resistance2, long_entry_high + (long_entry_high - long_stop) * 1.3), tick, "up")
    long_target2 = round_tick(long_entry_high + (long_entry_high - long_stop) * 2.2, tick, "up")
    short_target1 = round_tick(min(support2, short_entry_low - (short_stop - short_entry_low) * 1.3), tick, "down")
    short_target2 = round_tick(short_entry_low - (short_stop - short_entry_low) * 2.2, tick, "down")
    composite = (
        frameworks["ari"]["score"] * 0.30
        + frameworks["chan"]["score"] * 0.25
        + frameworks["macd"]["score"] * 0.25
        + frameworks["gann"]["score"] * 0.20
    )
    rsi15 = frames["15"]["rsi"] or 50
    if composite >= 1.2:
        bias, tone = "偏多", "up"
    elif composite <= -1.2:
        bias, tone = "偏空", "down"
    else:
        bias, tone = "震荡/等待", "neutral"
    long_priority = "优先观察" if composite > 0 else "次选"
    short_priority = "优先观察" if composite < 0 else "次选"
    if rsi15 >= 72:
        long_priority = "不追价·等回踩"
    if rsi15 <= 28:
        short_priority = "不追价·等反抽"
    long_status = "已触发待回踩" if price >= long_trigger else "未触发"
    short_status = "已触发待反抽" if price <= short_trigger else "未触发"
    return {
        "score": round(composite, 2),
        "bias": bias,
        "tone": tone,
        "support": [round_tick(support1, tick), round_tick(support2, tick)],
        "resistance": [round_tick(resistance1, tick), round_tick(resistance2, tick)],
        "long": {
            "priority": long_priority,
            "status": long_status,
            "trigger": long_trigger,
            "trigger_text": f"60分钟收盘站上 {format_price(long_trigger, tick)}，随后回踩不破",
            "entry": [long_entry_low, long_entry_high],
            "stop": long_stop,
            "targets": [long_target1, long_target2],
            "invalid": f"60分钟重新跌破 {format_price(long_stop, tick)}",
            "confirmation": "突破不是买点本身；回踩确认后才执行",
        },
        "short": {
            "priority": short_priority,
            "status": short_status,
            "trigger": short_trigger,
            "trigger_text": f"60分钟收盘跌破 {format_price(short_trigger, tick)}，随后反抽不过",
            "entry": [short_entry_low, short_entry_high],
            "stop": short_stop,
            "targets": [short_target1, short_target2],
            "invalid": f"60分钟重新站上 {format_price(short_stop, tick)}",
            "confirmation": "破位不是卖点本身；反抽确认后才执行",
        },
        "risk": {
            "multiplier": finite(product.get("mult"), 1.0),
            "tick": tick,
            "long_risk_per_lot": abs((sum([long_entry_low, long_entry_high]) / 2 - long_stop) * (finite(product.get("mult"), 1.0) or 1.0)),
            "short_risk_per_lot": abs((short_stop - sum([short_entry_low, short_entry_high]) / 2) * (finite(product.get("mult"), 1.0) or 1.0)),
        },
    }


def format_price(value: float, tick: float) -> str:
    decimals = max(0, min(4, len(str(tick).split(".")[1].rstrip("0")) if "." in str(tick) else 0))
    return f"{value:,.{decimals}f}"


def chart_payload(bars: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    selected = bars[-limit:]
    closes = [bar["close"] for bar in selected]
    result = {
        "bars": selected,
        "ma5": sma(closes, 5),
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
    }
    return result


def curve_quotes(product_code: str, lead_symbol: str) -> list[dict[str, Any]]:
    now = datetime.now()
    candidates: list[str] = []
    for offset in range(-1, 13):
        month_index = now.year * 12 + now.month - 1 + offset
        year, month_zero = divmod(month_index, 12)
        candidates.append(f"{product_code.lower()}{str(year)[-2:]}{month_zero + 1:02d}")
    if lead_symbol:
        candidates.insert(0, lead_symbol.lower())
    unique = list(dict.fromkeys(candidates))
    try:
        quotes = get_quote(",".join(unique), ttl=45)
    except DashboardError:
        return []
    clean = []
    for quote in quotes:
        last = finite(quote.get("last"))
        if last is None:
            continue
        clean.append(
            {
                "symbol": quote.get("symbol"),
                "last": last,
                "open_interest": finite(quote.get("open_interest"), 0),
                "volume": finite(quote.get("volume"), 0),
                "is_lead": bool(quote.get("is_lead")) or str(quote.get("symbol", "")).lower() == lead_symbol.lower(),
            }
        )
    clean.sort(key=lambda item: item["symbol"])
    return clean


def build_dashboard(
    symbol: str,
    product_override: dict[str, Any] | None = None,
    quote_override: dict[str, Any] | None = None,
    force_daily_proxy: bool = False,
) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    products = [] if product_override else get_products()
    product = product_override or next(
        (item for item in products if str(item.get("product", "")).upper() == symbol),
        None,
    )
    if product is None:
        raise DashboardError(f"不支持品种 {symbol}")
    if quote_override is None:
        quotes = get_quote(symbol)
        if not quotes:
            raise DashboardError(f"{symbol} 暂无实时行情")
        quote = quotes[0]
    else:
        quote = quote_override
    frame_errors: dict[str, str] = {}

    def optional_intraday(freq: str, ttl: float) -> list[dict[str, Any]] | None:
        if force_daily_proxy:
            frame_errors[freq] = "静态构建安全模式：分钟源待恢复"
            return None
        try:
            return get_bars(symbol, freq, 300, ttl)
        except DashboardError as exc:
            frame_errors[freq] = str(exc)
            return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            'D': executor.submit(get_bars, symbol, 'D', 300, 240),
            '60': executor.submit(optional_intraday, '60', 25),
            '15': executor.submit(optional_intraday, '15', 20),
        }
        daily_raw = futures['D'].result()
        hourly_raw = futures['60'].result()
        minute_raw = futures['15'].result()
    if hourly_raw and bars_are_same(daily_raw, hourly_raw):
        frame_errors["60"] = "上游周期串线：60分钟返回了日线"
        hourly_raw = None
    if minute_raw and bars_are_same(daily_raw, minute_raw):
        frame_errors["15"] = "上游周期串线：15分钟返回了日线"
        minute_raw = None
    daily = merge_live_daily(daily_raw, minute_raw or [], quote)
    hourly = hourly_raw or daily_proxy_frame(daily, "60")
    minute = minute_raw or daily_proxy_frame(daily, "15")
    weekly = resample_daily_to_weekly(daily)
    bars = {"D": daily, "W": weekly, "60": hourly, "15": minute}
    frames = {key: frame_metrics(key, bars[key]) for key in ("W", "D", "60", "15")}
    chan_d = chan_analysis(daily, frames["D"])
    chan_60 = chan_analysis(hourly, frames["60"])
    chan_15 = chan_analysis(minute, frames["15"])
    chan_score = chan_d["score"] * 0.55 + chan_60["score"] * 0.30 + chan_15["score"] * 0.15
    chan = dict(chan_d)
    chan["score"] = round(chan_score, 2)
    chan["tone"] = "up" if chan_score >= 1 else ("down" if chan_score <= -1 else "neutral")
    chan["frames"] = {
        "D": {key: chan_d[key] for key in ("last_pen", "structure", "divergence", "signal")},
        "60": {key: chan_60[key] for key in ("last_pen", "structure", "divergence", "signal")},
        "15": {key: chan_15[key] for key in ("last_pen", "structure", "divergence", "signal")},
    }
    frameworks = {
        "ari": ari_analysis(frames),
        "chan": chan,
        "macd": macd_framework(frames),
        "gann": gann_analysis(daily, frames["D"], finite(product.get("tick"), 1.0) or 1.0),
    }
    decision = decision_plan(product, quote, frames, frameworks, bars)
    data_quality = {
        "D": {"mode": "official", "label": "真实日线", "bars": len(daily)},
        "60": {
            "mode": "official" if hourly_raw else "daily_proxy",
            "label": "真实60分钟" if hourly_raw else "日线代理·60分钟待恢复",
            "bars": len(hourly),
            "error": frame_errors.get("60"),
        },
        "15": {
            "mode": "official" if minute_raw else "daily_proxy",
            "label": "真实15分钟" if minute_raw else "日线代理·15分钟待恢复",
            "bars": len(minute),
            "error": frame_errors.get("15"),
        },
    }
    proxy_frames = [
        key for key in ("60", "15") if data_quality[key]["mode"] != "official"
    ]
    if proxy_frames:
        proxy_label = "、".join(proxy_frames)
        decision["confidence"] = "降级观察"
        decision["quality_note"] = (
            f"{proxy_label}分钟源暂缺，触发位由真实日线代理计算，须盘中复核"
        )
        tick = finite(product.get("tick"), 1.0) or 1.0
        decision["long"]["trigger_text"] = (
            f"日线代理条件：收盘站上 {format_price(decision['long']['trigger'], tick)}；"
            "恢复分钟线后再确认"
        )
        decision["short"]["trigger_text"] = (
            f"日线代理条件：收盘跌破 {format_price(decision['short']['trigger'], tick)}；"
            "恢复分钟线后再确认"
        )
    else:
        decision["confidence"] = "完整多周期"
        decision["quality_note"] = "日线、60分钟、15分钟均为真实行情"
    daily_oi = [bar for bar in daily if bar.get("open_interest") is not None]
    oi_change = None
    if len(daily_oi) >= 2:
        oi_change = daily_oi[-1]["open_interest"] - daily_oi[-2]["open_interest"]
    return {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "知几·观（实时行情与K线）",
        "data_quality": data_quality,
        "product": product,
        "quote": quote,
        "frames": frames,
        "frameworks": frameworks,
        "decision": decision,
        "market": {
            "open_interest": finite(quote.get("open_interest")),
            "oi_change": oi_change,
            "volume": finite(quote.get("volume")),
            "curve": [],
            "curve_deferred": True,
            "seat_data": None,
            "seat_note": "当前知几只读接口不含交易所会员席位排名，暂无真源，不生成模拟数据。",
        },
        "charts": {
            "D": chart_payload(daily, 150),
            "60": chart_payload(hourly, 180),
            "15": chart_payload(minute, 180),
        },
        "method": {
            "weights": "综合判断：Ari 30% + 缠论 25% + MACD 25% + 江恩 20%",
            "timeframes": "方向看日线×60分钟；15分钟只做入场确认",
            "disclaimer": "程序化技术分析属于概率信号，不构成投资建议。",
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FuturesDecisionDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self.send_json({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
                return
            if parsed.path == "/api/products":
                self.send_json({"products": get_products()})
                return
            if parsed.path == "/api/dashboard":
                symbol = parse_qs(parsed.query).get("symbol", ["LC"])[0]
                self.send_json(build_dashboard(symbol))
                return
            if parsed.path in ALLOWED_STATIC:
                filename, content_type = ALLOWED_STATIC[parsed.path]
                body = (ROOT / filename).read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except DashboardError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 502)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"服务端异常：{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="期货四框架决策面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()
    load_config()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"期货四框架决策面板已启动：{url}")
    print("按 Ctrl+C 停止。API 密钥仅在本地后端使用。")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
