#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
泡泡玛特动向 · 周度抓取脚本
---------------------------------
- 数据源：Google News RSS（免费、无需 API Key）
- 仅使用 Python 标准库，无第三方依赖
- 自动按关键词归类到：销售数据 / 新店开业 / 展览活动 / 品牌合作 / 其他
- 与 seed.json（人工精选基线）合并去重
- 输出 data.json，供前端 index.html 读取
- 任何抓取异常都不会覆盖上一次的好数据（保底）

由 GitHub Actions 每周定时运行，详见 .github/workflows/update.yml
"""

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_PATH = os.path.join(ROOT, "data.json")
SEED_PATH = os.path.join(ROOT, "seed.json")
STORES_PATH = os.path.join(ROOT, "stores.json")   # 用于把中国开店线索的城市并入月度 china_loc

# Google News RSS 搜索（简体中文）。可按需增删关键词。
QUERIES = [
    "泡泡玛特",
    "泡泡玛特 LABUBU",
    "POP MART 泡泡玛特",
    "泡泡玛特 门店",
    "泡泡玛特 海外",
    "泡泡玛特 联名",
    "泡泡玛特 新品",
    "泡泡玛特 财报 业绩",
    "泡泡玛特 城市乐园",
    "泡泡玛特 星星人",
]
RSS_TMPL = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"

# 关键词 -> 分类。命中越多得分越高，取最高分；都为 0 归入 other。
# 注意：资讯前端分 4 类（展览快闪/销售数据/业务拓展/其他），cat 字段仅备用。
CATEGORY_KEYWORDS = {
    "store":  ["开业", "门店", "新店", "首店", "旗舰店", "落地", "开出", "拓店", "新增门店"],
    "expo":   ["展览", "展区", "消博会", "进博会", "乐园", "POP LAND", "POPLAND",
               "光影节", "艺术展", "巡展", "主题展", "快闪"],
    "collab": ["联名", "合作", "授权", "联动", "跨界", "签约", "电影", "动画",
               "影业", "代工", "OEM"],
}

# 只丢弃「纯资本市场」新闻（股价/回购/评级等投资者向内容）；
# 销量、销售额、业绩、营收等「经营/销售数据」予以保留，归入前端「销售数据」板块。
SALES_KEYWORDS = ["股价", "股票", "市值", "回购", "做空", "沽空", "评级", "增持", "减持",
                  "蒸发", "涨幅", "跌幅", "收盘", "开盘", "目标价", "恒指",
                  "市盈率", "市净率", "每股", "分红", "派息"]

# 海外关键词：命中则 region=overseas，否则默认 china。
OVERSEAS_KEYWORDS = ["海外", "美国", "美洲", "北美", "欧洲", "全球", "出海", "国际",
                     "泰国", "曼谷", "新加坡", "越南", "胡志明", "印尼", "雅加达",
                     "马来", "吉隆坡", "菲律宾", "马尼拉", "日本", "东京", "韩国", "首尔",
                     "英国", "伦敦", "法国", "巴黎", "意大利", "米兰", "荷兰", "西班牙",
                     "德国", "澳大利亚", "悉尼", "加拿大", "多伦多", "纽约", "洛杉矶",
                     "迪拜", "中东", "东南亚", "世界杯", "索尼", "海外市场"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_ITEMS = 90          # data.json 中保留的最大条数
MIN_FETCHED = 3         # 抓取结果少于此数则视为失败，不覆盖旧数据


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def categorize(text):
    """返回得分最高的分类；纯财经/销售类返回 None（丢弃，不展示）；无命中归 other。"""
    scores = {cat: sum(1 for kw in kws if kw in text)
              for cat, kws in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    if any(kw in text for kw in SALES_KEYWORDS):
        return None             # 纯财经新闻（营收/股价/财报/回购等）丢弃，不展示
    return "other"


def detect_region(text):
    """命中海外关键词 -> overseas，否则 china。"""
    return "overseas" if any(kw in text for kw in OVERSEAS_KEYWORDS) else "china"


# 「开店线索」识别：标题含这些词则疑似新开门店，供人工核实（不自动改地图数字）
STORE_LEAD_KEYWORDS = [
    "新店", "新开", "首店", "开业", "开出", "开设", "进驻", "入驻", "新增门店",
    "旗舰店", "落地", "开张", "正式营业", "门店亮相",
    "opening", "opens", "opened", "open store", "new store", "flagship",
    "debut", "grand opening", "now open",
]


def is_store_lead(text):
    low = text.lower()
    return any(kw.lower() in low for kw in STORE_LEAD_KEYWORDS)


def clean_title(title):
    """Google News 标题常为 '正文 - 来源'，拆出正文与来源。"""
    src = ""
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        # 来源一般较短且不含句号
        if 0 < len(tail) <= 20:
            title, src = head, tail
    return title.strip(), src.strip()


def parse_rss(xml_text):
    """解析 RSS XML，返回条目列表。"""
    items = []
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return items
    for it in channel.findall("item"):
        raw_title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        # <source> 元素优先作为来源
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""

        title, src_from_title = clean_title(raw_title)
        if not source:
            source = src_from_title or "Google News"

        # 日期 -> YYYY-MM-DD
        d = ""
        if pub:
            try:
                d = parsedate_to_datetime(pub).astimezone(timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                d = ""
        if not d:
            d = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not title or not link:
            continue

        cat = categorize(title)
        if cat is None:        # 纯财经新闻，跳过不展示
            continue

        items.append({
            "cat": cat,
            "lead": is_store_lead(title),   # 是否疑似「开店线索」
            "region": detect_region(title),
            "d": d,
            "title": title,
            "text": title,           # RSS 仅有标题，正文留作标题（前端展示用）
            "source": source,
            "url": link,
        })
    return items


def fetch_all():
    collected = []
    for q in QUERIES:
        url = RSS_TMPL.format(q=urllib.parse.quote(q))
        try:
            xml_text = http_get(url)
            got = parse_rss(xml_text)
            print(f"[ok] '{q}' -> {len(got)} 条")
            collected.extend(got)
        except Exception as e:
            print(f"[warn] 抓取 '{q}' 失败: {e}", file=sys.stderr)
    return collected


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] 读取 {path} 失败: {e}", file=sys.stderr)
    return None


def dedup(items):
    """按标题前 18 字去重，保留日期最新的一条。"""
    seen = {}
    for it in items:
        key = it["title"][:18]
        if key not in seen or it["d"] > seen[key]["d"]:
            seen[key] = it
    return list(seen.values())


# —— 中国开店线索 -> 月度 china_loc（城市级，增量并入 stores.json）——
# 常见城市表：用于从「开店线索」标题/正文中识别门店所在城市。可按需增删。
CHINESE_CITIES = [
    "北京", "上海", "广州", "深圳", "成都", "杭州", "重庆", "武汉", "南京", "西安",
    "苏州", "天津", "长沙", "郑州", "青岛", "宁波", "东莞", "沈阳", "昆明", "合肥",
    "佛山", "无锡", "厦门", "福州", "哈尔滨", "济南", "温州", "大连", "南宁", "石家庄",
    "泉州", "贵阳", "南昌", "太原", "长春", "南通", "常州", "嘉兴", "金华", "珠海",
    "惠州", "徐州", "海口", "三亚", "乌鲁木齐", "兰州", "烟台", "中山", "绍兴", "台州",
    "潍坊", "保定", "廊坊", "香港", "澳门",
]


def extract_cn_cities(text):
    """返回文本中命中的城市（按城市表顺序，去重）。"""
    text = text or ""
    return [c for c in CHINESE_CITIES if c in text]


def _recent_months(n=13):
    """返回最近 n 个月的 'YYYY-MM' 列表（含本月）。"""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def update_china_loc(items):
    """从中国开店线索（region=china 且 lead=True）提取城市，按月并入 stores.json 的 china_loc。
    - 增量并入：不覆盖已有（含手填）条目，只追加尚未出现的城市；
    - 仅处理最近 13 个月；该月若无行则新建（china 取检测到的城市数，作为下限估算）；
    - 任何异常都不影响 data.json 的写入。
    """
    if not os.path.exists(STORES_PATH):
        print("[skip] 未找到 stores.json，跳过 china_loc 更新。")
        return
    stores = load_json(STORES_PATH)
    if not stores or not isinstance(stores.get("monthly"), list):
        print("[skip] stores.json 无 monthly，跳过 china_loc 更新。")
        return

    monthly = stores["monthly"]
    allowed = set(_recent_months(13))
    by_month = {r.get("month"): r for r in monthly if isinstance(r, dict)}
    added = 0

    for it in items:
        if it.get("region") != "china" or not it.get("lead"):
            continue
        ym = (it.get("d") or "")[:7]
        if ym not in allowed:
            continue
        cities = extract_cn_cities(it.get("title", "")) or extract_cn_cities(it.get("text", ""))
        if not cities:
            continue
        row = by_month.get(ym)
        created = False
        if row is None:
            row = {"month": ym, "china": 0, "overseas": 0, "us": 0,
                   "china_loc": [], "us_loc": [], "overseas_loc": []}
            monthly.append(row)
            by_month[ym] = row
            created = True
        loc = row.setdefault("china_loc", [])
        for c in cities:
            if not any(c in e for e in loc):     # 该城市尚未出现在任何条目里
                loc.append(c)
                added += 1
        if created and not row.get("china"):
            row["china"] = len(loc)              # 新建行的计数取检测到的城市数（下限估算）

    # 保证所有月度行都带地点/计数字段，便于前端读取
    for r in monthly:
        if isinstance(r, dict):
            r.setdefault("china_loc", [])
            r.setdefault("us", 0)
            r.setdefault("us_loc", [])
            r.setdefault("overseas_loc", [])

    try:
        with open(STORES_PATH, "w", encoding="utf-8") as f:
            json.dump(stores, f, ensure_ascii=False, indent=2)
        print(f"[done] china_loc 增量更新：新增 {added} 个城市标记 -> {STORES_PATH}")
    except Exception as e:
        print(f"[warn] 写入 stores.json 失败: {e}", file=sys.stderr)


def main():
    # 1) 读人工精选基线
    seed = load_json(SEED_PATH) or {"updates": []}
    seed_items = seed.get("updates", [])

    # 2) 抓取
    fetched = fetch_all()

    # 3) 抓取过少视为失败，保底不覆盖
    if len(fetched) < MIN_FETCHED:
        print(f"[abort] 抓取结果过少（{len(fetched)} 条），保留现有 data.json，不覆盖。",
              file=sys.stderr)
        # 首次运行若没有 data.json，则至少写入 seed
        if not os.path.exists(DATA_PATH):
            write_data(seed_items, note="seed-only")
        sys.exit(0)

    # 4) 合并 seed + 抓取，去重、排序、截断
    merged = dedup(seed_items + fetched)
    merged.sort(key=lambda x: x["d"], reverse=True)
    merged = merged[:MAX_ITEMS]

    write_data(merged, note="fetched")
    print(f"[done] 写入 {len(merged)} 条 -> {DATA_PATH}")

    # 从中国开店线索提取城市，并入 stores.json 的月度 china_loc（异常不影响上面的结果）
    try:
        update_china_loc(merged)
    except Exception as e:
        print(f"[warn] china_loc 更新失败（已忽略）: {e}", file=sys.stderr)


def write_data(updates, note=""):
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "updates": updates,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import urllib.parse  # noqa: placed here so module import stays tidy
    main()
