# 预览筛选、跳过项清理、账号切换与 AI 微调实施计划

> **给执行智能体的说明：** 本文使用复选框 `- [ ]` 作为任务跟踪格式。

**目标：** 在现有 BiBiTool 基础上，增强整理流程和预览方案页面，支持多选源收藏夹批量智能整理、按目标收藏夹/分类筛选、树形折叠、跳过项原因展示与清理、多账号扫码切换、空源收藏夹识别与手动删除，以及在预览阶段通过自然语言让 AI 生成新版本方案。

**总体架构：** 在当前单源收藏夹、单会话、单方案模型上，增加“多源收藏夹维度”“账号维度”和“方案版本维度”。AI 分类阶段按视频去重后统一分类；执行移动阶段保留每个视频的来源收藏夹映射，按 `源收藏夹 -> 目标收藏夹` 分组调用 B 站移动接口。B 站接口仍集中在 `core/bilibili_api.py`，数据持久化仍集中在 `core/storage.py`，业务编排放在 `core/session.py`，HTTP 路由放在 `main.py`，前端交互放在 `static/app.js` 和 `static/index.html`。

**技术栈：** FastAPI、SQLite、httpx、OpenAI 兼容聊天接口、原生 JavaScript、Tailwind CDN、pytest、respx。

**2026-07-07 审查修订说明：** 已根据 `docs/review-2026-07-07-implementation-plan-issues.md` 逐项核实并修订本计划。该评审中多数问题属实，尤其是多源删除/重试、采集取消检查、账号扫码时序、WBI 缓存隔离、迁移回滚、AI 微调完整性校验、空收藏夹删除状态一致性等问题。本文已把相关修复要求落到对应任务中；后续执行应以本文为准。

---

## 一、需求解释

1. 预览方案页顶部需要显示所有目标收藏夹/分类名称和对应数量，例如“动漫 50”“编程 31”“音乐 20”，并额外提供“全部”。点击某个名称后，只展示该目标收藏夹/分类下的视频；点击“全部”则恢复展示全部内容。
2. 内容区域继续保持当前“分类标题 -> 视频列表”的树形结构，但每个分类组需要支持展开/折叠。
3. 跳过项不能只显示一个总数，需要记录并展示具体条目、跳过原因、是否可移除。
4. 对确实访问不了且能定位到资源 ID 的跳过项，提供“一键从收藏中移除”能力。该操作具有破坏性，必须有明确确认和结果反馈。
5. 增加“切换账号”能力。多个 B 站账号均通过扫码登录，用户可以在已登录账号之间切换。
6. 预览阶段增加“AI 微调”能力。用户输入类似“把官方的作品单独放在一个收藏夹内”的指令后，AI 基于当前方案生成一个新版本预览方案；用户可以在不同版本之间切换，最终执行当前激活版本。
7. 首页选择收藏夹时不再“点一个就开始”，而是支持多选已有收藏夹。用户勾选一个或多个收藏夹后，点击“开始智能整理”，系统把所有选中收藏夹的内容合并为一次整理任务。
8. 多源整理执行结束后，部分源收藏夹可能被移动空。系统需要识别这些空收藏夹，展示给用户，并由用户手动勾选后删除，不能自动删除。

## 二、当前实现状态

- 当前 `classifications` 表只保存一个 `session_id` 对应的一份方案，没有版本概念。
- 当前 `videos` 表使用 `avid` 作为主键，只保存一个 `fid`，多账号上线前必须迁移为 `(account_id, avid)` 组合主键或采用账号独立数据库，否则会出现跨账号覆盖。
- 当前 `classify_sessions` 只有单个 `source_fid`，无法表达一次整理选择多个源收藏夹。
- 当前执行阶段调用 `move_videos(src_media_id=s["source_fid"], ...)`，默认所有视频都来自同一个源收藏夹；多选源收藏夹后必须按来源分组移动。
- 当前 `classify_sessions.stats` 已有 `source_total`、`scanned_total`、`collected_total`、`skipped_total`、`skipped_by_reason` 等汇总统计，但没有保存跳过项明细。
- 当前跳过原因只有：
  - `attr_invalid`：B 站返回的 `medias[*].attr & 1` 命中。
  - `no_id`：资源行没有可用 `id`。
- 当前 B 站登录态是单文件 `bilibili_cookie.json`，无法同时管理多个账号。
- 当前已有移动收藏接口 `/x/v3/fav/resource/move` 的封装。
- 已核实删除/清理相关接口在无登录态下可达：
  - `POST https://api.bilibili.com/x/v3/fav/resource/batch-del` 返回 B 站业务码 `-101`，含义为账号未登录。
  - `POST https://api.bilibili.com/x/v3/fav/resource/clean` 返回 B 站业务码 `-101`，含义为账号未登录。
- 已核实删除收藏夹接口在无登录态下可达：
  - `POST https://api.bilibili.com/x/v3/fav/folder/del` 返回 B 站业务码 `-101`，含义为账号未登录。

## 三、数据模型设计

采用增量迁移，避免破坏已有本地数据。

### 1. 新增会话源收藏夹表

```sql
CREATE TABLE IF NOT EXISTS session_sources (
  session_id TEXT,
  account_id TEXT,
  source_fid INTEGER,
  title TEXT,
  media_count INTEGER,
  selected_order INTEGER,
  collected_count INTEGER DEFAULT 0,
  skipped_count INTEGER DEFAULT 0,
  emptied_after_execute INTEGER DEFAULT 0,
  delete_candidate INTEGER DEFAULT 0,
  delete_protected INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0,
  delete_error TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (session_id, source_fid)
);
```

用途：

- 保存一次整理任务选中的所有源收藏夹。
- 记录源收藏夹所属账号，避免多账号整理历史无法追踪。
- 展示本次整理来自哪些收藏夹，以及每个源收藏夹原始数量、采集数量、跳过数量。
- 执行结束后记录哪些源收藏夹可能已经为空。
- 用户手动删除空收藏夹后记录删除结果。
- `delete_protected=1` 的收藏夹永远不进入删除候选，用于保护默认收藏夹或无法确认可安全删除的收藏夹。

兼容规则：

- 旧会话只有 `classify_sessions.source_fid`，打开旧会话时可以自动补一条 `session_sources`。
- 新会话以 `session_sources` 为准，`classify_sessions.source_fid` 仅保留为兼容字段，可填入第一个选中的收藏夹。

### 2. 新增会话视频来源表

```sql
CREATE TABLE IF NOT EXISTS session_video_sources (
  session_id TEXT,
  avid INTEGER,
  source_fid INTEGER,
  resource_type INTEGER DEFAULT 2,
  moved INTEGER DEFAULT 0,
  move_error TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (session_id, avid, source_fid)
);
```

用途：

- 记录每个视频来自哪些源收藏夹。
- 同一个 `avid` 如果同时存在于多个选中的源收藏夹，只给 AI 分类一次，但执行移动时要从每个源收藏夹各移动一次。
- 执行阶段按 `source_fid` 分组调用 `/x/v3/fav/resource/move`。

### 3. 新增账号表

```sql
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  mid INTEGER UNIQUE,
  uname TEXT,
  avatar_url TEXT,
  cookie_path TEXT,
  is_active INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
```

用途：

- 保存多个 B 站账号。
- 每个账号使用独立 Cookie 文件。
- `is_active=1` 表示当前正在使用的账号。

### 3.1. WBI Key 缓存必须按账号隔离

现有 `wbi_keys` 如果是单行缓存，需要迁移为按 `account_id` 或 `mid` 作为主键保存：

```sql
CREATE TABLE IF NOT EXISTS wbi_keys (
  account_id TEXT PRIMARY KEY,
  img_key TEXT,
  sub_key TEXT,
  updated_at TEXT
);
```

`load_wbi_keys()`、`save_wbi_keys()`、`clear_wbi_keys()` 必须带 `account_id` 参数。切换账号时不再清空所有账号缓存，而是让 `BilibiliClient` 只读取当前账号的 WBI key；如果某账号缓存过期，则仅刷新该账号。

### 4. 新增跳过项明细表

```sql
CREATE TABLE IF NOT EXISTS skipped_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  account_id TEXT,
  source_fid INTEGER,
  avid INTEGER,
  bvid TEXT,
  title TEXT,
  resource_type INTEGER,
  raw_attr INTEGER,
  reason_code TEXT,
  reason_label TEXT,
  detail TEXT,
  removable INTEGER DEFAULT 0,
  removed INTEGER DEFAULT 0,
  remove_error TEXT,
  created_at TEXT,
  updated_at TEXT
);
```

用途：

- 记录每一个被跳过的收藏资源。
- 展示跳过原因。
- 判断是否允许从收藏夹中移除。
- 保存移除结果。

### 5. 新增方案版本表

```sql
CREATE TABLE IF NOT EXISTS classification_plan_versions (
  version_id TEXT PRIMARY KEY,
  session_id TEXT,
  version_no INTEGER,
  parent_version_id TEXT,
  instruction TEXT,
  status TEXT,
  is_active INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
```

用途：

- 保存同一个整理会话下的多个方案版本。
- 初始 AI 分类结果为版本 1。
- 每次 AI 微调生成一个新版本。
- `is_active=1` 表示当前预览和执行使用的版本。

### 6. 新增方案版本明细表

```sql
CREATE TABLE IF NOT EXISTS classification_plan_items (
  version_id TEXT,
  avid INTEGER,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,
  executed INTEGER DEFAULT 0,
  PRIMARY KEY (version_id, avid)
);
```

用途：

- 替代旧 `classifications` 表，按版本保存每个视频的目标分类。
- 允许用户在不同方案之间切换。
- 执行阶段只读取当前激活版本。

### 7. 对已有表增加字段

```sql
ALTER TABLE classify_sessions ADD COLUMN account_id TEXT;
ALTER TABLE fav_folders ADD COLUMN account_id TEXT;
```

迁移规则：

- 暂时保留旧 `classifications` 表。
- 打开旧会话时，如果还没有方案版本，则把旧 `classifications` 数据迁移成版本 1，并激活版本 1。
- 后续新增和执行均使用 `classification_plan_versions` 与 `classification_plan_items`。
- 新旧分类表必须双写到端到端验收通过为止；本计划不删除旧 `classifications` 表，删除旧表应单独立项。
- 首次执行涉及表重建的迁移前，应备份当前 `bibi.db` 为同目录时间戳备份文件，例如 `bibi.db.backup-20260707-HHMMSS`。
- 所有迁移必须幂等：重复启动应用不得重复重建表、重复插入版本、或覆盖已有账号/来源统计。
- 多源整理场景下，`videos` 仍可作为视频元数据缓存，但来源关系必须以 `session_video_sources` 为准，不能再只依赖 `videos.fid`。
- 多账号场景不能只给 `videos` 追加 `account_id` 字段，因为旧表主键仍是 `avid`，会导致不同账号的同一 `avid` 互相覆盖。实施时必须二选一：
  - 推荐方案：执行 SQLite 表重建迁移，把 `videos` 主键改为 `(account_id, avid)`。
  - 备选方案：每个账号使用独立数据库文件，但这会扩大改造面，第一版不推荐。
