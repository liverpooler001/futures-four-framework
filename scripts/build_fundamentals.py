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
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
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
    # ---- 黑色（2026-08-13 加入，ID 经 bindings 实测）----
    "RB": {"route": "", "contradiction": "高炉现金利润已亏损、社库低于往年，但建材成交仍淡；关键看低利润逼减产先落地，还是旺季需求先验证。", "metrics": [
        ("ID00183781", "35城螺纹社库", "万吨", "Mysteel", "库存水位与去化方向"),
        ("ID00183169", "全国建材成交量", "吨", "Mysteel", "真实需求强弱"),
        ("ID01975884", "螺纹高炉现金利润", "元/吨", "Mysteel", "减产压力有多大")]},
    "I": {"route": "", "contradiction": "45 港库存高位对着高铁水；关键看铁水维持多久——铁水一拐，高港存就变成压力。", "metrics": [
        ("ID00186052", "45港铁矿石库存", "万吨", "Mysteel", "港口压力"),
        ("ID00184088", "247家日均铁水", "万吨", "Mysteel", "需求总阀门"),
        ("ID00183109", "247家高炉开工率", "%", "Mysteel", "开工是否拐头")]},
    "J": {"route": "", "contradiction": "吨焦亏损但产量仍高；关键看亏损是逼出限产，还是被铁水高位继续消化。", "metrics": [
        ("ID00184171", "独立焦化厂吨焦利润", "元/吨", "Mysteel", "亏损深度"),
        ("ID00187978", "230家焦炭日均产量", "万吨", "Mysteel", "供应是否收缩"),
        ("FU00024294", "焦炭基差", "元/吨", "Mysteel", "现货跟不跟盘面")]},
    "JM": {"route": "", "contradiction": "蒙煤通关高位压制煤价，但主焦价格已贴成本区；关键看通关放量能否持续对冲国内减产。", "metrics": [
        ("RE00024806", "甘其毛都通关车数", "车", "Mysteel", "蒙煤供应闸门"),
        ("ID00401840", "安泽低硫主焦价格", "元/吨", "Mysteel", "现货锚是否松动"),
        ("ID00184904", "炼焦煤进口（蒙古）", "吨", "海关", "进口趋势验证")]},
    # ---- 能化 ----
    "TA": {"route": "", "contradiction": "加工费被压在低位，但开工与产量仍高；关键看低加工费能否逼出检修，把库存拐点等来。", "metrics": [
        ("ID01214625", "PTA 加工费", "元/吨", "隆众", "利润压缩程度"),
        ("RE00033616", "PTA 产量", "万吨", "隆众", "供应是否收缩"),
        ("RE00033615", "PTA 行业库存", "万吨", "隆众", "累库拐点")]},
    "V": {"route": "", "contradiction": "电石法毛利深度亏损、社库仍高；关键看亏损减产与旺季去库谁先兑现。", "metrics": [
        ("ID02326533", "PVC 电石法毛利率", "%", "隆众", "亏损深度"),
        ("ID01990129", "PVC 社会库存", "万吨", "隆众", "库存压力"),
        ("ID01230718", "PVC 产能利用率", "%", "隆众", "供应弹性")]},
    "MA": {"route": "", "contradiction": "内地开工高位而港口库存低；关键看 MTO 利润能否撑住需求，接住内地来的货。", "metrics": [
        ("ID02070872", "焦炉气制甲醇开工率", "%", "隆众", "供应强度"),
        ("ID01733685", "焦炉气制甲醇产量", "吨", "隆众", "产量验证"),
        ("ID01371740", "MTO 生产毛利", "元/吨", "隆众", "下游承接力")]},
    "SA": {"route": "", "contradiction": "纯碱产量高位而玻璃需求走平；关键看高供应是继续累库，还是出口与轻碱需求接住。", "metrics": [
        ("ID01037477", "纯碱周度产量", "万吨", "隆众", "供应压力"),
        ("FU00024313", "纯碱基差", "元/吨", "Mysteel", "现货强弱"),
        ("ID01230657", "浮法玻璃开工率", "%", "隆众", "下游需求")]},
    # ---- 农产品 ----
    "M": {"route": "", "contradiction": "大豆压榨量高位、豆粕库存仍升；关键看高压榨是追上需求，还是把库存继续推高。", "metrics": [
        ("ID01709995", "油厂大豆压榨量", "万吨", "Mysteel", "供应强度"),
        ("ID01709989", "油厂豆粕库存", "万吨", "Mysteel", "累库速度"),
        ("ID00188062", "进口大豆港口库存", "万吨", "Mysteel", "原料到港压力")]},
    "LH": {"route": "", "contradiction": "自养深度亏损而出栏体重仍高；关键看亏损是去产能的开始，还是二育压栏把供应后移。", "metrics": [
        ("ID01208659", "商品猪出栏均价", "元/公斤", "涌益系", "价格水位"),
        ("ID01208800", "出栏平均体重", "公斤", "涌益系", "压栏程度"),
        ("ID01208822", "生猪自养利润", "元/头", "Mysteel", "亏损深度")]},
    "C": {"route": "", "contradiction": "饲料企业库存天数回升而深加工库存仍低；关键看新季上市前，渠道是主动补库还是被动累库。", "metrics": [
        ("ID01528411", "饲料企业库存天数", "天", "Mysteel", "渠道补库意愿"),
        ("ID00262726", "深加工企业玉米库存", "万吨", "Mysteel", "工业库存水位"),
        ("ID00404038", "玉米淀粉开机率", "%", "Mysteel", "工业需求")]},
    "SR": {"route": "", "contradiction": "配额内进口利润高企、进口到港预期压制盘面；关键看利润兑现成多少实际到港。", "metrics": [
        ("ID01201856", "巴西糖配额内进口利润", "元/吨", "Mysteel", "进口动力"),
        ("ID01519879", "白糖期现价差（南宁）", "元/吨", "Mysteel", "现货强弱")]},
    "P": {"route": "", "contradiction": "进口利润修复、商业库存回升；关键看马棕产量高峰过去后，库存是继续回补还是再度转紧。", "metrics": [
        ("ID01529070", "棕榈油商业库存", "万吨", "隆众", "库存回补速度"),
        ("ID01446102", "棕榈油进口利润", "元/吨", "Mysteel", "进口窗口"),
        ("ID01202668", "油菜籽压榨量", "万吨", "Mysteel", "油脂替代供应")]},
    "JD": {"route": "", "contradiction": "蛋鸡盈利收窄但存栏仍高；关键看中秋备货能否消化高存栏，节后回落风险多大。", "metrics": [
        ("FU00039356", "鸡蛋基差", "元/吨", "DCE", "现货强弱"),
        ("ID01362874", "蛋鸡综合养殖盈利", "元/羽", "Mysteel", "利润方向"),
        ("ID01362852", "鸡蛋库存", "天", "Mysteel", "渠道库存")]},
}

