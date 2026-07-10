# B站收藏夹自动分类工具 设计文档

- 创建日期：2026-07-06
- 状态：待用户审查
- 作者：与用户共同 brainstorming 产出

## 1. 项目概述

一个**个人本地工具**，用于一键将 B 站某个收藏夹里的所有视频，由 AI 智能分类到多个子收藏夹中。运行后自动打开本地网页，扫码登录 B 站 → 选择源收藏夹 → AI 生成分类方案 → 用户预览并手动调整 → 确认后自动创建目标收藏夹并把视频移动过去。

分类结果通过 B 站 Web API 写入账号云端，因此手机 App、电脑客户端、网页版三端同步生效。

## 2. 需求

### 2.1 核心需求

| 编号 | 需求 | 说明 |
|---|---|---|
| R1 | AI 智能分类 | 用 OpenAI 兼容接口分析每个视频的标题/简介/标签/UP主，给出分类 |
| R2 | 扫码登录 B 站 | 模拟 Web 扫码登录流程，无需手动复制 Cookie |
| R3 | 半自动模式 | 生成方案 → 用户预览确认 → 才执行移动，不可逆操作有拦阻 |
| R4 | 本地网页界面 | 启动后自动开浏览器，单页应用驱动整个流程 |
| R5 | 响应式 + 移动端兼容 | 前端用 CSS media query 适配手机/平板/桌面 |
| R6 | 状态持久化 | 见第 7 节，免重启扫码、断点续作、视频缓存、手动调整保留 |
| R7 | 一键启动 | `pip install -r requirements.txt` + `python main.py` 即可运行，无前端构建步骤 |

### 2.2 非目标（YAGNI）

- 不支持多用户/多账号同时登录
- 不做云端部署、不做 Docker 镜像
- 不做视频下载、不做弹幕分析
- 不做收藏夹导出/导入
- 不做定时自动整理（一次性工具）
- 不做插件化、不做可配置分类算法

## 3. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端框架 | FastAPI + Uvicorn | 异步、自带 OpenAPI、单文件可启动 |
| HTTP 客户端 | httpx | 异步、API 友好、与 FastAPI 一致 |
| 数据库 | SQLite（标准库 `sqlite3`） | 零依赖、单文件、够用 |
| 二维码 | `qrcode[pil]` | 把扫码 URL 渲染成图片 |
| 前端 | 原生 HTML + Tailwind CSS(CDN) + Lucide 图标(CDN) + 原生 JS | 无构建步骤、CDN 引入、单文件 SPA、采用 Pinguo 设计 token（Apple 风格） |
| AI 接口 | OpenAI 官方 Python SDK | 配 `base_url` 即可兼容 DeepSeek/通义/Kimi/中转 |
| Python 版本 | 3.10+ | 用 match-case、type hint |

## 4. 架构

### 4.1 目录结构

```
bibi-tool/
├─ main.py                      # FastAPI 启动入口 + 路由
├─ requirements.txt
├─ config.json                  # 用户配置（AI key、模型）—— 永久
├─ bilibili_cookie.json         # B站登录凭证 —— 长期，重启免扫码
├─ bibi.db                      # SQLite 运行数据 —— 缓存与会话
├─ .gitignore                   # 忽略 cookie、config、db
├─ docs/superpowers/specs/      # 本文档所在
├─ core/
│  ├─ __init__.py
│  ├─ bilibili_api.py           # B站API封装（扫码、收藏夹、移动、WBI签名）
│  ├─ ai_classifier.py          # 调用 OpenAI 兼容接口
│  ├─ storage.py                # SQLite + JSON 读写
│  ├─ session.py                # 分类会话状态机
│  └─ errors.py                 # 自定义异常
└─ static/
   ├─ index.html                # SPA 单页（含6视图 + Pinguo设计token + Tailwind/Lucide CDN）
   └─ app.js                    # 前端逻辑（视图切换 + SSE消费 + 失败项展示）
```

### 4.2 模块划分

每个模块单一职责、可独立测试。

