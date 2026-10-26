# Goofish Monitor（闲鱼关键词监控）

面向「闲鱼关键词监控 + 自定义商品配置 + 评估流水线」的 Python 服务。

- **已实现**：商品配置 / 监控任务 CRUD、pause/resume、健康检查、Listing 去重写入、**规则评估（推荐/待观察/不推荐）**、**可选 AI 卖家画像 + 中文理由**、**运行日志（run_logs）**、联调 API（run-once / dry-run）
- **采集**：`COLLECTOR_BACKEND=stub`（默认安全启动）或 `playwright`（联调 / 真采集）
- **登录**：Playwright **持久化浏览器配置**（`data/browser_profile/`）；Cookie 导出在部分环境不可用
- **推送**：评估为「推荐」后可选 Webhook（企业微信机器人）/ Telegram；按 listing+结论去重
- **不做**：验证码绕过、凭证窃取、反爬 exploit；仅运营者自己账号登录到本地配置目录

技术栈：Python · FastAPI · Jinja2 · SQLite · APScheduler · httpx · Playwright

---

## 目录结构

```
goofish-monitor/
├── app/
│   ├── main.py
│   ├── db.py
│   ├── models/              # schemas + SQLAlchemy 表
│   ├── api/                 # dashboard / configs / tasks / listings / logs / collectors / health
│   ├── collectors/          # base + stub + goofish (Playwright 持久化配置)
│   ├── scripts/             # login_goofish 首次有头登录
│   ├── services/            # upsert / collect cycle / run_logs / evaluate
│   ├── analyzers/           # rules + ai（OpenAI 兼容）
│   ├── workers/             # APScheduler
│   └── push/                # 推送（企业微信 webhook / Telegram）
├── templates/               # Jinja2 监控概览页面
├── static/                  # 概览 CSS
├── tests/
│   ├── fixtures/sample_listings.json
│   └── test_*.py
├── data/                    # SQLite + browser_profile/
├── requirements.txt
├── .env.example
└── README.md
```

---

## 安装

```bash
cd /workspace/goofish-monitor   # 或你的项目路径
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

使用 Playwright 真采集时，还需安装浏览器：

```bash
playwright install chromium
# 部分 Linux 还需要：playwright install-deps chromium
```

---

## 启动

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **监控概览（中文 Dashboard）**：http://127.0.0.1:8000/  
  展示运行中任务数、已扫描商品数、推荐发现数、任务卡片（关键词 / 均价）、最近评估。
- **开始新监测**：http://127.0.0.1:8000/new  
  中文表单创建 `ProductConfig`（关键词 / 价格 / 规格 / 排除词等），并自动创建 `MonitorTask`；勾选「立即开始监测」则以 `running` 启动。成功后跳转 `/tasks?flash=…`。
- **任务管理**：http://127.0.0.1:8000/tasks  
  列表（配置名 / 关键词 / 状态 / 间隔 / 最近运行）+ 启动/暂停、立即执行、改间隔、创建任务（选 config + interval）。页面经 `fetch` 调用现有 `/api/tasks*`。
- **结果查看**：http://127.0.0.1:8000/results  
  卡片式评估（推荐 / 待观察 / 不推荐），含标题、价格、理由、商品链接；可按结论 / 任务筛选。
- **运行日志**：http://127.0.0.1:8000/logs
- **账号管理**：http://127.0.0.1:8000/accounts  
  会话状态（配置 mtime / 可选探测）+ 登录脚本说明 / 有头登录按钮。
- **系统设置**：http://127.0.0.1:8000/settings  
  只读展示采集 / AI / **推送是否已配置**（不展示密钥），链到 README。
- 文档：http://127.0.0.1:8000/docs  
- 健康：`curl -s http://127.0.0.1:8000/health`

---

## 环境变量（采集相关）

