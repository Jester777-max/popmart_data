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
import re
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


# —— 其他地区（非美海外）开店线索 -> 月度 overseas_loc ——
# 含美国标志的线索一律跳过（美国由官网接口单独统计，避免重复）。
US_MARKERS = [
    "美国", "美东", "美西", "北美", "纽约", "洛杉矶", "加州", "加利福尼亚", "旧金山",
    "拉斯维加斯", "德克萨斯", "德州", "得州", "休斯顿", "达拉斯", "芝加哥", "西雅图",
    "波士顿", "新泽西", "佛罗里达", "迈阿密", "奥兰多", "亚特兰大",
]
# 非美海外地名：城市优先、国家兜底（同一条线索若同时出现城市与国家，只取城市，避免重复计数）
OVERSEAS_CITIES_CN = [
    "曼谷", "清迈", "新加坡", "胡志明", "河内", "雅加达", "吉隆坡", "马尼拉", "东京", "大阪",
    "首尔", "伦敦", "巴黎", "米兰", "马德里", "巴塞罗那", "柏林", "阿姆斯特丹", "哥本哈根",
    "悉尼", "墨尔本", "奥克兰", "多伦多", "温哥华", "迪拜",
]
OVERSEAS_COUNTRIES_CN = [
    "泰国", "越南", "印度尼西亚", "印尼", "马来西亚", "菲律宾", "日本", "韩国", "英国",
    "法国", "意大利", "荷兰", "西班牙", "德国", "丹麦", "澳大利亚", "澳洲", "新西兰",
    "加拿大", "阿联酋", "墨西哥", "巴西",
]


def _dedupe_places(found):
    out = []
    for p in found:
        if not any(p in f or f in p for f in out):
            out.append(p)
    return out


def extract_overseas_places(text):
    """从非美海外开店线索里提取地名；含美国标志则返回空。
    有城市优先返回城市，否则返回国家（避免「德国柏林」被算成两家）。"""
    text = text or ""
    if any(k in text for k in US_MARKERS):
        return []
    cities = _dedupe_places([c for c in OVERSEAS_CITIES_CN if c in text])
    if cities:
        return cities
    return _dedupe_places([c for c in OVERSEAS_COUNTRIES_CN if c in text])


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


# —— 财报 / 官方门店数监测：检测到新财报则刷新基线 ——
# 现实：泡泡玛特财报只公布分区域营收与「海外门店总数」，不公布逐国门店数。
# 故本流程自动刷新「海外总数」并按比例重排非美各国估算（美国用实测），逐国精确拆分仍可人工微调。
# 将 AUTO_APPLY_BASELINE 置 False 可改为「只提示、不自动改数」。
AUTO_APPLY_BASELINE = True
REPORT_KW = ["财报", "半年报", "年报", "季报", "业绩", "业绩报告", "业绩快报",
             "中报", "年度业绩", "interim", "annual result"]
_OV_PAT = re.compile(r"(?:海外|港澳台[及和]?海外|海外及港澳台)[^。；;，,、]{0,14}?门店[^。；;，,、]{0,8}?(\d{2,4})\s*家")
_US_PAT = re.compile(r"美国[^。；;，,、]{0,12}?门店[^。；;，,、]{0,8}?(\d{2,4})\s*家")


def _recent_iso(days=45):
    """返回 days 天前的 'YYYY-MM-DD'（UTC）。"""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def extract_official_total(text, pat):
    """用给定正则从文本提取门店总数；匹配附近含『机器人/快闪』则跳过，避免混入非永久零售店。"""
    text = text or ""
    for m in pat.finditer(text):
        span = text[max(0, m.start() - 6): m.end() + 6]
        if "机器人" in span or "快闪" in span:
            continue
        try:
            return int(m.group(1))
        except ValueError:
            continue
    return None