- 若采用推荐方案，`videos` 新结构应为：

```sql
CREATE TABLE IF NOT EXISTS videos_new (
  account_id TEXT,
  avid INTEGER,
  bvid TEXT,
  title TEXT,
  intro TEXT,
  tags TEXT,
  up_name TEXT,
  up_mid INTEGER,
  cover_url TEXT,
  tname TEXT,
  fid INTEGER,
  cached_at TEXT,
  PRIMARY KEY (account_id, avid)
);
```

## 四、跳过原因分类

建议使用以下原因码：

| 原因码 | 含义 | 是否默认允许移除 | 检测方式 |
| --- | --- | --- | --- |
| `attr_invalid` | B 站标记为失效或不可访问 | 是，前提是有 `avid` | `attr & 1` |
| `no_id` | 缺少资源 ID，无法定位 | 否 | `not m.get("id")` |
| `non_video_type` | 不是普通视频稿件 | 第一版不移除 | `type != 2` |
| `missing_bvid` | 有 `avid` 但缺少 `bvid` | 是，前提是有 `avid` | `not m.get("bvid")` |
| `probe_not_found` | 详情探测显示已删除/不存在 | 是 | 视频详情或收藏资源详情接口返回不存在 |
| `probe_permission_denied` | 详情探测显示无权限/私密 | 是 | B 站接口返回权限相关错误 |
| `duplicate_avid` | 同一个 `avid` 重复出现 | 否 | 采集阶段发现重复 |
| `unknown_unusable` | 未分类的不可用资源 | 否 | 兜底原因 |

注意：

- `unknown_unusable` 和 `duplicate_avid` 不应默认删除。
- 第一版可以只实现 `attr_invalid`、`no_id`、`non_video_type`，后续再通过详情探测补充 `probe_*`。

## 五、B 站清理接口说明

### 1. 推荐优先实现：批量移除收藏资源

```text
POST https://api.bilibili.com/x/v3/fav/resource/batch-del
Content-Type: application/x-www-form-urlencoded

media_id=<源收藏夹 id>
resources=<avid>:2,<avid>:2
platform=web
csrf=<bili_jct cookie>
```

用途：

- 从指定收藏夹中移除选定资源。
- 只处理用户确认过的资源。
- 可控性最好，适合本需求。

### 2. 暂不作为默认能力：清空失效内容

```text
POST https://api.bilibili.com/x/v3/fav/resource/clean

media_id=<源收藏夹 id>
csrf=<bili_jct cookie>
```

说明：

- 该接口可能清理 B 站认为失效的全部内容。
- 第一版不建议作为默认按钮暴露，避免用户误删范围超出预期。
- 如后续要加，应作为“高级清理”并增加二次确认。

### 3. 手动删除空收藏夹

```text
POST https://api.bilibili.com/x/v3/fav/folder/del
Content-Type: application/x-www-form-urlencoded

media_ids=<收藏夹 id>,<收藏夹 id>
csrf=<bili_jct cookie>
```

用途：

- 删除用户自己创建的收藏夹。
- 本需求只用于“整理后被移动空的源收藏夹”。

安全规则：

- 只能展示系统确认或疑似为空的源收藏夹。
- 默认不勾选任何收藏夹。
- 用户必须手动勾选要删除的空收藏夹。
- 删除前必须重新刷新收藏夹列表或查询收藏夹详情，确认 `media_count == 0`。
- `delete_protected=1` 的收藏夹不允许删除，即使它显示为空；默认收藏夹、无法确认是否可删除的收藏夹都必须写入 `delete_protected=1`。
- 删除按钮必须显示破坏性确认文案：`将删除 N 个空收藏夹，此操作不可逆。是否继续？`

## 六、涉及文件

- 修改 `core/storage.py`
  - 新增表结构迁移。
  - 新增会话源收藏夹、视频来源、账号、跳过项、方案版本、空收藏夹删除记录的 CRUD。
- 修改 `core/bilibili_api.py`
  - 新增 `batch_delete_resources(media_id, resources)`。
  - 新增 `delete_folders(media_ids)`。
  - 新增收藏夹详情/刷新方法，用于执行后确认源收藏夹是否为空。
  - 新增账号信息获取方法。
  - 收藏夹分页返回跳过项明细。
- 修改 `core/ai_classifier.py`
  - 新增 `refine_plan(videos, current_items, instruction)`。
- 修改 `core/session.py`
  - 支持创建多源收藏夹整理会话。
  - 采集阶段循环拉取多个源收藏夹。
  - AI 分类阶段按 `avid` 去重。
  - 执行阶段按源收藏夹分组移动。
  - 执行后识别可能为空的源收藏夹。
  - 保存跳过项明细。
  - 初始分类后创建方案版本 1。
  - `get_plan()` 返回当前激活版本。
  - 支持 AI 微调生成新版本。
  - 执行阶段只执行当前激活版本。
  - 支持清理跳过项。
  - 支持手动删除空源收藏夹。
- 修改 `main.py`
  - 修改创建会话入参，从 `source_fid` 扩展为 `source_fids`。
  - 新增账号接口。
  - 新增跳过项查询和移除接口。
  - 新增方案版本、AI 微调接口。
  - 新增空收藏夹候选查询和删除接口。
- 修改 `static/index.html`
  - 首页收藏夹列表改成多选。
  - 增加“开始智能整理”按钮。
  - 增加预览筛选栏、版本栏、微调输入区、跳过项面板、账号切换入口、空收藏夹清理面板。
- 修改 `static/app.js`
  - 渲染多选收藏夹、开始按钮、筛选 chip、折叠组、跳过项清理、账号切换、版本切换、AI 微调、空收藏夹删除。
- 新增/修改测试：
  - `tests/test_storage.py`
  - `tests/test_bilibili_api.py`
  - `tests/test_ai_classifier.py`
  - `tests/test_session.py`
  - `tests/test_frontend_static.py`

---

## 任务 -1：先建立可靠的 SQLite 迁移机制

**文件：**

- 修改：`core/storage.py`
- 测试：`tests/test_storage.py`

- [ ] **步骤 1：新增迁移测试**

```python
def test_storage_migrates_existing_database_columns(tmp_path):
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(classify_sessions)")}
        assert "account_id" in cols
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(fav_folders)")}
        assert "account_id" in cols
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_sources)")}
        assert "account_id" in cols
        assert "delete_protected" in cols
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_storage.py::test_storage_migrates_existing_database_columns -q
```

预期：失败，因为当前 `_init_db()` 只执行 `CREATE TABLE IF NOT EXISTS`，不会给已有表补列。

- [ ] **步骤 3：实现 `_migrate_schema()`**

在 `Storage.__init__()` 中，`self._init_db()` 之后调用：

```python
self._migrate_schema()
```

新增：

```python
def _has_column(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

def _migrate_schema(self) -> None:
    with self._conn() as conn:
        if not self._has_column(conn, "classify_sessions", "account_id"):
            conn.execute("ALTER TABLE classify_sessions ADD COLUMN account_id TEXT")
        if not self._has_column(conn, "fav_folders", "account_id"):
            conn.execute("ALTER TABLE fav_folders ADD COLUMN account_id TEXT")
        if not self._has_column(conn, "session_sources", "account_id"):
            conn.execute("ALTER TABLE session_sources ADD COLUMN account_id TEXT")
        if not self._has_column(conn, "session_sources", "delete_protected"):
            conn.execute("ALTER TABLE session_sources ADD COLUMN delete_protected INTEGER DEFAULT 0")
        self._migrate_wbi_keys_to_account_key(conn)
```

迁移实现必须在第一次执行破坏性表重建前创建数据库备份；如果当前数据库文件存在，备份到同目录并带时间戳。备份失败时应中止迁移并提示用户，不要继续重建表。

- [ ] **步骤 4：处理 `videos` 主键迁移**

如果现有 `videos` 表主键仍是单列 `avid`，需要重建表：

```python
def _migrate_videos_to_account_key(self, conn: sqlite3.Connection) -> None:
    indexes = conn.execute("PRAGMA table_info(videos)").fetchall()
    pk_cols = [row["name"] for row in indexes if row["pk"]]
    if pk_cols == ["account_id", "avid"]:
        return
    conn.execute("ALTER TABLE videos RENAME TO videos_legacy")
    conn.execute("""
        CREATE TABLE videos (
          account_id TEXT,
          avid INTEGER,
          bvid TEXT,
          title TEXT,
          intro TEXT,
          tags TEXT,
          up_name TEXT,
          up_mid INTEGER,
          cover_url TEXT,
          tname TEXT,
          fid INTEGER,
          cached_at TEXT,
          PRIMARY KEY (account_id, avid)
        )
    """)
    conn.execute("""
        INSERT OR REPLACE INTO videos (account_id, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at)
        SELECT '', avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at
        FROM videos_legacy
    """)
    conn.execute("DROP TABLE videos_legacy")
```

在 `_migrate_schema()` 末尾调用该方法。

同时处理 `wbi_keys` 的账号隔离迁移：如果旧表不存在 `account_id` 主键，则把旧缓存迁移到 `account_id=''` 的兼容行，再重建为 `account_id PRIMARY KEY`。账号功能启用后，所有读写方法必须传入当前账号 ID。

- [ ] **步骤 5：运行存储测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_storage.py -q
```

预期：通过。

---

## 任务 0：支持多选源收藏夹的数据模型和会话创建

**文件：**

- 修改：`core/storage.py`
- 修改：`core/session.py`
- 修改：`main.py`
- 测试：`tests/test_storage.py`、`tests/test_session.py`

- [ ] **步骤 1：新增会话源收藏夹存储测试**

```python
def test_session_sources_create_and_list(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 405, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 7, "selected_order": 1},
    ])

    sources = storage.list_session_sources(sid)

    assert [s["source_fid"] for s in sources] == [100, 200]
    assert sources[0]["title"] == "默认收藏夹"
    assert sources[1]["media_count"] == 7
