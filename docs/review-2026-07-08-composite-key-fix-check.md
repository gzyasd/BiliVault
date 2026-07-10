# 组合键修复后复查报告

> **审查日期：** 2026-07-08  
> **审查对象：** 针对 `docs/review-2026-07-08-followup-fix-verification.md` 中剩余 P1 的修复  
> **审查方式：** 全量测试、关键路径静态审查、本地复现场景。

## 结论

### 2026-07-08 修复后复查更新

另一个智能体已按本报告修复后，我再次复查，结论如下：

- 本报告原 P1 已修复：`classify()`、`get_plan()`、`refine_plan()`、`execute()` 已改为按 `(resource_id, resource_type)` 精确查询资源缓存，当前会话只包含 `123:2` 时，不再误带入缓存中的 `123:11`。
- 本报告原 P2-A 已修复：`classifications` 表已迁移为 `(session_id, resource_id, resource_type)` 主键，`save/load/adjust/mark_executed` 均已带 `resource_type`。
- 本报告原 P2-B 已修复：`delete_one_failed_item()` 已增加 `resource_type` 条件，重试成功后不会清理错同 ID 其他类型的失败项。
- 本报告原 P2-C 已修复：`videos` 半迁移状态下已有的 `resource_id/resource_type` 会被保留，不再被 `avid` 回填覆盖成 `0:2` 或普通视频类型。

复查验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 134 passed in 7.94s
```

定向复现场景全部通过：

- 当前会话只有 `123:2`，同账号缓存另有 `123:11`：AI 输入只收到 `[(123, 2)]`。
- `classifications` 写入同 `resource_id` 不同 `resource_type` 两项：可同时保留 `123:2` 和 `123:11`。
- `failed_items` 中同 ID 同分类不同类型：删除 `123:2` 失败项后，`123:11` 正确保留。
- `videos` 半迁移表 `PRIMARY KEY(account_id, avid)` 且已有 `resource_id=123/resource_type=11/avid=0`：初始化后仍保留为 `123:11`。

补充核查：

- `main.py` 的 `/api/session/{sid}/adjust` 已接收并传递 `resource_type`。
- `static/app.js` 预览页调整分类时已按 `resource_id + resource_type` 发送请求，并按 `"resource_id:resource_type"` 读取视频详情。
- 使用 `Get-Content -Encoding utf8` 检查后，代码文件中的中文注释和提示文本不是实际乱码；之前 PowerShell 默认编码输出出现的乱码只是终端解码问题。

当前未发现新的阻断性问题。本报告下方保留的是修复前的问题记录和修复建议，作为历史依据。

---

本轮修复已经解决上一份报告指出的“同一会话内 `123:2` 与 `123:11` 同时存在时分类写回冲突”的问题。

验证结果：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 130 passed in 7.73s
```

已确认修复：

- `Classification` 已增加 `resource_type` 字段，并保持旧构造方式兼容。
- `AiClassifier.classify_batch()` 已按 `(resource_id, resource_type)` 匹配 AI 返回项。
- `AiClassifier.refine_plan()` 已按 `(resource_id, resource_type)` 校验和匹配。
- `get_plan()` 返回的 `videos` 使用 `"resource_id:resource_type"` 键，前端也按该键读取。
- `classify/refine/execute/retry` 中大量单 ID 映射已改为组合键。
- 上轮失败场景已通过：同一会话内 `123:2` 和 `123:11` 可以分别分类并写入两个方案项。

但是仍发现 1 个新的 P1 问题：资源详情查询仍按 `resource_id IN (...)` 取缓存行，没有按当前会话的 `(resource_id, resource_type)` 精确过滤，会把缓存中同 ID 但不属于本次会话的其他类型资源误带入分类方案。

进一步复查还发现 3 个 P2 问题，都是组合键改造的旁支未闭环：旧 `classifications` 表仍按单 `avid` 覆盖、失败项重试清理没有带 `resource_type`、`videos` 半迁移状态可能丢失已有类型信息。

---

## P1：分类/预览/微调/执行查询资源详情时未按 `(resource_id, resource_type)` 精确过滤

**位置：**

