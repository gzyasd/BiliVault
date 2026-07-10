# 2026-07-08 实施后深度审查报告

> 审查对象：`docs/implementation-plan-2026-07-07-review-filter-cleanup-account-refine.md` 对应实现  
> 审查范围：后端会话流程、B 站接口封装、账号管理、SQLite 迁移/存储、预览页前端、失败重试、跳过项清理、空收藏夹删除、自动化测试  
> 结论：实现覆盖了一部分计划，但仍存在会导致用户需求不满足或数据状态错误的问题。自动化测试全绿，说明当前测试没有覆盖这些关键用户路径。

## 验证记录

已执行：

```powershell
.\.venv\Scripts\pytest.exe -q
python -m py_compile core\storage.py core\session.py core\bilibili_api.py core\ai_classifier.py main.py
node --check static\app.js
```

结果：

- `pytest`：`93 passed in 6.10s`
- Python 编译检查：通过
- `static/app.js` 语法检查：通过

另用一次性脚本复现了本文中的 P1/P2 问题，包括手动调整分类不生效、`source_total` 翻倍、非法账号切换清空活跃账号、空收藏夹删除异常后不重新核验、多源重试残留失败项。

## P1 问题

### 1. 预览页手动调整分类不会写入当前激活方案，用户调整不会被执行

位置：

- `core/session.py:265`
- `core/session.py:269`

问题：

`adjust_item()` 仍只调用 `storage.adjust_classification()` 修改旧 `classifications` 表。新流程中 `classify()` 会创建 `classification_plan_versions` 和 `classification_plan_items`，`get_plan()` 与 `execute()` 都优先读取当前激活版本。因此预览页下拉框调整后，刷新出的方案仍是旧分类，最终执行也不会使用用户调整。

复现结果：

```text
legacy_count: 0
plan_category: OLD
```

影响：

用户在预览阶段“可下拉调整单个视频分类”的核心能力实际失效。

建议：

- 增加 `Storage.adjust_plan_item(version_id, avid, new_category)`。
- `ClassifySession.adjust_item()` 优先获取 active version 并更新 `classification_plan_items`，没有 active version 时再回退旧表。
- 增加回归测试：创建 active plan version 后调用 `adjust_item()`，断言 `get_plan()` 和 `execute()` 都使用新分类。

### 2. “切换账号/添加账号”前端入口未实现

位置：

- `static/index.html`
- `static/app.js`
- `main.py:198`
- `main.py:203`
- `main.py:211`

问题：

后端有 `/api/accounts`、`/api/accounts/{account_id}/switch`、`/api/accounts/login/start`，但前端没有账号按钮、账号列表、扫码添加账号入口，也没有调用这些 API。`rg` 只在 `main.py` 和测试中找到账号接口，`static` 中没有 `nav-account`、`renderAccounts` 或 `/api/accounts` 调用。

影响：

用户需求“我希望有切换账号的功能，也是扫码登录”在真实界面不可用。

建议：

- 在导航或设置页增加账号入口。
- 增加账号列表、当前账号标识、切换按钮、扫码添加账号按钮。
- 前端处理 `PIPELINE_RUNNING`，有任务运行时提示先完成或取消。
- 增加前端静态测试和最小浏览器流测试，断言账号入口可见且会调用账号 API。

### 3. 已登录账号状态下添加新账号会复用当前账号 Cookie 路径，可能覆盖旧账号登录态

位置：

- `main.py:166`
- `main.py:177`
- `main.py:211`
- `main.py:44`

问题：

计划要求新增账号扫码时使用临时 Cookie 文件，扫码成功后再根据 `mid` 移动到 `accounts/<mid>/bilibili_cookie.json`。当前实现的 `/api/accounts/login/start` 直接调用 `api_qrcode_generate()`，而 `api_qrcode_generate()` / `api_qrcode_poll()` 都通过 `get_bili()` 使用当前活跃账号客户端。

如果账号 A 已激活，添加账号 B 时，扫码成功的 Set-Cookie 会先写入账号 A 的 `accounts/A/bilibili_cookie.json`。随后 `_save_account_from_login()` 再把这个路径复制到账号 B，导致账号 A 的 Cookie 被 B 覆盖。

复现结果：

```text
active_client_path: tmp_review_repro_qr_path/accounts/old/bilibili_cookie.json
would_overwrite_active_cookie: True
```

影响：

多账号功能会破坏已有账号登录态，切回旧账号可能直接变成新账号或失效。

建议：

- `/api/accounts/login/start` 创建独立 login_id 和临时 Cookie 路径，例如 `accounts/_pending/<login_id>.json`。
- poll 接口必须携带 login_id，并使用对应临时 `BilibiliClient`。
- 成功后获取 profile，再移动/写入 `accounts/<mid>/bilibili_cookie.json`。
- 失败/过期后清理临时文件。
- 增加测试：已有账号 A 激活时扫码添加账号 B，不得修改 A 的 Cookie 文件。