```

- [ ] **步骤 2：新增视频来源存储测试**

```python
def test_session_video_sources_preserve_multiple_origins(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")

    storage.add_session_video_source(sid, avid=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, avid=1, source_fid=200, resource_type=2)

    rows = storage.list_session_video_sources(sid)

    assert {(r["avid"], r["source_fid"]) for r in rows} == {(1, 100), (1, 200)}
```

- [ ] **步骤 3：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_storage.py::test_session_sources_create_and_list tests\test_storage.py::test_session_video_sources_preserve_multiple_origins -q
```

预期：失败，因为相关方法和表尚不存在。

- [ ] **步骤 4：实现存储方法**

在 `Storage` 中新增：

```python
def save_session_sources(self, session_id: str, sources: list[dict]) -> None:
    with self._conn() as conn:
        for src in sources:
            conn.execute(
                "INSERT INTO session_sources (session_id, account_id, source_fid, title, media_count, selected_order, collected_count, skipped_count, emptied_after_execute, delete_candidate, delete_protected, deleted, delete_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 0, '', datetime('now'), datetime('now')) "
                "ON CONFLICT(session_id, source_fid) DO UPDATE SET "
                "account_id = excluded.account_id, "
                "title = excluded.title, "
                "media_count = excluded.media_count, "
                "selected_order = excluded.selected_order, "
                "delete_protected = excluded.delete_protected, "
                "updated_at = datetime('now')",
                (
                    session_id,
                    src.get("account_id"),
                    src["source_fid"],
                    src["title"],
                    src.get("media_count", 0),
                    src.get("selected_order", 0),
                    1 if src.get("delete_protected") else 0,
                ),
            )

def list_session_sources(self, session_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_sources WHERE session_id = ? ORDER BY selected_order, source_fid",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def update_session_account(self, session_id: str, account_id: str) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE classify_sessions SET account_id = ? WHERE session_id = ?",
            (account_id, session_id),
        )

def update_session_source_counts(self, session_id: str, source_fid: int, collected_count: int, skipped_count: int) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE session_sources SET collected_count = ?, skipped_count = ?, updated_at = datetime('now') WHERE session_id = ? AND source_fid = ?",
            (collected_count, skipped_count, session_id, source_fid),
        )

def add_session_video_source(self, session_id: str, avid: int, source_fid: int, resource_type: int = 2) -> None:
    with self._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_video_sources (session_id, avid, source_fid, resource_type, moved, move_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, '', datetime('now'), datetime('now'))",
            (session_id, avid, source_fid, resource_type),
        )

def list_session_video_sources(self, session_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_video_sources WHERE session_id = ? ORDER BY source_fid, avid",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def mark_session_video_source_moved(self, session_id: str, avid: int, source_fid: int, ok: bool, error: str = "") -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE session_video_sources SET moved = ?, move_error = ?, updated_at = datetime('now') "
            "WHERE session_id = ? AND avid = ? AND source_fid = ?",
            (1 if ok else 0, error, session_id, avid, source_fid),
        )
```

- [ ] **步骤 5：修改创建会话入参**

在 `main.py` 中把：

```python
class SessionIn(BaseModel):
    source_fid: int
    mode: str = "quick"
```

改为：

```python
class SessionIn(BaseModel):
    source_fids: list[int] | None = None
    source_fid: int | None = None
    mode: str = "quick"

    def normalized_source_fids(self) -> list[int]:
        if self.source_fids:
            return self.source_fids
        if self.source_fid is not None:
            return [self.source_fid]
        return []
```

兼容要求：后端必须继续接受旧字段 `source_fid`，直到任务 4.5 的前端多选改造完成并验证通过。中间部署状态下，旧前端传单个 `source_fid` 仍应能创建会话。

路由改为：

```python
@app.post("/api/session")
async def api_create_session(payload: SessionIn):
    source_fids = payload.normalized_source_fids()
    if not source_fids:
        raise BibiError("请至少选择一个收藏夹", code="NO_SOURCE_FOLDER")
    mgr = get_session_mgr()
    sid = await mgr.create_many(source_fids, payload.mode)
    return {"session_id": sid}
```

- [ ] **步骤 6：新增 `create_many`**

在 `ClassifySession` 中新增：

注意：如果使用 `BibiError`，需要在 `core/session.py` 顶部从 `core.errors` 引入；也可以复用现有 `StateError`，但错误码会不如 `SOURCE_FOLDER_NOT_FOUND` 清晰。

```python
async def create_many(self, source_fids: list[int], mode: str) -> str:
    if not self.bili.is_logged_in:
        raise NotLoggedInError()
    active_account = self.storage.get_active_account()
    account_id = active_account["account_id"] if active_account else ""
    unique_fids = []
    for fid in source_fids:
        if fid not in unique_fids:
            unique_fids.append(fid)
    sid = self.storage.create_session(source_fid=unique_fids[0], mode=mode)
    self.storage.update_session_account(sid, account_id)
    folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
    missing = [fid for fid in unique_fids if fid not in folders]
    if missing:
        raise BibiError(f"收藏夹不存在或不属于当前账号: {missing}", code="SOURCE_FOLDER_NOT_FOUND")
    self.storage.save_session_sources(sid, [
        {
            "account_id": account_id,
            "source_fid": fid,
            "title": folders[fid]["title"],
            "media_count": folders[fid].get("media_count", 0),
            "selected_order": idx,
            "delete_protected": bool(folders[fid].get("is_default") or folders[fid].get("fav_state") == 1),
        }
        for idx, fid in enumerate(unique_fids)
    ])
    return sid
```

保留旧 `create(source_fid, mode)`，内部可调用：

```python
return await self.create_many([source_fid], mode)
```

- [ ] **步骤 7：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_storage.py tests/test_session.py -q
```

预期：通过。

---

## 任务 1：增加方案版本存储

**文件：**

- 修改：`core/storage.py`
- 测试：`tests/test_storage.py`

- [ ] **步骤 1：先写失败测试**

新增测试：

```python
def test_plan_versions_create_activate_and_load(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")

    v1 = storage.create_plan_version(
        session_id=sid,
        parent_version_id=None,
        instruction="初始分类",
        items=[
            {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": "标题匹配"},
            {"avid": 2, "category": "编程", "confidence": 0.8, "reason": "UP主和分区匹配"},
        ],
        activate=True,
    )
    v2 = storage.create_plan_version(
        session_id=sid,
        parent_version_id=v1,
        instruction="把官方作品单独放一个收藏夹",
        items=[
            {"avid": 1, "category": "官方作品", "confidence": 0.92, "reason": "官方账号"},
            {"avid": 2, "category": "编程", "confidence": 0.8, "reason": "保持不变"},
        ],
        activate=True,
    )

    versions = storage.list_plan_versions(sid)
    assert [v["version_no"] for v in versions] == [1, 2]
    assert storage.get_active_plan_version(sid)["version_id"] == v2
    items = storage.load_plan_items(v2)
    assert items[0]["category"] == "官方作品"
    assert items[1]["category"] == "编程"
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_storage.py::test_plan_versions_create_activate_and_load -q
```

预期：失败，提示相关方法不存在。

- [ ] **步骤 3：实现存储方法**

在 `Storage` 中新增：

```python
def create_plan_version(self, session_id: str, parent_version_id: str | None, instruction: str, items: list[dict], activate: bool) -> str:
    version_id = str(uuid.uuid4())
    with self._conn() as conn:
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM classification_plan_versions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if activate:
            conn.execute("UPDATE classification_plan_versions SET is_active = 0 WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO classification_plan_versions (version_id, session_id, version_no, parent_version_id, instruction, status, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'ready', ?, datetime('now'), datetime('now'))",
            (version_id, session_id, version_no, parent_version_id, instruction, 1 if activate else 0),
        )
        for it in items:
            conn.execute(
                "INSERT INTO classification_plan_items (version_id, avid, category, confidence, reason, adjusted, executed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, it["avid"], it["category"], it.get("confidence", 0.0), it.get("reason", ""), it.get("adjusted", 0), it.get("executed", 0)),
            )
    return version_id
```

同时增加：

```python
def list_plan_versions(self, session_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM classification_plan_versions WHERE session_id = ? ORDER BY version_no",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def get_active_plan_version(self, session_id: str) -> dict | None:
    with self._conn() as conn:
        row = conn.execute(
            "SELECT * FROM classification_plan_versions WHERE session_id = ? AND is_active = 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

def activate_plan_version(self, session_id: str, version_id: str) -> None:
    with self._conn() as conn:
        conn.execute("UPDATE classification_plan_versions SET is_active = 0 WHERE session_id = ?", (session_id,))
        conn.execute(
            "UPDATE classification_plan_versions SET is_active = 1, updated_at = datetime('now') WHERE session_id = ? AND version_id = ?",
            (session_id, version_id),
        )

def load_plan_items(self, version_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM classification_plan_items WHERE version_id = ? ORDER BY avid",
            (version_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def mark_plan_item_executed(self, version_id: str, avid: int, ok: bool) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE classification_plan_items SET executed = ? WHERE version_id = ? AND avid = ?",
            (1 if ok else 0, version_id, avid),
        )

def migrate_legacy_classifications_to_version(self, session_id: str) -> str | None:
    active = self.get_active_plan_version(session_id)
    if active:
        return active["version_id"]
    legacy_items = self.load_classifications(session_id)
    if not legacy_items:
        return None
    return self.create_plan_version(
        session_id=session_id,
        parent_version_id=None,
        instruction="初始分类",
        items=[
            {
                "avid": it["avid"],
                "category": it["category"],
                "confidence": it["confidence"],
                "reason": it["reason"],
                "adjusted": it.get("adjusted", 0),
                "executed": it.get("executed", 0),
            }
            for it in legacy_items
        ],
        activate=True,
    )
```

- [ ] **步骤 4：运行存储测试**

```powershell
.\.venv\Scripts\pytest.exe tests\test_storage.py -q
```

预期：通过。

---

## 任务 2：保存跳过项明细

**文件：**

- 修改：`core/bilibili_api.py`
- 修改：`core/session.py`
- 修改：`core/storage.py`
- 测试：`tests/test_bilibili_api.py`、`tests/test_session.py`、`tests/test_storage.py`

- [ ] **步骤 1：新增 B 站解析测试**

```python
@respx.mock
async def test_get_folder_video_pages_returns_skipped_items(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 2}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "有效", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
            {"id": 2, "bvid": "", "title": "已失效", "upper": {"name": "U"}, "cover": "", "attr": 1, "type": 2, "tname": "科技"},
            {"id": 3, "bvid": "BV3", "title": "合集", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 11, "tname": "合集"},
        ], "has_more": False},
    }))

    pages = []
    async for page in client.get_folder_video_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(page)

    assert pages[0]["skipped_items"][0]["avid"] == 2
    assert pages[0]["skipped_items"][0]["reason_code"] == "attr_invalid"
    assert pages[0]["skipped_items"][0]["removable"] is True
    assert pages[0]["skipped_items"][1]["avid"] == 3
    assert pages[0]["skipped_items"][1]["reason_code"] == "non_video_type"
    assert pages[0]["skipped_items"][1]["removable"] is False
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_bilibili_api.py::test_get_folder_video_pages_returns_skipped_items -q
```

预期：失败，因为当前 page 没有 `skipped_items`。

- [ ] **步骤 3：在分页结果中返回跳过项**

在 `get_folder_video_pages()` 中增加：

```python
skipped_items = []

def skip_item(m: dict, reason_code: str, reason_label: str, removable: bool) -> None:
    skipped_reasons[reason_code] = skipped_reasons.get(reason_code, 0) + 1
    skipped_items.append({
        "source_fid": fid,
        "avid": m.get("id", 0),
        "bvid": m.get("bvid", ""),
        "title": m.get("title", ""),
        "resource_type": m.get("type", 0),
        "raw_attr": m.get("attr", 0),
        "reason_code": reason_code,
        "reason_label": reason_label,
        "detail": "",
        "removable": removable and bool(m.get("id")),
    })
```

规则说明：

- 第一版只对有 `avid/id` 的条目执行 `batch-del`。
- 如果 `attr_invalid` 同时缺少 `id`，应记录为不可移除，并在 `detail` 中说明“缺少资源 ID，无法安全定位删除对象”。
- 后续若要支持“无 id 但有 bvid”的删除，需要先通过视频详情反查 `avid`，不能直接调用删除接口。

替换原先直接计数的跳过逻辑：

```python
if m.get("attr", 0) & 1:
    skip_item(m, "attr_invalid", "B站标记为失效或不可访问", True)
    continue
if not m.get("id"):
    skip_item(m, "no_id", "缺少资源 ID，无法定位", False)
    continue
if m.get("type", 2) != 2:
    skip_item(m, "non_video_type", "不是普通视频稿件", False)
    continue
```

yield 的 page 中新增：

```python
"skipped_items": skipped_items,
```

- [ ] **步骤 4：持久化跳过项**

在 `Storage` 中新增：

```python
def add_skipped_items(self, session_id: str, account_id: str | None, items: list[dict]) -> None:
    with self._conn() as conn:
        for it in items:
            conn.execute(
                "INSERT INTO skipped_items (session_id, account_id, source_fid, avid, bvid, title, resource_type, raw_attr, reason_code, reason_label, detail, removable, removed, remove_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', datetime('now'), datetime('now'))",
                (session_id, account_id, it["source_fid"], it.get("avid", 0), it.get("bvid", ""), it.get("title", ""),
                 it.get("resource_type", 0), it.get("raw_attr", 0), it["reason_code"], it["reason_label"],
                 it.get("detail", ""), 1 if it.get("removable") else 0),
            )
```

在 `ClassifySession.collect()` 处理每页时调用：

```python
self.storage.add_skipped_items(sid, s.get("account_id"), page.get("skipped_items", []))
```

- [ ] **步骤 5：增加查询接口**

在 `main.py` 中新增：

```python
@app.get("/api/session/{sid}/skipped-items")
async def api_skipped_items(sid: str):
    return {"items": storage.list_skipped_items(sid)}
```

- [ ] **步骤 6：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests\test_bilibili_api.py tests\test_session.py tests\test_storage.py -q
```

预期：通过。

---

## 任务 2.5：多源收藏夹采集与去重分类

**文件：**

- 修改：`core/session.py`
- 修改：`core/storage.py`
- 测试：`tests/test_session.py`

- [ ] **步骤 1：新增多源采集测试**

```python
@pytest.mark.asyncio
async def test_collect_from_multiple_sources_dedupes_for_ai(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 2, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 2, "selected_order": 1},
    ])

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        if fid == 100:
            yield {
                "page": 1,
                "videos": [
                    {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                    {"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                ],
                "raw_count": 2,
                "usable_count": 2,
                "skipped_count": 0,
                "skipped_reasons": {},
                "skipped_items": [],
                "expected_total": 2,
                "has_more": False,
            }
        if fid == 200:
            yield {
                "page": 1,
                "videos": [
                    {"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                    {"avid": 3, "bvid": "BV3", "title": "C", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "舞蹈", "fid": fid},
                ],
                "raw_count": 2,
                "usable_count": 2,
                "skipped_count": 0,
                "skipped_reasons": {},
                "skipped_items": [],
                "expected_total": 2,
                "has_more": False,
            }

    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[
        Classification(1, "动漫", 0.9, ""),
        Classification(2, "动漫", 0.9, ""),
        Classification(3, "舞蹈", 0.9, ""),
    ])

    await ClassifySession(storage, bili, ai).run_pipeline(sid)

    ai.classify.assert_awaited_once()
    classified_videos = ai.classify.await_args.args[0]
    assert [v.avid for v in classified_videos] == [1, 2, 3]
    sources = storage.list_session_video_sources(sid)
    assert {(s["avid"], s["source_fid"]) for s in sources} == {(1, 100), (2, 100), (2, 200), (3, 200)}
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_session.py::test_collect_from_multiple_sources_dedupes_for_ai -q
```

预期：失败，因为当前 `collect()` 只拉取单个 `s["source_fid"]`。

- [ ] **步骤 3：修改 `collect()` 循环多个源收藏夹**

当前逻辑：

```python
async for page in self.bili.get_folder_video_pages(s["source_fid"], storage=self.storage):
    ...
```

改为：

```python
sources = self.storage.list_session_sources(sid)
if not sources and s.get("source_fid"):
    sources = [{"source_fid": s["source_fid"], "title": f"收藏夹 {s['source_fid']}", "media_count": 0}]

for src in sources:
    source_fid = src["source_fid"]
    source_collected = 0
    source_skipped = 0
    if self._is_cancelled(sid):
        return
    async for page in self.bili.get_folder_video_pages(source_fid, storage=self.storage):
        if self._is_cancelled(sid):
            return
        videos = page["videos"]
        for v in videos:
            if self._is_cancelled(sid):
                return
            v["fid"] = source_fid
            if s["mode"] == "full" and v.get("bvid"):
                v = await self._enrich_video(v)
                if self._is_cancelled(sid):
                    return
                v["fid"] = source_fid
            v["account_id"] = s.get("account_id") or ""
            self.storage.upsert_video(v)
            self.storage.add_session_video_source(sid, avid=v["avid"], source_fid=source_fid, resource_type=2)
        self.storage.add_skipped_items(sid, s.get("account_id"), page.get("skipped_items", []))
        source_collected += page["usable_count"]
        source_skipped += page["skipped_count"]
        self.storage.update_session_source_counts(sid, source_fid, source_collected, source_skipped)
        collected += page["usable_count"]
        scanned += page["raw_count"]
        skipped += page["skipped_count"]
        await _emit_progress(on_progress, {
            "stage": "collecting",
            "progress": None,
            "source_fid": source_fid,
            "collected": collected,
            "scanned": scanned,
            "skipped": skipped,
            "source_total": source_total,
        })
```

要求：

- 这段逻辑必须嵌入现有 `collect()` 的状态流转、stats 合并和完成事件中，不要删掉现有 `_is_cancelled()` 检查。
- 为兼容旧会话和现有测试，`if not sources and s.get("source_fid")` 的兜底逻辑必须保留。
- 每页处理前后、每个视频处理前、`full` 模式 `_enrich_video()` 前后都要检查取消状态。
- 每页结束后必须发送 collecting 进度事件，确保前端四格统计区持续更新。
- `source_total` 推荐取所有 `session_sources.media_count` 之和；若缺失则使用接口 page 的 `expected_total` 累加。

- [ ] **步骤 4：修改 `classify()` 使用本会话来源视频**

不要再只用单个 `fid`：

```python
videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
```

改为：

```python
video_sources = self.storage.list_session_video_sources(sid)
avids = sorted({row["avid"] for row in video_sources})
videos_rows = self.storage.list_videos_by_avids(avids, account_id=s.get("account_id"))
```

新增 `Storage.list_videos_by_avids()`：

```python
def list_videos_by_avids(self, avids: list[int], account_id: str | None = None) -> list[dict]:
    if not avids:
        return []
    placeholders = ",".join("?" * len(avids))
    with self._conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM videos WHERE account_id = ? AND avid IN ({placeholders}) ORDER BY avid",
            [account_id or "", *avids],
        ).fetchall()
        return [dict(r) for r in rows]
```

AI 分类完成后，必须同时写旧表和新版本表：

```python
results = await self.ai.classify(videos)
self.storage.save_classifications(sid, [
    {"avid": c.avid, "category": c.category, "confidence": c.confidence, "reason": c.reason}
    for c in results
])
self.storage.create_plan_version(
    session_id=sid,
    parent_version_id=None,
    instruction="初始分类",
    items=[
        {"avid": c.avid, "category": c.category, "confidence": c.confidence, "reason": c.reason}
        for c in results
    ],
    activate=True,
)
```

保留 `save_classifications()` 是为了旧表双写回滚；后续确认新版本表稳定后，再单独移除旧表依赖。

- [ ] **步骤 5：修改 `get_plan()` 返回来源信息**

`get_plan()` 返回中增加：

```python
return {
    "session": s,
    "sources": self.storage.list_session_sources(sid),
    "video_sources": self.storage.list_session_video_sources(sid),
    "items": items,
    "videos": videos,
}
```

这样前端后续可以展示“本方案来自哪些源收藏夹”。

- [ ] **步骤 6：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_session.py tests/test_storage.py -q
```

预期：通过。

---

## 任务 3：实现不可访问收藏项的安全移除

**文件：**

- 修改：`core/bilibili_api.py`
- 修改：`core/session.py`
- 修改：`core/storage.py`
- 修改：`main.py`
- 测试：`tests/test_bilibili_api.py`、`tests/test_session.py`

- [ ] **步骤 1：新增 B 站删除接口测试**

```python
@respx.mock
async def test_batch_delete_resources(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    route = respx.post(f"{API_BASE}/x/v3/fav/resource/batch-del").mock(
        return_value=httpx.Response(200, json={"code": 0, "message": "0", "ttl": 1, "data": 0})
    )

    ok = await client.batch_delete_resources(media_id=100, resources=[{"id": 1, "type": 2}, {"id": 2, "type": 2}])

    assert ok is True
    form = route.calls[0].request.content.decode()
    assert "media_id=100" in form
    assert "resources=1%3A2%2C2%3A2" in form
    assert "csrf=csrf_tok" in form
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_bilibili_api.py::test_batch_delete_resources -q
```

预期：失败，因为方法不存在。

- [ ] **步骤 3：封装 `batch-del`**

在 `BilibiliClient` 中新增：

```python
async def batch_delete_resources(self, media_id: int, resources: list[dict]) -> bool:
    resource_text = ",".join(f"{r['id']}:{r.get('type', 2)}" for r in resources)
    await self._post_form(
        "/x/v3/fav/resource/batch-del",
        {"media_id": media_id, "resources": resource_text, "platform": "web"},
    )
    return True
```

- [ ] **步骤 4：增加业务编排**

先在 `core/session.py` 中补充通用分块工具，后续跳过项移除、移动执行、重试逻辑都复用它：

```python
def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
```

在 `ClassifySession` 中新增：

```python
async def remove_skipped_items(self, sid: str, item_ids: list[int]) -> dict:
    s = self.storage.load_session(sid)
    if not s:
        raise StateError("会话不存在")
    items = self.storage.list_skipped_items_by_ids(sid, item_ids)
    removable = [it for it in items if it["removable"] and not it["removed"] and it["avid"] and it["source_fid"]]
    success = 0
    failed = 0
    by_source: dict[int, list[dict]] = {}
    for it in removable:
        by_source.setdefault(int(it["source_fid"]), []).append(it)
    for source_fid, source_items in by_source.items():
        for chunk in _chunks(source_items, 50):
            try:
                await self.bili.batch_delete_resources(
                    media_id=source_fid,
                    resources=[{"id": it["avid"], "type": it.get("resource_type") or 2} for it in chunk],
                )
                for it in chunk:
                    self.storage.mark_skipped_item_removed(it["id"], True, "")
                success += len(chunk)
            except Exception as e:
                for it in chunk:
                    self.storage.mark_skipped_item_removed(it["id"], False, str(e))
                failed += len(chunk)
    return {"success": success, "failed": failed, "total": len(removable)}
```

- [ ] **步骤 5：增加路由**

在 `main.py` 中新增：

```python
class RemoveSkippedIn(BaseModel):
    item_ids: list[int]

@app.post("/api/session/{sid}/skipped-items/remove")
async def api_remove_skipped_items(sid: str, payload: RemoveSkippedIn):
    mgr = get_session_mgr()
    return {"stats": await mgr.remove_skipped_items(sid, payload.item_ids)}
```

- [ ] **步骤 6：前端确认要求**

前端必须显示确认文案：

```text
将从 B 站收藏夹中移除 N 个不可访问条目。此操作不可逆。是否继续？
```

用户取消时不得调用后端删除接口。

- [ ] **步骤 7：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_bilibili_api.py tests/test_session.py -q
```

预期：通过。

---

## 任务 4：预览页增加筛选栏和折叠树

**文件：**

- 修改：`static/index.html`
- 修改：`static/app.js`
- 测试：`tests/test_frontend_static.py`

- [ ] **步骤 1：增加页面占位区域**

在 review section 中，`review-plan` 上方增加：

```html
<div data-dom-id="review-version-bar" class="mb-4 flex flex-wrap items-center gap-2"></div>
<div data-dom-id="review-refine-panel" class="mb-4"></div>
<div data-dom-id="review-filter-bar" class="mb-5 flex flex-wrap items-center gap-2"></div>
<div data-dom-id="review-skipped-panel" class="mb-6"></div>
```

- [ ] **步骤 2：新增前端静态测试**

```python
def test_frontend_review_has_filter_version_refine_and_skipped_regions():
    for dom_id in [
        "review-filter-bar",
        "review-version-bar",
        "review-refine-panel",
        "review-skipped-panel",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML
    assert "renderReviewFilters" in APP_JS
    assert "toggleReviewGroup" in APP_JS
    assert "activeReviewFilter" in APP_JS
```

- [ ] **步骤 3：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_frontend_static.py::test_frontend_review_has_filter_version_refine_and_skipped_regions -q
```

预期：失败。

- [ ] **步骤 4：新增前端状态**

在 `static/app.js` 顶部增加：

```javascript
let activeReviewFilter = 'ALL';
const collapsedReviewGroups = new Set();
```

- [ ] **步骤 5：渲染筛选 chip**

新增：

```javascript
function escapeDomId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, ch => ch.charCodeAt(0).toString(16));
}

