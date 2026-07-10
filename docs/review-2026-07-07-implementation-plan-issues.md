# 实施计划审查问题清单

> **审查对象：** `docs/implementation-plan-2026-07-07-review-filter-cleanup-account-refine.md`
> **审查日期：** 2026-07-07
> **审查依据：** 对照当前代码状态（`core/storage.py`、`core/session.py`、`core/bilibili_api.py`、`core/ai_classifier.py`、`main.py`、`static/app.js`、`static/index.html`）核实计划描述是否准确、方案是否可行、是否有遗漏或冲突。
> **处理方式：** 本文档仅列出问题，不修改原计划文档与代码。执行智能体在实施前应先修复本文档中标记为「致命」的问题。

## 总览

共发现 26 个问题，按严重程度分三组：

- **致命问题（1-8）：** 会导致功能直接错误或数据错乱，照计划实施必然出 bug。
- **重要问题（9-15）：** 会导致迁移失败或功能不完整。
- **次要问题（16-26）：** 实现细节缺失或描述不准，实施时可修正。

建议优先修复顺序：1-8 → 9-15 → 16-26。最严重的三个是问题 1、问题 3、问题 8，不修则计划无法实施。

---

## 一、致命问题

### 问题 1：`save_session_sources` 保留旧计数的 SQL 完全失效

**位置：** 任务 0 步骤 4，原计划第 397-401 行

**问题：**

```python
"INSERT OR REPLACE INTO session_sources (...) VALUES (?, ?, ?, ?, ?, COALESCE((SELECT collected_count FROM session_sources WHERE session_id=? AND source_fid=?), 0), ...)"
```

`INSERT OR REPLACE` 会先 DELETE 旧行再 INSERT，子查询在 INSERT 时**已查不到旧值**，`COALESCE(..., 0)` 永远返回 0。

**影响：**

任何对 session_sources 的更新都会清空 `collected_count` / `skipped_count`，导致多源采集后每个源的统计数据丢失。

**修复建议：**

改用 SQLite 的 UPSERT 语法：

```sql
INSERT INTO session_sources (...) VALUES (...)
ON CONFLICT(session_id, source_fid) DO UPDATE SET
  title=excluded.title,
  media_count=excluded.media_count,
  selected_order=excluded.selected_order,
  updated_at=datetime('now')
```

---

### 问题 2：多源场景下 `remove_skipped_items` 从错误的源收藏夹删除

**位置：** 任务 3 步骤 4，原计划第 1009 行

**问题：**

```python
await self.bili.batch_delete_resources(media_id=s["source_fid"], resources=[...])
```

任务 0 已支持多源，`skipped_items` 表也存了 `source_fid`（原计划第 129 行），但 `remove_skipped_items` 把所有跳过项都从 `s["source_fid"]`（第一个源）删，而不是按各自来源分组调用 batch-del。

**影响：**

与任务 0 的多源目标直接冲突。删除操作会作用于错误的收藏夹，可能误删第一个源里的同名资源，或因资源不在该源而 B 站返回错误。

**修复建议：**

按 `it["source_fid"]` 分组后分别调用 `batch_delete_resources`：

```python
from collections import defaultdict
groups: dict[int, list] = defaultdict(list)
for it in removable:
    groups[it["source_fid"]].append(it)
for source_fid, chunk_items in groups.items():
    try:
        await self.bili.batch_delete_resources(
            media_id=source_fid,
            resources=[{"id": it["avid"], "type": it.get("resource_type") or 2} for it in chunk_items],
        )
        ...
```

---

### 问题 3：任务 2.5 的 collect 改造丢失取消检查和进度事件

**位置：** 任务 2.5 步骤 3，原计划第 861-882 行

**问题：**

新 collect 循环里**没有任何 `_is_cancelled` 检查**，也没有 `await _emit_progress`。

**影响：**