- `core/storage.py:468`
- `core/session.py:202`
- `core/session.py:205`
- `core/session.py:294`
- `core/session.py:295`
- `core/session.py:331`
- `core/session.py:335`
- `core/session.py:419`

**问题：**

`videos` 表现在已经是组合主键：

```sql
PRIMARY KEY (account_id, resource_id, resource_type)
```

但查询方法仍是：

```python
def list_videos_by_avids(self, avids: list[int], account_id: str | None = None) -> list[dict]:
    SELECT * FROM videos WHERE account_id = ? AND resource_id IN (...)
```

它只按 `resource_id` 查询，不按 `resource_type` 过滤。

`ClassifySession.classify()` 当前先从会话来源得到：

```python
resource_ids = sorted({row["resource_id"] for row in video_sources})
videos_rows = self.storage.list_videos_by_avids(resource_ids, account_id=s.get("account_id"))
```

如果当前会话只包含 `123:2`，但缓存表里同账号已有 `123:11`，`list_videos_by_avids([123])` 会把两个资源都返回，导致 `123:11` 被错误送入 AI 分类和方案。

另外，`execute()` 中用于失败项标题的查询仍是：

```python
videos = {(r["resource_id"], r.get("resource_type", 2)): r for r in self.storage.list_videos_by_fid(s["source_fid"])}
```

多源会话时 `s["source_fid"]` 只是第一个源收藏夹，第二个及之后源收藏夹的失败项标题可能查不到或取错。这个问题也应随 `list_videos_by_resource_keys()` 一起修掉。

**本地复现：**

构造缓存：

- `videos`: `123:2`，属于当前源收藏夹 `fid=1`
- `videos`: `123:11`，来自其他缓存来源 `fid=999`
- 当前会话 `session_video_sources` 只有 `123:2`

执行分类后输出：

```text
ai input [(123, 2, '视频123'), (123, 11, '合集123-不属于本会话')]
plan [(123, 2, '分类2'), (123, 11, '分类11')]
```

这说明 `123:11` 并不属于当前会话，却被加入了当前整理方案。

**影响：**

用户可能只选择了一个源收藏夹中的视频条目，但程序会把历史缓存里的同 ID 其他类型资源也加入预览和执行。执行阶段会尝试从当前源收藏夹移动 `123:11`，轻则 B 站接口报错，重则移动非预期资源。

这个问题不要求 B 站同一源收藏夹里真的同时出现同 ID 不同类型，只要缓存中曾经存在同账号同 ID 的另一种类型，就可能被误带入。

## 建议修复

### 1. 新增按资源键查询的方法

在 `core/storage.py` 新增：

```python
def list_videos_by_resource_keys(self, keys: list[tuple[int, int]], account_id: str | None = None) -> list[dict]:
    if not keys:
        return []
    clauses = " OR ".join(["(resource_id = ? AND resource_type = ?)"] * len(keys))
    params: list[int | str] = [account_id or ""]
    for rid, rtype in keys:
        params.extend([rid, rtype])
    with self._conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM videos WHERE account_id = ? AND ({clauses}) ORDER BY resource_id, resource_type",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
```

保留 `list_videos_by_avids()` 作为旧兼容方法，但新会话流程不要再用它查资源详情。

### 2. `classify()` 按会话来源组合键查询

把：

```python
resource_ids = sorted({row["resource_id"] for row in video_sources})
videos_rows = self.storage.list_videos_by_avids(resource_ids, account_id=s.get("account_id"))
```

改为：

```python
resource_keys = sorted({(row["resource_id"], row.get("resource_type", 2)) for row in video_sources})
if resource_keys:
    videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
else:
    videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
```

### 3. `get_plan()` 按方案项组合键查询

把：

```python
resource_ids = [it.get("resource_id", it.get("avid")) for it in items]
videos_rows = self.storage.list_videos_by_avids(resource_ids, account_id=s.get("account_id"))
```

改为：

```python
resource_keys = [
    (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
    for it in items
]
videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
```

### 4. `refine_plan()` 按当前方案项组合键查询

把：

```python
resource_ids = sorted({row["resource_id"] for row in video_sources} | {it.get("resource_id", it.get("avid")) for it in current_items})
videos_by_key = {
    (r["resource_id"], r.get("resource_type", 2)): r
    for r in self.storage.list_videos_by_avids(resource_ids, account_id=s.get("account_id"))
}
```