function renderReviewFilters(cats, byCat) {
  const total = cats.reduce((sum, c) => sum + byCat[c].length, 0);
  const chips = [
    { key: 'ALL', label: '全部', count: total },
    ...cats.map(c => ({ key: c, label: c, count: byCat[c].length })),
  ];
  $('review-filter-bar').innerHTML = chips.map(chip => `
    <button data-dom-id="review-filter-${escapeDomId(chip.key)}" type="button"
      class="btn ${activeReviewFilter === chip.key ? 'btn-primary' : 'btn-secondary'}"
      style="height:32px;padding:0 12px;font-size:12px;">
      <span>${escapeHtml(chip.label)}</span>
      <span class="inline-flex items-center justify-center h-5 min-w-5 px-1.5 rounded-full text-xs"
        style="background:var(--background-100);color:var(--foreground);">${chip.count}</span>
    </button>`).join('');
  chips.forEach(chip => {
    $(`review-filter-${escapeDomId(chip.key)}`).onclick = () => {
      activeReviewFilter = chip.key;
      renderReview(currentSid, window.__lastReviewPlan);
    };
  });
}
```

- [ ] **步骤 6：实现折叠分组**

新增：

```javascript
function toggleReviewGroup(cat) {
  if (collapsedReviewGroups.has(cat)) collapsedReviewGroups.delete(cat);
  else collapsedReviewGroups.add(cat);
  renderReview(currentSid, window.__lastReviewPlan);
}
```

渲染每个分类时，分类头改为按钮：

```javascript
const collapsed = collapsedReviewGroups.has(cat);
return `<section class="mb-8">
  <button type="button" data-dom-id="toggle-cat-${escapeDomId(cat)}" class="w-full relative overflow-hidden rounded-xl mb-4 px-4 py-3 flex items-center gap-3 text-left" style="background: var(--background-100); border:0; cursor:pointer;">
    <i data-lucide="${collapsed ? 'chevron-right' : 'chevron-down'}" class="w-4 h-4"></i>
    <h2 class="text-base font-semibold tracking-tight" style="color: var(--foreground);">${escapeHtml(cat)}</h2>
    <span class="inline-flex items-center justify-center h-6 px-2.5 rounded-full text-xs font-semibold">${byCat[cat].length}</span>
  </button>
  <div class="flex flex-col gap-3" style="${collapsed ? 'display:none;' : ''}">${rows}</div>
</section>`;
```

HTML 插入后绑定：

```javascript
catsToRender.forEach(cat => {
  const btn = $(`toggle-cat-${escapeDomId(cat)}`);
  if (btn) btn.onclick = () => toggleReviewGroup(cat);
});
```

- [ ] **步骤 7：运行前端测试**

```powershell
.\.venv\Scripts\pytest.exe tests\test_frontend_static.py -q
```

预期：通过。

---

## 任务 4.5：首页收藏夹改为多选并增加开始按钮

**文件：**

- 修改：`static/index.html`
- 修改：`static/app.js`
- 修改：`main.py`
- 测试：`tests/test_frontend_static.py`

- [ ] **步骤 1：新增前端静态测试**

```python
def test_frontend_folder_selection_supports_multi_select_start_button():
    assert "selectedSourceFids" in APP_JS
    assert "start-organize" in INDEX_HTML
    assert "toggleFolderSelection" in APP_JS
    assert "source_fids" in APP_JS
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_frontend_static.py::test_frontend_folder_selection_supports_multi_select_start_button -q
```

预期：失败，因为当前点击单个收藏夹后会直接进入流程。

- [ ] **步骤 3：增加开始按钮**

在首页 `folder-list` 下方增加：

```html
<div class="sticky bottom-0 mt-6 py-4" style="background: color-mix(in srgb, var(--background-50) 88%, transparent); backdrop-filter: blur(10px);">
  <button data-dom-id="start-organize" type="button" class="btn btn-primary btn-lg w-full" disabled>
    <i data-lucide="sparkles" class="w-5 h-5"></i>
    <span>开始智能整理</span>
  </button>