| 变量 | 说明 | 默认 |
|------|------|------|
| `COLLECTOR_BACKEND` | `stub` \| `playwright` | `stub` |
| `GOOFISH_HEADLESS` | Playwright 无头（定时采集） | `true` |
| `GOOFISH_TIMEOUT_MS` | 页面超时（毫秒） | `30000` |
| `GOOFISH_USER_DATA_DIR` | Chromium 持久化配置目录 | `data/browser_profile` |
| `GOOFISH_COOKIES` / `GOOFISH_COOKIES_FILE` | 可选遗留 Cookie（优先用持久化配置） | 空 |
| `GOOFISH_FIXTURE_PATH` | JSON 样例列表路径（CI/冒烟，不走真网） | 空 |
| `GOOFISH_SKIP_PROBE` | 跳过 /accounts 实时会话探测 | `false` |
| `SCHEDULER_ENABLED` | 是否启动调度 | `true` |
| `SCHEDULER_TIMEZONE` | 调度时区 | `Asia/Shanghai` |
| `AI_API_BASE` | OpenAI 兼容 API 根路径（含 `/v1`） | `https://api.openai.com/v1` |
| `AI_API_KEY` | API Key；**未设置则跳过 AI** | 空 |
| `AI_MODEL` | 模型名 | `gpt-4o-mini` |
| `AI_ALWAYS` | `true` 时对「不推荐」也调用 AI | `false` |
| `PUSH_WEBHOOK_URL` | 企业微信机器人或通用 Webhook URL | 空（不推送） |
| `PUSH_TELEGRAM_BOT_TOKEN` | Telegram Bot Token（可选） | 空 |
| `PUSH_TELEGRAM_CHAT_ID` | Telegram Chat ID（可选） | 空 |
| `PUSH_INCLUDE_WATCH` | `true` 时「待观察」也推送 | `false` |

---

## 联调步骤（推荐）

### A. 无网络冒烟（fixture）

适合验证「搜索 → 过滤 → upsert → API」整条链路，不访问闲鱼。

```bash
export COLLECTOR_BACKEND=playwright
export GOOFISH_FIXTURE_PATH=tests/fixtures/sample_listings.json
export SCHEDULER_ENABLED=false

# 单元/冒烟
pytest -q

# 或起服务后 dry-run / run-once
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
sleep 1

curl -s -X POST http://127.0.0.1:8000/api/collectors/goofish/dry-run \
  -H 'Content-Type: application/json' \
  -d '{"keyword":"MacBook","price_max":6000}'

# 先创建 config + task，再：
curl -s -X POST http://127.0.0.1:8000/api/tasks/1/run-once
curl -s 'http://127.0.0.1:8000/api/listings?limit=10'
```

### B. 首次登录 → 定时真扫（持久化浏览器配置）

Cookie 导出在部分环境会被拦截；**产品路径**是 Playwright `launch_persistent_context`，会话落在 `data/browser_profile/`（可用 `GOOFISH_USER_DATA_DIR` 覆盖）。

#### 1. 有头登录一次

需要图形界面（本机桌面 / VNC / 云主机有头一次）：

```bash
cd /workspace/goofish-monitor   # 或你的项目路径
source .venv/bin/activate
playwright install chromium
python -m app.scripts.login_goofish
```

在弹出的 Chromium 中登录闲鱼；脚本检测到个人入口后退出，配置已写入磁盘。  
无桌面服务器：在本地登录后，把整个 `data/browser_profile/` 同步到服务器同路径。

也可打开页面 **账号管理** `/accounts`，查看会话状态；若本机有 `DISPLAY`，可点「尝试打开登录窗口」（`POST /api/accounts/goofish/open-login`）。

#### 2. 开启 24h 调度真扫

```bash
export COLLECTOR_BACKEND=playwright
export GOOFISH_HEADLESS=true
# export GOOFISH_USER_DATA_DIR=data/browser_profile   # 可选
unset GOOFISH_FIXTURE_PATH
export SCHEDULER_ENABLED=true

uvicorn app.main:app --host 0.0.0.0 --port 8000
# 在 /tasks 将任务设为「运行中」，或：
curl -s -X POST http://127.0.0.1:8000/api/tasks/1/resume
```

调度器按 `interval_minutes` 调用 `run_collect_cycle`。未登录时写入运行日志事件 **`login_required`**，任务置为 `error`（不崩循环）；登录并 resume 后继续。

#### 3. 联调确认

```bash
curl -s -X POST http://127.0.0.1:8000/api/collectors/goofish/dry-run   -H 'Content-Type: application/json'   -d '{"keyword":"MacBook","price_max":6000}'
curl -s 'http://127.0.0.1:8000/api/accounts/goofish/status'
```

遇到验证码 / 风控时返回 `blocked`，**不会绕过**；请人工处理后重试。

### 搜索与排序说明