- **`core/bilibili_api.py`**：封装所有 B 站 HTTP 调用。对外暴露 `BilibiliClient` 类，方法包括 `qrcode_generate()` / `qrcode_poll()` / `get_my_folders()` / `get_folder_videos(fid)` / `get_video_info(bvid)` / `create_folder(title)` / `move_videos(src_fid, dst_fid, avids)`。内部处理 WBI 签名、cookie 注入、错误码翻译。
- **`core/ai_classifier.py`**：封装 AI 分类。对外暴露 `classify(videos: list[VideoInfo]) -> list[Classification]`。内部组装 prompt、调用 OpenAI 兼容接口、解析 JSON 返回、处理 token 超限分批。
- **`core/storage.py`**：封装所有持久化。对外暴露 `Storage` 类，方法包括 `load_config()` / `save_config()` / `load_cookie()` / `save_cookie()` / `upsert_folder()` / `upsert_video()` / `create_session()` / `save_classification()` / `load_session()` 等。内部用 `sqlite3` + JSON 文件。
- **`core/session.py`**：分类会话状态机。维护一个 `ClassifySession`，状态转换：`draft`（创建会话）→ `collecting`（拉视频）→ `classifying`（AI 调用中）→ `pending_review`（等用户预览）→ `executing`（移动中）→ `done`/`failed`。协调 `bilibili_api` + `ai_classifier` + `storage`。
- **`main.py`**：FastAPI 路由层，薄。每个路由调 `session` 或 `storage` 的方法，返回 JSON。不写业务逻辑。
- **`static/`**：前端。`app.js` 调后端接口驱动流程，`style.css` 处理响应式。

### 4.3 数据流（半自动主流程）

```
[用户启动] main.py
   │ 自动开浏览器 → http://127.0.0.1:8765
   ▼
[前端 index.html]
   │ GET /api/state  → 后端检查 cookie
   ├─ 未登录 → 显示二维码
   │           POST /api/qrcode/generate → B站API
   │           GET  /api/qrcode/poll      → 轮询直到成功
   │           成功 → 保存 cookie.json → 跳下一步
   ▼
[已登录主界面]
   │ GET /api/folders → B站API + SQLite缓存
   │ 用户选源收藏夹 → POST /api/session {source_fid}
   ▼
[会话 draft→collecting]
   │ GET /api/session/{id}/collect → 分页拉视频 + 缓存到 videos 表
   │ 完成后 collecting→classifying
   ▼
[会话 classifying]
   │ 前端建立 SSE: GET /api/session/{id}/stream
   │   后端推送事件：
   │     event: stage  data: {"stage":"collecting","progress":0.3}
   │     event: stage  data: {"stage":"classifying","progress":0.6,"batch":2,"total_batches":4}
   │     event: done  data: {"stage":"pending_review"}
   │   session 在每个阶段写进度到 SQLite + 通过 SSE 推送
   │ 结果存 classifications 表 → classifying→pending_review
   ▼
[预览界面 pending_review]
   │ GET /api/session/{id} → 返回方案 + 视频详情
   │ 用户手动调整：
   │   POST /api/session/{id}/adjust {avid, new_category}
   │ 用户点"确认执行" → POST /api/session/{id}/execute
   ▼
[会话 executing]
   │ 后端按方案：
   │   1. 对每个新分类名 → create_folder(title) → 拿到 dst_fid
   │   2. 对该分类下的所有 avid → move_videos(src_fid, dst_fid, avids)
   │   3. 失败的记到 failed_items 表（avid, title, error_code, error_message）
   │ 完成后 executing→done（含失败列表）
   ▼
[结果界面 done]
   │ GET /api/session/{id}/failed-items → 返回失败项详情列表
   │ 显示成功/失败/总计三栏统计 + 失败项卡片（标题+错误原因）
   │ 失败的可重试 → POST /api/session/{id}/retry-failed
```

## 5. B站 API 方案

> 注：B 站 API 非官方、会变动。实现时第一步是用真实 cookie 跑通每个接口并验证参数。下表是设计阶段基于已知信息的规划，实现时若接口已变更，以实际为准并在代码注释中记录。

### 5.1 扫码登录

- 生成二维码：`GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate?returnType=0`
  - 返回 `data: { url, qrcode_key, returnMessage }`
  - `url` 是扫码内容，用 `qrcode` 库渲染成图片返回前端
- 轮询状态：`GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={key}`
  - `code=0` 成功，响应头里的 `Set-Cookie` 含 `SESSDATA`/`DedeUserID`/`bili_jct` 等
  - `code=86038` 未扫码；`code=86090` 已扫码待确认；`code=86090`（不同子状态）已确认待登录
  - 成功后用 `httpx` 的 cookie jar 提取所有 cookie，存 `bilibili_cookie.json`

### 5.2 收藏夹操作