</div>
```

- [ ] **步骤 4：增加多选状态**

在 `static/app.js` 顶部增加：

```javascript
const selectedSourceFids = new Set();
```

新增：

```javascript
function toggleFolderSelection(fid) {
  if (selectedSourceFids.has(fid)) selectedSourceFids.delete(fid);
  else selectedSourceFids.add(fid);
  updateFolderSelectionUi();
}

function updateFolderSelectionUi() {
  document.querySelectorAll('[data-folder-id]').forEach(el => {
    const fid = Number(el.getAttribute('data-folder-id'));
    const selected = selectedSourceFids.has(fid);
    el.style.borderColor = selected ? 'var(--brand-500)' : 'var(--border)';
    el.style.background = selected ? 'var(--brand-50)' : 'var(--card)';
    const check = el.querySelector('[data-role="folder-check"]');
    if (check) check.style.display = selected ? 'flex' : 'none';
  });
  const btn = $('start-organize');
  const count = selectedSourceFids.size;
  btn.disabled = count === 0;
  btn.querySelector('span').textContent = count ? `开始智能整理（${count} 个收藏夹）` : '开始智能整理';
}
```

- [ ] **步骤 5：修改收藏夹卡片渲染**

先读取并保留当前 `renderHome()` 中已有收藏夹卡片结构，在原结构上增加 `data-folder-id`、勾选图标和点击多选行为，不要把整个卡片 HTML 换成与现有样式脱节的新结构。

`renderHome()` 每次进入首页并重新加载收藏夹时，必须先清空旧选择，避免从其他页面返回后残留上一次的勾选状态：

```javascript
selectedSourceFids.clear();
```

卡片根元素增加 `data-folder-id`，并加入勾选图标：

```javascript
<div data-dom-id="select-folder-${f.fid}" data-folder-id="${f.fid}" class="...">
  <div data-role="folder-check" class="shrink-0 hidden items-center justify-center w-6 h-6 rounded-full" style="background:var(--brand-500);color:var(--primary-foreground);display:none;">
    <i data-lucide="check" class="w-4 h-4"></i>
  </div>
  ...
</div>
```

点击行为改为：

```javascript
data.folders.forEach(f => {
  $(`select-folder-${f.fid}`).onclick = () => toggleFolderSelection(f.fid);
});

$('start-organize').onclick = () => {
  if (!selectedSourceFids.size) return;
  newSession([...selectedSourceFids]);
};
```

- [ ] **步骤 6：修改 `newSession`**

```javascript
async function newSession(sourceFids) {
  const r = await api('/api/session', {
    method: 'POST',
    body: JSON.stringify({ source_fids: sourceFids, mode: currentMode }),
  });
  currentSid = r.session_id;
  showView('progress');
  runPipeline(r.session_id);
}
```

- [ ] **步骤 7：运行前端测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_frontend_static.py -q
```

预期：通过。

---

## 任务 5：增加账号切换

**文件：**

- 修改：`core/storage.py`
- 修改：`core/bilibili_api.py`
- 修改：`main.py`
- 修改：`static/index.html`
- 修改：`static/app.js`
- 测试：`tests/test_storage.py`、`tests/test_bilibili_api.py`、`tests/test_frontend_static.py`

- [ ] **步骤 1：新增账号存储测试**

```python
def test_accounts_create_switch_and_active(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_account({"account_id": "a1", "mid": 1, "uname": "账号1", "avatar_url": "", "cookie_path": "accounts/a1.json"})
    storage.upsert_account({"account_id": "a2", "mid": 2, "uname": "账号2", "avatar_url": "", "cookie_path": "accounts/a2.json"})

    storage.activate_account("a2")

    assert storage.get_active_account()["account_id"] == "a2"
    accounts = storage.list_accounts()
    assert [a["is_active"] for a in accounts if a["account_id"] == "a2"] == [1]
```

- [ ] **步骤 2：实现账号存储方法**

