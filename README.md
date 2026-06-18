# 泡泡玛特 · 动向监测站（全自动周更版）

一个**无人值守、每周自动抓取**泡泡玛特最新动向的网页。零服务器、零成本，
用 GitHub Actions（免费定时任务）+ GitHub Pages（免费静态托管）即可跑起来。

```
泡泡玛特动向监测站
├─ index.html            前端页面（读取 data.json 动态渲染）
├─ data.json             当前展示的数据（每周被自动覆盖更新）
├─ seed.json             人工精选基线（始终保底展示，可手动维护）
├─ scripts/
│   └─ fetch_news.py      周度抓取脚本（仅标准库，无依赖）
├─ .github/workflows/
│   └─ update.yml         GitHub Actions 定时任务（每周一自动跑）
└─ README.md             本文件
```

## 它是怎么运转的

1. **每周一 09:00（北京时间）**，GitHub Actions 自动运行 `fetch_news.py`。
2. 脚本从 **Google News RSS**（免费、无需 API Key）抓取「泡泡玛特」相关新闻，
   按关键词自动归类到 **销售数据 / 新店开业 / 展览活动 / 品牌合作 / 其他**，
   与 `seed.json` 合并去重后写入 `data.json`，并提交回仓库。
3. **GitHub Pages** 托管的 `index.html` 打开时读取最新 `data.json` 渲染；
   页面前端每 5 分钟也会回源检查一次，点「刷新」可立即重新拉取。

> 真正的"按周抓取"发生在云端（GitHub 的服务器），**不需要你的电脑或浏览器开着**。

---

## 部署步骤（约 10 分钟，全程网页操作）

### 1. 建仓库并上传文件
- 登录 GitHub → 右上角「+」→ **New repository**。
- 取个名字，比如 `popmart-tracker`，选 **Public**（Pages 免费版需公开仓库），创建。
- 进入仓库 → **Add file → Upload files**，把本文件夹里的所有内容
  （`index.html`、`data.json`、`seed.json`、`scripts/`、`.github/`）拖进去 → Commit。
  > 注意保持目录结构：`scripts/fetch_news.py` 和 `.github/workflows/update.yml` 的路径不能变。

### 2. 开启 GitHub Pages（托管网页）
- 仓库 → **Settings → Pages**。
- **Source** 选 `Deploy from a branch`，**Branch** 选 `main` + `/ (root)` → Save。
- 等 1~2 分钟，页面顶部会显示你的网址，形如
  `https://你的用户名.github.io/popmart-tracker/`。打开它就是你的监测站。

### 3. 开启并测试定时任务
- 仓库 → **Settings → Actions → General** →
  下拉到 **Workflow permissions**，选 **Read and write permissions** → Save。
  （这一步让定时任务有权限把更新写回仓库，**必做**，否则无法自动提交。）
- 仓库 → **Actions** 标签 → 左侧选「泡泡玛特动向 · 周度抓取」→
  右侧 **Run workflow** 手动跑一次，验证能成功抓取并更新 `data.json`。
- 之后它会**每周一自动运行**，无需再管。

完成。以后泡泡玛特有新动向，页面会在每周更新后自动出现。

---

## 常用调整

- **改抓取频率**：编辑 `.github/workflows/update.yml` 里的 `cron`。
  - 每天 09:00（北京时间）：`'0 1 * * *'`
  - 每周一、四：`'0 1 * * 1,4'`
  - cron 用的是 **UTC 时间**，北京时间 = UTC + 8 小时。
- **改/加搜索关键词**：编辑 `scripts/fetch_news.py` 顶部的 `QUERIES` 列表。
- **改分类规则**：编辑 `fetch_news.py` 里的 `CATEGORY_KEYWORDS`。
- **维护精选基线**：直接编辑 `seed.json`，这些条目带完整描述、始终展示。
- **保留条数**：改 `fetch_news.py` 里的 `MAX_ITEMS`。

## 本地预览
因为页面用 `fetch` 读取 `data.json`，直接双击打开可能被浏览器拦截（会自动显示离线兜底数据）。
想本地完整预览，在文件夹内起一个简单服务即可：
```bash
python3 -m http.server 8000
# 浏览器打开 http://localhost:8000
```

## 注意事项
- GitHub Pages 免费版需 **公开仓库**；不想公开可改用 Cloudflare Pages 等。
- Google News RSS 偶有访问波动；脚本已做保底：**抓取失败不会覆盖**上次的好数据。
- 自动抓取的标题/链接直接来自新闻源，**未经人工核实**；正式用途请以官方公告与财报为准。
- 若长期无人访问，GitHub 可能在 60 天后暂停定时任务，进仓库点一下即可恢复。