def _rescale_nonus(world, target_nonus, us_key="United States of America"):
    """把非美各国按比例缩放到 target_nonus（每个市场至少保留 1），并修正取整误差到精确值。"""
    us = world.get(us_key, 0)
    items = [[k, v] for k, v in world.items() if k != us_key]
    cur = sum(v for _, v in items)
    if cur <= 0 or target_nonus <= 0:
        return dict(world)
    factor = target_nonus / cur
    for it in items:
        it[1] = max(1, round(it[1] * factor)) if it[1] > 0 else 0
    diff = target_nonus - sum(v for _, v in items)
    items.sort(key=lambda x: -x[1])
    guard = 0
    while diff != 0 and items and guard < 10000:
        for it in items:
            if diff == 0:
                break
            step = 1 if diff > 0 else -1
            if it[1] + step >= 1:
                it[1] += step
                diff -= step
        guard += 1
    out = {us_key: us}
    for k, v in items:
        out[k] = v
    return out


def review_baseline(stores, items):
    """检测最近 45 天内的财报/官方门店数信号：
    - 发现未处理过的新财报项 -> 写入 baseline_review 提示（前端会显示横幅）；
    - 若同时提取到可信的新『海外门店总数』(> 现值、且 ≤ 现值×3) 且 AUTO_APPLY_BASELINE：
        自动刷新 overseas_total_base/totals.overseas_total、按比例重排非美各国估算、
        推进 world_base_month 到财报月份，并记录出处与新旧值；否则只提示，等待人工校准。
    幂等：同一篇（按 url）只处理一次。
    """
    if not isinstance(stores.get("world"), dict):
        return
    review = stores.get("baseline_review") or {}
    ack_url = review.get("acknowledged_url")
    since = _recent_iso(45)

    cand = None
    for it in items:
        title = it.get("title", "") or ""
        text = it.get("text", "") or ""
        d = (it.get("d") or "")[:10]
        if d < since:
            continue
        if any(k in title or k in text for k in REPORT_KW):
            if cand is None or d > cand["d"]:
                cand = {"d": d, "title": title, "text": text, "url": it.get("url", "")}

    if not cand:
        return                                      # 无新财报信号 -> 不动
    if cand.get("url") and cand["url"] == ack_url:
        return                                      # 已处理过 -> 幂等跳过

    cur_total = int(stores.get("overseas_total_base") or
                    (stores.get("totals", {}) or {}).get("overseas_total") or 0)
    n_ov = (extract_official_total(cand["title"], _OV_PAT) or
            extract_official_total(cand["text"], _OV_PAT))
    plausible = (n_ov is not None and cur_total > 0 and cur_total < n_ov <= cur_total * 3)

    new_review = {
        "needed": True,
        "detected_on": cand["d"],
        "headline": cand["title"][:120],
        "url": cand["url"],
        "detected_overseas_total": n_ov,
    }

    if AUTO_APPLY_BASELINE and plausible:
        us_key = "United States of America"
        world = dict(stores["world"])
        us_now = world.get(us_key, stores.get("us_total_base", 0))
        new_world = _rescale_nonus(world, n_ov - us_now, us_key)
        stores["world"] = dict(new_world)
        stores["world_base"] = dict(new_world)
        stores["overseas_total_base"] = n_ov
        stores.setdefault("totals", {})["overseas_total"] = n_ov
        stores["world_base_month"] = cand["d"][:7]
        new_review.update({
            "needed": False,
            "auto_applied": True,
            "applied_overseas_total": n_ov,
            "prev_overseas_total": cur_total,
            "acknowledged_url": cand["url"],
            "note": "海外总数已按财报自动刷新；非美各国为按比例重排的估算，可手动微调。",
        })
        stores["baseline_review"] = new_review
        print(f"[done] 检测到新财报（{cand['d']}）：海外门店总数 {cur_total} -> {n_ov}，"
              f"已自动刷新基线并按比例重排非美各国估算。")
    else:
        reason = ("未能提取到可信的海外门店总数" if n_ov is None
                  else (f"提取到的总数 {n_ov} 不在合理区间，已忽略自动刷新" if not plausible else ""))
        new_review["note"] = ("检测到疑似新财报，请人工校准基线"
                              "（world 各国值 / overseas_total_base）。" + reason).strip()
        stores["baseline_review"] = new_review
        print(f"[info] 检测到疑似新财报（{cand['d']}）：{cand['title'][:40]}…"
              f"{'（' + reason + '）' if reason else ''} 已写入提示，等待人工校准。")


