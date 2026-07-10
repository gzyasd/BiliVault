# 实施计划审查报告：进度、资源分类、CPU 治理与账号退出

> **被审查文档：** `docs/implementation-plan-2026-07-08-progress-cpu-resource-logout.md`
> **审查日期：** 2026-07-08
> **审查方法：** 逐 Task 对照当前代码（`core/session.py`、`core/bilibili_api.py`、`core/ai_classifier.py`、`core/storage.py`、`main.py`、`static/app.js`）核实计划中的假设、实现方案和测试用例。

## 审查结论

计划整体方向正确，6 个 Task 覆盖了用户提出的全部需求。但存在 **3 个 P1 问题（会导致实现后功能不完整或测试无法通过）**、**5 个 P2 问题（会导致边界场景异常或旧会话兼容失败）**、**4 个 P3 问题（改进建议）**。建议修正后再进入执行。

---

## P1 问题（必须修正后才能执行）

### P1-1：Task 2 把分批逻辑移到 session.py 后，`merge_categories` 逻辑丢失

**位置：** 计划 Task 2 Step 3

**问题：**

计划说"不要一次性调用 `self.ai.classify(videos)`，改为按 `_chunks(videos, batch_size)` 调用 `classify_batch()`"。但当前 `ai.classify()`（[ai_classifier.py:113-124](file:///d:/GZY/TraeComm/BiBiTool/core/ai_classifier.py#L113)）内部不仅分批，还在分类数超过 10 时调用 `merge_categories()` 做类别合并：

```python
async def classify(self, videos, batch_size=50):
    results = []
    for i in range(0, len(videos), batch_size):
        batch = videos[i:i + batch_size]
        results.extend(await self.classify_batch(batch))
    if results:
        cats = list({c.category for c in results if c.category != "未分类"})
        if len(cats) > 10:
            mapping = await self.merge_categories(cats)
            for c in results:
                if c.category in mapping:
                    c.category = mapping[c.category]
    return results
```

计划 Step 3 的实现代码只展示了分批调用和进度发送，**没有包含 `merge_categories` 调用**。Step 3 末尾仅用一句话提到"批次完成后沿用现有合并分类逻辑"，但没有给出具体代码。这会导致：

- 分类数超过 10 时，类别不会被合并，预览页会出现大量重复/相似分类。
- 或者执行者自行实现 merge 逻辑，导致 `session.py` 重复 `ai_classifier.py` 的内部策略，违反职责分离。

**建议修正：**

更简洁的方案是**不把分批逻辑移到 session.py**，而是给 `ai.classify()` 新增 `on_progress` 回调参数：

```python
# core/ai_classifier.py
async def classify(self, videos, batch_size=50, on_progress=None):
    results = []
    total = len(videos)
    for i in range(0, len(videos), batch_size):
        batch = videos[i:i + batch_size]
        results.extend(await self.classify_batch(batch))
        if on_progress:
            await on_progress({
                "stage": "classifying",
                "progress": len(results) / total if total else 1.0,
                "classified": len(results),
                "total": total,
            })
    # merge 逻辑保持不变
    if results:
        cats = list({c.category for c in results if c.category != "未分类"})
        if len(cats) > 10:
            mapping = await self.merge_categories(cats)
            for c in results:
                if c.category in mapping:
                    c.category = mapping[c.category]
    return results
```

`session.py` 的 `classify()` 只需把 `on_progress` 透传：

```python
results = await self.ai.classify(videos, on_progress=on_progress)
```

这样 merge 逻辑不丢失，session.py 不需要知道 AI 内部策略，且进度仍按批次推进。

### P1-2：Task 5 的 `failed_items` 表缺少 `resource_type` 字段，但 Step 6 要求写入

**位置：** 计划 Task 5 Step 2（表结构定义）与 Step 6（会话流程改用资源）

**问题：**

Step 6 明确说"失败记录可以继续写 `failed_items`，但要新增 `resource_id`、`resource_type` 字段；如果暂时保留 `avid`，非视频写 `avid=resource_id`，同时必须保存 `resource_type`，避免同 ID 不同类型混淆"。

但 Step 2 的表结构定义（3 个新表）**没有包含 `failed_items` 表的修改**。当前 `failed_items` 表（[storage.py:61-72](file:///d:/GZY/TraeComm/BiBiTool/core/storage.py#L61)）只有 `avid`，没有 `resource_type`：

```sql
CREATE TABLE IF NOT EXISTS failed_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  avid INTEGER,
  title TEXT,
  category TEXT,
  target_fid INTEGER,
  error_code TEXT,
  error_message TEXT,
  retried INTEGER DEFAULT 0,
  created_at TEXT
);
```

如果非视频资源（如 `resource_type=11` 合集）移动失败，写入 `failed_items` 时 `avid=resource_id`，但无法区分这是视频还是合集。重试时 `_retry_failed_sources()` 按 `avid` 查找 `plan_items`，非视频资源在旧 `classification_plan_items` 表中不存在（只有 `avid` 主键），会导致重试失败。

**建议修正：**

Step 2 需要补充 `failed_items` 表的迁移：

```sql
-- 迁移：新增 resource_type 列，默认 2（视频）
ALTER TABLE failed_items ADD COLUMN resource_type INTEGER DEFAULT 2;
```

并在 Step 6 的 `add_failed_item()` 调用中传入 `resource_type`。`_retry_failed_sources()` 也需要按 `(resource_id, resource_type)` 而非纯 `avid` 查找 plan items。

### P1-3：Task 5 的 `classification_plan_resource_items` 与旧 `classification_plan_items` 的兼容策略缺失

**位置：** 计划 Task 5 Step 2

**问题：**

计划新增了 `classification_plan_resource_items` 表（主键 `version_id, resource_id, resource_type`），但没有说明以下关键方法如何同时支持新旧两种表：

1. `create_plan_version(items=...)`：旧 items 用 `avid`，新 items 用 `resource_id`+`resource_type`。方法如何判断写哪个表？是两个表都写？
2. `load_plan_items(version_id)`：读哪个表？如果两个表都写了，返回结果如何合并？`avid` 和 `resource_id` 字段如何统一？
3. `adjust_plan_item(version_id, avid, new_category)`：当前签名用 `avid`，新会话需要 `(resource_id, resource_type)`。是否要新增 `adjust_plan_resource_item()`？
4. `mark_plan_item_executed(version_id, avid, executed)`：同上。
5. `get_plan()` 返回的 `items` 中，旧会话每项有 `avid`，新会话每项有 `resource_id`+`resource_type`。前端 `renderReview()` 如何统一渲染？

如果这些问题不明确，执行者要么自行猜测（导致实现不一致），要么两个表都写（导致数据冗余和同步问题）。

**建议修正：**

选择以下两种策略之一，并在计划中明确：

**策略 A（推荐：扩展现有表）：** 不新增 `classification_plan_resource_items` 表，而是给 `classification_plan_items` 新增 `resource_type` 列（默认 2），主键改为 `(version_id, resource_id, resource_type)`，`avid` 列保留为兼容字段（视频时 `avid=resource_id`）。所有方法签名统一用 `resource_id, resource_type`，旧调用方传 `resource_type=2`。

**策略 B（双表并存）：** 新增表但明确 `create_plan_version` 只写新表（旧会话通过迁移补写），`load_plan_items` 优先读新表回退旧表，`adjust_plan_item` 和 `mark_plan_item_executed` 同时写两个表。此策略复杂度高，不推荐。

---

## P2 问题（应在执行前明确）

### P2-1：Task 5 的 `get_folder_video_pages()` 兼容包装会导致 `skipped_count`/`usable_count` 语义变化

**位置：** 计划 Task 5 Step 3

**问题：**

计划说"`get_folder_video_pages()` 保留为兼容包装，内部调用 `get_folder_resource_pages()` 后只返回 `resource_type == 2` 的旧格式数据"。

但当前 `get_folder_video_pages()` 返回的 page 包含 `raw_count`（原始媒体数）、`usable_count`（可用视频数）、`skipped_count`（跳过数）。如果包装只过滤 `resource_type == 2`，那么：

- `raw_count` 仍应是所有媒体数（包括非视频），还是只算视频？
- `usable_count` 只算视频，还是算所有可用资源？
- `skipped_count` 是否还包括 `non_video_type` 跳过项？

如果 `raw_count` 变为只算视频，那么 `collect()` 中的 `scanned` 累加会变小，进度计算会不准。如果 `skipped_count` 不再包括非视频，那么 stats 中的 `skipped_total` 会减少，但非视频资源实际上没有被跳过（而是被分类了），这是合理的。但这些语义变化需要在计划中明确。

**建议修正：** 在 Step 3 中明确：`get_folder_video_pages()` 兼容包装返回的 `raw_count` = 所有媒体数（不变），`usable_count` = 视频数（不变），`skipped_count` = 仍包含 `non_video_type`（因为从旧视角看，非视频确实被"跳过"了）。新方法 `get_folder_resource_pages()` 的 `usable_count` = 所有可用资源数（包括非视频），`skipped_count` = 只有 `attr_invalid` 和 `no_id`。

### P2-2：Task 5 的 `collect()` 在 `mode == "full"` 下无法 enrich 非视频资源

**位置：** 计划 Task 5 Step 6 与 [session.py:122-126](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L122)

**问题：**

当前 `collect()` 在 full 模式下调用 `_enrich_video(v)`，它依赖 `v["bvid"]` 调用 `get_video_info()` 获取标题、简介、标签等。非视频资源（如合集 `type=11`）没有 `bvid`，无法调用 `get_video_info()`，会被 `_enrich_video` 跳过（返回原始 dict）。

但合集、课程等资源可能需要调用不同的 B 站 API 才能获取详情（如 `/x/v3/fav/resource/info` 或 `/x/polymer/web-dynamic/v1/opus/info`）。计划没有说明非视频资源在 full 模式下如何获取详情，也没有新增对应的 B 站 API 方法。

**建议修正：** 在 Step 6 中明确：full 模式下，非视频资源暂时不做 enrich（使用分页接口返回的基础字段：title、up_name、cover_url、tname），后续按需新增资源详情 API。或者在 Step 3 中新增 `get_resource_info(resource_id, resource_type)` 方法。

### P2-3：Task 6 的 `api_logout()` 没有清除 `accounts.is_active` 状态

**位置：** 计划 Task 6 Step 4 与 [main.py:187-196](file:///d:/GZY/TraeComm/BiBiTool/main.py#L187)

**问题：**

当前 `api_logout()` 已经清理了 cookie 文件（计划假设它没有清理，实际上已经有了）。但它**没有更新 `accounts` 表的 `is_active` 状态**。退出后：

- `get_active_account()` 仍返回该账号记录（`is_active=1`）。
- `/api/accounts` 返回的 `active` 仍指向已退出的账号。
- 前端账号列表显示该账号仍为"当前账号"，但实际 Cookie 已清除，无法使用。
- `get_bili()` 仍返回该账号的客户端（cookie 文件已删，`is_logged_in=False`），但前端导航栏可能仍显示账号名。

计划的测试 `test_logout_clears_active_account_cookie` 只验证了 cookie 文件被删除和 `is_logged_in=False`，**没有验证 `is_active` 状态被清除**。

**建议修正：**

1. `api_logout()` 应增加 `storage.deactivate_account(account_id)` 调用（或直接 `UPDATE accounts SET is_active = 0`）。
2. 测试应增加断言：`assert real_storage.get_active_account() is None`。
3. 前端 `logoutAccount()` 应刷新导航栏账号名显示（调用 `renderAccounts()` 或 `updateNavAccount()`）。

### P2-4：Task 5 的 `adjust_item()` 签名兼容性未明确

**位置：** 计划 Task 5 Step 6 与 [session.py:268](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L268)

**问题：**

当前 `adjust_item(sid, avid, new_category)` 和 `adjust_plan_item(version_id, avid, new_category)` 都用 `avid` 作为主键。新会话需要按 `(resource_id, resource_type)` 调整分类。计划没有说明：

- `adjust_item()` 是否需要改签名为 `(sid, resource_id, resource_type, new_category)`？
- 前端 `renderReview()` 中下拉调整分类的请求体如何变化？
- 旧会话（只有 `avid`）和新会话（有 `resource_id`+`resource_type`）的 API 请求如何统一？

**建议修正：** API 请求体统一为 `{resource_id, resource_type, category}`，视频时 `resource_type=2`。后端 `adjust_item` 签名改为 `(sid, resource_id, resource_type, new_category)`，内部根据 `resource_type` 决定写哪个表。

### P2-5：Task 5 的 AI prompt 中 `resource_type` 数字含义未解释

**位置：** 计划 Task 5 Step 5

**问题：**

计划在 prompt 中加入"resource_type 表示 B 站收藏资源类型"，但只给了数字（2=视频，11=合集等），AI 模型不一定理解这些数字的含义。如果 AI 把 `resource_type=11` 当成无关字段忽略，分类质量不会提升。

**建议修正：** 在 prompt 中明确映射关系，或直接传入类型名称字符串：

```text
resource_type 含义：2=视频，11=合集，12=音频，21=课程，31=专栏，其他=未知类型。
分类时应根据标题、UP、分区、类型名称、简介和标签推断主题，不要因为不是视频就归为"其他"。
```

---

## P3 问题（改进建议）

### P3-1：测试命令使用 `pytest.exe`，但项目约定是 `python -m pytest`

**位置：** 计划多个 Task 的验证步骤

**问题：** 计划中验证命令写为 `.\.venv\Scripts\pytest.exe`，但项目实际使用 `.venv\Scripts\python.exe -m pytest`（前序对话中一致使用此格式）。虚拟环境中不一定有 `pytest.exe`（取决于安装方式）。

**建议：** 统一改为 `.\.venv\Scripts\python.exe -m pytest`。

### P3-2：`node --check static\app.js` 依赖 Node.js，但未确认环境

**位置：** 计划 Task 1 Step 6、Task 4 Step 4、Task 6 Step 5

**问题：** 多处使用 `node --check` 验证 JS 语法，但项目是 Python 后端 + 原生 JS 前端，不依赖 Node.js 构建工具链。如果开发环境未安装 Node.js，验证步骤会失败。

**建议：** 改为在 `tests/test_frontend_static.py` 中用 Python 读取 `app.js` 做基础语法检查（如括号匹配），或直接依赖前端静态测试覆盖。

### P3-3：Task 4 的折叠状态切换会重复请求 API

**位置：** 计划 Task 4 Step 2

**问题：** `toggleSkippedPanel()` 和 `toggleSkippedReason()` 都调用 `renderSkippedPanel(currentSid)`，后者会 `await api('/api/session/${sid}/skipped-items')` 重新请求。频繁点击折叠/展开会导致不必要的网络请求和渲染。

**建议：** 缓存上次请求的 items 数据，toggle 时只切换 CSS class（`display:none`/`display:block`），不重新请求。或在 `renderSkippedPanel` 中先检查数据是否已缓存。

### P3-4：Task 3 的 `access_log=False` 会影响调试

**位置：** 计划 Task 3 Step 2

**问题：** 完全关闭 access_log 后，调试时无法看到请求路径和响应时间。虽然减少了控制台输出，但也降低了可观测性。

**建议：** 考虑用 logging 过滤器只过滤高频路径（`/api/session/.*/stream`、`/api/accounts/login/poll`），而非全关：

```python
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        return "/stream" not in record.args[2] and "/login/poll" not in record.args[2]
```

或保留 `access_log=True` 但设置 `log_level="warning"`。

---

## 逐 Task 核实结论

| Task | 计划假设是否准确 | 实现方案是否完整 | 测试是否可执行 | 问题编号 |
|------|------------------|------------------|----------------|----------|
| Task 1（进度条） | ✅ 准确（第 142 行 `progress: None` 确实是问题） | ✅ 完整 | ✅ 可执行（39/266=0.1466 在 0.14-0.15 范围内） | 无 |
| Task 2（AI 批次进度） | ⚠️ 部分准确（当前 `ai.classify()` 已分批，但 session.py 不发进度） | ❌ merge 逻辑丢失 | ⚠️ 测试可执行但实现不完整 | P1-1 |
| Task 3（CPU 治理） | ✅ 准确（`uvicorn.run` 无 `access_log` 参数） | ✅ 基本完整 | ✅ 可执行 | P3-1, P3-4 |
| Task 4（跳过折叠） | ✅ 准确（`renderSkippedPanel` 无折叠） | ✅ 完整 | ✅ 可执行 | P3-2, P3-3 |
| Task 5（非视频分类） | ⚠️ 部分准确（`type != 2` 确实跳过） | ❌ 数据模型过渡不完整 | ⚠️ 部分测试缺少前置条件 | P1-2, P1-3, P2-1, P2-2, P2-4, P2-5 |
| Task 6（退出账号） | ⚠️ 部分准确（`api_logout` 已清 cookie，但未清 is_active） | ⚠️ 遗漏 is_active 清除 | ⚠️ 测试缺少 is_active 断言 | P2-3 |

---

## 建议执行顺序调整

计划原顺序：Task 1 → Task 2 → Task 3 → Task 4 → Task 6 → Task 5

建议调整为：

1. **Task 3**（CPU 治理）优先执行，确保后续测试环境稳定。
2. **Task 1**（进度条）和 **Task 2**（AI 批次进度）一起执行，两者都是进度问题。Task 2 需先修正 P1-1。
3. **Task 4**（跳过折叠）和 **Task 6**（退出账号）执行。Task 6 需先修正 P2-3。
4. **Task 5**（非视频分类）最后执行。需先修正 P1-2、P1-3、P2-1~P2-5。这是本轮最大改动，建议单独执行并完整回归。

---

## 自检

- [x] 逐 Task 核实了计划中的假设与当前代码是否匹配
- [x] 检查了每个 Task 的实现方案是否完整（含数据模型、API、前端、迁移）
- [x] 检查了测试用例是否可执行、断言是否合理
- [x] 检查了跨 Task 的依赖和兼容性
- [x] 每个问题都附了具体代码位置和修正建议
