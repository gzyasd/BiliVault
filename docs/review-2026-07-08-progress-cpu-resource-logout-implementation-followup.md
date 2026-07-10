# 实施后复审报告：进度、CPU、资源分类、退出账号与 AI 批大小

> **审查日期：** 2026-07-08  
> **审查对象：** 根据 `docs/implementation-plan-2026-07-08-progress-cpu-resource-logout.md` 完成后的代码实现  
> **审查方式：** 静态代码审查、全量 pytest、针对非视频资源/迁移边界的本地复现场景。

## 结论

当前实现已经覆盖了不少需求，并且全量测试通过：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 128 passed in 7.41s
```

但仍存在 2 个 P1 问题、3 个 P2 问题和 2 个 P3 问题。最需要优先修复的是：非视频资源在微调方案后会被错误降级成视频类型，以及存储层仍有多处只用 `avid` 做主键/索引，无法真正保证 `id:type` 语义。

---

## P1 问题

### P1-1：非视频资源微调后会丢失 `resource_type`，执行时可能按视频移动

**位置：**
- `core/session.py:334`
- `core/session.py:346`
- `core/ai_classifier.py:22`

**问题：**

`refine_plan()` 构造给 AI 的 `VideoInfo` 时没有传入当前方案项的 `resource_type`，因此非视频资源会使用 `VideoInfo.resource_type` 默认值 `2`。随后创建新方案版本时只写：

```python
{"avid": c.avid, "category": c.category, "confidence": c.confidence, "reason": c.reason}
```

`Storage.create_plan_version()` 会把缺失的 `resource_type` 默认成 `2`。也就是说，原本 `resource_id=201, resource_type=11` 的合集，用户使用“把官方的作品单独放在一个收藏夹内”这类微调后，新版本会变成 `resource_type=2`。

**本地复现结果：**

构造一个 `resource_type=11` 的方案项后调用 `refine_plan()`：

```text
video resource_types passed to AI: [2]
新方案项 resource_type: 2
```

**影响：**

用户在预览方案阶段使用 AI 微调后，非视频资源可能在执行阶段被拼成 `201:2`，而不是正确的 `201:11`，导致移动失败或移动错误资源。

**建议修复：**

`refine_plan()` 应按当前 plan item 的资源键保留类型：

```python
videos = [
    VideoInfo(
        avid=it["resource_id"],
        title=videos_by_id[it["resource_id"]]["title"],
        up_name=videos_by_id[it["resource_id"]]["up_name"],
        tname=videos_by_id[it["resource_id"]].get("tname", ""),
        intro=videos_by_id[it["resource_id"]].get("intro", ""),
        tags=_parse_tags(videos_by_id[it["resource_id"]].get("tags", "[]")),
        resource_type=it.get("resource_type", 2),
    )
    for it in current_items if it["resource_id"] in videos_by_id
]
```

创建新版本时也必须把旧方案项的 `resource_type` 写回：

```python
type_by_id = {it["resource_id"]: it.get("resource_type", 2) for it in current_items}
items = [
    {
        "avid": c.avid,
        "resource_id": c.avid,
        "resource_type": type_by_id.get(c.avid, 2),
        "category": c.category,
        "confidence": c.confidence,
        "reason": c.reason,
    }
    for c in refined
]
```

并新增测试：非视频方案项 refine 后 `resource_type` 仍为 11。

### P1-2：存储层仍按 `avid` 唯一存资源，不能真正支持 `id:type`

**位置：**
- `core/storage.py:20`
- `core/storage.py:93`
- `core/session.py:135`
- `core/session.py:149`

**问题：**

计划要求让 `id:type` 成为分类和移动的核心标识，但当前实现仍把所有资源塞进旧 `videos` 和 `session_video_sources`：

```sql
PRIMARY KEY (account_id, avid)
PRIMARY KEY (session_id, avid, source_fid)
```

非视频采集时又执行：

```python
v["avid"] = resource_id
self.storage.upsert_video(v)
self.storage.add_session_video_source(... avid=resource_id, resource_type=resource_type)
```

这意味着同一账号/同一源收藏夹内如果出现相同数字 ID、不同 `resource_type` 的资源，只能保存一条。

**本地复现结果：**

先写入 `(123, type=2)`，再写入 `(123, type=11)`：

```text
videos 只剩后一条“合集123”
session_video_sources 只剩 type=2 的一条记录，type=11 被 INSERT OR IGNORE 丢弃
```

**影响：**

同 ID 不同类型的资源会被覆盖或漏掉；后续分类、预览、调整、执行、失败重试都可能使用错误标题或错误 `resource_type`。这与“智能整理不是只针对视频的”和 `id:type` 移动要求不完全一致。

**建议修复：**

不要继续用 `videos/session_video_sources` 承载所有资源。至少需要：

```sql
CREATE TABLE favorite_resources (
  account_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER,
  bvid TEXT,
  title TEXT,
  intro TEXT,
  tags TEXT,
  up_name TEXT,
  up_mid INTEGER,
  cover_url TEXT,
  tname TEXT,
  source_fid INTEGER,
  cached_at TEXT,
  PRIMARY KEY (account_id, resource_id, resource_type)
);