# 全板块矛盾卡扩展（2026-08-13：能化 20 + 黑色剩余/贵金属 9 + 农产品 11，ID 均经 series 实测）
try:
    from _focus_frag_nenghua import FOCUS_FRAG as _FRAG_NH
    from _focus_frag_blackpm import FOCUS_FRAG as _FRAG_BP
    from _focus_frag_agri import FOCUS_FRAG as _FRAG_AG
    for _frag in (_FRAG_NH, _FRAG_BP, _FRAG_AG):
        FOCUS.update(_frag)
except ImportError as _e:
    print(f"[warn] focus 片段未加载: {_e}")

NON_PHYSICAL_SECTORS = {"金融", "其他"}
NON_PHYSICAL_SYMBOLS = {"ZC"}
SEARCH_ALIASES = {"AG":"白银","AU":"黄金","PD":"钯","PT":"铂","HC":"热轧板卷","I":"铁矿石","J":"焦炭","JM":"焦煤","RB":"螺纹钢","SF":"硅铁","SM":"锰硅","WR":"线材","BR":"丁二烯橡胶","BU":"沥青","BZ":"纯苯","EB":"苯乙烯","EG":"乙二醇","FG":"玻璃","FU":"燃料油","L":"聚乙烯","LU":"低硫燃料油","MA":"甲醇","PF":"短纤","PG":"液化气","PL":"丙烯","PP":"聚丙烯","PR":"瓶片","PX":"对二甲苯","SA":"纯碱","SC":"原油","SH":"烧碱","TA":"PTA","UR":"尿素","V":"PVC","A":"豆一","AP":"苹果","B":"豆二","C":"玉米","CF":"棉花","CJ":"红枣","CS":"玉米淀粉","JD":"鸡蛋","LG":"原木","LH":"生猪","M":"豆粕","NR":"20号胶","OI":"菜油","P":"棕榈油","PK":"花生","RM":"菜粕","RU":"天然橡胶","SP":"纸浆","SR":"白糖","Y":"豆油"}
INVENTORY_PREFERRED = {"JD":"ID01362852"}
INVENTORY_CACHE = CACHE / "inventory-map.json"


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


