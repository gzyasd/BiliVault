# 405/57 计数差异与页面返回/取消体验修复实施计划

日期：2026-07-07

## 背景

用户在首页看到「默认收藏夹 405 个视频」，开始整理后进度页显示「共 57 个视频，AI 分析中」。同时多个页面缺少返回、取消、放弃或后台运行入口，导致用户一旦进入整理、登录、配置、预览等流程，很难安全地退出当前步骤。

本计划只分析和规划修复步骤，不直接修改代码。

## 结论摘要

405 和 57 当前不是同一个计数口径。

- 405 来自 B 站收藏夹列表接口返回的 `media_count`，代码位于 `core/bilibili_api.py` 的 `get_my_folders()`。
- 57 来自实际拉取 `/x/v3/fav/resource/list` 后，经过 `attr & 1` 过滤、写入本地 `videos` 表，再由 `core/session.py` 的 `classify()` 读取缓存数量得到。

这说明用户看到的是「收藏夹声明总数」，而系统整理的是「当前代码认为可处理的视频数」。问题在于系统没有解释差异，也没有记录跳过原因；同时分页结束条件也可能过早停止，导致 57 不一定可信。

## 现有链路

1. 首页调用 `/api/folders`。
2. 后端调用 `BilibiliClient.get_my_folders()`。
3. `get_my_folders()` 调用 `/x/v3/fav/folder/created/list-all`，把 `f["media_count"]` 返回前端。
4. 用户点击收藏夹后创建 session。
5. SSE 流触发 `ClassifySession.run_pipeline()`。
6. `collect()` 调用 `BilibiliClient.get_folder_videos()` 分页拉取 `/x/v3/fav/resource/list`。
7. `get_folder_videos()` 对 `medias` 做过滤：`if m.get("attr", 0) & 1: continue`。
8. `collect()` 只把过滤后的 batch 写入 `videos` 表，并按 batch 长度累计 `collected`。
9. `classify()` 从 `storage.list_videos_by_fid()` 读出已缓存视频，`total = len(videos)`，前端显示「共 X 个视频」。

## 405 变 57 的可能原因

### 已确认的代码层原因

- 进度页显示的是本地可分类视频数，不是收藏夹列表里的 `media_count`。
- `get_folder_videos()` 会跳过 `attr & 1` 的条目，但没有统计跳过数量，也没有把跳过原因反馈给前端。
- `get_folder_videos()` 以 `len(medias) < page_size` 作为唯一结束条件。如果 B 站接口存在短页、风控降级、失效条目折叠、或响应中另有 `has_more` / `info.media_count` 指示，当前逻辑可能提前停止。
- 本地 `videos` 表按 `avid` 做主键，如果同一个视频在不同来源中被复用，可能发生覆盖；本问题发生在单个收藏夹时不一定是主因，但后续修复统计时应顺手验证。

### 需要实测确认的接口层原因

- `/x/v3/fav/resource/list` 是否把失效、已删除、私密、不可访问、非视频资源计入 `media_count`，但不完整返回到 `medias`。
- `medias[*].attr` 各 bit 的真实含义，尤其是当前 `attr & 1` 是否等价于「失效/不可处理」。
- 响应里是否存在 `has_more`、`info.media_count`、`ttl`、`count`、`total` 等字段，可以作为分页停止条件或统计依据。
- 默认收藏夹这类特殊收藏夹是否有额外规则，例如一部分条目不可移动或不可通过普通收藏夹资源接口列出。

## 修复目标

1. 让系统准确拉取所有 B 站接口可返回的可处理视频，不因分页逻辑提前停止。
2. 让用户明确看到三个数字：收藏夹总数、可整理视频数、跳过/不可处理数量。
3. 对跳过条目记录原因，避免「405 变 57」看起来像数据丢失。
4. 在关键页面补齐返回、取消、放弃、后台运行等入口，且不会造成重复任务、重复轮询或状态错乱。
5. 用自动化测试覆盖计数、分页、跳过统计、取消/返回按钮存在性和主要交互。

## 实施计划

### 阶段 1：记录真实接口返回形态

- 新增一个仅开发/调试使用的脚本或临时测试入口，使用当前登录 Cookie 请求：
  - `/x/v3/fav/folder/created/list-all`
  - `/x/v3/fav/resource/list`