CREATE TABLE session_resource_sources (
  session_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER,
  source_fid INTEGER,
  moved INTEGER DEFAULT 0,
  move_error TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (session_id, resource_id, resource_type, source_fid)
);
```

如果暂时继续复用旧表，也必须把主键迁移为包含 `resource_type`，并修改所有 `list_*_by_avids`、`sources_by_avid`、`failed_items_by_avid` 之类的映射，改成 `(resource_id, resource_type)`。

---

## P2 问题

### P2-1：迁移函数遇到“半迁移状态”会直接跳过，后续写入报错

**位置：**
- `core/storage.py:287`
- `core/storage.py:315`

**问题：**

`_migrate_plan_items_to_resource_key()` 只要发现 `resource_id` 列存在就直接返回，没有检查 `resource_type` 是否存在，也没有检查主键是否已经是 `(version_id, resource_id, resource_type)`。

`_migrate_failed_items_to_resource_columns()` 同样只要发现 `resource_id` 就返回，没有补 `resource_type`。

**本地复现结果：**

构造一个有 `resource_id`、但没有 `resource_type` 的旧表：

```text
failed_items: OperationalError table failed_items has no column named resource_type
classification_plan_items: OperationalError table classification_plan_items has no column named resource_type
```

**影响：**

如果用户数据库经历过某个中间版本或失败迁移，程序启动不会修复结构，之后写入失败。

**建议修复：**

`classification_plan_items` 迁移要检查主键列：

```python
pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)") if row["pk"]]
cols = {row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)")}
if pk_cols == ["version_id", "resource_id", "resource_type"] and "resource_type" in cols:
    return
```

`failed_items` 应逐列判断：

```python
if not self._has_column(conn, "failed_items", "resource_id"):
    conn.execute("ALTER TABLE failed_items ADD COLUMN resource_id INTEGER")
if not self._has_column(conn, "failed_items", "resource_type"):
    conn.execute("ALTER TABLE failed_items ADD COLUMN resource_type INTEGER DEFAULT 2")