```python
def upsert_account(self, account: dict) -> None:
    with self._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts (account_id, mid, uname, avatar_url, cookie_path, is_active, created_at, updated_at) "
            "VALUES (:account_id, :mid, :uname, :avatar_url, :cookie_path, COALESCE(:is_active, 0), datetime('now'), datetime('now'))",
            {**account, "is_active": account.get("is_active", 0)},
        )

def activate_account(self, account_id: str) -> None:
    with self._conn() as conn:
        conn.execute("UPDATE accounts SET is_active = 0")
        conn.execute("UPDATE accounts SET is_active = 1, updated_at = datetime('now') WHERE account_id = ?", (account_id,))

def get_active_account(self) -> dict | None:
    with self._conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE is_active = 1").fetchone()
        return dict(row) if row else None

def list_accounts(self) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
```

- [ ] **步骤 3：重构 B 站客户端获取方式**

当前全局变量：

```python
bili = BilibiliClient(cookie_store_path=BASE_DIR / "bilibili_cookie.json")
```

无法支持多账号。改成辅助函数：

```python
def get_bili() -> BilibiliClient:
    account = storage.get_active_account()
    if account:
        return BilibiliClient(cookie_store_path=BASE_DIR / account["cookie_path"])
    return BilibiliClient(cookie_store_path=BASE_DIR / "bilibili_cookie.json")
```

然后把路由和 `get_session_mgr()` 中的 B 站客户端改为调用 `get_bili()`。

- [ ] **步骤 4：扫码成功后保存账号**

扫码登录开始时还不知道 `mid`，不能直接把 Cookie 写入 `accounts/<mid>/...`。必须先使用临时 Cookie 路径完成扫码轮询，成功后再用临时客户端读取账号信息，最后把 Cookie 文件移动到账号目录：

```python
login_id = uuid.uuid4().hex
temp_cookie_path = BASE_DIR / "accounts" / "_pending" / f"{login_id}.json"
temp_bili = BilibiliClient(cookie_store_path=temp_cookie_path)

# 二维码生成和轮询都使用 temp_bili；确认扫码成功并写入 temp_cookie_path 后：
profile = await temp_bili.get_my_profile()
account_id = str(profile["mid"])
cookie_path = f"accounts/{account_id}/bilibili_cookie.json"
final_cookie_path = BASE_DIR / cookie_path
final_cookie_path.parent.mkdir(parents=True, exist_ok=True)
shutil.move(str(temp_cookie_path), str(final_cookie_path))
storage.upsert_account({
    "account_id": account_id,
    "mid": profile["mid"],
    "uname": profile["uname"],
    "avatar_url": profile.get("face", ""),
    "cookie_path": cookie_path,
    "is_active": 1,
})
storage.activate_account(account_id)
```

实现注意：

- 每个账号 Cookie 必须写入自己的 `accounts/<mid>/bilibili_cookie.json`。
- 登录流程失败或超时时要删除临时 Cookie 文件，避免留下不可用账号。
- WBI key 按账号隔离，账号切换不应清空其他账号的缓存。
- 如果登录成功的 `mid` 已存在，则覆盖该账号 Cookie 并刷新账号资料。

- [ ] **步骤 5：新增账号接口**

```python
@app.get("/api/accounts")
async def api_accounts():
    return {"accounts": storage.list_accounts(), "active": storage.get_active_account()}

@app.post("/api/accounts/{account_id}/switch")
async def api_switch_account(account_id: str):
    if _running_pipelines:
        raise BibiError("有整理任务正在运行，请完成或取消后再切换账号", code="PIPELINE_RUNNING")
    storage.activate_account(account_id)
    return {"ok": True}

@app.post("/api/accounts/login/start")
async def api_account_login_start():
    return await api_qrcode_generate()
```

- [ ] **步骤 6：新增前端入口**

导航或设置页增加账号按钮：

```html
<button data-dom-id="nav-account" type="button" class="btn btn-secondary">
  <i data-lucide="user-round" class="w-4 h-4"></i><span data-dom-id="active-account-name">账号</span>
</button>
```

JS 中增加：

```javascript
async function renderAccounts() {
  const data = await api('/api/accounts');
  // 渲染账号列表、切换按钮、扫码添加账号按钮
}
```

前端切换账号按钮需要处理 `PIPELINE_RUNNING` 错误：提示“当前有整理任务正在运行，请先完成或取消后再切换账号”，不得在前端本地强行切换显示状态。

- [ ] **步骤 7：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_storage.py tests/test_frontend_static.py -q
```

预期：通过。

---

## 任务 6：增加 AI 微调和方案版本切换

**文件：**

- 修改：`core/ai_classifier.py`
- 修改：`core/session.py`
- 修改：`core/storage.py`
- 修改：`main.py`
- 修改：`static/app.js`
- 修改：`static/index.html`
- 测试：`tests/test_ai_classifier.py`、`tests/test_session.py`、`tests/test_frontend_static.py`

- [ ] **步骤 1：新增 AI 微调测试**

```python
@pytest.mark.asyncio
async def test_refine_plan_uses_instruction(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def fake_chat_json(system, user):
        assert "微调" in system
        assert "把官方的作品单独放在一个收藏夹内" in user
        return {"items": [
            {"avid": 1, "category": "官方作品", "confidence": 0.93, "reason": "UP主名称包含官方"},
            {"avid": 2, "category": "动漫", "confidence": 0.9, "reason": "保持原分类"},
        ]}

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)
    videos = [
        VideoInfo(avid=1, title="官方PV", up_name="某某官方", tname="动画"),
        VideoInfo(avid=2, title="剪辑", up_name="普通UP", tname="动画"),
    ]
    current = [
        Classification(1, "动漫", 0.9, ""),
        Classification(2, "动漫", 0.9, ""),
    ]

    result = await classifier.refine_plan(videos, current, "把官方的作品单独放在一个收藏夹内")

    assert result[0].category == "官方作品"
    assert result[1].category == "动漫"
```

- [ ] **步骤 2：实现 `refine_plan`**

```python
REFINE_PROMPT = """你是B站收藏夹分类方案微调助手。
输入包含视频元数据、当前分类方案、用户微调指令。
你必须返回完整的新方案，不要只返回变化项。
输出严格JSON: {"items":[{"avid":int,"category":"中文2-10字","confidence":0-1,"reason":"<=40字原因"}]}
"""

async def refine_plan(self, videos: list[VideoInfo], current: list[Classification], instruction: str) -> list[Classification]:
    user = json.dumps({
        "instruction": instruction,
        "videos": [v.__dict__ for v in videos],
        "current_plan": [c.__dict__ for c in current],
    }, ensure_ascii=False)
    data = await self._chat_json(REFINE_PROMPT, user)
    by_avid = {it["avid"]: it for it in data.get("items", [])}
    expected_avids = {c.avid for c in current}
    returned_avids = set(by_avid)
    if returned_avids != expected_avids:
        raise AiApiError(
            f"AI 微调结果数量不一致，缺少 {sorted(expected_avids - returned_avids)}，多出 {sorted(returned_avids - expected_avids)}",
            code="AI_BAD_JSON",
        )
    result = []
    for old in current:
        it = by_avid.get(old.avid)
        result.append(Classification(
            avid=old.avid,
            category=it.get("category", old.category),
            confidence=float(it.get("confidence", old.confidence)),
            reason=it.get("reason", old.reason),
        ))
    return result
```

- [ ] **步骤 3：增加 session 微调方法**

```python
async def refine_plan(self, sid: str, instruction: str) -> dict:
    s = self.storage.load_session(sid)
    if not s or s["status"] != "pending_review":
        raise StateError("仅预览状态可微调方案")
    active = self.storage.get_active_plan_version(sid)
    if not active:
        self.storage.migrate_legacy_classifications_to_version(sid)
        active = self.storage.get_active_plan_version(sid)
    current_items = self.storage.load_plan_items(active["version_id"])
    video_sources = self.storage.list_session_video_sources(sid)
    avids = sorted({row["avid"] for row in video_sources} | {it["avid"] for it in current_items})
    videos_by_id = {r["avid"]: r for r in self.storage.list_videos_by_avids(avids, account_id=s.get("account_id"))}
    videos = [
        VideoInfo(
            avid=it["avid"],
            title=videos_by_id[it["avid"]]["title"],
            up_name=videos_by_id[it["avid"]]["up_name"],
            tname=videos_by_id[it["avid"]].get("tname", ""),
            intro=videos_by_id[it["avid"]].get("intro", ""),
            tags=_parse_tags(videos_by_id[it["avid"]].get("tags", "[]")),
        )
        for it in current_items if it["avid"] in videos_by_id
    ]
    current = [Classification(it["avid"], it["category"], it["confidence"], it["reason"]) for it in current_items]
    refined = await self.ai.refine_plan(videos, current, instruction)
    self.storage.create_plan_version(
        sid,
        active["version_id"],
        instruction,
        [{"avid": c.avid, "category": c.category, "confidence": c.confidence, "reason": c.reason} for c in refined],
        activate=True,
    )
    return self.get_plan(sid)
```

- [ ] **步骤 4：增加路由**

```python
class RefineIn(BaseModel):
    instruction: str

@app.post("/api/session/{sid}/refine")
async def api_refine_plan(sid: str, payload: RefineIn):
    mgr = get_session_mgr()
    return await mgr.refine_plan(sid, payload.instruction)

@app.post("/api/session/{sid}/versions/{version_id}/activate")
async def api_activate_version(sid: str, version_id: str):
    storage.activate_plan_version(sid, version_id)
    return get_session_mgr().get_plan(sid)
```

- [ ] **步骤 5：前端微调面板**

```javascript
function renderRefinePanel(sid) {
  $('review-refine-panel').innerHTML = `
    <div class="flex flex-col sm:flex-row gap-2">
      <div class="field flex-1">
        <i data-lucide="sparkles" class="w-4 h-4"></i>
        <input data-dom-id="refine-instruction" class="control" type="text" placeholder="例如：把官方的作品单独放在一个收藏夹内">
      </div>
      <button data-dom-id="refine-submit" type="button" class="btn btn-primary">
        <i data-lucide="wand-sparkles" class="w-4 h-4"></i><span>生成新方案</span>
      </button>
    </div>`;
  $('refine-submit').onclick = async () => {
    const instruction = $('refine-instruction').value.trim();
    if (!instruction) return;
    $('refine-submit').disabled = true;
    try {
      const plan = await api(`/api/session/${sid}/refine`, {
        method: 'POST',
        body: JSON.stringify({ instruction }),
      });
      renderReview(sid, plan);
    } catch (e) {
      alert(e.message);
    } finally {
      $('refine-submit').disabled = false;
    }
  };
}
```

- [ ] **步骤 6：版本切换栏**

```javascript
function renderVersionBar(sid, versions) {
  $('review-version-bar').innerHTML = versions.map(v => `
    <button data-dom-id="plan-version-${escapeDomId(v.version_id)}" type="button"
      class="btn ${v.is_active ? 'btn-primary' : 'btn-secondary'}"
      style="height:32px;padding:0 12px;font-size:12px;">
      方案 ${v.version_no}
    </button>`).join('');
  versions.forEach(v => {
    $(`plan-version-${escapeDomId(v.version_id)}`).onclick = async () => {
      const plan = await api(`/api/session/${sid}/versions/${v.version_id}/activate`, { method: 'POST' });
      renderReview(sid, plan);
    };
  });
}
```

- [ ] **步骤 7：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_ai_classifier.py tests/test_session.py tests/test_frontend_static.py -q
```