| 用途 | 方法 + URL | 关键参数 | 备注 |
|---|---|---|---|
| 我的收藏夹列表 | `GET /x/v3/fav/folder/created/list-all` | `up_mid`, `type=0` | 需 WBI 签名 |
| 收藏夹内容 | `GET /x/v3/fav/resource/list` | `media_id`, `pn`, `ps=20`, `order=mtime` | 需 WBI 签名，分页拉完 |
| 创建收藏夹 | `POST /x/v3/fav/folder/add` | `title`, `intro`, `privacy=1`（默认私密）, `cover` | 需 WBI 签名 + CSRF token(`bili_jct`) |
| 移动视频 | `POST /x/v3/fav/resource/move` | `src_media_id`, `tar_media_id`, `resources`(格式 `avid:2` 用逗号拼接) | 需 CSRF token |

**CSRF**：POST 请求需带表单字段 `csrf` = cookie 里的 `bili_jct`。

### 5.3 视频信息（用于 AI 分析）

- 单视频详情：`GET /x/web-interface/view?bvid={bvid}` → 标题、简介、UP主、封面、分P等
- 标签：`GET /x/tag/archive/tags?bvid={bvid}` → 标签列表

但**收藏夹内容接口本身已经返回**标题、封面、UP主名、首个avid，**不返回简介和标签**。设计上分两档：

- **快速模式（默认）**：只用收藏夹接口返回的 标题 + UP主 + 分区名 做 AI 分类，不额外请求。快、省调用。
- **完整模式（可选）**：对每个视频额外请求 `view` + `tags` 拿简介和标签。准、慢、API 调用多。

前端在选源收藏夹后给一个开关。默认快速模式。

### 5.4 WBI 签名

部分 GET 接口需要 WBI 签名。流程：

1. `GET /x/web-interface/nav` → 取 `data.wbi_img.img_url` 和 `data.wbi_img.sub_url`
2. 从 URL 末段提取 img_key、sub_key，按 B 站固定的 64 位打乱表合成 mixin_key（62 位有效）
3. 给请求参数加 `wts`(秒级时间戳)，按 key 字典序排序，拼成 `k1=v1&k2=v2`
4. 计算 `w_rid = md5(query + mixin_key)`，加到参数中

`core/bilibili_api.py` 内置一个 `_wbi_sign(params)` 私有方法，img_key/sub_key 缓存到 SQLite（key 失效再重新拉 nav）。

### 5.5 速率限制

- 轮询扫码：前端每 2 秒一次，最多 3 分钟超时
- 拉收藏夹视频：每页之间 sleep 300ms，避免触发风控
- 移动视频：批量接口一次最多 50 个 avid，每批之间 sleep 500ms

## 6. AI 分类流程

### 6.1 Prompt 设计

给 AI 一个 system prompt 定义任务，再给一批视频的精简元数据，要求返回 JSON。

System prompt（要点）：
- 角色：B站内容分类助手
- 输入：一批视频的 {title, up_name, [tags], tname(分区)}
- 输出：JSON 数组，每项 {avid, category, confidence(0-1), reason(≤20字)}
- 约束：分类名是中文短语（2-6字），如"编程教程""美食""游戏解说""音乐MV""知识科普""生活vlog"
- 约束：同一批视频的分类数控制在 3-10 个之间，相近主题合并
- 约束：confidence < 0.6 的也给出最佳猜测，不要"其他"

User message：JSON 序列化的视频列表。

返回解析：用 OpenAI SDK 的 `response_format={"type":"json_object"}` 或在 prompt 里强制 JSON，后端 `json.loads` + 校验。

### 6.2 分批策略

- 单批最多 50 个视频（控制 token）
- 分批结果合并后，对所有出现的 category 做一次**二次归并**：再给 AI 一轮，把"编程"/"编程教程"/"代码教学"合并成一个标准名。避免跨批次分类名漂移。
- 二次归并只对 category 名列表做，不重跑视频，开销小。

### 6.3 失败处理

- AI 调用超时/限流：自动重试 3 次，指数退避
- 返回 JSON 解析失败：记录到 session.failed_items，该批视频标记为"未分类"，不阻塞整体流程
- API key 无效：直接返回错误到前端，不重试

## 7. 状态持久化

### 7.1 三层存储

| 层 | 文件 | 内容 | 生命周期 | 写入时机 |
|---|---|---|---|---|
| 配置 | `config.json` | `ai_base_url`, `ai_api_key`, `ai_model`, `default_privacy` | 永久 | 前端"设置"页保存 |
| 凭证 | `bilibili_cookie.json` | SESSDATA, DedeUserID, bili_jct 等 + 过期时间 | 长期 | 扫码成功时 |
| 运行数据 | `bibi.db` (SQLite) | 见 7.2 | 可清理 | 流程各步骤 |

### 7.2 SQLite 表