- 回归刚完成的取消能力。现有 `core/session.py` 的 collect 在每页、每视频都有取消检查（[core/session.py](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L58-L82)）。
- collect 阶段用户看不到进度，SSE 流没事件，前端进度页四格统计区不更新。

这是对 405/57 修复 + 取消功能的严重回归。

**修复建议：**

新 collect 循环必须保留：
- 每页拉取前/后检查 `_is_cancelled(sid)`
- 每个视频处理前检查 `_is_cancelled(sid)`（特别是 full 模式的 `_enrich_video` 前后）
- 每页结束后 `await _emit_progress(on_progress, {...})` 推送 collected/scanned/skipped/source_total
- 页间检查 `_is_cancelled(sid)`

参考现有 `core/session.py` 第 57-105 行的取消检查密度。

---

### 问题 4：任务 6 `refine_plan` 在多源场景下丢失视频

**位置：** 任务 6 步骤 3，原计划第 1539 行

**问题：**

```python
videos_by_id = {r["avid"]: r for r in self.storage.list_videos_by_fid(s["source_fid"])}
```

任务 2.5 步骤 4 已把 classify 改成用 `list_session_video_sources` + `list_videos_by_avids`，但任务 6 的 refine_plan **仍用单源 `list_videos_by_fid`**。

**影响：**

多源会话的微调会丢失非首源的视频，AI 拿不到完整视频列表，新方案版本不完整。

**修复建议：**

同步改为：

```python
video_sources = self.storage.list_session_video_sources(sid)
avids = sorted({row["avid"] for row in video_sources})
videos_rows = self.storage.list_videos_by_avids(avids)
videos_by_id = {r["avid"]: r for r in videos_rows}
```

---

### 问题 5：任务 7 多源执行后 `retry_failed` 完全未适配

**位置：** 任务 7 全文 + 现有 `core/session.py` 第 245-291 行

**问题：**

现有 `retry_failed`（[core/session.py](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L279)）用 `s["source_fid"]` 作为 src_media_id。多源场景下失败项的 avid 来自不同源，retry 会从错误的源重试。**计划通篇没有提到 retry_failed 的多源适配**，这是遗漏。

**影响：**

多源整理的失败项重试会全部失败或误操作非源收藏夹。

**修复建议：**

retry_failed 必须按失败项的来源收藏夹分组。失败项应记录 `source_fid`（在 `failed_items` 表新增列或通过 `session_video_sources` 反查），重试时按来源分组调用 `move_videos(src_media_id=it["source_fid"], ...)`。计划应新增「任务 7.5：retry_failed 多源适配」。

---

### 问题 6：任务 7 `total` 计数与 `success` 口径不一致

**位置：** 任务 7 步骤 5，原计划第 1776-1778 行 + 现有 `core/session.py` 第 240 行

**问题：**

步骤 5 明确说「success 统计成功移动的来源实例数量」，但现有代码 `stats = {"success": success, "failed": failed, "total": len(items)}`（[core/session.py](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L240)）的 `total` 仍是唯一视频数。步骤 5 代码片段**没有更新 total 的计算**。

**影响：**

多源场景下会出现 `success=3, total=2` 的诡异情况，前端无法正确显示进度。

**修复建议：**

`total` 应统计来源实例总数：

```python
total_sources = sum(len(avids) for avids in move_groups.values())
stats = {"success": success, "failed": failed, "total": total_sources}
```

同时在执行进度事件中区分 `unique_videos` 和 `source_instances`。

---

### 问题 7：任务 7 缺失 `mark_plan_item_executed` 方法

**位置：** 任务 7 步骤 2，原计划第 1691 行 + 任务 1 存储方法列表（第 600-630 行）

**问题：**

步骤 2 说「执行结果标记也要优先写入版本明细 `mark_plan_item_executed`」，但任务 1 的存储方法列表**完全没有定义这个方法**。

**影响：**

执行智能体照计划走会调用一个不存在的方法，要么报 AttributeError，要么自己实现一个签名不一致的版本。