- 搜索 URL：`https://www.goofish.com/search?q={keyword}`
- **新发布 / 捡漏**：采集器会尝试点击「新发布」「最新」。列表接口常见为 `mtop.taobao.idlemtopsearch.pc.search`，签名在浏览器侧生成，**不要**用 httpx 伪造 sign。
- 优先拦截 XHR/fetch JSON，失败再 DOM；商品 URL：`https://www.goofish.com/item?id={id}`。

---

## 示例：创建配置 + 任务 + 跑一次

```bash
# 1. 配置
curl -s -X POST http://127.0.0.1:8000/api/configs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "MacBook Air M2 捡漏",
    "keyword": "MacBook Air M2",
    "price_min": 4000,
    "price_max": 6500,
    "exclude_keywords": ["碎屏", "进水", "维修机"],
    "sort_default": "newest",
    "enabled": true
  }'

# 2. 任务（paused 也可 run-once）
curl -s -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"config_id": 1, "interval_minutes": 30, "status": "paused"}'

# 3. 立即采集一次
curl -s -X POST http://127.0.0.1:8000/api/tasks/1/run-once

# 4. 恢复定时
curl -s -X POST http://127.0.0.1:8000/api/tasks/1/resume
```

---

## 已实现接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含 `collector_backend`） |
| GET/POST | `/api/configs` | 配置 CRUD |
| GET/PUT/DELETE | `/api/configs/{id}` | |
| GET/POST | `/api/tasks` | 任务 CRUD |
| GET/PUT/DELETE | `/api/tasks/{id}` | |
| POST | `/api/tasks/{id}/pause` | 暂停 |
| POST | `/api/tasks/{id}/resume` | 恢复 |
| POST | `/api/tasks/{id}/run-once` | **联调**：立即采集并 upsert，返回 fetched/inserted/updated/errors |
| POST | `/api/collectors/goofish/dry-run` | **联调**：搜索不写库，body=`{keyword, price_max?}` |
| GET | `/api/accounts/goofish/status` | 会话状态（`?probe=true` 可选实时探测） |
| POST | `/api/accounts/goofish/open-login` | 有 DISPLAY 时启动有头登录；否则返回脚本说明 |
| GET | `/api/listings` | 列表 |
| GET | `/api/listings/{id}` | 单条 |
| GET | `/api/evaluations` | 评估列表（可筛 `?verdict=` / `?config_id=`） |
| GET | `/api/logs` | **运行日志**（可筛 `?limit=` / `?task_id=` / `?level=`） |
| GET | `/api/listings/{id}/evaluations` | 某商品的评估历史 |
| POST | `/api/listings/{id}/evaluate` | **手动重评**（规则 + 条件触发 AI，默认 force） |

---


## 规则评估

采集 upsert 之后，对每条 listing 跑 `app/analyzers/rules.py`，结果写入 `evaluations`（`analyzer=rules`）。

### 结论方案

| 结论 | 条件 |
|------|------|
| **不推荐** | 任一硬条件失败（超预算 / 低于预算 / 型号不符 / 尺寸不符 / 品牌不符 / 命中排除词 / 疑似非个人） |
| **待观察** | 硬条件全过，但软偏好有未命中（内存 / 存储 / 成色 / 电池等） |
| **推荐** | 硬条件全过，且软偏好均满足（或未设置软偏好） |

中文 `reason` + `tags[]` 便于卡片展示。规则之后可选跑 AI（见下节）。

### 重评策略

- **content_hash 未变**且已有同 config 的 rules 评估 → **跳过**（规则与 AI 都不重跑）
- content_hash 变化或 `POST .../evaluate?force=true` → 追加新 rules 行；若门控通过再追加 ai 行

### 查看结果

```bash
# run-once 后
curl -s 'http://127.0.0.1:8000/api/evaluations?limit=20'
curl -s 'http://127.0.0.1:8000/api/evaluations?verdict=推荐'
curl -s 'http://127.0.0.1:8000/api/listings/1/evaluations'
curl -s -X POST 'http://127.0.0.1:8000/api/listings/1/evaluate'
```

MacBook 示例配置（预算 6500、14 寸 M2 Pro）见 `tests/test_rules_eval.py`。

---

## AI 评估（卖家画像 + 中文理由）

在规则之后可选调用 **OpenAI 兼容** `POST {AI_API_BASE}/chat/completions`（DeepSeek / 通义 / 本地 vLLM 等只要兼容即可）。