```sql
-- 收藏夹缓存
CREATE TABLE fav_folders (
  fid INTEGER PRIMARY KEY,
  title TEXT,
  media_count INTEGER,
  cover_url TEXT,
  cached_at TEXT
);

-- 视频缓存
CREATE TABLE videos (
  avid INTEGER PRIMARY KEY,
  bvid TEXT,
  title TEXT,
  intro TEXT,
  tags TEXT,           -- JSON 数组
  up_name TEXT,
  up_mid INTEGER,
  cover_url TEXT,
  tname TEXT,          -- B站分区名
  fid INTEGER,         -- 所属收藏夹
  cached_at TEXT
);

-- 分类会话
CREATE TABLE classify_sessions (
  session_id TEXT PRIMARY KEY,    -- uuid
  source_fid INTEGER,
  status TEXT,                    -- draft/collecting/classifying/pending_review/executing/done/failed
  mode TEXT,                      -- quick/full
  created_at TEXT,
  updated_at TEXT,
  stats TEXT                      -- JSON: {total, success, failed}
);

-- 分类结果
CREATE TABLE classifications (
  session_id TEXT,
  avid INTEGER,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,     -- 用户是否手动调整过
  executed INTEGER DEFAULT 0,     -- 是否已成功移动
  PRIMARY KEY (session_id, avid)
);

-- WBI 密钥缓存
CREATE TABLE wbi_keys (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  img_key TEXT,
  sub_key TEXT,
  cached_at TEXT
);

-- 失败项记录（执行阶段）
CREATE TABLE failed_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  avid INTEGER,
  title TEXT,
  category TEXT,
  target_fid INTEGER,
  error_code TEXT,
  error_message TEXT,
  retried INTEGER DEFAULT 0,
  created_at TEXT,
  FOREIGN KEY (session_id) REFERENCES classify_sessions(session_id)
);
```

### 7.3 状态机不变式

- `status` 转换只能按第 4.3 节的箭头方向走，`session.py` 内集中校验
- `pending_review` 状态下允许调整 classifications（用户改方案）；其他状态只读
- `executing` 状态下 classifications 不可改
- 重启后：加载所有未 `done` 的会话，状态回退到最近的可交互点：
  - `collecting`/`classifying` → 回退到 `draft`，提示重做
  - `pending_review` → 直接回到预览界面（这是断点续作的核心）
  - `executing` → 回退到 `pending_review`，提示重新执行

### 7.4 敏感信息

`config.json`（含 AI key）和 `bilibili_cookie.json`（含登录态）为**明文 JSON**。理由：个人本地工具，加密引入密钥管理新问题，收益不抵复杂度。通过 `.gitignore` 防止提交：

```
config.json
bilibili_cookie.json
bibi.db
__pycache__/
.venv/
```

README 会显眼提示不要分享这两个文件。

## 8. 错误处理

| 场景 | 处理 |
|---|---|
| B站接口返回 -101（未登录） | 清 cookie 文件，前端跳回登录页 |
| B站接口返回 -403（风控/权限） | 停止流程，前端提示"被风控，稍后再试" |
| B站接口返回 -509（限流） | 自动 sleep 10s 重试 1 次，仍失败则停止 |
| AI 接口 401 | 前端提示"API Key 无效"，跳设置页 |
| AI 接口 429 限流 | 指数退避重试 3 次 |
| 移动视频部分失败 | 记入 `failed_items` 表（含 avid/title/分类/错误码/错误信息），继续其他，最后给重试入口 |
| 网络异常 | httpx 抛 `TransportError`，前端显示"网络异常" |
| SSE 连接中断 | 前端 EventSource 自动重连；重连后从 SQLite 读最新 stage 恢复进度条 |
| SQLite 写失败 | 不可恢复，抛错到前端，提示重启 |
| 扫码 3 分钟未确认 | 前端提示二维码过期，提供"刷新二维码"按钮 |

所有自定义异常在 `core/errors.py` 定义，继承 `BibiError`，带 `code` 和 `user_message` 字段，FastAPI 全局异常处理器统一翻译成前端可读的 JSON。

## 9. 测试策略

- **单元测试（pytest）**：
  - `ai_classifier`：mock OpenAI SDK，测分批、测 JSON 解析失败兜底、测二次归并
  - `storage`：用临时 SQLite 文件，测各表 CRUD、测会话状态回退逻辑
  - `session`：mock `bilibili_api` 和 `ai_classifier`，测状态机转换、测断点续作回退
  - `bilibili_api._wbi_sign`：用 B 站官方文档给的样例参数和样例 mixin_key，验证 `w_rid` 计算正确