def search_inventory(product: dict[str, Any], key: str) -> dict[str, Any] | None:
    symbol = str(product.get("symbol") or product.get("product") or "").upper()
    if product.get("sector") in NON_PHYSICAL_SECTORS or symbol in NON_PHYSICAL_SYMBOLS:
        return None
    cached_map = load_json(INVENTORY_CACHE, {})
    cached = cached_map.get(symbol)
    if cached and cached.get("id"):
        return cached
    query_name = SEARCH_ALIASES.get(symbol) or str(product.get("name") or symbol).removeprefix("沪")
    if not key:
        return None
    url = f"{API}/search?" + urllib.parse.urlencode({"q": f"{query_name} 库存", "source": "all", "limit": 12})
    request = urllib.request.Request(url, headers={"X-Data-Key": key, "User-Agent": "YAFCO-Inventory-Snapshot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            results = json.loads(response.read().decode("utf-8")).get("results") or []
    except Exception:
        return cached
    preferred = INVENTORY_PREFERRED.get(symbol)
    choice = next((x for x in results if x.get("id") == preferred), None) if preferred else None
    if not choice:
        def rank(item: dict[str, Any]) -> float:
            text = f"{item.get('name','')} {item.get('path','')}"
            if "库存" not in text or query_name not in text:
                return -100
            score = 3 + (4 if any(w in text for w in ("中国","全国","总库存","总量","社会库存")) else 0)
            score += 2 if any(w in text for w in ("仓单","交易所库存","库存可用天数")) else 0
            score -= 5 if any(w in text for w in ("省","市","港：","企业：","样本：")) else 0
            score -= 3 if any(w in text for w in ("原料库存","下游","产成品")) else 0
            return score
        ranked = sorted(results, key=rank, reverse=True)
        choice = ranked[0] if ranked and rank(ranked[0]) >= 3 else None
    if not choice:
        return None
    selected = {"id":choice.get("id"),"name":choice.get("name"),"unit":choice.get("unit") or "","source":str(choice.get("source") or "知几·料").upper(),"path":choice.get("path")}
    cached_map[symbol] = selected
    write_json(INVENTORY_CACHE, cached_map)
    return selected


def summarize_inventory(product: dict[str, Any], key: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    selected = search_inventory(product, key)
    if not selected:
        return {"status":"unmatched","latest":None,"name":"库存口径待匹配","source":"知几·料"}
    result = summarize((selected["id"],selected["name"],selected.get("unit") or "",selected.get("source") or "知几·料","基础库存"), key, previous)
    result["path"] = selected.get("path")
    return result


def build_spread(product: dict[str, Any]) -> dict[str, Any]:
    lead = str(product.get("contract") or "")
    try:
        curve = engine.curve_quotes(str(product.get("symbol") or ""), lead)
    except Exception as error:
        return {"status":"failed","value":None,"error":str(error)[:120]}
    if not curve:
        return {"status":"unavailable","value":None}
    near = next((x for x in curve if x.get("is_lead")), None) or max(curve,key=lambda x:float(x.get("open_interest") or 0))
    following = [x for x in curve if str(x.get("symbol") or "").upper()>str(near.get("symbol") or "").upper() and (x.get("open_interest") or 0)>0]
    if not following:
        return {"status":"no_far_contract","value":None,"near_symbol":str(near.get("symbol") or "").upper()}
    far = max(following,key=lambda x:(float(x.get("open_interest") or 0),float(x.get("volume") or 0)))
    value = float(near["last"])-float(far["last"])
    return {"status":"ok","definition":"主力－下一活跃合约","value":round(value,6),"near_symbol":str(near.get("symbol") or "").upper(),"near_price":near["last"],"far_symbol":str(far.get("symbol") or "").upper(),"far_price":far["last"],"structure":"近强·BACK" if value>0 else ("远强·CONTANGO" if value<0 else "平水"),"source":"知几·观","end":date.today().isoformat()}


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
        step = max(1, len(points) // 130)
        slim = points[::step]
        if slim[-1][0] != points[-1][0]:
            slim = slim + [points[-1]]
        return {
            "id": metric_id, "name": name, "unit": unit, "source": source, "why": why,
            "latest": round(latest, 6), "end": latest_day.isoformat(),
            "change_pct": round((latest - previous_value) / scale * 100, 2),
            "comparison": f"较{lookback}{freq}前",
            "stale": (date.today() - latest_day).days > max_age,
            "status": "ok", "points": len(points), "previous_end": previous_day.isoformat(),
            "series": [[d.isoformat(), round(v, 6)] for d, v in slim],
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
    old_products = {str(item.get("symbol") or "").upper(): item for item in old.get("products") or []}
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
            spread = build_spread(product)
            previous_inventory = (old_products.get(symbol) or {}).get("inventory")
            inventory = summarize_inventory(product, key, previous_inventory)
            inventory_ok = inventory.get("latest") is not None
            spread_ok = spread.get("value") is not None
            if product.get("sector") in NON_PHYSICAL_SECTORS or symbol in NON_PHYSICAL_SYMBOLS:
                inventory = {"status":"not_applicable","latest":None,"name":"库存不适用","source":"—"}
            auto_contra = "库存与月差数据待更新。"
            if inventory_ok:
                chg = inventory.get("change_pct")
                dir_txt = "累库" if (chg or 0) > 3 else ("去库" if (chg or 0) < -3 else "持平")
                struct = spread.get("structure") or spread.get("status") or "结构待更新"
                key_watch = "累库能否止步" if dir_txt == "累库" else ("去库能否延续" if dir_txt == "去库" else "库存方向选择")
                auto_contra = (f"{inventory.get('name','库存')}{dir_txt}（{inventory.get('comparison','')} "
                               f"{chg:+.1f}%），月差{struct}；关键看{key_watch}。（自动初判）")
            output_products.append({
                "symbol": symbol, "name": product.get("name"), "covered": inventory_ok or spread_ok,
                "kind": "market_snapshot", "summary": "库存验证现货松紧；主力－下一活跃合约验证期限结构。",
                "inventory": inventory, "spread": spread,
                "contradiction": auto_contra, "metrics": [], "library_url": LIBRARY,
            })
            continue
        output_products.append({
            "symbol": symbol, "name": product.get("name"), "covered": True,
            "kind": "focus",
            "contradiction": config["contradiction"],
            "metrics": [results[item[0]] for item in config["metrics"]],
            "library_url": f"{LIBRARY}#/c/{config['route']}",
        })

    payload = {
        "schema_version": 2,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "知几·料（SMM/Mysteel 镜像）",
        "coverage": {"total": len(output_products), "covered": sum(bool(item.get("covered")) for item in output_products), "focus": len(FOCUS)},
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