### 何时调用

| 条件 | 行为 |
|------|------|
| 未设置 `AI_API_KEY` | **跳过**，打日志，仅规则写库 |
| 规则结论为 **推荐 / 待观察** | 调用 AI |
| 规则结论为 **不推荐**（硬失败） | **默认不调用**（省 token） |
| `AI_ALWAYS=true` | 连「不推荐」也调用 |

### 与规则的关系 / 持久化选择

1. **规则永远先跑**，并写入 `evaluations`，`analyzer=rules`。
2. AI 跑通后 **再追加一行** `analyzer=ai`（同一 listing / config），用 AI 的 `reason`（中文 1～3 句卡片文案）、`tags`、以及 `details.seller_profile`（`个人` / `疑似商家` / `不确定`）。
3. AI 可给出可选 `verdict_adjust`；落库的 ai 行 `verdict` = adjust 或沿用规则结论。**不覆盖** rules 行，便于对比。
4. AI HTTP/解析失败时只打 warning，流水线仍以规则结果为准。

### 配置示例

```bash
export AI_API_BASE=https://api.openai.com/v1   # 或其他兼容端点
export AI_API_KEY=sk-...
export AI_MODEL=gpt-4o-mini
# export AI_ALWAYS=false
```

### 费用控制建议

- 默认不对硬失败烧 token；只有候选（推荐/待观察）才问模型。
- 选小模型（如 `gpt-4o-mini` / 国产低价兼容模型）。
- 内容未变（`content_hash` 相同）会跳过整段评估，避免重复计费。
- 联调 / CI：不设 `AI_API_KEY`；单测用 mock httpx（见 `tests/test_ai_eval.py`）。

---


## 运行日志

采集 / 评估 / 调度会写入 `run_logs` 表，侧栏「运行日志」页与概览「实时动态」均可查看。

| 字段 | 说明 |
|------|------|
| `level` | `info` / `warn` / `error` |
| `event` | `collect_start` / `collect_done` / `evaluate_done` / `push_sent` / `push_skipped` / `error` / `scheduler` / `login_required` |
| `message` | 中文摘要 |
| `details` | JSON（计数、错误码等） |

```bash
# 跑一轮后查看
curl -s 'http://127.0.0.1:8000/api/logs?limit=20'
curl -s 'http://127.0.0.1:8000/api/logs?task_id=1&level=error'
# 页面：http://127.0.0.1:8000/logs
```

---

## Listing 去重

按 `external_id` 去重：首次写入 `first_seen` / `collected_at`；再次见到更新 `last_seen`、标题/价格/URL 与 `content_hash`。

---

## 推送通知（推荐）

评估流水线在 **新写出** rules 结论且结论为 **推荐**（可选 **待观察**）时推送；按 `listing_id + verdict` 去重，同一商品同一结论不会反复刷屏。

### 开启企业微信群机器人

1. 在企业微信群 → 添加群机器人 → 复制 Webhook 地址（形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`）。
2. 写入项目根目录 `.env`（或导出环境变量）后 **重启** uvicorn：

```bash
# .env
PUSH_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的KEY
# 可选：待观察也推
# PUSH_INCLUDE_WATCH=true
```

3. 打开 **系统设置** `/settings`，确认 `PUSH_WEBHOOK_URL` 显示为「已配置」（页面不展示密钥）。
4. 跑一轮采集 / `run-once`，对「推荐」商品应出现运行日志事件 **`push_sent`**；未配置则为 **`push_skipped`**（reason=`not_configured`）；已推过则为 `already_pushed`。

通用 Webhook：任意接收 `POST JSON` 的 URL 亦可；非企业微信域名时 body 含 `title` / `price` / `reason` / `url` / `verdict` / `listing_id` 以及兼容字段 `text.content`。

### 可选 Telegram

```bash
PUSH_TELEGRAM_BOT_TOKEN=123456:ABC...
PUSH_TELEGRAM_CHAT_ID=你的chat_id
```

可与 Webhook 同时启用（都会发）。

### 联调 / 单测

```bash
# 不设 PUSH_* 时安全跳过
pytest -q tests/test_push.py
```

---

## 后续扩展

- 详情字段补全
- 前端评估卡片（优先展示 `analyzer=ai` 的 reason）
- 推送渠道配置 UI（当前仅 env）

---

## License

内部项目，按需使用。
