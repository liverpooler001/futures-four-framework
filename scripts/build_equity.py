#!/usr/bin/env python3
"""品种相关权益篮子构建（2026-08-14）：商品↔股票联动。
数据：新浪实时行情（一次批量）+ 日线（逐股，算 5 日/20 日变动与篮子 60 日归一化序列）。
产出：data/equity.json {symbol: {stocks:[{code,name,price,chg,chg5,chg20}], basket:[60日归一化...], updated}}。
本地跑（CI 访问新浪不稳）；与席位日更同一节奏。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import re
import urllib.request

# 品种 → 相关股票篮子（代码, 名称）——按产业链对应关系人工配置
EQUITY_MAP = {
 "CU": [("sh600362", "江西铜业"), ("sh601899", "紫金矿业"), ("sz000630", "铜陵有色"), ("sz000878", "云南铜业")],
 "AL": [("sh601600", "中国铝业"), ("sz000933", "神火股份"), ("sz000807", "云铝股份"), ("sz002532", "天山铝业")],
 "ZN": [("sz000060", "中金岭南"), ("sh600497", "驰宏锌锗"), ("sh600961", "株冶集团"), ("sh601168", "西部矿业")],
 "PB": [("sz000060", "中金岭南"), ("sh600497", "驰宏锌锗"), ("sh600338", "西藏珠峰")],
 "NI": [("sh603799", "华友钴业"), ("sz002340", "格林美"), ("sz300919", "中伟股份")],
 "SN": [("sz000960", "锡业股份"), ("sh600301", "华锡有色"), ("sz000426", "兴业银锡")],
 "LC": [("sz002460", "赣锋锂业"), ("sz002466", "天齐锂业"), ("sz000792", "盐湖股份"), ("sz002738", "中矿资源")],
 "SI": [("sh603260", "合盛硅业"), ("sh600438", "通威股份"), ("sh688303", "大全能源")],
 "PS": [("sh600438", "通威股份"), ("sh688303", "大全能源"), ("sz002129", "TCL中环")],
 "AU": [("sh600547", "山东黄金"), ("sh600489", "中金黄金"), ("sh600988", "赤峰黄金"), ("sz000975", "山金国际")],
 "AG": [("sz000426", "兴业银锡"), ("sz000603", "盛达资源"), ("sh600988", "赤峰黄金")],
 "RB": [("sh600019", "宝钢股份"), ("sz000932", "华菱钢铁"), ("sh600507", "方大特钢")],
 "HC": [("sh600019", "宝钢股份"), ("sz000932", "华菱钢铁"), ("sh600782", "新钢股份")],
 "WR": [("sh600019", "宝钢股份"), ("sh600782", "新钢股份")],
 "SS": [("sz000825", "太钢不锈"), ("sh603995", "甬金股份")],
 "I": [("sz000923", "河钢资源"), ("sh601969", "海南矿业")],
 "J": [("sh601666", "平煤股份"), ("sh600985", "淮北矿业"), ("sz000723", "美锦能源")],
 "JM": [("sz000983", "山西焦煤"), ("sh601666", "平煤股份"), ("sh600985", "淮北矿业")],
 "SF": [("sh600295", "鄂尔多斯"), ("sh601216", "君正集团")],
 "SM": [("sh600295", "鄂尔多斯"), ("sh601216", "君正集团")],
 "ZC": [("sh601088", "中国神华"), ("sh601225", "陕西煤业")],
 "SC": [("sh601857", "中国石油"), ("sh600028", "中国石化"), ("sh601808", "中海油服")],
 "FU": [("sh601857", "中国石油"), ("sh600028", "中国石化")],
 "LU": [("sh601857", "中国石油"), ("sh600028", "中国石化")],
 "BU": [("sh600028", "中国石化"), ("sz002221", "东华能源")],
 "TA": [("sh600346", "恒力石化"), ("sz002493", "荣盛石化"), ("sh601233", "桐昆股份"), ("sh603225", "新凤鸣")],
 "PX": [("sh600346", "恒力石化"), ("sz002493", "荣盛石化"), ("sz000301", "东方盛虹")],
 "PF": [("sh601233", "桐昆股份"), ("sh603225", "新凤鸣")],
 "PR": [("sh601233", "桐昆股份"), ("sz000301", "东方盛虹")],
 "MA": [("sh600989", "宝丰能源"), ("sh600426", "华鲁恒升")],
 "UR": [("sh600426", "华鲁恒升"), ("sh600096", "云天化")],
 "SA": [("sz000683", "远兴能源"), ("sz000822", "山东海化")],
 "SH": [("sh601678", "滨化股份"), ("sh600618", "氯碱化工")],
 "V": [("sz002092", "中泰化学"), ("sh600075", "新疆天业")],
 "PP": [("sz002221", "东华能源"), ("sz002648", "卫星化学")],
 "L": [("sz002648", "卫星化学"), ("sz002221", "东华能源")],
 "EB": [("sz002648", "卫星化学"), ("sh600346", "恒力石化")],
 "EG": [("sz002648", "卫星化学"), ("sh600989", "宝丰能源")],
 "FG": [("sh601636", "旗滨集团"), ("sz000012", "南玻A")],
 "RU": [("sh601118", "海南橡胶"), ("sh601058", "赛轮轮胎")],
 "NR": [("sh601118", "海南橡胶"), ("sh601058", "赛轮轮胎")],
 "BR": [("sh601058", "赛轮轮胎"), ("sh601966", "玲珑轮胎")],
 "M": [("sz300999", "金龙鱼"), ("sz000505", "京粮控股")],
 "Y": [("sz300999", "金龙鱼"), ("sz002852", "道道全")],
 "P": [("sz300999", "金龙鱼"), ("sz002852", "道道全")],
 "RM": [("sz300999", "金龙鱼"), ("sz000505", "京粮控股")],
 "OI": [("sz300999", "金龙鱼"), ("sz002852", "道道全")],
 "A": [("sh600598", "北大荒"), ("sz300087", "荃银高科")],
 "B": [("sh600598", "北大荒")],
 "C": [("sz000930", "中粮科技"), ("sz002041", "登海种业")],
 "CS": [("sz000930", "中粮科技")],
 "LH": [("sz002714", "牧原股份"), ("sz300498", "温氏股份"), ("sz000876", "新希望"), ("sz002567", "唐人神")],
 "JD": [("sz002746", "仙坛股份"), ("sz002234", "民和股份")],
 "SR": [("sh600737", "中粮糖业"), ("sz000911", "广农糖业")],
 "CF": [("sz002042", "华孚时尚"), ("sh600540", "新赛股份")],
 "CJ": [("sz002582", "好想你")],
}


def sina_quotes(codes: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        url = "https://hq.sinajs.cn/list=" + ",".join(chunk)
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode("gbk", "replace")
        for m in re.finditer(r'var hq_str_(\w+)="([^"]*)"', text):
            parts = m.group(2).split(",")
            if len(parts) > 32 and parts[0]:
                last = float(parts[3]) if float(parts[3] or 0) > 0 else float(parts[2] or 0)  # 盘前最新=0 → 用昨收
                prev = float(parts[2] or 0)
                out[m.group(1)] = {"name": parts[0], "price": last,
                                   "chg": round((last / prev - 1) * 100, 2) if prev else 0}
        time.sleep(0.4)
    return out


def sina_daily(code: str, n: int = 70) -> list[float]:
    """akshare 新浪前复权日线（klc_kl.js 已加密，走 akshare 解码）。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
        return [float(x) for x in df["close"].tolist()[-n:]]
    except Exception:
        return []