conn.execute("UPDATE failed_items SET resource_id = avid WHERE resource_id IS NULL")
```

### P2-2：SSE 重连复用任务时仍只发送 `progress: None`

**位置：**
- `main.py:369`
- `main.py:370`
- `core/session.py:141`

**问题：**

用户点击“后台运行”后再次进入整理会话，`api_session_stream()` 如果复用已有任务，只发送：

```python
{"stage": cur["status"], "progress": None, "reused": True}
```

没有附带 `source_total/scanned/collected/skipped`，而且采集中途的统计也没有持续写入 `classify_sessions.stats`。因此前端无法从 `deriveProgressPercent()` 推导百分比，重连视角仍可能显示 0% 或无进度。

**影响：**

这会让“进度条一直是 0%”在后台运行/重新打开场景下继续出现。

**建议修复：**

采集阶段每页更新 session stats，或在重连事件中从 `session_sources.collected_count/skipped_count/media_count` 汇总：

```python
stats = storage.compute_session_progress(sid)
yield stage event with {
  "stage": cur["status"],
  "progress": stats["scanned_total"] / stats["source_total"],
  "source_total": stats["source_total"],
  "scanned": stats["scanned_total"],
  "collected": stats["collected_total"],
  "skipped": stats["skipped_total"],
  "reused": True,
}
```

并新增测试覆盖 reused stream 的进度恢复。

### P2-3：AI 输入缺少 `resource_type_name`，分类质量和计划不一致

**位置：**
- `core/ai_classifier.py:22`
- `core/ai_classifier.py:37`
- `core/ai_classifier.py:94`

**问题：**

实现只给 AI 传了 `resource_type` 数字，没有 `resource_type_name` 字符串。虽然 prompt 里解释了部分数字，但计划明确要求传入类型名称，尤其对未知类型或 B 站类型变更时更稳。

**影响：**

非视频分类质量会下降；例如合集、课程、专栏只靠标题可能还能猜，但模型不能直接看到“合集/课程/专栏”字段。

**建议修复：**

增加映射函数：

```python
RESOURCE_TYPE_NAMES = {2: "视频", 11: "合集", 12: "音频", 21: "课程", 31: "专栏"}
```

`VideoInfo` 或新 `ResourceInfo` 增加 `resource_type_name`，并在 `classify_batch()` JSON 中输出该字段。测试应断言发给 AI 的 JSON 包含 `resource_type_name`。

---

## P3 问题

### P3-1：前端仍大量显示“视频”，与非视频整理能力不一致

**位置：**
- `static/index.html:522`
- `static/index.html:599`
- `static/app.js:361`
- `static/app.js:584`
- `static/app.js:655`
- `static/app.js:730`
- `static/app.js:740`

**问题：**

页面仍显示：

- “开始整理你的视频内容”
- “拉取视频”
- “可整理视频”
- “移动视频”
- “个视频已移动”

**影响：**

用户选择合集、音频、专栏等非视频资源时，界面文案会误导用户，以为程序仍只处理视频。

**建议修复：**

统一改为“收藏条目”“条目”“移动条目”等。

### P3-2：调整分类 API 不再兼容旧 `avid` 请求体

**位置：**
- `main.py:161`
- `main.py:401`

**问题：**

`AdjustIn` 当前要求 `resource_id` 必填：

```python
class AdjustIn(BaseModel):
    resource_id: int
    new_category: str
    resource_type: int = 2
```

计划中要求保留旧前端/旧调用方 `{avid, new_category}` 兼容，但实现没有 `avid` fallback。

**影响：**

当前新版前端可用，但旧调用方或旧页面缓存请求会 422。风险不高，但与计划不一致。

**建议修复：**

```python
class AdjustIn(BaseModel):
    resource_id: int | None = None
    avid: int | None = None
    resource_type: int = 2
    new_category: str

    def normalized_resource_id(self) -> int:
        if self.resource_id is not None:
            return self.resource_id
        if self.avid is not None:
            return self.avid
        raise ValueError("resource_id or avid is required")
```

路由使用 `payload.normalized_resource_id()`。

---

## 已通过的部分

- 全量测试通过：`128 passed`。
- 设置页已经增加 `ai_batch_size`，后端 `ConfigIn` 有 `10-200` 范围校验。
- `AiClassifier.classify()` 支持 `on_progress`，并保留 `merge_categories()`。
- `inspect.isawaitable()` 已按计划使用。
- `api_get_config()` 函数名已正确使用。
- 启动脚本已增加 `/api/runtime` 探测，避免重复启动正常服务实例。
- 退出账号会删除 cookie 并清除 `accounts.is_active`。
- 跳过条目折叠已有缓存，不会每次折叠都请求接口。

## 建议修复顺序

1. 先修 P1-1：保证 refine 不丢 `resource_type`。这是最容易被用户直接触发的问题。
2. 再修 P1-2：补齐真正的资源键存储，或者至少让旧表主键包含 `resource_type`。
3. 修 P2-1：增强迁移函数，避免半迁移数据库启动后写入失败。
4. 修 P2-2：补 SSE 复用进度恢复。
5. 修 P2-3 和 P3 文案/兼容问题。

## 自检

- [x] 运行全量 pytest。
- [x] 核查配置、进度、迁移、非视频、微调、执行、前端文案路径。
- [x] 对 P1-1、P1-2、P2-1 做本地复现。
- [x] 未修改业务代码，仅形成审查报告。