**修复建议：**

任务 1 必须新增方法定义：

```python
def mark_plan_item_executed(self, version_id: str, avid: int, ok: bool) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE classification_plan_items SET executed = ? WHERE version_id = ? AND avid = ?",
            (1 if ok else 0, version_id, avid),
        )
```

并在任务 7 步骤 2 明确调用时机。

---

### 问题 8：任务 5 账号切换有未解决的时序与并发问题

**位置：** 任务 5 步骤 3-4，原计划第 1372-1397 行

**问题：**

- **扫码时序：** 扫码前不知道 mid，无法确定 `cookie_path`；扫码成功后 cookie 已写到临时路径。计划没解决这个先有鸡还是先有蛋的问题。
- **`get_bili()` 每次新建实例：** `_running_pipelines` 字典里存的任务引用旧 bili 实例，切换账号后正在运行的 pipeline 还在用旧账号 client。计划没说明如何处理运行中的 pipeline。
- **并发安全：** 多账号并发请求时 WBI key 缓存（`core/storage.py` 第 48-53 行是 `id=1` 单行）会互相覆盖签名失败。

**影响：**

- 扫码后无法正确保存账号 Cookie。
- 切换账号瞬间正在运行的 pipeline 会出现一半用旧账号一半用新账号的混乱状态。
- 多账号场景下 WBI 签名频繁失效，接口大面积报 -403。

**修复建议：**

- **扫码时序：** 扫码用临时 client + 临时 cookie 文件，扫码成功后立即调 `nav` 接口拿 mid，再 `mv` 临时文件到 `accounts/<mid>/bilibili_cookie.json`。
- **运行中 pipeline：** 切换账号前必须检查 `_running_pipelines`，若有运行中的任务则拒绝切换或先 cancel。前端也要在切换前 confirm。
- **WBI key 缓存：** 改为按 mid 维度存储，`wbi_keys` 表加 `mid` 列，`load_wbi_keys(mid)` / `save_wbi_keys(mid, ...)`。

---

## 二、重要问题

### 问题 9：ALTER TABLE 缺少迁移机制

**位置：** 任务 0，原计划第 197-203 行

**问题：**

```sql
ALTER TABLE classify_sessions ADD COLUMN account_id TEXT;
ALTER TABLE fav_folders ADD COLUMN account_id TEXT;
ALTER TABLE videos ADD COLUMN account_id TEXT;
```