- 对目标收藏夹至少记录：
  - folder list 的 `media_count`
  - 每页 `pn`、`ps`
  - 每页 `len(medias)`
  - 响应里的 `info`、`has_more` 或类似字段
  - 每个 media 的 `id`、`bvid`、`type`、`attr`、`title` 是否为空
- 输出脱敏后的 JSON 样例放入 `docs/`，或只记录字段摘要，避免泄露 Cookie 和个人数据。

验收标准：

- 能解释 405 中有多少是接口实际返回、多少被当前过滤规则跳过、是否存在分页提前停止。

### 阶段 2：修正收藏夹分页与统计

建议修改 `core/bilibili_api.py`：

- 不再只用 `len(medias) < page_size` 判断结束。
- 优先使用 B 站响应里的 `has_more` 或等价字段。
- 如果没有 `has_more`，使用 `info.media_count` 和已扫描条目数做双保险。
- 为每页返回统计信息，至少包括：
  - `page`
  - `raw_count`
  - `usable_count`
  - `skipped_count`
  - `skipped_reasons`
  - `expected_total`
- 如果仍保留 async generator，可以把返回 batch 扩展成结构化对象；如果要降低改动面，也可以新增 `get_folder_video_pages()`，让旧方法包装新方法。

建议修改 `core/session.py`：

- `collect()` 保存 session 级统计：
  - `source_total`
  - `scanned_total`
  - `collected_total`
  - `skipped_total`
  - `skipped_by_reason`
  - `page_count`
- SSE collecting 阶段推送这些统计，让前端可以显示真实口径。
- `classify()` 的 total 仍应使用可分类视频数，但文案要明确为「可整理视频」。

建议修改 `core/storage.py`：

- 复用 `classify_sessions.stats` 保存汇总统计。
- 如需要明细追踪，新增 `skipped_items` 表：
  - `session_id`
  - `avid`
  - `bvid`
  - `title`
  - `reason`
  - `raw_attr`
  - `raw_type`
  - `created_at`
- 若暂不建表，至少把 `skipped_by_reason` 写入 session stats。

验收标准：

- 对短页但仍有 `has_more` 的模拟响应，系统继续翻页。
- 对包含失效条目的响应，系统显示跳过数量，而不是悄悄少算。
- 进度页不再用「共 57 个视频」误导用户，应显示类似「405 个收藏条目，57 个可整理，348 个已跳过/不可处理」。

### 阶段 3：前端计数展示修复

建议修改 `static/app.js` 和 `static/index.html`：

- 首页收藏夹卡片继续显示 B 站 `media_count`，文案改为「收藏条目」或「B 站显示 X 个」。
- 整理进度页增加统计区：
  - 总收藏条目：`source_total`
  - 已扫描：`scanned_total`
  - 可整理：`collected_total`
  - 已跳过：`skipped_total`
- AI 分类阶段文案改为「共 X 个可整理视频，AI 分析中」。
- 预览页顶部增加提示：
  - 如果有跳过条目，显示「本次跳过 X 个不可处理条目，可查看原因」。
- 结果页统计保留成功/失败/总数，但总数应明确为执行总数或可整理总数。

验收标准：

- 用户能一眼理解 405 和 57 的关系。
- 不需要打开开发者工具也能知道系统为什么没有处理全部 405 个条目。

### 阶段 4：补齐页面返回/取消能力

当前页面与建议动作如下。

| 页面 | 当前问题 | 建议新增动作 |
| --- | --- | --- |
| 配置页 | 只有保存；从设置进入后无法取消返回 | 「取消/返回」按钮；首次未配置时隐藏或禁用取消 |
| 登录页 | 只有刷新二维码；无法返回配置或首页；刷新会启动新轮询但旧轮询没有显式停止 | 「返回」按钮；刷新/离开页面时停止旧二维码轮询 |
| 首页 | 选择收藏夹即开始整理，用户没有确认所选模式和目标 | 点击收藏夹后增加开始确认弹窗或底部确认条；保留设置入口 |
| 进度页 | 无法取消或返回；连接断开直接回首页；重复进入可能造成重复任务 | 「后台运行」和「取消本次整理」；取消需后端支持 |
| 预览页 | 只有确认执行；无法返回首页、重新选择、重新分类或放弃 | 「返回首页/稍后处理」「重新分类」「放弃本次整理」 |
| 执行/结果页 | 结果页已有返回首页和重试失败；执行过程无取消 | 执行中如未来单独做页面，应提供「后台运行」；结果页保留现状，可增加「查看方案」 |

