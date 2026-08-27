# TikTok Script Extractor

Automated TikTok video transcript extraction and translation pipeline powered by GitHub Actions.

## Features

- **Auto-extract** TikTok video transcripts every 30 minutes via GitHub Actions
- **Dual extraction**: tikwm.com API for metadata + faster-whisper for speech-to-text
- **Auto-translate** transcripts to Chinese using Google Translate (free, no API key)
- **Video ID extraction** from TikTok URLs
- **CSV + JSON output** with full history tracking
- **Zero cost**: all tools are free and open-source

## Quick Start

### 1. Create a repository
Create a new GitHub repository (e.g. `tiktok-script-extractor`) and push all files from this project.

### 2. Input source — choose one

**A. 直读飞书表格（推荐，无需导出 CSV）**
见下方「直读飞书表格」章节，配置 3 个 Secrets 后，自动化每 30 分钟直接读你的飞书 wiki 表格、提取、并把译文/脚本写回原表。

**B. CSV 输入（兜底）**
不配置 Secrets 时，脚本读取 `input/tiktok_links.csv`（已含从飞书导出的 33 条链接作为初始数据）。追加行用 `url,video_id` 格式即可，脚本自动去重、只处理新链接。

### 3. Enable GitHub Actions
- Go to repository Settings > Actions > General
- Ensure "Allow all actions and reusable workflows" is selected
- The workflow runs automatically every 30 minutes

### 4. Check results
- 直读飞书模式：结果直接写回飞书表格（B 脚本原文 / C 译文 / D video_id）
- 两种模式都会把历史备份到仓库 `output/results.csv` 和 `output/results.json`

## Output Format