预期：通过。

---

## 任务 7：执行阶段只使用当前激活版本，并按源收藏夹分组移动

**文件：**

- 修改：`core/session.py`
- 修改：`core/storage.py`
- 测试：`tests/test_session.py`

- [ ] **步骤 1：新增激活版本执行测试**

```python
@pytest.mark.asyncio
async def test_execute_uses_active_plan_version(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100})
    v1 = storage.create_plan_version(sid, None, "初始", [{"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""}], activate=False)
    storage.create_plan_version(sid, v1, "官方单独放", [{"avid": 1, "category": "官方作品", "confidence": 0.95, "reason": ""}], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_videos = AsyncMock(return_value=True)

    stats = await ClassifySession(storage, bili, ai).execute(sid)

    assert stats["success"] == 1
    bili.create_folder.assert_awaited_once_with(title="官方作品", privacy=1)
```

- [ ] **步骤 2：修改执行数据来源**

将：

```python
items = self.storage.load_classifications(sid)
```

替换为：

```python
active = self.storage.get_active_plan_version(sid)
if active:
    items = self.storage.load_plan_items(active["version_id"])
else:
    items = self.storage.load_classifications(sid)
```

执行结果标记也要优先写入版本明细：

```python
self.storage.mark_plan_item_executed(active["version_id"], avid, ok)
```

如果没有 active version，再回退到旧 `mark_classification_executed()`。

