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

# Google News RSS 搜索（简体中文）。可按需增删关键词。
QUERIES = [
    "泡泡玛特",
    "泡泡玛特 LABUBU",
    "POP MART 泡泡玛特",
]
RSS_TMPL = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"

# 关键词 -> 分类。命中越多得分越高，取最高分；都为 0 归入 other。
CATEGORY_KEYWORDS = {
    "sales":  ["财报", "营收", "净利", "利润", "业绩", "收益", "季度", "同比",
               "增长", "股价", "市值", "毛利", "回购", "营业额", "销售额", "指引"],
    "store":  ["开业", "门店", "新店", "首店", "旗舰店", "落地", "开出", "拓店", "新增门店"],
    "expo":   ["展览", "展区", "消博会", "进博会", "乐园", "POP LAND", "POPLAND",
               "光影节", "艺术展", "巡展", "主题展", "快闪"],
    "collab": ["联名", "合作", "授权", "联动", "跨界", "签约", "电影", "动画",
               "影业", "代工", "OEM"],
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_ITEMS = 48          # data.json 中保留的最大条数
MIN_FETCHED = 3         # 抓取结果少于此数则视为失败，不覆盖旧数据


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def categorize(text):
    """根据关键词命中计分，返回得分最高的分类。"""
    scores = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in kws if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


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

        items.append({
            "cat": categorize(title),
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
