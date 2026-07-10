# 修复后复查报告：资源类型键修复验证

> **审查日期：** 2026-07-08  
> **审查对象：** 根据 `docs/review-2026-07-08-progress-cpu-resource-logout-implementation-followup.md` 完成后的修复  
> **审查方式：** 全量测试、关键路径静态审查、针对上轮 P1/P2 问题的本地复现场景。

## 结论

本轮修复解决了上一份报告中的大部分问题：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 129 passed in 7.75s
```

已验证通过的点：

- `videos` 表已迁移为 `(account_id, resource_id, resource_type)` 主键。
- `session_video_sources` 已迁移为 `(session_id, resource_id, resource_type, source_fid)` 主键。
- `refine_plan()` 对普通非视频资源能保留 `resource_type`。
- 半迁移状态下，`classification_plan_items` 和 `failed_items` 能补齐 `resource_type`。
- AI prompt 已包含 `resource_type_name`。
- `AdjustIn` 已恢复 `avid` 兼容。
- SSE 复用已有任务时会带上 `source_total/scanned/collected/skipped`。
- 前端主要“视频”文案已替换为“条目/收藏条目”。

但仍有 1 个 P1 问题：代码的数据表已经支持 `id:type`，但 AI 分类结果和若干会话内存映射仍只按 `resource_id/avid` 建模。同一个数字 ID 同时存在不同资源类型时，分类阶段会失败或后续执行会串类型。

---

## P1：AI 分类与会话映射仍只按 `resource_id`，未真正按 `(resource_id, resource_type)` 区分

**位置：**

- `core/ai_classifier.py:48`
- `core/ai_classifier.py:124`
- `core/ai_classifier.py:179`
- `core/session.py:206`
- `core/session.py:242`
- `core/session.py:299`
- `core/session.py:335`
- `core/session.py:417`
- `core/session.py:629`
- `core/session.py:643`

**问题：**

虽然存储表已经支持组合键，但 AI 分类结果仍是：

```python
@dataclass
class Classification:
    avid: int
    category: str
    confidence: float
    reason: str
```

`Classification` 没有 `resource_type`。同时 `AiClassifier.classify_batch()` 和 `refine_plan()` 都用 `by_avid` 映射 AI 返回结果：

```python
by_avid = {it["avid"]: it for it in items}
```

`ClassifySession.classify()` 也把来源类型压成单值字典：

```python
resource_type_by_id = {row["resource_id"]: row.get("resource_type", 2) for row in video_sources}
```

如果同一批里存在 `123:2` 视频和 `123:11` 合集，两个条目都会以 `avid=123` 交给 AI，返回结果也都只有 `avid=123`。后端无法知道哪个结果对应哪个类型。

**本地复现：**

构造同一会话下两个资源：

- `resource_id=123, resource_type=2`
- `resource_id=123, resource_type=11`

分类阶段输入 AI 时确实有两条：

```text
ai input [(123, 2, '视频123'), (123, 11, '合集123')]
```

但 `classification_items` 写回时会使用 `resource_type_by_id.get(123)`，只能得到一个类型，最终两条 plan item 试图写入同一个 `(version_id, 123, type)` 键，触发：

```text
IntegrityError UNIQUE constraint failed:
classification_plan_items.version_id,
classification_plan_items.resource_id,
classification_plan_items.resource_type
```

**影响：**

这是上轮 P1-2 的残留：表层主键修好了，但业务层仍没有彻底使用组合键。虽然 B 站同 ID 不同类型可能不高频，但既然移动接口和存储目标已经按 `id:type` 建模，这个场景必须闭环。

潜在影响不止分类：

- `get_plan()` 中 `videos = {r["resource_id"]: r for r in videos_rows}` 会让同 ID 不同类型互相覆盖。
- `refine_plan()` 中 `videos_by_id` / `type_by_id` 仍按单 `resource_id` 映射。
- `execute()` 中 `sources_by_id` 按单 `resource_id` 聚合，可能把同 ID 不同类型移动到同一分类。
- `_retry_failed_sources()` 中 `items_by_id` / `failed_items_by_id` 也按单 `resource_id` 聚合。

**建议修复：**

把 AI 和会话层的身份从 `avid` 升级为组合键。

1. 扩展 `Classification`：

```python
@dataclass
class Classification:
    resource_id: int
    resource_type: int
    category: str
    confidence: float
    reason: str

    @property
    def avid(self) -> int:
        return self.resource_id