后端建议：

- 增加 session 任务注册表，沿用之前 P6 建议：同一个 `sid` 只允许一个后台任务。
- 增加取消接口：`POST /api/session/{sid}/cancel`。
- 增加状态：`cancelled`，并允许以下转换：
  - `draft -> cancelled`
  - `collecting -> cancelled`
  - `classifying -> cancelled`
  - `pending_review -> cancelled`
- 对执行阶段是否允许取消要谨慎。移动收藏夹是有副作用操作，建议第一版不允许取消 `executing`，只允许后台运行并提示正在执行。
- 前端离开 progress 页面时关闭当前 `EventSource`，但不取消后端任务；只有用户点击「取消本次整理」才调用 cancel。

前端建议：

- 新增轻量导航状态：
  - `lastStableView`
  - `activeEventSource`
  - `qrPollToken`
- `showView()` 时根据视图清理不再需要的轮询或 SSE。
- 所有按钮使用明确动词：
  - 返回首页
  - 返回配置
  - 取消登录
  - 后台运行
  - 取消本次整理
  - 放弃本次方案
  - 重新分类
- 对 destructive 动作用 `confirm()` 或后续统一 modal 确认。

验收标准：

- 从配置、登录、进度、预览、结果页都能回到合理上一步或稳定页面。
- 离开登录页不会继续多个二维码轮询。
- 离开进度页不会让前端残留多个 SSE 连接。
- 点击取消后，后端 session 状态可查，刷新页面不会又显示为可继续任务。

### 阶段 5：测试计划

后端测试：

- `tests/test_bilibili_api.py`
  - 覆盖 `has_more=true` 且 `len(medias) < page_size` 时继续翻页。
  - 覆盖失效条目统计，不只验证被跳过。
  - 覆盖 `info.media_count` 与实际返回数量不一致时的统计输出。
- `tests/test_session.py`
  - 覆盖 collect 写入 session stats。
  - 覆盖 SSE progress 包含 `source_total`、`collected_total`、`skipped_total`。
  - 覆盖 cancel 状态转换。
  - 覆盖同一个 sid 重复连接 stream 不会启动重复 pipeline。
- `tests/test_storage.py`
  - 覆盖 session stats 保存/读取。
  - 如新增 `skipped_items` 表，覆盖插入和查询。

前端静态测试：

- `tests/test_frontend_static.py`
  - 检查新增按钮 DOM id 存在。
  - 检查 `EventSource` 有关闭路径。
  - 检查二维码轮询有 token 或 abort 机制。
  - 检查进度文案包含「可整理」等清晰口径。

手工验证：

1. 登录真实 B 站账号。
2. 打开默认收藏夹，记录首页显示总数。
3. 开始整理，确认进度页显示总数、已扫描、可整理、已跳过。
4. 点击后台运行，回首页后继续 session，不重复创建任务。
5. 点击取消本次整理，刷新页面后该 session 不再出现在继续列表。
6. 进入预览页，验证返回首页、重新分类、放弃方案、确认执行均可用。

## 风险与注意事项

- B 站接口字段可能随时间变化，分页修复要保留兼容回退，并记录未知响应字段摘要。
- 不应把跳过条目默认视为错误；失效、删除、私密、不可移动都应作为正常不可处理项展示。
- 取消执行阶段涉及已移动一部分视频后的补偿问题，第一版不建议支持强取消执行。
- 真实接口调试日志不能保存 Cookie、CSRF、二维码 key 或用户敏感信息。
- 新增按钮时要避免在窄屏底部操作栏挤压文字，按钮应分主次并支持换行。

## 推荐落地顺序

1. 先做接口实测和后端统计测试，确定 405/57 的真实来源。
2. 修分页与跳过统计，让后端数据可信。
3. 更新进度页/预览页文案，先解决误导性计数。
4. 补任务注册表和取消接口。
5. 补所有页面返回/取消按钮与轮询清理。
6. 跑完整测试，并用真实 B 站默认收藏夹做一次手工验证。

## 完成定义

- 默认收藏夹显示 405 时，整理流程能解释最终处理数量，不再只显示孤立的 57。
- 用户可以在每个关键步骤安全返回、取消、放弃或后台运行。
- 后端不会因用户刷新、返回、重进页面而重复启动同一 session 的整理任务。
- 自动化测试覆盖新增统计、分页、取消和关键按钮。