### 4. AI 认证/配置错误被吞掉，用户不会进入配置页

位置：

- `core/ai_classifier.py:92`
- `core/ai_classifier.py:94`

问题：

`_chat_json()` 会把认证错误转换为 `AiApiError(code="AI_AUTH_FAILED")`，但 `classify_batch()` 捕获所有 `AiApiError` 并返回“未分类”。这会把 API Key 错误、连接错误、限流等系统级错误伪装成正常分类结果。

复现结果：

```text
category: 未分类
confidence: 0.0
```

影响：

如果 API Key 错误，前端不会收到 `AI_AUTH_FAILED`，不会跳转配置页；用户只会得到一批“未分类”结果，误以为 AI 分类质量很差。

建议：

- 只对 `AI_BAD_JSON` 这类单批解析问题考虑降级为“未分类”。
- 对 `AI_AUTH_FAILED`、`AI_CONNECTION`、`AI_TIMEOUT`、`AI_RATE_LIMIT` 向上抛出，让 SSE fail 事件引导用户处理配置或网络问题。
- 增加测试：`_chat_json` 抛 `AI_AUTH_FAILED` 时 `classify()` 必须抛出，而不是返回“未分类”。

### 5. 跳过项面板没有展示具体条目和原因

位置：

- `static/app.js:425`
- `static/app.js:432`

问题：

`renderSkippedPanel()` 只显示“共 N 个，M 个可移除”和按钮，没有列出 `reason_label`、`detail`、标题、来源收藏夹、是否已移除等明细。

影响：

用户无法判断“跳过的那些除了失效还有什么原因”，也无法在执行删除前核对要删除的具体内容。这个与需求“跳过项原因展示与清理”不匹配。

建议：

- 在面板内渲染跳过项列表，至少包含标题、原因、来源收藏夹、是否可移除、删除结果/错误。
- 删除按钮应只针对当前可移除项，最好允许逐项勾选。
- 增加前端测试，断言 `reason_label`、`detail`、`removed/remove_error` 被渲染。

## P2 问题

### 6. 采集进度 `source_total` 会翻倍，405 会显示成 810

位置：

- `core/session.py:90`
- `core/session.py:107`
- `core/session.py:112`

问题：

`source_total` 初始已取 `session_sources.media_count` 之和。随后每个源第一页又把 `page.expected_total` 加上去。单个 405 视频收藏夹会变成 810。

复现结果：

```text
source_total: 810
```

影响：

进度统计会误导用户，也可能让“405 vs 57”这类问题更难判断真实原因。

建议：

- 如果 `session_sources.media_count` 已有值，不再累加 `expected_total`。
- 只有在 `source_total is None` 或某个源的 `media_count` 缺失时，才用该源第一页的 `expected_total` 补齐。
- 增加测试：`media_count=405` 且第一页 `expected_total=405` 时最终 `source_total == 405`。

### 7. 切换不存在的账号会清空当前活跃账号

位置：

- `main.py:203`
- `core/storage.py:602`
- `core/storage.py:604`

问题：

`api_switch_account()` 不校验账号是否存在。`Storage.activate_account()` 先把所有账号 `is_active=0`，再更新目标账号。如果目标账号不存在，系统会没有任何活跃账号。

复现结果：

```text
before: a1
after: None
```

影响：

一个错误的账号 id 或前端状态不同步，就能让用户掉出当前账号；后续 `get_bili()` 会回退全局 Cookie，可能造成账号错乱。

建议：

- `activate_account()` 先查询账号是否存在，不存在时抛 `BibiError(code="ACCOUNT_NOT_FOUND")`。
- 更新后检查 affected rows。
- API 层返回明确错误，不改变当前活跃账号。

### 8. 空收藏夹删除接口异常后没有重新拉取收藏夹，仍会产生状态不一致

位置：

- `core/session.py:477`
- `core/session.py:480`
- `core/session.py:483`

问题：

计划要求删除后无论成功失败都重新拉取 `get_my_folders()`，用实际存在情况更新 `deleted` 字段。当前代码只在 `delete_folders()` 不抛异常时重新拉取；一旦异常，直接把所有 `deletable` 标记失败并返回。

复现结果：

```text
stats: {'success': 0, 'failed': 1, 'deleted': [], 'rejected': [200]}
deleted_flag: 0
get_my_folders_calls: 1
```

影响：

B 站批量删除可能部分成功后返回异常，本地会把已删除的收藏夹仍显示为未删除/失败。

建议：