def update_store_locs(items):
    """从开店线索并入 stores.json 的月度地点：
    - 中国（region=china 且 lead）-> china_loc（最近 13 个月，增量）；
    - 其他地区（region=overseas 且 lead，且非美）-> overseas_loc（截止月之后、最近 13 个月，增量，
      每识别到一个新地名同时给当月 overseas 计数 +1，因「其他地区新增 = overseas − us」）；
    - 增量并入，不覆盖已有（含手填）条目；任何异常都不影响 data.json 的写入。
    """
    if not os.path.exists(STORES_PATH):
        print("[skip] 未找到 stores.json，跳过门店地点更新。")
        return
    stores = load_json(STORES_PATH)
    if not stores or not isinstance(stores.get("monthly"), list):
        print("[skip] stores.json 无 monthly，跳过门店地点更新。")
        return

    monthly = stores["monthly"]
    allowed = set(_recent_months(13))
    cutoff = stores.get("world_base_month", "")     # 此月及之前已计入 world_base，不再自动加（避免与历史估算/基线重复）
    by_month = {r.get("month"): r for r in monthly if isinstance(r, dict)}

    def ensure_row(ym):
        row = by_month.get(ym)
        if row is None:
            row = {"month": ym, "china": 0, "overseas": 0, "us": 0,
                   "china_loc": [], "us_loc": [], "overseas_loc": []}
            monthly.append(row)
            by_month[ym] = row
        return row

    cn_added, ov_added = 0, 0

    for it in items:
        if not it.get("lead"):
            continue
        ym = (it.get("d") or "")[:7]
        if ym not in allowed:
            continue
        region = it.get("region")

        if region == "china":
            cities = extract_cn_cities(it.get("title", "")) or extract_cn_cities(it.get("text", ""))
            if not cities:
                continue
            row = ensure_row(ym)
            created_china = (row.get("china") in (None, 0)) and not row.get("china_loc")
            loc = row.setdefault("china_loc", [])
            for c in cities:
                if not any(c in e for e in loc):
                    loc.append(c)
                    cn_added += 1
            if created_china and not row.get("china"):
                row["china"] = len(loc)             # 新建行计数取检测到的城市数（下限估算）

        elif region == "overseas":
            if ym <= cutoff:                        # 截止月及之前已含在基线/估算里，跳过
                continue
            places = extract_overseas_places(it.get("title", "")) or extract_overseas_places(it.get("text", ""))
            if not places:
                continue
            row = ensure_row(ym)
            loc = row.setdefault("overseas_loc", [])
            for p in places:
                if not any(p in e for e in loc):
                    loc.append(p)
                    row["overseas"] = (row.get("overseas") or 0) + 1   # 其他地区 +1
                    ov_added += 1

    # 保证所有月度行都带地点/计数字段，便于前端读取
    for r in monthly:
        if isinstance(r, dict):
            r.setdefault("china_loc", [])
            r.setdefault("us", 0)
            r.setdefault("us_loc", [])
            r.setdefault("overseas_loc", [])

    # 财报/官方门店数监测：检测到新财报则刷新基线（详见 review_baseline）
    try:
        review_baseline(stores, items)
    except Exception as e:
        print(f"[warn] 财报基线检查失败（已忽略）: {e}", file=sys.stderr)

    try:
        with open(STORES_PATH, "w", encoding="utf-8") as f:
            json.dump(stores, f, ensure_ascii=False, indent=2)
        print(f"[done] 门店地点增量更新：中国新增 {cn_added} 城、其他地区新增 {ov_added} 处 -> {STORES_PATH}")
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

    # 从开店线索并入 stores.json 的月度地点（中国 china_loc + 其他地区 overseas_loc；异常不影响上面的结果）
    try:
        update_store_locs(merged)
    except Exception as e:
        print(f"[warn] 门店地点更新失败（已忽略）: {e}", file=sys.stderr)


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
