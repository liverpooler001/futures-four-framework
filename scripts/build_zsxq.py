#!/usr/bin/env python3
"""知识星球「纪要小能手」爬取 → 品种匹配 → 新闻面板（2026-08-13）。
通道：zsxq-cli（OAuth 设备流已登录）。拉取近 7 天主题，按品种关键词匹配期货品种，
匹配的写进 data/news.json（sector=星球纪要，tag=品种名），原始全量存档 D:\\Kimi\\纪要小能手_存档.json。
用法：python scripts/build_zsxq.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GROUP_ID = "28888112822211"
ARCHIVE = Path(r"D:\Kimi\纪要小能手_存档.json")
NEWS = ROOT / "data" / "news.json"

# 品种关键词 → 站点 symbol（宁滥勿缺的多义词已收窄）
KEYWORDS = {
    "碳酸锂": "LC", "锂矿": "LC", "锂盐": "LC", "多晶硅": "PS", "工业硅": "SI",
    "黄金": "AU", "白银": "AG", "金价": "AU", "银价": "AG",
    "螺纹": "RB", "热卷": "HC", "不锈钢": "SS", "铁矿": "I", "焦炭": "J", "焦煤": "JM",
    "硅铁": "SF", "锰硅": "SM", "线材": "WR", "动力煤": "ZC",
    "燃料油": "FU", "低硫": "LU", "沥青": "BU", "纸浆": "SP",
    "PTA": "TA", "甲醇": "MA", "尿素": "UR", "纯碱": "SA", "烧碱": "SH",
    "PVC": "V", "聚丙烯": "PP", "聚乙烯": "L", "苯乙烯": "EB", "乙二醇": "EG",
    "纯苯": "BZ", "丙烯": "PL", "对二甲苯": "PX", "PX": "PX", "短纤": "PF", "瓶片": "PR",
    "液化气": "PG", "LPG": "PG", "丁二烯": "BR", "20号胶": "NR",
    "豆一": "A", "豆二": "B", "豆粕": "M", "豆油": "Y", "棕榈": "P", "菜粕": "RM", "菜油": "OI",
    "玉米淀粉": "CS", "玉米": "C", "生猪": "LH", "猪价": "LH", "鸡蛋": "JD",
    "白糖": "SR", "棉花": "CF", "红枣": "CJ", "花生": "PK", "原木": "LG", "浮法": "FG", "光伏玻璃": "FG", "冷库苹果": "AP", "富士": "AP",
    "集运": "EC", 
}
# 股票语境标题 veto（除非同时含强商品词）
NEGATIVE = ["股价", "个股", "龙虎榜", "涨停", "A股", "港股", "美股", "债券", "转债"]
STOCK_VETO = ["推荐", "首次覆盖", "评级", "龙头", "市值", "涨停", "龙虎榜", "定增", "减持",
              "IPO", "股权激励", "业绩前瞻", "大涨", "飙升", "强call", "重点关注"]
STRONG_CMDTY = ["库存", "产量", "开工", "产能", "价格", "矿", "煤", "原油", "猪价", "基差",
                "仓单", "进出口", "进口", "出口", "通关", "发运", "到港", "压榨", "出栏", "屠宰"]
# 弱关键词：单字金属名/品种名在公司名里大量误伤，必须配合商品语境才计数
WEAK_KW = {"铜": "CU", "铝": "AL", "锌": "ZN", "铅": "PB", "镍": "NI", "锡": "SN",
           "金": "AU", "银": "AG", "钯": "PD", "铂": "PT"}
CMDTY_CONTEXT = STRONG_CMDTY + ["冶炼", "现货", "期货", "升贴水", "加工费", "TC", "供给", "需求", "报价"]


def cli(*args) -> dict:
    out = subprocess.run(["zsxq-cli.cmd", *args, "--json"], capture_output=True, timeout=60)
    return json.loads(out.stdout.decode("utf-8", "replace"))


def strip_tags(text: str) -> str:
    text = re.sub(r"<e type=\"hashtag\"[^>]*title=\"([^\"]+)\"[^>]*/>", lambda m: "#" + m.group(1) + "#", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def emit_zsxq() -> None:
    """审核定稿后调用：从 news.json 的星球纪要条目生成 data/zsxq.json（按品种对齐）。"""
    news = json.loads(NEWS.read_text(encoding="utf-8"))
    by_sym: dict[str, list] = {}
    for x in news.get("items", []):
        if x.get("sector") != "星球纪要":
            continue
        for sym in x.get("tag", "").split("/"):
            if sym:
                by_sym.setdefault(sym, []).append(x)
    out = ROOT / "data" / "zsxq.json"
    out.write_text(json.dumps({"updated_at": news.get("updated_at"), "by_symbol": by_sym},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"zsxq.json: {len(by_sym)} 品种 {sum(len(v) for v in by_sym.values())} 条")


def main() -> None:
    since = datetime.now().astimezone() - timedelta(days=7)
    topics: list[dict] = []
    cursor = None
    for _ in range(15):  # 最多 15 页
        args = ["group", "+topics", "--group-id", GROUP_ID]
        if cursor:
            args += ["--end-time", cursor]
        d = cli(*args)
        page = d.get("topics_brief") or []
        if not page:
            break
        topics.extend(page)
        cursor = d.get("next_end_time")
        if not d.get("has_more") or not cursor:
            break
        oldest = min(t["create_time"] for t in page)
        if datetime.fromisoformat(oldest) < since:
            break
    # 存档（按 create_time+首行 去重）
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8")) if ARCHIVE.exists() else []
    seen = {a.get("create_time", "") + (a.get("content") or "")[:30] for a in archive}
    added = 0
    for t in topics:
        key = t.get("create_time", "") + (t.get("content") or "")[:30]
        if key not in seen:
            archive.append(t)
            seen.add(key)
            added += 1
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")

    # 品种匹配
    matched = []
    for t in topics:
        if datetime.fromisoformat(t["create_time"]) < since:
            continue
        text = strip_tags(t.get("content") or "")
        file_names = " ".join(f.get("name", "") for f in t.get("files") or [])
        full = text + " " + file_names
        if len(full) < 12:
            continue
        syms = []
        for kw, sym in KEYWORDS.items():
            if kw in full and sym not in syms:
                syms.append(sym)
        has_ctx = any(w in full for w in CMDTY_CONTEXT)
        if not syms:
            continue
        title = file_names.split(" ")[0].replace(".pdf", "") if file_names else full[:40]
        # 股票语境 veto：标题命中个股话术且全文无强商品词 → 跳过
        if any(v in title for v in STOCK_VETO) and not has_ctx:
            continue
        if any(neg in full for neg in NEGATIVE) and len(full) < 60:
            continue
        summary = full[:140] + ("…" if len(full) > 140 else "")
        matched.append({
            "date": t["create_time"][:10],
            "sector": "星球纪要", "tag": "/".join(syms[:3]),
            "title": title, "summary": summary,
            "source": "知识星球·纪要小能手",
            "url": f"https://wx.zsxq.com/group/{GROUP_ID}",
        })
    # 标题去重
    seen_t: set = set()
    matched = [m for m in matched if not (m["title"] in seen_t or seen_t.add(m["title"]))]
    # 合并进 news.json（替换旧的星球纪要条目）
    news = json.loads(NEWS.read_text(encoding="utf-8"))
    news["items"] = [x for x in news.get("items", []) if x.get("sector") != "星球纪要"] + matched[:20]
    news["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    NEWS.write_text(json.dumps(news, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"主题 {len(topics)}（新增存档 {added}），品种匹配 {len(matched)} 条入 news.json")


if __name__ == "__main__":
    if "--emit-zsxq" in sys.argv:
        emit_zsxq()
    else:
        main()