- `delete_folders()` 抛异常后也要 best-effort 重新拉取收藏夹列表。
- 对不存在的 fid 标记 `deleted=1`，仍存在的记录 `delete_error`。
- 增加“部分成功后异常”的测试。

### 9. 激活不存在的版本会清空当前激活版本

位置：

- `main.py:365`
- `core/storage.py:418` 附近的 `activate_plan_version()`

问题：

`activate_plan_version()` 先把当前 session 的所有版本设为 `is_active=0`，再更新传入的 `version_id`。如果 `version_id` 不存在或不属于该 session，当前活跃版本会被清空。

复现结果：

```text
before: True
after: None
```

影响：

前端缓存的错误 version id、手动请求或并发状态都可能让预览页回退到旧 `classifications` 或空方案。

建议：

- 先查询 `version_id` 是否属于该 `session_id`。
- 不存在时抛 `BibiError(code="PLAN_VERSION_NOT_FOUND")`，不改变现有 active。
- 增加测试覆盖错误 version id。

### 10. 多源重试成功后 `failed_items` 可能残留

位置：

- `core/session.py:568`
- `core/session.py:605`

问题：

`_retry_failed_sources()` 用 `{it["avid"]: it}` 把 failed_items 按 avid 做成单值字典。同一个 avid 来自多个源收藏夹时，`failed_items` 可能有多条，但字典只保留最后一条。重试成功后也只删除这一条，其他失败记录会残留。

复现结果：

```text
stats: {'success': 2, 'failed': 0}
remaining_failed_items: 1
```

影响：

结果页可能继续显示失败项，用户会看到“成功 2、失败 0”但失败列表仍不为空。

建议：

- `failed_items` 增加 `source_fid` 字段，或建立 `(avid, source_fid)` 映射。
- 重试成功后只删除对应来源实例的失败记录。
- 如果暂不改表，至少删除同一 avid/category/target_fid 下所有已成功来源对应记录，并避免误删仍失败来源。

### 11. `fav_folders` 仍以 `fid` 为唯一主键，账号间缓存会互相覆盖

位置：

- `core/storage.py:9`
- `core/storage.py:264`
- `core/storage.py:272`

问题：

表里增加了 `account_id`，但主键仍是单列 `fid`。不同 B 站账号如果存在相同 fid，本地缓存会互相覆盖。`list_folders()` 也没有按账号过滤。

影响：

账号切换后如果某些地方使用本地收藏夹缓存，会出现串号或显示旧账号收藏夹。

建议：

- 将 `fav_folders` 迁移为 `PRIMARY KEY(account_id, fid)`。
- `upsert_folder()` 写入当前账号 id。
- `list_folders(account_id)` 按账号过滤。
- 当前若暂不依赖缓存，也应在文档和代码中明确不要跨账号使用 `list_folders()`。

## P3 问题

### 12. 默认收藏夹保护仍部分依赖标题

位置：

- `core/session.py:64`

问题：

当前实现使用 `fav_state == 1 or title == "默认收藏夹"` 判断 `delete_protected`。标题判断不稳定：用户可能改名，接口语言也可能不同。

影响：

默认收藏夹保护主要依赖 `fav_state` 时风险较低，但标题兜底仍不是可靠规则。

建议：

- 优先确认 B 站接口中稳定的默认收藏夹字段。
- 无法确认时，对来源为默认/系统收藏夹的判断宁可保守：未知则标记 `delete_protected=1`，不靠标题推断。

### 13. 测试覆盖有明显盲区

问题：

现有 `pytest` 通过，但没有覆盖以下真实用户路径：

- active plan version 下 `adjust_item()` 生效。
- 前端账号入口存在并可切换/扫码添加。
- 新账号扫码不覆盖当前账号 Cookie。
- 跳过项明细逐条展示。
- `source_total` 不翻倍。
- 删除空收藏夹异常后的部分成功核验。
- 激活不存在版本不破坏当前版本。
- AI 认证错误向上传递。

建议：

- 增加上述回归测试后再修复。
- 前端至少加入 `node --check static/app.js` 到测试脚本；如果可行，再用 Playwright 覆盖账号入口、预览调整、跳过项面板。

## 总体建议

建议先修 P1：

1. 修正 `adjust_item()` 写当前 active plan item。
2. 补齐账号前端入口，并把新增账号扫码改成临时 Cookie 流程。
3. 让 AI 认证/连接类错误向上抛出。
4. 跳过项面板展示明细和原因。

随后修 P2 的状态一致性问题：

1. 修正 `source_total` 统计。
2. 账号切换和版本切换先校验再变更状态。
3. 空收藏夹删除异常后重新核验。
4. 多源重试失败项按来源实例清理。
5. `fav_folders` 做账号隔离迁移。