```

如果担心改动面，可以暂时保留 `avid` 字段，但必须新增 `resource_type` 字段。

2. AI prompt 输出必须包含 `resource_type`：

```text
输出：严格JSON，形如
{"items":[{"resource_id":int,"resource_type":int,"category":"中文2-6字","confidence":0-1,"reason":"≤20字原因"}]}
```

为了兼容旧响应，可以读取时 fallback：

```python
rid = it.get("resource_id", it.get("avid"))
rtype = it.get("resource_type", v.resource_type)
```

3. `classify_batch()` 用组合键匹配：

```python
by_key = {
    (it.get("resource_id", it.get("avid")), it.get("resource_type", 2)): it
    for it in items
}
for v in videos:
    it = by_key.get((v.avid, v.resource_type))
```

4. `ClassifySession.classify()` 写回时使用结果自身的类型：

```python
classification_items = [
    {
        "avid": c.resource_id if c.resource_type == 2 else 0,
        "resource_id": c.resource_id,
        "resource_type": c.resource_type,
        "category": c.category,
        "confidence": c.confidence,
        "reason": c.reason,
    }
    for c in results
]
```

5. 所有内存映射改为组合键：

```python
key = (resource_id, resource_type)
```

需要覆盖：

- `resource_type_by_id` -> `resource_type_by_key`
- `videos = {r["resource_id"]: r ...}` -> `videos_by_key = {(r["resource_id"], r["resource_type"]): r ...}`
- `videos_by_id`
- `sources_by_id`
- `items_by_id`
- `failed_items_by_id`

6. 前端 `plan.videos` 如果仍返回对象，键也要避免冲突。可以改为：

```python
videos = {f"{r['resource_id']}:{r['resource_type']}": r for r in videos_rows}
```

前端读取：

```javascript
const videoKey = `${rid}:${rtype}`;
const v = videos[videoKey] || videos[rid] || {};
```

7. 新增测试：

```python
@pytest.mark.asyncio
async def test_classify_distinguishes_same_resource_id_different_type(deps):
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
        "bvid": "", "title": "合集123", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 100,
    })
    storage.add_session_video_source(sid, resource_id=123, resource_type=2, source_fid=100)
    storage.add_session_video_source(sid, resource_id=123, resource_type=11, source_fid=100)

    async def classify(videos, batch_size=50, on_progress=None):
        return [
            Classification(resource_id=v.avid, resource_type=v.resource_type,
                           category=f"类型{v.resource_type}", confidence=0.9, reason="")
            for v in videos
        ]

    ai.classify = classify
    await ClassifySession(storage, bili, ai).classify(sid)

    active = storage.get_active_plan_version(sid)
    items = storage.load_plan_items(active["version_id"])
    by_key = {(it["resource_id"], it["resource_type"]): it for it in items}
    assert by_key[(123, 2)]["category"] == "类型2"
    assert by_key[(123, 11)]["category"] == "类型11"
```

---

## 其他验证记录

### 全量测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 129 passed in 7.75s
```

### JS 语法检查

当前环境未安装 Node.js，已跳过：

```text
node not found; skipped
```

### 编码检查

以下文件未发现 Unicode replacement character：

- `core/session.py`
- `core/ai_classifier.py`
- `core/storage.py`
- `main.py`
- `static/app.js`
- `static/index.html`

## 自检

- [x] 复查上一份报告的 P1/P2/P3 项。
- [x] 运行全量测试。
- [x] 复现微调非视频保留类型：已通过。
- [x] 复现同 ID 不同类型存储共存：已通过。
- [x] 复现半迁移库：已通过。
- [x] 发现 AI/会话层仍按单 ID 映射的剩余 P1。