- [ ] **步骤 3：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_session.py -q
```

预期：通过。

- [ ] **步骤 4：新增多源分组移动测试**

```python
@pytest.mark.asyncio
async def test_execute_groups_moves_by_source_folder(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 2, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 1, "selected_order": 1},
    ])
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100})
    storage.upsert_video({"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 200})
    storage.add_session_video_source(sid, avid=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, avid=2, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, avid=2, source_fid=200, resource_type=2)
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_videos = AsyncMock(return_value=True)
    bili.get_my_folders = AsyncMock(return_value=[])

    stats = await ClassifySession(storage, bili, ai).execute(sid)

    assert stats["success"] == 3
    bili.move_videos.assert_any_await(src_media_id=100, tar_media_id=9001, avids=[1, 2])
    bili.move_videos.assert_any_await(src_media_id=200, tar_media_id=9001, avids=[2])
```

- [ ] **步骤 5：修改执行分组逻辑**

当前逻辑按目标分类分组后，直接使用单个 `s["source_fid"]`：

```python
await self.bili.move_videos(
    src_media_id=s["source_fid"],
    tar_media_id=target_fid,
    avids=chunk,
)
```

需要改为：

```python
sources = self.storage.list_session_video_sources(sid)
sources_by_avid: dict[int, list[dict]] = {}
for src in sources:
    sources_by_avid.setdefault(src["avid"], []).append(src)

move_groups: dict[tuple[str, int], list[int]] = {}
for it in items:
    if it["category"] == "未分类":
        continue
    for src in sources_by_avid.get(it["avid"], [{"source_fid": s["source_fid"], "resource_type": 2}]):
        key = (it["category"], src["source_fid"])
        move_groups.setdefault(key, []).append(it["avid"])

total_sources = sum(len(set(avids)) for avids in move_groups.values())
success = 0
failed = 0
for (cat, source_fid), avids in move_groups.items():
    target_fid = cat_to_fid[cat]
    for chunk in _chunks(sorted(set(avids)), batch_size):
        try:
            await self.bili.move_videos(
                src_media_id=source_fid,
                tar_media_id=target_fid,
                avids=chunk,
            )
            success += len(chunk)
            for avid in chunk:
                if active:
                    self.storage.mark_plan_item_executed(active["version_id"], avid, True)
                self.storage.mark_session_video_source_moved(sid, avid, source_fid, True, "")
        except Exception as e:
            failed += len(chunk)
            for avid in chunk:
                if active:
                    self.storage.mark_plan_item_executed(active["version_id"], avid, False)
                self.storage.mark_session_video_source_moved(sid, avid, source_fid, False, str(e))

stats = {
    "success": success,
    "failed": failed,
    "total": total_sources,
    "unique_videos": len(items),
}
```

计数规则：

- `success` 统计成功移动的“来源实例”数量，而不是唯一视频数量。
- 如果同一个视频存在于两个源收藏夹，并从两个源收藏夹都移动成功，则成功数加 2。
- `total` 应显示来源实例总数，同时预览页仍可显示唯一视频数。

- [ ] **步骤 6：运行 session 测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_session.py -q
```

预期：通过。

## 任务 7.5：适配多源场景下的失败重试

**文件：**

- 修改：`core/session.py`
- 修改：`core/storage.py`
- 测试：`tests/test_session.py`

- [ ] **步骤 1：新增失败重试测试**

```python
@pytest.mark.asyncio
async def test_retry_failed_retries_by_original_source_folder(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 1, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 1, "selected_order": 1},
    ])
    storage.add_session_video_source(sid, avid=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, avid=1, source_fid=200, resource_type=2)
    storage.mark_session_video_source_moved(sid, 1, 100, False, "timeout")
    storage.mark_session_video_source_moved(sid, 1, 200, False, "timeout")
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_videos = AsyncMock(return_value=True)

    stats = await ClassifySession(storage, bili, ai).retry_failed(sid)

    assert stats["success"] == 2
    bili.move_videos.assert_any_await(src_media_id=100, tar_media_id=9001, avids=[1])
    bili.move_videos.assert_any_await(src_media_id=200, tar_media_id=9001, avids=[1])
```

- [ ] **步骤 2：把失败项定位到来源实例**

现有 `failed_items` 如果只保存 `avid/category`，需要补充 `source_fid`。推荐直接从 `session_video_sources` 查询 `moved=0 AND move_error<>''` 的来源实例，再按 `(category, source_fid)` 分组重试。

```python
def list_failed_session_video_sources(self, session_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_video_sources WHERE session_id = ? AND moved = 0 AND COALESCE(move_error, '') <> ''",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

failed_sources = self.storage.list_failed_session_video_sources(sid)
items_by_avid = {it["avid"]: it for it in self.storage.load_plan_items(active["version_id"])}
move_groups: dict[tuple[str, int], list[int]] = {}
for src in failed_sources:
    it = items_by_avid.get(src["avid"])
    if not it or it["category"] == "未分类":
        continue
    move_groups.setdefault((it["category"], src["source_fid"]), []).append(src["avid"])
```

- [ ] **步骤 3：重试成功后只更新对应来源实例**

`retry_failed()` 不得用单个 `s["source_fid"]` 重试全部失败视频。每次成功或失败都调用 `mark_session_video_source_moved(sid, avid, source_fid, ok, error)`，并保持 `success/failed/total` 均按来源实例计数。

- [ ] **步骤 4：运行 session 测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_session.py -q
```

预期：通过。

## 任务 8：预览页展示跳过项和清理按钮

**文件：**

- 修改：`static/app.js`
- 修改：`static/index.html`
- 测试：`tests/test_frontend_static.py`

- [ ] **步骤 1：渲染跳过项面板**

```javascript
async function renderSkippedPanel(sid) {
  const data = await api(`/api/session/${sid}/skipped-items`);
  const removable = data.items.filter(it => it.removable && !it.removed);
  if (!data.items.length) {
    $('review-skipped-panel').innerHTML = '';
    return;
  }
  $('review-skipped-panel').innerHTML = `
    <section class="rounded-xl p-4" style="background:var(--background-100);border:1px solid var(--border);">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold" style="color:var(--foreground);">跳过条目</h2>
          <p class="text-xs mt-1" style="color:var(--muted-foreground);">共 ${data.items.length} 个，${removable.length} 个可从收藏夹移除</p>
        </div>
        <button data-dom-id="remove-skipped" type="button" class="btn btn-secondary" ${removable.length ? '' : 'disabled'}>
          <i data-lucide="trash-2" class="w-4 h-4"></i><span>移除不可访问项</span>
        </button>
      </div>
    </section>`;
  const btn = $('remove-skipped');
  if (btn) btn.onclick = async () => {
    if (!confirm(`将从 B 站收藏夹中移除 ${removable.length} 个不可访问条目。此操作不可逆。是否继续？`)) return;
    const r = await api(`/api/session/${sid}/skipped-items/remove`, {
      method: 'POST',
      body: JSON.stringify({ item_ids: removable.map(it => it.id) }),
    });
    alert(`已移除 ${r.stats.success} 个，失败 ${r.stats.failed} 个`);
    renderSkippedPanel(sid);
  };
}
```

- [ ] **步骤 2：在预览页调用**

在 `renderReview(sid, plan)` 中调用：

```javascript
renderSkippedPanel(sid);
```

- [ ] **步骤 3：新增前端测试**

```python
def test_frontend_skipped_cleanup_ui():
    assert "renderSkippedPanel" in APP_JS
    assert "remove-skipped" in APP_JS
    assert "/skipped-items/remove" in APP_JS
    assert "此操作不可逆" in APP_JS
```

- [ ] **步骤 4：运行前端测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_frontend_static.py -q
```

预期：通过。

---

## 任务 8.5：整理后识别空源收藏夹并允许手动删除

**文件：**

- 修改：`core/bilibili_api.py`
- 修改：`core/session.py`
- 修改：`core/storage.py`
- 修改：`main.py`
- 修改：`static/app.js`
- 修改：`static/index.html`
- 测试：`tests/test_bilibili_api.py`、`tests/test_session.py`、`tests/test_frontend_static.py`

- [ ] **步骤 1：新增删除收藏夹接口测试**

```python
@respx.mock
async def test_delete_folders(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    route = respx.post(f"{API_BASE}/x/v3/fav/folder/del").mock(
        return_value=httpx.Response(200, json={"code": 0, "message": "0", "ttl": 1, "data": 0})
    )

    ok = await client.delete_folders(media_ids=[100, 200])

    assert ok is True
    form = route.calls[0].request.content.decode()
    assert "media_ids=100%2C200" in form
    assert "csrf=csrf_tok" in form
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
.\.venv\Scripts\pytest.exe tests\test_bilibili_api.py::test_delete_folders -q
```

预期：失败，因为 `delete_folders` 尚不存在。

- [ ] **步骤 3：实现 B 站删除收藏夹封装**

在 `BilibiliClient` 中新增：

```python
async def delete_folders(self, media_ids: list[int]) -> bool:
    await self._post_form(
        "/x/v3/fav/folder/del",
        {"media_ids": ",".join(str(mid) for mid in media_ids)},
    )
    return True
```

- [ ] **步骤 4：执行后标记空收藏夹候选**

在 `ClassifySession.execute()` 完成移动后，刷新收藏夹列表：

```python
async def refresh_empty_source_candidates(self, sid: str) -> None:
    sources = self.storage.list_session_sources(sid)
    source_ids = {s["source_fid"] for s in sources}
    try:
        folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
    except Exception as e:
        logger.warning("移动已完成，但刷新空源收藏夹候选失败: %s", e)
        return
    for src in sources:
        fid = src["source_fid"]
        folder = folders.get(fid)
        if not folder:
            continue
        is_empty = int(folder.get("media_count", 0)) == 0
        self.storage.mark_session_source_empty_candidate(
            sid,
            fid,
            delete_candidate=is_empty and not src.get("delete_protected"),
            emptied_after_execute=is_empty,
        )
```

在 `execute()` 写入 done 前调用：

```python
await self.refresh_empty_source_candidates(sid)
```

如果使用 `logger.warning()`，需在 `core/session.py` 中按现有项目风格复用或新增 logger。该刷新只影响“空收藏夹候选”展示，不能因为网络失败把已经移动成功的整理任务改成失败。

- [ ] **步骤 5：新增删除空收藏夹业务方法**

```python
async def delete_empty_source_folders(self, sid: str, source_fids: list[int]) -> dict:
    sources = {s["source_fid"]: s for s in self.storage.list_session_sources(sid)}
    folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
    deletable = []
    rejected = []
    for fid in source_fids:
        src = sources.get(fid)
        folder = folders.get(fid)
        if not src or not folder:
            rejected.append(fid)
            continue
        if src.get("delete_protected"):
            rejected.append(fid)
            continue
        if int(folder.get("media_count", 0)) != 0:
            rejected.append(fid)
            continue
        deletable.append(fid)
    deleted = []
    if deletable:
        try:
            await self.bili.delete_folders(deletable)
        except Exception as e:
            for fid in deletable:
                self.storage.mark_session_source_deleted(sid, fid, False, str(e))
            return {"success": 0, "failed": len(rejected) + len(deletable), "deleted": [], "rejected": rejected + deletable}
        latest = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
        for fid in deletable:
            if fid not in latest:
                self.storage.mark_session_source_deleted(sid, fid, True, "")
                deleted.append(fid)
            else:
                self.storage.mark_session_source_deleted(sid, fid, False, "B站返回成功但收藏夹仍存在")
                rejected.append(fid)
    return {"success": len(deleted), "failed": len(rejected), "deleted": deleted, "rejected": rejected}
```

- [ ] **步骤 6：新增路由**

```python
class DeleteEmptyFoldersIn(BaseModel):
    source_fids: list[int]

@app.get("/api/session/{sid}/empty-source-folders")
async def api_empty_source_folders(sid: str):
    return {"items": storage.list_empty_source_candidates(sid)}

@app.post("/api/session/{sid}/empty-source-folders/delete")
async def api_delete_empty_source_folders(sid: str, payload: DeleteEmptyFoldersIn):
    mgr = get_session_mgr()
    return {"stats": await mgr.delete_empty_source_folders(sid, payload.source_fids)}
```

- [ ] **步骤 7：新增前端面板**

结果页或预览页底部增加空收藏夹面板：

```javascript
async function renderEmptySourceFolders(sid) {
  const data = await api(`/api/session/${sid}/empty-source-folders`);
  const candidates = data.items.filter(it => it.delete_candidate && !it.deleted);
  if (!candidates.length) {
    $('empty-source-folders').innerHTML = '';
    return;
  }
  $('empty-source-folders').innerHTML = `
    <section class="mt-6 rounded-xl p-4" style="background:var(--background-100);border:1px solid var(--border);">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold" style="color:var(--foreground);">空收藏夹</h2>
          <p class="text-xs mt-1" style="color:var(--muted-foreground);">整理后发现 ${candidates.length} 个源收藏夹为空，可手动选择删除。</p>
        </div>
        <button data-dom-id="delete-empty-folders" type="button" class="btn btn-secondary">
          <i data-lucide="trash-2" class="w-4 h-4"></i><span>删除选中的空收藏夹</span>
        </button>
      </div>
      <div class="mt-3 flex flex-col gap-2">
        ${candidates.map(it => `
          <label class="flex items-center gap-2 text-sm" style="color:var(--foreground);">
            <input type="checkbox" data-empty-folder-id="${it.source_fid}">
            <span>${escapeHtml(it.title)}</span>
          </label>`).join('')}
      </div>
    </section>`;
  $('delete-empty-folders').onclick = async () => {
    const selected = [...document.querySelectorAll('[data-empty-folder-id]:checked')].map(el => Number(el.getAttribute('data-empty-folder-id')));
    if (!selected.length) return;
    if (!confirm(`将删除 ${selected.length} 个空收藏夹，此操作不可逆。是否继续？`)) return;
    const r = await api(`/api/session/${sid}/empty-source-folders/delete`, {
      method: 'POST',
      body: JSON.stringify({ source_fids: selected }),
    });
    alert(`已删除 ${r.stats.success} 个，拒绝或失败 ${r.stats.failed} 个`);
    renderEmptySourceFolders(sid);
  };
}
```

- [ ] **步骤 8：新增前端静态测试**

```python
def test_frontend_empty_folder_cleanup_ui():
    assert "empty-source-folders" in INDEX_HTML
    assert "renderEmptySourceFolders" in APP_JS
    assert "/empty-source-folders/delete" in APP_JS
    assert "此操作不可逆" in APP_JS
```

- [ ] **步骤 9：运行测试**

```powershell
.\.venv\Scripts\pytest.exe tests/test_bilibili_api.py tests/test_session.py tests/test_frontend_static.py -q
```

预期：通过。

---

## 任务 9：端到端验收

- [ ] **步骤 1：运行完整自动化测试**

```powershell
.\.venv\Scripts\pytest.exe -q
```

预期：全部通过。

- [ ] **步骤 2：人工验证预览筛选**

1. 启动应用。
2. 在首页勾选两个或更多源收藏夹。
3. 确认“开始智能整理”按钮显示已选择数量。
4. 点击“开始智能整理”并完成一次整理。
5. 进入预览页。
6. 确认顶部显示“全部”和每个目标收藏夹/分类名称及数量。
7. 点击某个分类。
8. 确认只展示该分类内容。
9. 点击“全部”。
10. 确认所有分类恢复展示。
11. 折叠某个分类。
12. 确认视频列表隐藏，但分类名称和数量仍可见。

- [ ] **步骤 3：人工验证跳过项清理**

1. 打开有跳过项的会话。
2. 确认跳过项面板显示具体原因。
3. 点击“移除不可访问项”。
4. 确认出现破坏性操作确认框。
5. 第一次取消，确认没有调用删除接口。
6. 第二次确认执行。
7. 确认已移除项更新为 `removed=1`，界面刷新显示结果。

- [ ] **步骤 4：人工验证账号切换**

1. 使用账号 A 扫码登录。
2. 确认收藏夹列表属于账号 A。
3. 通过扫码添加账号 B。
4. 切回账号 A。
5. 确认收藏夹列表恢复为账号 A。
6. 确认切换账号后只使用当前账号的 WBI key，不会复用其他账号缓存。
7. 确认会话按账号隔离展示。

- [ ] **步骤 5：人工验证 AI 微调**

1. 打开预览页。
2. 输入 `把官方的作品单独放在一个收藏夹内`。
3. 点击“生成新方案”。
4. 确认出现新版本。
5. 在方案 1 和方案 2 之间切换。
6. 确认分类数量和视频分组随版本变化。
7. 执行方案 2。
8. 确认创建的收藏夹来自当前激活版本，而不是旧版本。

- [ ] **步骤 6：人工验证多源移动与空收藏夹删除**

1. 选择两个源收藏夹，其中至少一个收藏夹的视频会全部移动走。
2. 执行整理。
3. 确认同一目标分类下的视频能从不同源收藏夹移动到同一个新目标收藏夹。
4. 进入结果页或空收藏夹面板。
5. 确认只展示整理后为空的源收藏夹候选。
6. 确认默认收藏夹不会出现在可删除候选中。
7. 手动勾选一个空收藏夹。
8. 点击删除，确认出现“此操作不可逆”的确认框。
9. 取消一次，确认没有删除。
10. 再次确认删除。
11. 刷新收藏夹列表，确认该空收藏夹已删除。

## 七、风险与保护措施

- 从 B 站收藏夹移除内容是不可逆操作，不能自动触发。
- `batch-del` 必须只处理用户确认过的条目 ID，不能默认删除所有跳过项。
- 删除收藏夹是不可逆操作，只能删除用户手动勾选的空收藏夹。
- 删除空收藏夹前必须重新向 B 站确认该收藏夹仍为空。
- `delete_protected=1` 的收藏夹不允许删除；不要只按标题判断默认收藏夹。
- `clean` 第一版不要暴露为默认功能，避免清理范围不可控。
- WBI key 缓存必须按账号隔离，切换账号时不能复用其他账号的 key。
- 多账号上线前必须完成 `videos` 表账号隔离迁移，推荐主键为 `(account_id, avid)`。
- 多源整理时，AI 分类可以按 `avid` 去重，但执行移动必须按 `session_video_sources` 中的来源实例分组。
- 同一个视频如果出现在多个源收藏夹，需要从每个源收藏夹各移动一次。
- AI 微调必须返回完整方案，不能只返回变化项，否则版本切换和执行会不明确。
- AI 微调失败时，旧的激活版本必须保持不变。
- 执行阶段只能读取当前激活版本。

## 八、完成标准

- 预览页显示“全部”和所有目标收藏夹/分类名称及数量。
- 首页支持多选源收藏夹，并通过“开始智能整理”显式启动流程。
- 一次整理可以合并多个源收藏夹的视频内容。
- 预览页分组支持折叠/展开。
- 跳过项有可见的具体原因。
- 可确认移除不可访问的跳过项，并通过 `batch-del` 从源收藏夹移除。
- 执行移动按源收藏夹分组，不会把所有视频错误地当成来自同一个收藏夹。
- 整理后可识别空源收藏夹，并允许用户手动选择删除。
- 默认收藏夹不会被删除。
- 支持多个 B 站账号扫码登录和切换。
- 预览方案支持版本化。
- AI 微调会生成一个新的可切换版本。
- 执行阶段只使用当前激活版本。
- 全量测试通过。

## 九、外部资料与核实结果

- B 站收藏夹操作接口资料：`batch-del` 和 `clean`  
  https://github.com/pskdje/bilibili-API-collect/blob/main/docs/fav/action.md
- B 站收藏夹删除接口资料：`folder/del`  
  https://github.com/pskdje/bilibili-API-collect/blob/main/docs/fav/action.md
- 2026-07-07 本地无 Cookie 可达性核实：
  - `/x/v3/fav/resource/batch-del` 返回 HTTP 200，B 站业务码 `-101`。
  - `/x/v3/fav/resource/clean` 返回 HTTP 200，B 站业务码 `-101`。
  - `/x/v3/fav/folder/del` 返回 HTTP 200，B 站业务码 `-101`。