改为：

```python
resource_keys = sorted({
    (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
    for it in current_items
})
videos_by_key = {
    (r["resource_id"], r.get("resource_type", 2)): r
    for r in self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
}
```

### 5. 增加回归测试

新增测试覆盖“缓存有同 ID 其他类型，但当前会话只包含其中一个类型”：

```python
@pytest.mark.asyncio
async def test_classify_does_not_include_cached_same_id_other_type(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 2, "avid": 123,
        "bvid": "BV123", "title": "视频123", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "视频", "fid": 100,
    })
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 11, "avid": 0,
        "bvid": "", "title": "缓存中的其他合集", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 999,
    })
    storage.add_session_video_source(sid, resource_id=123, source_fid=100, resource_type=2)

    seen = []

    async def classify(videos, batch_size=50, on_progress=None):
        seen.extend((v.avid, v.resource_type) for v in videos)
        return [Classification(v.avid, "视频类", 0.9, "", resource_type=v.resource_type) for v in videos]

    ai.classify = classify
    await ClassifySession(storage, bili, ai).classify(sid)

    assert seen == [(123, 2)]
    active = storage.get_active_plan_version(sid)
    items = storage.load_plan_items(active["version_id"])
    assert [(it["resource_id"], it["resource_type"]) for it in items] == [(123, 2)]
```

### 6. `execute()` 也按方案项组合键查询标题

把：

```python
videos = {(r["resource_id"], r.get("resource_type", 2)): r for r in self.storage.list_videos_by_fid(s["source_fid"])}
```

改为：

```python
resource_keys = [
    (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
    for it in items
]
videos = {
    (r["resource_id"], r.get("resource_type", 2)): r
    for r in self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
}
```

---

## P2-A：旧 `classifications` 表仍按单 `avid` 主键，会覆盖同 ID 不同类型

**位置：**

- `core/storage.py:47`
- `core/storage.py:527`
- `core/storage.py:536`
- `core/storage.py:543`
- `core/storage.py:558`
- `core/session.py:249`
- `core/session.py:293`
- `core/session.py:417`
- `core/session.py:635`

**问题：**

虽然新方案表 `classification_plan_items` 已经按 `(version_id, resource_id, resource_type)` 存储，但旧 `classifications` 表仍是：

```sql
PRIMARY KEY (session_id, avid)
```

`save_classifications()` 也仍只写 `avid`：

```python
INSERT OR REPLACE INTO classifications (session_id, avid, category, ...)
```

**本地复现：**

写入同一 session 的两条结果：

- `123:2`
- `123:11`

最后 `load_classifications()` 只剩一条，后一条覆盖前一条。

**影响：**

正常新流程会立即创建 `classification_plan_items` 版本，所以多数情况下用户看不到这个问题。但以下路径仍会受影响：

- `create_plan_version()` 失败或中断后回退到 `classifications`。
- `get_plan()`、`execute()`、`_retry_failed_sources()` 中 `active` 不存在时的 fallback。
- `migrate_legacy_classifications_to_version()` 只能迁移单 ID 结果，无法表示非视频类型。

**建议修复：**

二选一：

1. 彻底把 `classifications` 也迁移为资源键：

```sql
CREATE TABLE classifications (
  session_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
  avid INTEGER,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,
  executed INTEGER DEFAULT 0,
  PRIMARY KEY (session_id, resource_id, resource_type)
)
```

并同步修改 `save_classifications/load_classifications/adjust_classification/mark_classification_executed`。

2. 如果确认新流程永远依赖 `classification_plan_items`，则停止在新流程写 `classifications`，并只把它视为旧数据迁移源。这样要删除或隔离 `active is None` 的新流程 fallback，避免误用旧表。

建议选择方案 1，兼容性更稳。

## P2-B：`delete_one_failed_item()` 删除失败项时没有带 `resource_type`

**位置：**

- `core/storage.py:896`
- `core/session.py:695`

**问题：**

当前实现：

