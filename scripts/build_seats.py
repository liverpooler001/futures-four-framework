#!/usr/bin/env python3
"""持仓分析数据构建 v2（2026-08-13）：国泰君安/永安期货/东证期货 三席位净持仓。
分所通道：
  CZCE  get_rank_table_czce(date)          一键全品种（品种级合计表，券商名带「（代客）」后缀）
  SHFE  get_shfe_rank_table(date, vars)    按合约 dict
  GFEX  futures_gfex_position_rank(date)   按合约 dict
  CFFEX get_cffex_rank_table(date, vars)   按合约 dict
  DCE   新浪 futures_hold_pos_sina         按合约（DCE 官网反爬 412）
  INE   get_shfe_rank_table（含 SC/LU 等，取不到则标缺）
口径：各合约前 20 榜合计，多-空=净持仓；未上榜按 0；近 12 个日历日。
产出：data/seats.json。本地运行后用 _gh_git_push.py 推送。
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

BROKERS = {"国泰君安": "国泰君安", "永安期货": "永安", "东证期货": "东证"}
DAYS_BACK = 12
OUT = ROOT / "data" / "seats.json"

EXCH = {"上期所": "SHFE", "上期能源": "INE", "大商所": "DCE", "郑商所": "CZCE", "广期所": "GFEX", "中金所": "CFFEX"}


def match_broker(name: str) -> str | None:
    n = name.replace("（代客）", "").replace("期货", "").replace("有限公司", "")
    for disp, key in BROKERS.items():
        if key in n:
            return disp
    return None


def agg_frames(frames) -> dict[str, float]:
    """从若干持仓表汇总三席位净持仓（多-空）。"""
    import pandas as pd
    net = {b: 0.0 for b in BROKERS}
    for df in frames:
        if df is None or not len(df):
            continue
        for side_col, party_col, sign in (
            ("long_open_interest", "long_party_name", 1),
            ("short_open_interest", "short_party_name", -1),
        ):
            if side_col not in df.columns or party_col not in df.columns:
                continue
            for _, row in df.iterrows():
                b = match_broker(str(row.get(party_col) or ""))
                if not b:
                    continue
                try:
                    v = float(str(row[side_col]).replace(",", ""))
                except (TypeError, ValueError):
                    v = 0.0
                net[b] += sign * v
    return net


def fetch_day(ak, day: str, by_exch: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    """返回 {symbol: {broker: net}}。"""
    import pandas as pd
    out: dict[str, dict[str, float]] = {}

    def put(sym, net):
        if sym not in out:
            out[sym] = {b: 0.0 for b in BROKERS}
        for b in BROKERS:
            out[sym][b] += net[b]

    if by_exch.get("CZCE"):
        try:
            t = ak.get_rank_table_czce(date=day)
            for sym in by_exch["CZCE"]:
                for key, df in t.items():
                    if key.upper() == sym:
                        put(sym, agg_frames([df]))
                        break
        except Exception as e:
            print(f"  CZCE {day} fail {str(e)[:60]}")
    for exch, func_name in (("SHFE", "get_shfe_rank_table"), ("INE", "get_shfe_rank_table"),
                            ("GFEX", "futures_gfex_position_rank"), ("CFFEX", "get_cffex_rank_table")):
        syms = by_exch.get(exch)
        if not syms:
            continue
        try:
            t = getattr(ak, func_name)(date=day, vars_list=syms)
            for key, df in t.items():
                head = "".join(ch for ch in str(key) if ch.isalpha()).upper()
                if head in syms and len(df):
                    put(head, agg_frames([df]))
        except Exception as e:
            print(f"  {exch} {day} fail {str(e)[:60]}")
    for sym in by_exch.get("DCE", []):
        contract = DCE_CONTRACTS.get(sym, f"{sym.lower()}0")
        for side, col in (("多单持仓", 1), ("空单持仓", -1)):
            try:
                t = ak.futures_hold_pos_sina(symbol=side, date=day, contract=contract)
                if t is None or not len(t):
                    continue
                net = {b: 0.0 for b in BROKERS}
                for _, row in t.iterrows():
                    b = match_broker(str(row.get("会员简称") or ""))
                    if b:
                        try:
                            net[b] += col * float(str(row.iloc[2]).replace(",", ""))
                        except (TypeError, ValueError):
                            pass
                put(sym, net)
            except Exception:
                continue
            time.sleep(0.2)
    return out


def main() -> None:
    import akshare as ak
    market = json.loads((ROOT / "data" / "market.json").read_text(encoding="utf-8"))
    products = market.get("products") or []
    by_exch: dict[str, list[str]] = {}
    for p in products:
        ex = EXCH.get(p.get("exchange") or "")
        if ex:
            by_exch.setdefault(ex, []).append(p["symbol"])
    print({k: len(v) for k, v in by_exch.items()})

    global DCE_CONTRACTS
    DCE_CONTRACTS = {p["symbol"]: str(p.get("contract") or "").lower() for p in products}
    today = date.today()
    days = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(DAYS_BACK, -1, -1)]
    series: dict[str, dict[str, dict[str, float]]] = {}
    for day in days:
        res = fetch_day(ak, day, by_exch)
        for sym, net in res.items():
            if any(abs(v) > 0 for v in net.values()):
                series.setdefault(day, {})[sym] = net
        print(day, "品种数", len(res), flush=True)
        time.sleep(0.4)

    all_days = sorted(series)
    products_out = {}
    for p in products:
        sym = p["symbol"]
        rows = [series[d][sym] for d in all_days if sym in series.get(d, {})]
        if len(rows) < 2:
            continue
        used_days = [d for d in all_days if sym in series.get(d, {})]
        products_out[sym] = {
            "dates": used_days,
            "brokers": {b: [r[b] for r in rows] for b in BROKERS},
        }
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "各合约前20榜合计净持仓（多-空），未上榜按0；CZCE/SHFE/GFEX/CFFEX 交易所官网 + DCE 新浪",
        "products": products_out,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"seats.json: {len(products_out)}/{len(products)} 品种")


if __name__ == "__main__":
    main()
