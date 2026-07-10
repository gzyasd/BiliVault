# BiBiTool 项目审查报告

- 审查日期：2026-07-07
- 审查范围：`docs/superpowers` 规划文档、后端 `main.py`/`core/`、前端 `static/`、单元测试 `tests/`
- 结论：当前代码不是完整可验收版本。界面和主要模块已搭起来，项目自有单测在限定 `tests/` 时通过，但存在会阻断真实使用的缺陷：扫码登录误判、SSE 进度不工作、完整模式未实现、失败重试会丢失仍失败的记录，默认测试命令也不可用。

> 后续修复：上述 P0/P1 问题已在 2026-07-07 修复，修复内容与接口核验记录见 `docs/fix-verification-2026-07-07.md`。

## 总体判断

项目已经实现了一个可运行的 FastAPI + 原生前端骨架，包含配置页、扫码页、收藏夹选择、整理中、预览、结果页，以及 SQLite 持久化表和若干 mocked 单元测试。

但它目前不能可靠满足原始需求中的“扫码登录后整理收藏夹并实时展示进度”的核心闭环。真实 B 站二维码轮询接口的返回结构与代码假设不一致，导致未扫码也会被当成成功；SSE 进度回调在 FastAPI 路由中是异步函数，但状态机没有 await，导致前端收不到阶段进度；完整模式 UI 虽然存在，但后端没有任何获取简介和标签的逻辑；失败重试在仍有失败时会清空失败表，用户无法继续追踪。

## 关键问题

### P0：扫码登录轮询解析错误，真实登录流程不可用

证据：

- 代码在 `core/bilibili_api.py:95` 使用顶层 `data["code"]` 判断扫码状态，`code == 0` 即认为登录成功，并保存响应头 cookie。
- 2026-07-07 实测 B 站二维码未扫码轮询返回结构为：顶层 `code=0`，实际状态在 `data.code=86101`，`data.message="未扫码"`。
- 因此当前实现会把“未扫码”误判为 `success`。没有真实登录 cookie 时，前端随后又回到登录页，表现为二维码反复刷新或无法完成登录。

影响：

- R2“扫码登录 B 站”不能通过真实接口验收。
- 后续收藏夹列表、分类、移动都依赖登录，因此真实主流程被阻断。

建议：

- 改为读取 `data["data"]["code"]` 作为二维码状态。
- 按当前接口处理：`86101` 未扫码、`86090` 已扫码待确认、`86038` 二维码失效、`0` 登录成功。
- 补充一条基于真实返回结构的单元测试，替换现有 mocked 旧结构测试。

### P0：SSE 进度事件不会进入队列，整理中页面基本没有实时进度

证据：

- `main.py:141` 定义的 `on_progress` 是 async 函数，内部 `await queue.put(event)`。
- `core/session.py:44`、`51`、`53`、`67`、`74`、`77` 直接调用 `on_progress(...)`，没有 `await`，也没有判断返回值是否是 coroutine。
- 最小复现脚本结果：会话能到 `pending_review`，但 async 回调收集到的事件数为 0，并出现 `RuntimeWarning: coroutine ... was never awaited`。

影响：

- R4/R6 中的实时流程反馈打折。
- 规划文档和 README 承诺的“整理中页面三步骤指示器和百分比进度（SSE 推送）”不能正常工作。
- 前端只能在任务结束后收到 `done`，中间阶段不会可靠更新。

建议：

- 统一约定 progress 回调为同步函数，或在 session 层支持：

```python
result = on_progress(event)
if inspect.isawaitable(result):
    await result
```

- 增加一条路由级或最小 async callback 测试，覆盖 FastAPI SSE 使用方式，而不是只用 `list.append` 测同步回调。

### P1：完整模式未实现，AI 输入缺少简介和标签

证据：

- 前端有快速/完整模式开关，`static/index.html:488` 文案说明完整模式会额外拉取简介和标签，`static/app.js:140` 也把 `mode` 发给后端。
- 后端只把 `mode` 存到会话，`core/session.py:60` 之后无论 quick/full 都只读取已缓存视频。
- `core/bilibili_api.py:182-183` 固定写入 `intro=""`、`tags="[]"`。
- `BilibiliClient` 未实现设计文档要求的 `get_video_info(bvid)`，也没有调用 `/x/web-interface/view` 或 `/x/tag/archive/tags`。

影响：

- R1“用标题/简介/标签/UP主分析”只部分满足。
- 6.3 和 README 中“完整模式更准但慢”的功能不存在，用户选择完整模式不会改变分类输入。

建议：

- 明确 quick/full 两条路径。
- full 模式对每个视频补拉 view 和 tags，写入 `videos.intro`/`videos.tags`。
- `VideoInfo` 增加 `intro` 字段，并把 prompt 输入与测试同步更新。

### P1：失败重试会清空仍失败的记录

证据：

- `core/session.py:160-171` 中 retry 失败只累加 `still_failed`。
- `core/session.py:172` 无条件调用 `self.storage.clear_failed_items(sid)`。
- 如果重试仍失败，接口会返回 `failed > 0`，但失败项详情已经被删除，前端无法展示哪些视频仍失败。

影响：

- README 承诺的“失败项可一键重试”只能做一次，而且失败详情会丢失。
- 用户无法继续重试或人工处理残留失败项。

建议：

- 只删除重试成功的 failed item。
- 对仍失败的 failed item 更新 `retried`、`error_message`、`created_at` 或新增 retry_count。
- 同步更新 session stats，保证刷新结果页后统计一致。

### P1：默认测试命令失败，项目测试配置不符合 README/计划

证据：