| Field | Description |
|-------|-------------|
| `video_id` | TikTok video ID |
| `url` | Original TikTok URL |
| `author` | Author username |
| `description` | Video description (creator's text) |
| `original_text` | Transcribed transcript (spoken content) |
| `translated_text` | Chinese translation |
| `language` | Detected language |
| `duration` | Video duration |
| `extracted_at` | Extraction timestamp |
| `status` | success / error / no_audio_detected |
| `error_message` | Error details if any |

## Extraction Pipeline

1. **tikwm.com API** - Fetch video metadata (author, duration, description, download URL)
2. **Subtitle check** - If TikTok provides subtitles, parse them directly
3. **Whisper fallback** - Download video, extract audio, transcribe with faster-whisper (tiny model)
4. **Translation** - Google Translate via deep-translator library

## Configuration

### Whisper Model
Edit `src/main.py` to change the Whisper model:
- `tiny` (75MB, fastest, lower accuracy) - default
- `base` (150MB, balanced)
- `small` (500MB, better accuracy)

### Max Videos Per Run
Edit `src/main.py` and change `MAX_VIDEOS_PER_RUN` (default: 10).

### Schedule Frequency
Edit `.github/workflows/extract.yml` and change the cron expression:
- `*/30 * * * *` - Every 30 minutes (default)
- `0 * * * *` - Every hour
- `*/15 * * * *` - Every 15 minutes

## Manual Trigger
Go to Actions tab > "TikTok Script Extractor" > "Run workflow" to run manually.

## 直读飞书表格（推荐，无需导出 CSV）

自动化直接读你的飞书 wiki 表格并写回结果，用飞书**自建应用**的 `tenant_access_token`（不是 WorkBuddy 连接器）。`src/feishu_reader.py` 已实现，由 `src/main.py` 在检测到 Secrets 时自动启用。

### ✅ 已验证状态（2026-08-12 实跑）
- **读路径完全可用**：App ID/Secret → `tenant_access_token` → wiki 节点解析 → `sheets/query` 取 sheetId → 读 A:D 全部通过。
- **写回暂被权限挡住（HTTP 403）**：当前应用只有「只读」权限，写单元格会 403。按下面第 2、4 步开启「编辑」权限后即可写回。写回失败时 `main.py` 会自动降级为只产出仓库内的 `output/results.csv`（不崩溃）。

### 步骤
1. 打开 [飞书开放平台](https://open.feishu.cn/) → 进入**企业自建应用**（用你登录飞书的同一企业）。
2. **权限管理** → 开通并**发布版本**：
   - `wiki:readonly`（读取 wiki 节点 —— 已验证可用）
   - **`sheets:record`（读写电子表格）** ← 写回必须，否则 403
   - （备选）`sheets:spreadsheet` 或「查看、评论、编辑、管理云文档」也可
3. 应用凭证页拿到 `App ID` 和 `App Secret`。
4. **把应用加为表格的「可编辑」协作者**（关键，否则仍 403）：
   打开该电子表格 `https://ja0zlzeurz8.feishu.cn/sheets/UlRjsGSaXhejYit53vycr9Hlnbh`
   → 右上角「⋯」→「更多」→「添加文档应用」→ 搜索你的应用 → 选「可编辑」→ 确认。
   （也可在 wiki 页面 `https://ja0zlzeurz8.feishu.cn/wiki/CjWowMCM1ihF1jk1jmJcXbfAn6f` 同样添加。）
5. 仓库 `Settings > Secrets and variables > Actions > New repository secret` 添加：
   - `FEISHU_APP_ID` = 应用的 App ID
   - `FEISHU_APP_SECRET` = 应用的 App Secret
   - `FEISHU_WIKI_TOKEN` = wiki 链接里 `wiki/` 之后那段：`https://ja0zlzeurz8.feishu.cn/wiki/<这段>` → `CjWowMCM1ihF1jk1jmJcXbfAn6f`
   - （可选）`FEISHU_SHEET_TITLE` = 子表名，默认 `脚本读取`
6. 手动 Run 一次工作流验证；之后每 30 分钟自动跑。

> ⚠️ **切勿把 App Secret 提交进仓库代码**。只放在 GitHub Secrets（或你本地环境变量）。本项目的 `src/feishu_reader.py` 只从环境变量 `FEISHU_*` 读取，任何地方都不硬编码密钥。

### 数据流
```
GitHub Actions (ubuntu-latest)
  └─ tenant_access_token (App ID/Secret)
       └─ wiki node → spreadsheet token → 子表 sheetId
            ├─ 读 A:D：链接 / 原文 / 译文 / video_id
            ├─ 只对「原文空或 [download_failed] / 译文空」的行提取+翻译
            └─ 写回 B(原文) C(译文) D(video_id)，已有内容不覆盖
```

> 注：本机 WorkBuddy 里我读你表格用的是连接器（OAuth），与上面自动化用的自建应用是两套凭证。你只需按上面建好自建应用并填 Secrets，自动化就能脱离登录独立运行。

## 已知限制 / 调优
- **tikwm.com 免费档限流 1 req/s**：代码已加重试退避；若仍频繁失败，可调小 `src/main.py` 的 `MAX_VIDEOS_PER_RUN` 或降低频率。
- **带追踪参数的链接**（如 `?lang=en&is_copy_url=1`）tikwm 解析失败：代码已自动清洗 URL；仍失败者走 **yt-dlp 直接下载 + faster-whisper 转写** 兜底（GitHub 环境已装 yt-dlp + ffmpeg）。
- **Whisper 转写较慢**（每条约 1–3 分钟）：单次 run 默认处理 10 条，超出顺延到下一周期；`extract.yml` 的 `timeout-minutes` 已设 30，如仍不够可上调。

## TikTok Transcript Extraction Resources

| Tool | Type | Free Tier | URL |
|------|------|-----------|-----|
| tikwm.com | API | Unlimited | https://tikwm.com |
| RapidAPI TikTok Transcripts | API | 100/month | https://rapidapi.com |
| SocialKit | API | 20 requests | https://socialkit.dev |
| Social Fetch | API + Web | Limited | https://socialfetch.dev |
| Masa TikTok Scraper | Tool + API | 100 queries | https://masa.fi |
| Apify TikTok Scraper | Actor | Free tier | https://apify.com |
| sm_transcribe | Local tool | Unlimited | https://github.com/alobbs/sm_transcribe |

## Tech Stack

- Python 3.11
- faster-whisper (speech-to-text)
- tikwm.com API (video metadata)
- deep-translator (Google Translate)
- ffmpeg (audio extraction)
- GitHub Actions (automation)

## License

MIT

---

## Balance Monitor（广告余额监控）

工作日 09:00（北京时间）自动检查指定 TikTok 广告账户余额，低于安全线（近 7 天日均花费 × 14）时通过飞书机器人告警。

### 实现说明

- 使用 `advertiser/info` 接口读取余额，**无需 BC finance role**。
- 使用 `report/integrated/get` 读取近 7 天花费。
- 脚本会自动用 `TT_APP_ID` + `TT_APP_SECRET` 刷新 24h access token。

### 新增 Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret | 说明 |
|--------|------|
| `TT_APP_ID` | TikTok Marketing API App ID |
| `TT_APP_SECRET` | TikTok Marketing API App Secret |
| `TT_ACCESS_TOKEN` | 当前有效 access token（24h；脚本会自动刷新） |
| `FEISHU_BOT_WEBHOOK` | 飞书机器人 Webhook（已有，复用） |

### 可选 Variables（非必须）

| Variable | 说明 |
|----------|------|
| `TT_ADVERTISER_IDS` | 要监控的广告主 ID，逗号分隔；默认监控 5 个 Drbcare-MX-feishu 账户 |
| `TT_ADVERTISER_NAMES` | 名称映射，格式 `id1=名称1,id2=名称2` |

### 手动触发

仓库 Actions 页面 → `balance-monitor` → Run workflow。

### 结果

- 飞书推送：每账户余额 / 7 天花费 / 日均花费 / 安全线 / 是否告警
- `balance_report.json` 以 artifact 形式保留 30 天