现有 `_init_db` 用 `CREATE TABLE IF NOT EXISTS`（[core/storage.py](file:///d:/GZY/TraeComm/BiBiTool/core/storage.py#L102-L104)），**对已存在的表不会添加新列**。计划没说明这些 ALTER 在哪个迁移函数里执行、如何避免重复执行报错。

**影响：**

旧库升级时 `account_id` 列会缺失，访问时 SQLite 报 `no such column`。

**修复建议：**

在 `_init_db` 后增加 `_migrate_schema()`，用 `PRAGMA table_info(tablename)` 检查列是否存在，不存在则执行 ALTER。或引入 schema_version 机制：

```python
def _migrate_schema(self) -> None:
    with self._conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(classify_sessions)")}
        if "account_id" not in cols:
            conn.execute("ALTER TABLE classify_sessions ADD COLUMN account_id TEXT")
        # 同理处理 fav_folders、videos
```

---

### 问题 10：初始分类不创建版本 1，`migrate_legacy_classifications_to_version` 未定义

**位置：** 任务 6 步骤 3，原计划第 1536 行 + 任务 7 步骤 2

**问题：**

任务 6 步骤 3 的 `refine_plan` 依赖 `get_active_plan_version`，但**正常流程的 `classify()` 完成后没有自动创建版本 1**。任务 7 步骤 2 又说「没有 active version 就回退到旧 load_classifications」。

这种双轨制下：
- 新会话：classify 后没版本 → 执行时回退到 classifications 表 → 任务 6 的 refine 又会触发 `migrate_legacy_classifications_to_version`
- `migrate_legacy_classifications_to_version` 在计划里**只提到名字，没有定义签名和实现**

**影响：**

执行智能体无法实现这个关键方法，refine_plan 流程跑不通。

**修复建议：**

- 任务 2.5 步骤 4（classify 改造）末尾必须增加：classify 完成后调 `storage.create_plan_version(sid, None, "初始分类", items, activate=True)`。
- 任务 1 必须新增 `migrate_legacy_classifications_to_version(session_id)` 方法定义：读取旧 `classifications` 表数据，构造 version 1 并激活。

---

### 问题 11：任务 2 新增 `non_video_type` 检测但无测试覆盖

**位置：** 任务 2 步骤 1-3，原计划第 663-723 行

**问题：**

现有 `core/bilibili_api.py` 第 208-214 行只检查 attr 和 id，不检查 type，非 type=2 资源会被当普通视频塞进 batch。任务 2 步骤 3 新增了 `non_video_type` 检查，但**步骤 1 的测试用 mock 数据全是 `type: 2`**，新分支没测试覆盖。

**影响：**

新增的 type 检查可能写错逻辑而不被发现。

**修复建议：**

测试 mock 数据加入非视频类型项（如 `type: 11` 的剧集、`type: 24` 的电影），断言它们进入 `skipped_items` 且 reason_code 为 `non_video_type`。

---

### 问题 12：任务 8.5 用 title=="默认收藏夹" 判断默认收藏夹不可靠

**位置：** 任务 8.5 步骤 4-5，原计划第 1925、1954 行

**问题：**

用户可重命名默认收藏夹，B 站也可能改文案。用 `folder.get("title") == "默认收藏夹"` 判断不可靠。

**影响：**

用户重命名后默认收藏夹可能被误删（不可逆）。

**修复建议：**

优先用 B 站返回的更稳定字段。可考虑：
- `fav_state` 字段（B 站收藏夹接口可能返回）
- 不允许删除任何 `media_count == 0` 且 fid 最小的收藏夹（默认收藏夹通常最早创建）
- 或干脆要求用户手动确认每个删除项，把判断责任交还用户

至少要加注释说明判断依据并要求人工二次确认。

---

### 问题 13：任务 8.5 `refresh_empty_source_candidates` 网络失败会拖垮 execute

**位置：** 任务 8.5 步骤 4，原计划第 1937 行

**问题：**

`refresh_empty_source_candidates` 会调用 `bili.get_my_folders()`（网络请求）。如果执行过程中账号掉线或网络异常，这个调用抛异常会导致 `execute()` 失败但**移动已经完成**。

**影响：**

视频已移动但 session 状态卡在 executing，无法进入 done，前端无法显示结果页。

**修复建议：**

两种方案任选其一：
- 把 `refresh_empty_source_candidates` 用 `try/except` 包裹，失败时记日志但不影响 done 状态写入。
- 放到 done 状态写入之后异步执行（`asyncio.create_task`），失败时通过 SSE 推送告警。

---

### 问题 14：videos 表主键 avid 在多账号下会数据覆盖

**位置：** 原计划第 202 行 + 第 2128 行风险说明

**问题：**

计划第 202 行只是 `ALTER TABLE videos ADD COLUMN account_id TEXT`，没改主键。多账号场景下同一个 avid 被不同账号收藏时，`upsert_video`（[core/storage.py](file:///d:/GZY/TraeComm/BiBiTool/core/storage.py#L125-L131) 用 `INSERT OR REPLACE`）会覆盖。

计划承认问题但「留给后续」，可多账号功能现在就要上线，**实际使用即数据错乱**。

**影响：**

A 账号的视频元数据会被 B 账号覆盖，导致分类时拿到错误的 title/up_name/tname。

**修复建议：**

要么现在就迁移为 `(account_id, avid)` 组合主键，要么在多账号上线前明确文档说明「同 avid 跨账号会被覆盖，建议每个账号独立数据库文件」。

---

### 问题 15：session_sources 表缺少 account_id 列

**位置：** 原计划第 48-65 行

**问题：**

`skipped_items` 表有 `account_id`（第 129 行），但 `session_sources` 表没有。多账号多源场景下无法区分来源账号，与多账号目标不一致。

**影响：**

切换账号后无法知道某个源收藏夹属于哪个账号，整理历史无法追溯。

**修复建议：**

`session_sources` 表增加 `account_id TEXT` 列，`save_session_sources` 时填入当前 active account_id。

---

## 三、次要问题

### 问题 16：「当前实现状态」描述不准

**位置：** 原计划第 30 行

**问题：**

说 stats「只保存 `skipped_total`、`skipped_by_reason`」，但实际 [core/session.py](file:///d:/GZY/TraeComm/BiBiTool/core/session.py#L83-L89) 还保存 `source_total` / `scanned_total` / `collected_total`。

**影响：** 描述不准，但不影响实施。

**修复建议：** 修正描述。

---

### 问题 17：任务 0 步骤 6 `create_many` 对不在列表的 fid 处理未说明

**位置：** 原计划第 487-496 行

**问题：**

`folders.get(fid, {}).get("title", f"收藏夹 {fid}")` 会回退，但计划没说明这种情况（旧 fid 或 fid 不属于当前账号）。

**影响：** 实施时可能遗漏异常处理。

**修复建议：** 明确「fid 不在用户收藏夹列表时抛 BibiError 提示」。

---

### 问题 18：任务 2.5 与现有测试的兼容性依赖兜底逻辑

**位置：** 任务 2.5 步骤 3，原计划第 862-864 行

**问题：**

现有 collect 测试（`tests/test_session.py` 第 24-57 行）创建 session 时没调 `save_session_sources`，靠兜底 `if not sources and s.get("source_fid")` 工作。计划未强调这个依赖。

**影响：**

执行智能体若忘兜底，所有现有测试失败。

**修复建议：** 在步骤 3 兜底逻辑旁加注释「为兼容旧会话和现有测试」。

---

### 问题 19：`_chunks` 函数未定义

**位置：** 任务 7 步骤 5，原计划第 1766 行

**问题：** 现有代码和计划都没定义这个函数，执行智能体需自行实现。

**修复建议：** 在任务 7 步骤 5 前补充工具函数定义：

```python
def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
```

---

### 问题 20：任务 4.5 `selectedSourceFids` 全局 Set 未说明清空时机

**位置：** 任务 4.5 步骤 4，原计划第 1230 行

**问题：** 返回首页后残留旧选择，下次进首页会显示已选状态。

**修复建议：** 在 `renderHome()` 开头清空 `selectedSourceFids.clear()`，并调用 `updateFolderSelectionUi()`。

---

### 问题 21：任务 4.5 首页卡片 HTML 结构改造未展示现有结构

**位置：** 任务 4.5 步骤 5，原计划第 1262-1269 行

**问题：** 直接给了新结构，但现有卡片（`static/app.js` 第 123 行之后）可能结构不同，执行智能体需先重构。

**修复建议：** 步骤 5 前增加「先 Read 现有 `renderHome` 的卡片 HTML，再在原结构上增加 `data-folder-id` 和勾选图标」。

---

### 问题 22：任务 0 `SessionIn` 改动需前后端同步部署

**位置：** 任务 0 步骤 5，原计划第 449-451 行

**问题：** `source_fid` 从 `int` 改为 `int | None`，前端不同步传 `source_fids` 会出问题。任务 0 后任务 4.5 前的中间状态，前端传 `source_fid` 还能工作（兜底），但计划没明确部署顺序。

**修复建议：** 任务 0 步骤 5 明确「后端兼容旧 `source_fid` 字段，前端改造在任务 4.5 完成」。

---

### 问题 23：任务 2 `removable` 判断在真实失效资源上可能失效

**位置：** 任务 2 步骤 3，原计划第 707 行

**问题：**

`removable: removable and bool(m.get("id"))`。但 B 站失效资源可能**没有 id 字段**（被 attr 标记失效但同时 id 缺失），此时 removable=False，无法删除。

与「对 attr_invalid 默认允许移除」矛盾。

**修复建议：** 对于 `attr_invalid` 类型，即使没有 `id` 也应允许尝试移除（用 bvid 反查 avid 后删除）。或在跳过明细中明确标注「无 id，无法移除」并解释原因。

---

### 问题 24：任务 6 AI 微调未处理返回多余 avid

**位置：** 任务 6 步骤 2，原计划第 1511-1516 行

**问题：**

AI 若返回不在 current 里的 avid，会被 `by_avid.get(old.avid)` 静默忽略，无日志无报错。

**修复建议：** 增加 diff 检测，AI 返回的 avid 集合与 current 不一致时记日志或抛 `AiApiError(code="AI_BAD_JSON")`。

---

### 问题 25：任务 8.5 `delete_folders` 部分失败后状态不一致

**位置：** 任务 8.5 步骤 5，原计划第 1961-1965 行

**问题：**

若 B 站返回错误，`mark_session_source_deleted` 不执行，但**部分收藏夹可能已删除**。下次查 folders 时它们已不存在，但 session_sources 里 `deleted=0`。

**修复建议：** 删除后无论成功失败都重新拉取 `get_my_folders`，对比 session_sources 里的候选，根据实际存在情况更新 `deleted` 字段。

---

### 问题 26：无数据回滚方案

**位置：** 全文

**问题：** 7 张新表 + classifications/plan_items 双轨制，无降级路径。

**修复建议：** 至少保留 classifications 双写直到新表验证稳定。建议在任务 7 完成且端到端验收通过后，再单独提一个 PR 删除旧 classifications 表。

---

## 四、建议的执行顺序

执行智能体在实施前应先做以下准备工作：

1. **修复原计划文档** 中标记为致命的问题 1-8（特别是问题 1 的 SQL、问题 3 的取消检查、问题 8 的账号时序）。
2. **补充关键方法定义**：问题 7 的 `mark_plan_item_executed`、问题 10 的 `migrate_legacy_classifications_to_version`、问题 19 的 `_chunks`。
3. **新增任务 7.5**：retry_failed 的多源适配（问题 5）。
4. **明确迁移机制**：问题 9 的 schema 迁移函数、问题 14 的 videos 主键决策。
5. 然后按原任务顺序 0 → 1 → 2 → 2.5 → 3 → 4 → 4.5 → 5 → 6 → 7 → 7.5 → 8 → 8.5 → 9 实施。

实施过程中遇到次要问题 16-26 可顺手修正。

---

## 五、参考文件

- 原计划：[implementation-plan-2026-07-07-review-filter-cleanup-account-refine.md](file:///d:/GZY/TraeComm/BiBiTool/docs/implementation-plan-2026-07-07-review-filter-cleanup-account-refine.md)
- 当前代码：
  - [core/storage.py](file:///d:/GZY/TraeComm/BiBiTool/core/storage.py)
  - [core/session.py](file:///d:/GZY/TraeComm/BiBiTool/core/session.py)
  - [core/bilibili_api.py](file:///d:/GZY/TraeComm/BiBiTool/core/bilibili_api.py)
  - [core/ai_classifier.py](file:///d:/GZY/TraeComm/BiBiTool/core/ai_classifier.py)
  - [main.py](file:///d:/GZY/TraeComm/BiBiTool/main.py)
  - [static/app.js](file:///d:/GZY/TraeComm/BiBiTool/static/app.js)
  - [static/index.html](file:///d:/GZY/TraeComm/BiBiTool/static/index.html)