- **接口契约测试**：对 `bilibili_api` 的每个方法，用 `respx` mock B 站 HTTP 响应，验证请求参数和返回解析
- **手动验证清单**（README 提供）：
  - 用真实账号扫码登录跑通全流程
  - 测试断点续作：分类中关浏览器，重启，能回到预览
  - 测试 cookie 过期：手动改 cookie 文件让它失效，验证能跳回登录
  - 移动端浏览器访问 `http://电脑IP:8765`，验证响应式

不写 E2E 自动化（一次性工具，手动验证清单够用，符合 YAGNI）。

## 10. 风险与权衡

| 风险 | 应对 |
|---|---|
| B 站 API 变动 | 所有调用集中在 `bilibili_api.py`，变更只改一处；接口路径常量集中定义 |
| WBI 签名实现出错 | 用官方文档样例做单元测试锚定；签名失败时回退到不签名直接请求（部分接口不强制） |
| AI 分类质量不稳定 | 二次归并 + 用户手动调整兜底；confidence 透明展示给用户 |
| 大收藏夹（1000+视频）耗时 | 前端展示进度条；视频信息缓存；分批 AI 调用并行（受限于 API 限流，最多并发 2） |
| 移动视频不可逆 | 半自动预览 + 执行前再确认弹窗 + 默认创建私密收藏夹 |
| 明文存 cookie/key | 个人本地工具，`.gitignore` + README 警告，不做加密（YAGNI） |

## 11. UI 设计规范

采用 `bibi-tool-ui-draft/` 中的 Pinguo 设计系统（Apple HIG 风格）。

### 11.1 设计 token

所有颜色/阴影/圆角/间距用 CSS 变量定义在 `:root`，支持 `.dark` 暗色覆盖。关键 token：
- brand：`--brand-50` 到 `--brand-900`（主色 #007AFF 系）
- background/text/icon：各 10 阶
- state-success（#34C759）/ state-error（#FF3B30）
- 语义角色：`--primary` `--card` `--border` `--muted-foreground` 等
- 圆角 `--radius: 1.2rem`，阴影 8 级，间距 `--spacing: 0.24rem`

### 11.2 6 个视图

草稿提供 6 个 HTML 设计稿，实现时合并为单 `index.html`，用 JS 切换 `<section>` 显示：

| 视图 | 草稿文件 | 关键元素 |
|---|---|---|
| AI配置 | config.html | Base URL/API Key/模型名 三字段，`config-save` 按钮 |
| 扫码登录 | login.html | 二维码图（替换SVG占位为后端base64 PNG）、状态脉冲点、`login-refresh-qr` |
| 选源收藏夹 | home.html | `resume-session` 继续横幅、快速/完整模式 segmented control、`select-folder` 卡片列表 |
| 整理中 | progress.html | 三步骤指示器（拉取视频→AI分类→预览方案）、百分比进度条、SSE驱动 |
| 预览方案 | review.html | 按分类分组（chart色板色条）、视频行（封面/标题/UP主/置信度badge/分类下拉）、底部sticky `execute-confirm` |
| 完成 | result.html | 成功/失败/总计三栏、失败项列表（标题+错误原因）、`retry-failed` + `back-home` |

### 11.3 交互钩子约定

所有交互元素用 `data-dom-id="xxx"` 标记，前端 JS 通过 `document.querySelector('[data-dom-id="xxx"]')` 查找。这是草稿统一约定，实现必须沿用。

### 11.4 外部依赖（CDN）

- Tailwind CSS：`https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.1/dist/index.global.js`
- Lucide 图标：`https://unpkg.com/lucide@1.8.0/dist/umd/lucide.min.js`

工具需联网调用 B 站和 AI API，CDN 依赖可接受。每次视图切换后调用 `lucide.createIcons()` 渲染图标。

### 11.5 动画

草稿定义了 pulse-dot（扫码状态）、shimmer（进度条）、pulse-ring（步骤指示器）、scale-in（成功图标）、spin-slow（加载图标）等动画。均需 `@media (prefers-reduced-motion: reduce)` 关闭。

### 11.6 响应式

草稿用 Tailwind 响应式类（`sm:` 断点）+ `flex-wrap` 处理移动端。视频行在窄屏下下拉框换行到整行。无需额外手写 media query。

## 12. 开放问题

无。所有关键决策已敲定：
- UI 采用 Pinguo 设计系统（bibi-tool-ui-draft）
- 进度报告用 SSE 实时推送
- 失败项存独立 failed_items 表
- 实现时若 B 站接口实测与第 5 节不符，以实测为准并回填本文档