```python
def delete_one_failed_item(self, session_id: str, resource_id: int, category: str) -> None:
    DELETE ... WHERE session_id = ? AND resource_id = ? AND category = ?
```

没有按 `resource_type` 限定。

**本地复现：**

失败项中有：

- `123:11`，category 相同
- `123:2`，category 相同

调用 `delete_one_failed_item("sid", 123, "同类")` 会删除最早的一条，不一定是当前重试成功的那个类型。

**影响：**

同 ID 不同类型且分类相同的失败项，在重试成功后可能清理错记录，导致失败列表残留或误删。

**建议修复：**

改签名：

```python
def delete_one_failed_item(self, session_id: str, resource_id: int, resource_type: int, category: str) -> None:
    ...
```

SQL 增加类型条件：

```sql
DELETE FROM failed_items WHERE id = (
  SELECT id FROM failed_items
  WHERE session_id = ? AND resource_id = ? AND resource_type = ? AND category = ?
  ORDER BY id LIMIT 1
)
```

调用处改为：

```python
self.storage.delete_one_failed_item(sid, resource_id, resource_type, cat)
```

并新增同 ID 不同类型失败项的重试清理测试。

## P2-C：`videos` 半迁移状态可能丢失已有 `resource_id/resource_type`

**位置：**

- `core/storage.py:245`
- `core/storage.py:280`

**问题：**

当前 `_migrate_videos_to_account_key()` 会处理很多旧表形态，但对“已有 `resource_id/resource_type` 列、主键仍不是最终组合键”的半迁移状态不够稳。

**本地复现：**

构造表：

```sql
CREATE TABLE videos (
  account_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER,
  avid INTEGER,
  ...
  PRIMARY KEY (account_id, avid)
)
```

插入一条 `resource_id=123, resource_type=11, avid=0` 的半迁移数据。初始化 `Storage` 后，结果变成：

```text
pk ['account_id', 'resource_id', 'resource_type']
rows [{'resource_id': 0, 'resource_type': 2, 'avid': 0, 'title': '合集半迁移'}]
```

原来的 `123:11` 被错误变成了 `0:2`。

**影响：**

如果用户数据库经历过中途失败迁移、其他智能体的半成品迁移，或未来升级过程中出现半迁移状态，资源键会被破坏，导致非视频资源丢失身份。

**建议修复：**

`_migrate_videos_to_account_key()` 不应该在发现 `resource_id/resource_type` 列时按旧 `avid` 逻辑重建。建议：

```python
cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
pk_cols = [...]
if pk_cols == ["account_id", "resource_id", "resource_type"]:
    return
if "resource_id" in cols or "resource_type" in cols:
    self._migrate_videos_to_resource_key(conn)
    return
```

同时 `_migrate_videos_to_resource_key()` 在只有 `resource_id`、缺 `resource_type` 时也应保留 `resource_id`：

```python
if has_resource_id:
    SELECT account_id, resource_id, COALESCE(resource_type, 2), COALESCE(avid, resource_id), ...
else:
    SELECT account_id, avid, 2, avid, ...
```

## 其他验证

- 全量 pytest：`130 passed in 7.55s`
- 独立复现“同一会话内 `123:2` 与 `123:11` 同时存在”：通过。
- 独立复现“微调同 ID 不同类型”：通过。
- 独立复现“当前会话只含 `123:2`，缓存存在 `123:11`”：会误纳入，见 P1。
- 独立复现 `classifications` 单 ID 覆盖：属实，见 P2-A。
- 独立复现 `delete_one_failed_item()` 未带类型误删风险：属实，见 P2-B。
- 独立复现 `videos` 半迁移丢类型：属实，见 P2-C。
- 编码检查：`core/session.py`、`core/ai_classifier.py`、`core/storage.py`、`main.py`、`static/app.js`、`static/index.html` 未发现 replacement character。

## 自检

- [x] 确认上一轮报告中的 P1 已基本修复。
- [x] 运行全量测试。
- [x] 复现同 ID 同会话不同类型场景。
- [x] 复现同 ID 其他类型缓存误纳入当前会话的新问题。
- [x] 复查旧 `classifications`、失败项清理、迁移半状态等旁支路径。
- [x] 未修改业务代码，仅形成复查报告。
