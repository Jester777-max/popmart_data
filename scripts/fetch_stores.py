#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
泡泡玛特美国门店 · 自动抓取脚本
---------------------------------
抓取官网 store-list 接口，重建 stores.json 的美国部分（us / us_stores / totals.us_total /
world["United States of America"] / as_of）。world 其他国家与 monthly 原样保留。

【必须配置】把官网门店接口的"请求地址"填到 API_URL（或设环境变量 STORE_API_URL）。
获取方法：在 popmart.com/us/store-list 页面按 F12 → Network → 刷新 → 找到返回门店 JSON 的
那条请求 → Headers 标签 → 复制 "Request URL"。

注意：该接口可能要求特定请求头/地区参数，或对数据中心 IP 有风控；若在 GitHub Actions 中抓取失败，
脚本不会覆盖现有数据（保底），可退回到"手动粘贴 JSON 让我更新"的方式。
"""

import json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORES_PATH = os.path.join(ROOT, "stores.json")

# ← 官网北美门店接口地址（环境变量 STORE_API_URL 优先；为空则用默认）
API_URL = os.environ.get("STORE_API_URL") or "https://prod-na-api.popmart.com/shop/v1/store/mapStoreList"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 模拟浏览器的请求头（部分接口需要 Origin/Referer 才放行）
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.popmart.com",
    "Referer": "https://www.popmart.com/us/store-list",
    "Accept-Language": "en-US,en;q=0.9",
}

# 仅统计 type==1 的永久零售店（type 2=机器人商店，type 4=快闪，均不计入门店）
RETAIL_TYPE = 1
MIN_STORES = 5   # 解析出的门店少于此数视为异常，不覆盖

STATE_ABBR = {
 "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO",
 "Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID",
 "Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
 "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
 "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
 "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
 "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
 "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN","Texas":"TX",
 "Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV",
 "Wisconsin":"WI","Wyoming":"WY",
}


def fetch_raw(url, timeout=25):
    """先试 GET，失败或非 JSON 再试 POST 空体。返回响应文本。"""
    last = None
    # 1) GET
    try:
        req = urllib.request.Request(url, headers=BASE_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            if '"storeList"' in txt or '"data"' in txt:
                return txt
            last = "GET 返回内容不含门店数据"
    except Exception as e:
        last = f"GET 失败: {e}"
    # 2) POST 空 JSON 体
    try:
        h = dict(BASE_HEADERS); h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=b"{}", headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"{last}；POST 也失败: {e}")


def parse_stores(raw):
    j = json.loads(raw)
    lst = (j.get("data") or {}).get("storeList") or j.get("storeList") or []
    out = []
    for s in lst:
        if s.get("type") != RETAIL_TYPE:        # 只要永久零售店
            continue
        if s.get("status") != 1:                # 仅在营
            continue
        name = (s.get("nameLocal") or "").strip()
        addr = (s.get("addressLocal") or "").strip()
        state_full = (s.get("administrativeDivisionLevel1") or "").strip()
        city = (s.get("administrativeDivisionLevel2") or "").strip()
        try:
            lat = float(s.get("lat")); lng = float(s.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (name and addr and state_full and city):
            continue
        temp = "Temp" in name
        disp = name.replace("Temp", "").strip()
        out.append({"name": disp, "addr": addr, "city": city,
                    "state": STATE_ABBR.get(state_full, state_full[:2].upper()),
                    "lat": round(lat, 6), "lng": round(lng, 6),
                    **({"temp": True} if temp else {})})
    return out


def main():
    if not API_URL:
        print("[skip] 未配置 API_URL / STORE_API_URL，跳过门店抓取（保留现有 stores.json）。")
        return
    if not os.path.exists(STORES_PATH):
        print("[err] 找不到 stores.json", file=sys.stderr); sys.exit(1)
    base = json.load(open(STORES_PATH, encoding="utf-8"))

    try:
        stores = parse_stores(fetch_raw(API_URL))
    except Exception as e:
        print(f"[abort] 抓取/解析失败，不覆盖：{e}", file=sys.stderr); sys.exit(0)

    if len(stores) < MIN_STORES:
        print(f"[abort] 解析门店过少（{len(stores)}），不覆盖。", file=sys.stderr); sys.exit(0)

    # 按州计数（全称键，供备用）
    from collections import Counter
    abbr2full = {v: k for k, v in STATE_ABBR.items()}
    cnt = Counter(s["state"] for s in stores)
    us_counts = {abbr2full.get(a, a): n for a, n in
                 sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))}

    # —— 检测本周新增的美国门店（对比上一份快照），写入当月美国数据 ——
    from datetime import datetime, timezone
    def _key(s): return (s.get("name", ""), s.get("addr", ""))
    prev_keys = {_key(s) for s in (base.get("us_stores") or [])}
    new_stores = [s for s in stores if _key(s) not in prev_keys]

    monthly = base.setdefault("monthly", [])
    # 保证每条月度数据都带地点/计数字段（前端按列读取）
    for r in monthly:
        r.setdefault("china_loc", [])
        r.setdefault("us_loc", [])          # 美国新增地点
        r.setdefault("overseas_loc", [])    # 其他地区（非美）新增地点
        r.setdefault("us", 0)               # 美国新增（家）

    if new_stores:
        ym = datetime.now(timezone.utc).strftime("%Y-%m")
        row = next((r for r in monthly if r.get("month") == ym), None)
        if row is None:
            row = {"month": ym, "china": 0, "overseas": 0, "us": 0,
                   "china_loc": [], "us_loc": [], "overseas_loc": []}
            monthly.append(row)
        # 新增美国门店所在城市（City, ST）去重后写入当月「美国新增地点」
        added = 0
        existing = set(row.get("us_loc") or [])
        for s in new_stores:
            loc = f'{s.get("city","")}, {s.get("state","")}'.strip(", ")
            if loc and loc not in existing:
                existing.add(loc)
                row["us_loc"].append(loc)
        # 计数：美国与海外总数同步 +k（其他地区 = overseas − us 因此保持不变）
        k = len(new_stores)
        row["us"] = (row.get("us") or 0) + k
        row["overseas"] = (row.get("overseas") or 0) + k
        print(f"[info] 本周新增美国门店 {k} 家，已并入 {ym} 的 us / us_loc / overseas。")
    else:
        print("[info] 本周未检测到新增美国门店，月度数据不变。")

    base["us_stores"] = stores
    base["us"] = us_counts
    base.setdefault("totals", {})["us_total"] = len(stores)
    base.setdefault("world", {})["United States of America"] = len(stores)
    base["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    json.dump(base, open(STORES_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[done] 美国门店 {len(stores)} 家，{len(us_counts)} 州 -> stores.json")


if __name__ == "__main__":
    main()