- README 和计划都写 `pytest -v`。
- 当前环境中 `pytest` 全局命令不存在；使用 `.venv\Scripts\python.exe -m pytest -q` 会收集 `_libs` 中第三方库测试，出现 import mismatch，收集阶段失败。
- 限定 `.venv\Scripts\python.exe -m pytest -q tests` 时，项目自有 33 个测试通过。
- `pytest.ini` 只有 `asyncio_mode = auto`，没有 `testpaths = tests`，也没有排除 `_libs`/`.libs`。

影响：

- 一键开发/验证体验不符合 R7。
- CI 或其他智能体直接跑 README 命令会得到失败结果。

建议：

- 在 `pytest.ini` 增加：

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
norecursedirs = .venv _libs .libs .pytest_cache __pycache__
```

- README 改成 `python -m pytest -q` 或明确使用虚拟环境命令。

## 其他问题和风险

### 执行阶段的失败处理不完整

- `core/session.py:109-110` 创建目标收藏夹发生异常时不在 try 内，任何一个分类创建失败都会让整个 execute 抛错，状态停在 `executing`，且不会写入 `failed_items`。
- `core/session.py:104-106` 对 `未分类` 项直接跳过，但最终 `stats.total = len(items)`；这会导致成功数 + 失败数小于总数，用户不知道这些视频没有移动。
- `core/session.py:110` 创建收藏夹固定 `privacy=1`，没有使用配置里的 `default_privacy`。如果未来配置暴露隐私选项，这里会失效。

### AI 分类可靠性低于设计

- `core/ai_classifier.py:45-49` 没有使用 `response_format={"type":"json_object"}` 或兼容的结构化输出能力。
- 没有实现设计文档中的 AI 超时/限流指数退避重试。
- `core/ai_classifier.py:86` 只有分类名超过 10 个才做二次归并；设计意图是跨批次分类名统一，即使少于 10 个也可能出现“编程/代码教学/编程教程”漂移。
- 只捕获 JSON 解析失败。401/429/网络异常会直接抛出，前端只看到通用错误。

### B 站 API 实现存在兼容性风险

- `_wbi_sign` 在 `core/bilibili_api.py:32-34` 直接拼接 query，没有 URL encoding 和特殊字符过滤；当前收藏夹列表参数多为简单数字/英文，短期可能不暴露，但签名实现并不完整。
- WBI key 缓存没有过期策略，`core/bilibili_api.py:123-127` 只要 SQLite 里有缓存就一直使用。
- 未实现 `get_video_info`，没有对完整模式所需的详情/标签 API 做 mock 契约测试。

### 前端存在 XSS/HTML 注入风险

- `static/app.js:107-117` 用 `innerHTML` 渲染收藏夹标题。
- `static/app.js:248-259` 用 `innerHTML` 渲染 AI 分类名、视频标题、UP 主等外部数据。
- 数据来自 B 站用户内容和 AI 输出，理论上可包含 HTML。虽然这是本地工具，仍建议用 DOM API 或 HTML escape。

### 断点续作只做了状态回退，进度恢复不完整

- 设计文档要求 SSE 重连后从 SQLite 读最新 stage 恢复进度条。
- 当前 schema 没有保存 stage/progress，`classify_sessions.stats` 也只在 done 时写入。
- `resume_on_startup` 只把 `collecting/classifying` 回退到 `draft`，把 `executing` 回退到 `pending_review`，可以恢复可交互点，但不能恢复进度。

## 需求覆盖矩阵

| 需求 | 当前状态 | 说明 |
|---|---|---|
| R1 AI 智能分类 | 部分实现 | quick 模式可调用 AI；完整模式没有简介/标签；重试/结构化输出不足 |
| R2 扫码登录 B站 | 未通过 | 真实 poll 返回结构解析错误，未扫码会误判成功 |
| R3 半自动模式 | 部分实现 | 有预览和确认；但未分类会被静默跳过，执行失败处理不完整 |
| R4 本地网页界面 | 基本实现 | 6 个视图存在，能启动静态 SPA |
| R5 响应式 + 移动端兼容 | 基本实现但未实测 | 使用 Tailwind 响应式类；未做浏览器/手机实际截图验收 |
| R6 状态持久化 | 部分实现 | 配置/cookie/db 有；断点续作有限；失败重试会丢详情 |
| R7 一键启动/无构建 | 部分实现 | 无前端构建；但默认测试命令和 pytest 配置有问题 |

## 验证记录

- `Get-Content -Encoding UTF8 README.md`：README 中文内容正常。
- `.venv\Scripts\python.exe -m pytest -q tests`：33 passed。
- `.venv\Scripts\python.exe -m pytest -q`：失败，原因是收集 `_libs` 第三方库测试导致 import mismatch。
- 真实 B 站二维码接口抽查：
  - generate 顶层 `code=0`
  - poll 未扫码时顶层 `code=0`
  - poll 实际状态 `data.code=86101`
  - poll 实际信息 `data.message="未扫码"`
- 最小 async progress callback 复现：
  - 会话状态：`pending_review`
  - 收到进度事件数：0
  - 输出多条 `coroutine ... was never awaited`

## 建议修复顺序

1. 修复二维码轮询解析，并用真实返回结构更新测试。
2. 修复 `on_progress` async callback，增加 SSE/async callback 测试。
3. 给 `pytest.ini` 增加 `testpaths` 和排除目录，保证默认测试命令可靠。
4. 实现 full 模式的详情/标签拉取，补齐 `get_video_info` 和相关 mock 测试。
5. 修复 `retry_failed`，保留仍失败项并更新统计。
6. 改进执行阶段异常处理：创建收藏夹失败、未分类项、统计口径。
7. 对前端外部数据做 HTML escape 或改用 DOM API。
8. 补充一次真实账号的小收藏夹手动验收：登录、拉取、分类、调整、执行、失败重试、手机访问。