def main() -> None:
    all_codes = sorted({c for stocks in EQUITY_MAP.values() for c, _ in stocks})
    quotes = sina_quotes(all_codes)
    print(f"实时行情 {len(quotes)}/{len(all_codes)}")

    daily = {}
    for i, code in enumerate(all_codes, 1):
        daily[code] = sina_daily(code)
        if i % 20 == 0:
            print(f"日线 {i}/{len(all_codes)}", flush=True)
        time.sleep(0.25)

    out = {}
    for sym, stocks in EQUITY_MAP.items():
        rows = []
        basket_series = []
        for code, name in stocks:
            q = quotes.get(code)
            closes = daily.get(code) or []
            chg5 = chg20 = None
            if len(closes) > 21:
                chg5 = round((closes[-1] / closes[-6] - 1) * 100, 2)
                chg20 = round((closes[-1] / closes[-21] - 1) * 100, 2)
            rows.append({"code": code, "name": q["name"] if q else name,
                         "price": q["price"] if q else None, "chg": q["chg"] if q else None,
                         "chg5": chg5, "chg20": chg20})
            if len(closes) >= 60:
                norm = [c / closes[-60] * 100 for c in closes[-60:]]
                if not basket_series:
                    basket_series = norm
                else:
                    basket_series = [a + b for a, b in zip(basket_series, norm)]
        n_valid = sum(1 for c, _ in stocks if len(daily.get(c) or []) >= 60)
        if basket_series and n_valid:
            basket_series = [round(v / n_valid, 2) for v in basket_series]
        out[sym] = {"stocks": rows, "basket": basket_series,
                    "basket_chg60": round(basket_series[-1] - 100, 2) if basket_series else None}
    payload = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "note": "相关股票篮子：实时涨跌+5日/20日变动+60日归一化（起点=100）；新浪行情",
               "products": out}
    (ROOT / "data" / "equity.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"equity.json: {len(out)} 品种")


if __name__ == "__main__":
    main()
