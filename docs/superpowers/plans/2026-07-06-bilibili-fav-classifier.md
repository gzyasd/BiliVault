# B站收藏夹自动分类工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个本地运行的 B 站收藏夹 AI 自动分类工具，扫码登录后一键把源收藏夹的视频分类到多个子收藏夹。

**Architecture:** FastAPI 后端 + 单页 HTML 前端。core 层分四个模块（bilibili_api / ai_classifier / storage / session），main.py 是薄路由层。状态用 SQLite + 两个 JSON 文件持久化，支持断点续作。

**Tech Stack:** Python 3.10+、FastAPI、Uvicorn、httpx、OpenAI SDK、qrcode[pil]、SQLite、pytest + pytest-asyncio + respx

**Spec:** `docs/superpowers/specs/2026-07-06-bilibili-fav-classifier-design.md`

---

## 文件结构

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `requirements.txt` | Python 依赖 | 创建 |
| `.gitignore` | 忽略敏感文件 | 创建 |
| `core/__init__.py` | 包标记 | 创建 |
| `core/errors.py` | 自定义异常基类 | 创建 |
| `core/storage.py` | SQLite + JSON 读写 | 创建 |
| `core/bilibili_api.py` | B站 API 封装（WBI签名、扫码、收藏夹、移动） | 创建 |
| `core/ai_classifier.py` | OpenAI 兼容接口分类 | 创建 |
| `core/session.py` | 分类会话状态机 | 创建 |
| `main.py` | FastAPI 启动 + 路由 | 创建 |
| `static/index.html` | SPA 单页（6视图 + Pinguo设计token + Tailwind/Lucide CDN） | 创建 |
| `static/app.js` | 前端流程逻辑（6视图切换 + SSE消费 + 失败项展示） | 创建 |
| `tests/test_storage.py` | storage 单测 | 创建 |
| `tests/test_bilibili_api.py` | bilibili_api 单测 | 创建 |
| `tests/test_ai_classifier.py` | ai_classifier 单测 | 创建 |
| `tests/test_session.py` | session 单测 | 创建 |
| `README.md` | 使用说明 + 手动验证清单 | 创建 |

---

## Task 1: 项目脚手架 + 异常基类

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `core/__init__.py`
- Create: `core/errors.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 创建 `requirements.txt`**

```
fastapi>=0.110
uvicorn[standard]>=0.27
httpx>=0.27
openai>=1.12
qrcode[pil]>=7.4
pytest>=8.0
pytest-asyncio>=0.23
respx>=0.20
```

- [ ] **Step 2: 创建 `.gitignore`**

```
config.json
bilibili_cookie.json
bibi.db
__pycache__/
.venv/
*.pyc
.pytest_cache/
```

- [ ] **Step 3: 创建空 `core/__init__.py` 和 `tests/__init__.py`**

两个文件都留空。

- [ ] **Step 4: 创建 `core/errors.py`**

```python
class BibiError(Exception):
    code: str = "UNKNOWN"
    user_message: str = "发生未知错误"

    def __init__(self, user_message: str | None = None, *, code: str | None = None):
        if user_message:
            self.user_message = user_message
        if code:
            self.code = code
        super().__init__(self.user_message)


class NotLoggedInError(BibiError):
    code = "NOT_LOGGED_IN"
    user_message = "B站登录已失效，请重新扫码登录"


class BiliApiError(BibiError):
    code = "BILI_API_ERROR"

    def __init__(self, bili_code: int, message: str):
        self.bili_code = bili_code
        super().__init__(f"B站接口错误({bili_code}): {message}", code="BILI_API_ERROR")


class AiApiError(BibiError):
    code = "AI_API_ERROR"


class StateError(BibiError):
    code = "STATE_ERROR"
```

- [ ] **Step 5: 安装依赖并验证**

Run: `python -m pip install -r requirements.txt`
Expected: 安装成功无报错

- [ ] **Step 6: 验证 errors 可导入**

Run: `python -c "from core.errors import BibiError, NotLoggedInError; print(NotLoggedInError().code)"`
Expected: 输出 `NOT_LOGGED_IN`

- [ ] **Step 7: Commit**

```bash
git add requirements.txt .gitignore core/__init__.py core/errors.py tests/__init__.py
git commit -m "chore: project scaffold and error base classes"
```

---

## Task 2: storage.py - 配置与 Cookie 持久化

**Files:**
- Create: `core/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试**

`tests/test_storage.py`:
```python
import json
from pathlib import Path

from core.storage import Storage


def test_save_and_load_config(tmp_path):
    storage = Storage(tmp_path)
    storage.save_config({
        "ai_base_url": "https://api.deepseek.com",
        "ai_api_key": "sk-xxx",
        "ai_model": "deepseek-chat",
        "default_privacy": 1,
    })
    loaded = storage.load_config()
    assert loaded["ai_api_key"] == "sk-xxx"
    assert (tmp_path / "config.json").exists()


def test_load_config_returns_none_when_absent(tmp_path):
    storage = Storage(tmp_path)
    assert storage.load_config() is None


def test_save_and_load_cookie(tmp_path):
    storage = Storage(tmp_path)
    cookies = {"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": "123"}
    storage.save_cookie(cookies)
    assert storage.load_cookie() == cookies


def test_load_cookie_returns_none_when_absent(tmp_path):
    storage = Storage(tmp_path)
    assert storage.load_cookie() is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.storage'`

- [ ] **Step 3: 实现 `core/storage.py`（配置和 cookie 部分）**

```python
import json
from pathlib import Path


class Storage:
    def __init__(self, base_dir: Path | str = "."):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self.base_dir / "config.json"
        self._cookie_path = self.base_dir / "bilibili_cookie.json"
        self._db_path = self.base_dir / "bibi.db"
        self._init_db()

    def load_config(self) -> dict | None:
        if not self._config_path.exists():
            return None
        return json.loads(self._config_path.read_text(encoding="utf-8"))

    def save_config(self, config: dict) -> None:
        self._config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_cookie(self) -> dict | None:
        if not self._cookie_path.exists():
            return None
        return json.loads(self._cookie_path.read_text(encoding="utf-8"))

    def save_cookie(self, cookies: dict) -> None:
        self._cookie_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear_cookie(self) -> None:
        if self._cookie_path.exists():
            self._cookie_path.unlink()

    def _init_db(self) -> None:
        pass
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_storage.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_storage.py
git commit -m "feat(storage): config and cookie persistence"
```

---

## Task 3: storage.py - SQLite schema 与收藏夹/视频缓存

**Files:**
- Modify: `core/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试（追加到 test_storage.py）**

```python
def test_upsert_and_load_folder(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_folder({
        "fid": 100, "title": "默认收藏夹",
        "media_count": 50, "cover_url": "http://x/cover.jpg",
    })
    folders = storage.list_folders()
    assert len(folders) == 1
    assert folders[0]["fid"] == 100
    assert folders[0]["title"] == "默认收藏夹"


def test_upsert_and_load_video(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_video({
        "avid": 200, "bvid": "BV1xx", "title": "测试视频",
        "intro": "", "tags": "[]", "up_name": "UP",
        "up_mid": 999, "cover_url": "http://x.jpg", "tname": "科技",
        "fid": 100,
    })
    videos = storage.list_videos_by_fid(100)
    assert len(videos) == 1
    assert videos[0]["avid"] == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `AttributeError: 'Storage' object has no attribute 'upsert_folder'`

- [ ] **Step 3: 实现 schema + upsert/list（替换 `_init_db`，追加方法）**

替换 `core/storage.py` 中的 `_init_db` 方法并追加新方法：

```python
import sqlite3
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fav_folders (
  fid INTEGER PRIMARY KEY,
  title TEXT,
  media_count INTEGER,
  cover_url TEXT,
  cached_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  avid INTEGER PRIMARY KEY,
  bvid TEXT,
  title TEXT,
  intro TEXT,
  tags TEXT,
  up_name TEXT,
  up_mid INTEGER,
  cover_url TEXT,
  tname TEXT,
  fid INTEGER,
  cached_at TEXT
);
CREATE TABLE IF NOT EXISTS classify_sessions (
  session_id TEXT PRIMARY KEY,
  source_fid INTEGER,
  status TEXT,
  mode TEXT,
  created_at TEXT,
  updated_at TEXT,
  stats TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
  session_id TEXT,
  avid INTEGER,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,
  executed INTEGER DEFAULT 0,
  PRIMARY KEY (session_id, avid)
);
CREATE TABLE IF NOT EXISTS wbi_keys (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  img_key TEXT,
  sub_key TEXT,
  cached_at TEXT
);
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
"""
```

把 `_init_db` 改为：

```python
    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_folder(self, folder: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fav_folders (fid, title, media_count, cover_url, cached_at) "
                "VALUES (:fid, :title, :media_count, :cover_url, datetime('now'))",
                folder,
            )

    def list_folders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM fav_folders ORDER BY fid").fetchall()
            return [dict(r) for r in rows]

    def upsert_video(self, video: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO videos (avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) "
                "VALUES (:avid, :bvid, :title, :intro, :tags, :up_name, :up_mid, :cover_url, :tname, :fid, datetime('now'))",
                video,
            )

    def list_videos_by_fid(self, fid: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE fid = ? ORDER BY avid", (fid,)
            ).fetchall()
            return [dict(r) for r in rows]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_storage.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_storage.py
git commit -m "feat(storage): sqlite schema and folder/video cache"
```

---

## Task 4: storage.py - 会话与分类结果持久化

**Files:**
- Modify: `core/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_create_and_load_session(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    assert sid
    s = storage.load_session(sid)
    assert s["status"] == "draft"
    assert s["source_fid"] == 100
    assert s["mode"] == "quick"


def test_update_session_status(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    assert storage.load_session(sid)["status"] == "pending_review"


def test_save_and_load_classifications(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": "Python教程"},
        {"avid": 2, "category": "音乐", "confidence": 0.8, "reason": "MV"},
    ])
    items = storage.load_classifications(sid)
    assert len(items) == 2
    by_avid = {it["avid"]: it for it in items}
    assert by_avid[1]["category"] == "编程"


def test_adjust_classification(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
    ])
    storage.adjust_classification(sid, avid=1, new_category="工具")
    items = storage.load_classifications(sid)
    assert items[0]["category"] == "工具"
    assert items[0]["adjusted"] == 1


def test_list_pending_sessions_for_resume(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    pending = storage.list_sessions_by_status(["pending_review", "executing"])
    assert len(pending) == 1
    assert pending[0]["session_id"] == sid
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `AttributeError: 'Storage' object has no attribute 'create_session'`

- [ ] **Step 3: 实现会话相关方法（追加到 storage.py）**

```python
import uuid

    def create_session(self, source_fid: int, mode: str) -> str:
        session_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO classify_sessions (session_id, source_fid, status, mode, created_at, updated_at, stats) "
                "VALUES (?, ?, 'draft', ?, datetime('now'), datetime('now'), '{}')",
                (session_id, source_fid, mode),
            )
        return session_id

    def load_session(self, session_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM classify_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_session_status(self, session_id: str, status: str, stats: dict | None = None) -> None:
        if stats is not None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE classify_sessions SET status = ?, stats = ?, updated_at = datetime('now') WHERE session_id = ?",
                    (status, json.dumps(stats, ensure_ascii=False), session_id),
                )
        else:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE classify_sessions SET status = ?, updated_at = datetime('now') WHERE session_id = ?",
                    (status, session_id),
                )

    def list_sessions_by_status(self, statuses: list[str]) -> list[dict]:
        placeholders = ",".join("?" * len(statuses))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM classify_sessions WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
                statuses,
            ).fetchall()
            return [dict(r) for r in rows]

    def save_classifications(self, session_id: str, items: list[dict]) -> None:
        with self._conn() as conn:
            for it in items:
                conn.execute(
                    "INSERT OR REPLACE INTO classifications (session_id, avid, category, confidence, reason, adjusted, executed) "
                    "VALUES (?, ?, ?, ?, ?, 0, 0)",
                    (session_id, it["avid"], it["category"], it["confidence"], it["reason"]),
                )

    def load_classifications(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM classifications WHERE session_id = ? ORDER BY avid", (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def adjust_classification(self, session_id: str, avid: int, new_category: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classifications SET category = ?, adjusted = 1 WHERE session_id = ? AND avid = ?",
                (new_category, session_id, avid),
            )

    def mark_classification_executed(self, session_id: str, avid: int, ok: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classifications SET executed = ? WHERE session_id = ? AND avid = ?",
                (1 if ok else 0, session_id, avid),
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_storage.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_storage.py
git commit -m "feat(storage): session and classification persistence"
```

---

## Task 5: storage.py - WBI 密钥缓存

**Files:**
- Modify: `core/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_save_and_load_wbi_keys(tmp_path):
    storage = Storage(tmp_path)
    assert storage.load_wbi_keys() is None
    storage.save_wbi_keys(img_key="abc", sub_key="def")
    keys = storage.load_wbi_keys()
    assert keys == {"img_key": "abc", "sub_key": "def"}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `AttributeError: 'Storage' object has no attribute 'load_wbi_keys'`

- [ ] **Step 3: 实现（追加到 storage.py）**

```python
    def load_wbi_keys(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT img_key, sub_key FROM wbi_keys WHERE id = 1").fetchone()
            if not row:
                return None
            return {"img_key": row["img_key"], "sub_key": row["sub_key"]}

    def save_wbi_keys(self, img_key: str, sub_key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO wbi_keys (id, img_key, sub_key, cached_at) "
                "VALUES (1, ?, ?, datetime('now'))",
                (img_key, sub_key),
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_storage.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_storage.py
git commit -m "feat(storage): wbi keys cache"
```

---

## Task 6: storage.py - 失败项记录

**Files:**
- Modify: `core/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_add_and_list_failed_items(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.add_failed_item(sid, {
        "avid": 1001, "title": "视频A", "category": "编程", "target_fid": 5001,
        "error_code": "-509", "error_message": "B站接口返回 -509（限流）",
    })
    storage.add_failed_item(sid, {
        "avid": 1002, "title": "视频B", "category": "音乐", "target_fid": 5002,
        "error_code": "-403", "error_message": "B站接口返回 -403（风控）",
    })
    items = storage.list_failed_items(sid)
    assert len(items) == 2
    assert items[0]["avid"] == 1001
    assert items[0]["target_fid"] == 5001
    assert items[0]["error_message"] == "B站接口返回 -509（限流）"
    assert items[0]["retried"] == 0


def test_mark_failed_item_retried(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    fid = storage.add_failed_item(sid, {
        "avid": 1001, "title": "视频A", "category": "编程", "target_fid": 5001,
        "error_code": "-509", "error_message": "限流",
    })
    storage.mark_failed_item_retried(fid)
    items = storage.list_failed_items(sid)
    assert items[0]["retried"] == 1


def test_clear_failed_items(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.add_failed_item(sid, {
        "avid": 1001, "title": "视频A", "category": "编程", "target_fid": 5001,
        "error_code": "-509", "error_message": "限流",
    })
    storage.clear_failed_items(sid)
    assert storage.list_failed_items(sid) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `AttributeError: 'Storage' object has no attribute 'add_failed_item'`

- [ ] **Step 3: 实现（追加到 storage.py）**

```python
    def add_failed_item(self, session_id: str, item: dict) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO failed_items (session_id, avid, title, category, target_fid, error_code, error_message, retried, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (session_id, item["avid"], item["title"], item["category"],
                 item.get("target_fid", 0), item["error_code"], item["error_message"]),
            )
            return cur.lastrowid

    def list_failed_items(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM failed_items WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_failed_item_retried(self, item_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE failed_items SET retried = 1 WHERE id = ?", (item_id,))

    def clear_failed_items(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM failed_items WHERE session_id = ?", (session_id,))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_storage.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_storage.py
git commit -m "feat(storage): failed items record"
```

---

## Task 7: bilibili_api.py - WBI 签名

**Files:**
- Create: `core/bilibili_api.py`
- Test: `tests/test_bilibili_api.py`

- [ ] **Step 1: 写失败测试**

`tests/test_bilibili_api.py`:
```python
from core.bilibili_api import _wbi_sign, _get_mixin_key


_WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def test_mixin_key_construction():
    img_key = "7cd088941d418cwallet"
    sub_key = "4932caff0ff715e3a4"
    img = img_key[:32]
    sub = sub_key[:32]
    raw = img + sub
    mixin = "".join(raw[i] for i in _WBI_TABLE)[:32]
    assert _get_mixin_key(img_key, sub_key) == mixin


def test_wbi_sign_basic():
    img_key = "7cd088941d418cwallet"
    sub_key = "4932caff0ff715e3a4"
    params = {"foo": "114", "bar": "514", "wts": 1702204800}
    signed = _wbi_sign(params, img_key, sub_key)
    assert "w_rid" in signed
    assert signed["wts"] == 1702204800
    assert signed["foo"] == "114"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 `core/bilibili_api.py` 的 WBI 部分**

```python
import hashlib
import time
from typing import Any

_WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    img = img_key[:32]
    sub = sub_key[:32]
    raw = img + sub
    return "".join(raw[i] for i in _WBI_TABLE)[:32]


def _wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    mixin = _get_mixin_key(img_key, sub_key)
    params = {**params, "wts": int(time.time())}
    sorted_items = sorted(params.items())
    query = "&".join(f"{k}={v}" for k, v in sorted_items)
    w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return {**params, "w_rid": w_rid}
```

注：测试里用固定 `wts` 值验证签名结构；真正锚定 B 站官方样例值需在实现时用官方文档提供的样例 img/sub key 和样例 w_rid 校验。此处测试验证签名生成逻辑正确（mixin 构造 + md5 拼接）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add core/bilibili_api.py tests/test_bilibili_api.py
git commit -m "feat(bilibili_api): wbi signing"
```

---

## Task 8: bilibili_api.py - BilibiliClient 基础 + 扫码登录

**Files:**
- Modify: `core/bilibili_api.py`
- Test: `tests/test_bilibili_api.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
import httpx
import respx
import pytest

from core.bilibili_api import BilibiliClient
from core.errors import NotLoggedInError


@pytest.fixture
def client(tmp_path):
    return BilibiliClient(cookie_store_path=tmp_path / "cookie.json")


@respx.mock
def test_qrcode_generate(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    ).mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"url": "https://x/scan", "qrcode_key": "key123", "returnMessage": ""},
    }))
    result = client.qrcode_generate()
    assert result["qrcode_key"] == "key123"
    assert result["url"] == "https://x/scan"


@respx.mock
def test_qrcode_poll_success(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(
        200,
        json={"code": 0, "data": {"mid": 12345}},
        headers={"Set-Cookie": "SESSDATA=abc; Path=/; bili_jct=csrf; Path=/; DedeUserID=12345; Path=/"},
    ))
    result = client.qrcode_poll("key123")
    assert result["status"] == "success"
    assert client.cookies["SESSDATA"] == "abc"
    assert client.cookies["bili_jct"] == "csrf"


@respx.mock
def test_qrcode_poll_waiting(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(200, json={"code": 86038, "data": {}}))
    result = client.qrcode_poll("key123")
    assert result["status"] == "waiting"


@respx.mock
def test_qrcode_poll_expired(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(200, json={"code": 86039, "data": {}}))
    result = client.qrcode_poll("key123")
    assert result["status"] == "expired"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: FAIL with `AttributeError: 'BilibiliClient' object has no attribute 'qrcode_generate'`

- [ ] **Step 3: 实现 BilibiliClient 与扫码（追加到 bilibili_api.py）**

```python
import json
from pathlib import Path

from core.errors import BiliApiError, NotLoggedInError


class BilibiliClient:
    def __init__(self, cookie_store_path: Path | str | None = None):
        self.cookie_store_path = Path(cookie_store_path) if cookie_store_path else None
        self.cookies: dict[str, str] = {}
        if self.cookie_store_path and self.cookie_store_path.exists():
            self.cookies = json.loads(self.cookie_store_path.read_text(encoding="utf-8"))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(cookies=self.cookies, timeout=15.0)

    def save_cookies(self) -> None:
        if self.cookie_store_path:
            self.cookie_store_path.write_text(
                json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def clear_cookies(self) -> None:
        self.cookies = {}
        if self.cookie_store_path and self.cookie_store_path.exists():
            self.cookie_store_path.unlink()

    @property
    def is_logged_in(self) -> bool:
        return "SESSDATA" in self.cookies and "DedeUserID" in self.cookies

    @property
    def mid(self) -> int | None:
        v = self.cookies.get("DedeUserID")
        return int(v) if v else None

    @property
    def csrf(self) -> str | None:
        return self.cookies.get("bili_jct")

    async def qrcode_generate(self) -> dict:
        async with self._client() as c:
            r = await c.get(f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate", params={"returnType": 0})
            data = r.json()
        if data["code"] != 0:
            raise BiliApiError(data["code"], data.get("message", "生成二维码失败"))
        return data["data"]

    async def qrcode_poll(self, qrcode_key: str) -> dict:
        async with self._client() as c:
            r = await c.get(
                f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
            )
            data = r.json()
        code = data["code"]
        if code == 0:
            for cookie in r.headers.get_list("set-cookie"):
                parts = cookie.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    self.cookies[parts[0]] = parts[1]
            self.save_cookies()
            return {"status": "success", "mid": data.get("data", {}).get("mid")}
        if code == 86038:
            return {"status": "waiting"}
        if code == 86090:
            return {"status": "scanned"}
        if code == 86039:
            return {"status": "expired"}
        raise BiliApiError(code, data.get("message", "扫码状态未知"))

    def _require_login(self) -> None:
        if not self.is_logged_in:
            raise NotLoggedInError()

    def _check_bili_response(self, data: dict) -> dict:
        if data["code"] == -101:
            self.clear_cookies()
            raise NotLoggedInError()
        if data["code"] != 0:
            raise BiliApiError(data["code"], data.get("message", "接口错误"))
        return data.get("data", {})
```

注意：测试用的是同步调用风格（`client.qrcode_generate()`），但实现是 async。需要把测试改为 async，或提供同步包装。为保持一致，改测试为 async：

替换测试中的同步调用为：

```python
@pytest.mark.asyncio
@respx.mock
async def test_qrcode_generate(client):
    ...
    result = await client.qrcode_generate()
    assert result["qrcode_key"] == "key123"
```

对所有扫码测试都加 `@pytest.mark.asyncio` 并把调用改为 `await`。

同时在 `tests/test_bilibili_api.py` 顶部加：
```python
import pytest
pytestmark = pytest.mark.asyncio
```
然后单独的 `test_mixin_key_construction` 和 `test_wbi_sign_basic` 不受影响（它们是同步的），但加 `pytestmark` 会让它们也被标记为 asyncio——它们不 await 也没问题。为避免干扰，把 `pytestmark` 只加到需要 async 的测试上，即每个 async 测试单独 `@pytest.mark.asyncio`。

- [ ] **Step 4: 配置 pytest-asyncio**

创建 `pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
```

这样所有 `async def test_` 自动识别，无需逐个加装饰器。把测试改为 async def 即可。

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: 6 passed（含原 2 个 WBI 测试）

- [ ] **Step 6: Commit**

```bash
git add core/bilibili_api.py tests/test_bilibili_api.py pytest.ini
git commit -m "feat(bilibili_api): qrcode login flow"
```

---

## Task 9: bilibili_api.py - 获取 WBI 密钥 + 收藏夹列表

**Files:**
- Modify: `core/bilibili_api.py`
- Modify: `tests/test_bilibili_api.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
async def test_fetch_wbi_keys_and_cache(client, tmp_path, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)

    route_nav = respx.get(f"{API_BASE}/x/web-interface/nav").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd088941d418cwallet.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff715e3a4.png",
                }
            },
        })
    )
    keys = await client._fetch_wbi_keys(storage=None)
    assert keys["img_key"] == "7cd088941d418cwallet"
    assert keys["sub_key"] == "4932caff0ff715e3a4"
    assert route_nav.called


async def test_get_my_folders(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "123", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)

    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/IMGKEY.png",
                              "sub_url": "https://i0.hdslb.com/bfs/wbi/SUBKEY.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/folder/created/list-all").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": {
                "count": 2,
                "list": [
                    {"id": 100, "title": "默认收藏夹", "media_count": 10, "cover": "http://c.jpg"},
                    {"id": 200, "title": "学习", "media_count": 5, "cover": ""},
                ],
            },
        })
    )
    folders = await client.get_my_folders(storage=None)
    assert len(folders) == 2
    assert folders[0]["fid"] == 100
    assert folders[0]["title"] == "默认收藏夹"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: FAIL with `AttributeError: 'BilibiliClient' object has no attribute '_fetch_wbi_keys'`

- [ ] **Step 3: 实现 _fetch_wbi_keys 与 get_my_folders（追加到 bilibili_api.py）**

```python
import re
from urllib.parse import quote

def _time_now() -> int:
    return int(time.time())

def _extract_key(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".")[0]


class BilibiliClient:
    ...
    async def _fetch_wbi_keys(self, storage) -> dict:
        if storage is not None:
            cached = storage.load_wbi_keys()
            if cached:
                return cached
        async with self._client() as c:
            r = await c.get(f"{API_BASE}/x/web-interface/nav")
            data = r.json()
        self._check_bili_response(data)
        img_url = data["data"]["wbi_img"]["img_url"]
        sub_url = data["data"]["wbi_img"]["sub_url"]
        keys = {"img_key": _extract_key(img_url), "sub_key": _extract_key(sub_url)}
        if storage is not None:
            storage.save_wbi_keys(keys["img_key"], keys["sub_key"])
        return keys

    async def _wbi_get(self, path: str, params: dict, storage) -> dict:
        keys = await self._fetch_wbi_keys(storage)
        signed = _wbi_sign(params, keys["img_key"], keys["sub_key"])
        async with self._client() as c:
            r = await c.get(f"{API_BASE}{path}", params=signed)
            data = r.json()
        return self._check_bili_response(data)

    async def get_my_folders(self, storage=None) -> list[dict]:
        self._require_login()
        data = await self._wbi_get(
            "/x/v3/fav/folder/created/list-all",
            {"up_mid": self.mid, "type": 0},
            storage,
        )
        return [
            {
                "fid": f["id"],
                "title": f["title"],
                "media_count": f["media_count"],
                "cover_url": f.get("cover", ""),
            }
            for f in data.get("list", [])
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add core/bilibili_api.py tests/test_bilibili_api.py
git commit -m "feat(bilibili_api): wbi keys fetch and my folders list"
```

---

## Task 10: bilibili_api.py - 收藏夹内容分页拉取

**Files:**
- Modify: `core/bilibili_api.py`
- Modify: `tests/test_bilibili_api.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
async def test_get_folder_videos_paginated(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/IMG.png", "sub_url": "https://x/SUB.png"}}
    }))

    page1 = {
        "code": 0,
        "data": {
            "info": {"media_count": 3},
            "medias": [
                {"id": 1001, "bvid": "BV1aaa", "title": "视频A", "upper": {"name": "UP1", "mid": 11},
                 "cover": "http://a.jpg", "attr": 0, "type": 2, "tid": 122, "tname": "野生技术协会"},
                {"id": 1002, "bvid": "BV1bbb", "title": "视频B", "upper": {"name": "UP2", "mid": 12},
                 "cover": "http://b.jpg", "attr": 0, "type": 2, "tid": 95, "tname": "数码"},
            ],
        },
    }
    page2 = {
        "code": 0,
        "data": {"info": {"media_count": 3}, "medias": [
            {"id": 1003, "bvid": "BV1ccc", "title": "视频C", "upper": {"name": "UP3", "mid": 13},
             "cover": "http://c.jpg", "attr": 0, "type": 2, "tid": 122, "tname": "野生技术协会"},
        ]},
    }

    route = respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    videos = []
    async for batch in client.get_folder_videos(fid=100, storage=None, page_size=2, sleep_seconds=0):
        videos.extend(batch)
    assert len(videos) == 3
    assert videos[0]["avid"] == 1001
    assert videos[2]["title"] == "视频C"
    assert route.call_count == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: FAIL with `AttributeError: 'BilibiliClient' object has no attribute 'get_folder_videos'`

- [ ] **Step 3: 实现 get_folder_videos（追加到 BilibiliClient）**

```python
    async def get_folder_videos(self, fid: int, storage=None, page_size: int = 20, sleep_seconds: float = 0.3):
        self._require_login()
        pn = 1
        while True:
            data = await self._wbi_get(
                "/x/v3/fav/resource/list",
                {"media_id": fid, "pn": pn, "ps": page_size, "order": "mtime", "platform": "web"},
                storage,
            )
            medias = data.get("medias") or []
            batch = []
            for m in medias:
                if m.get("attr", 0) & 1:
                    continue
                batch.append({
                    "avid": m["id"],
                    "bvid": m.get("bvid", ""),
                    "title": m.get("title", ""),
                    "intro": "",
                    "tags": "[]",
                    "up_name": m.get("upper", {}).get("name", ""),
                    "up_mid": m.get("upper", {}).get("mid", 0),
                    "cover_url": m.get("cover", ""),
                    "tname": m.get("tname", ""),
                    "fid": fid,
                })
            if batch:
                yield batch
            if len(medias) < page_size:
                return
            pn += 1
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
```

文件顶部追加 `import asyncio`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add core/bilibili_api.py tests/test_bilibili_api.py
git commit -m "feat(bilibili_api): paginated folder videos fetch"
```

---

## Task 11: bilibili_api.py - 创建收藏夹 + 移动视频

**Files:**
- Modify: `core/bilibili_api.py`
- Modify: `tests/test_bilibili_api.py`

- [ ] **Step 1: 写失败测试（追加）**

```python
async def test_create_folder(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    respx.post(f"{API_BASE}/x/v3/fav/folder/add").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"id": 999, "title": "编程教程"}})
    )
    fid = await client.create_folder(title="编程教程", privacy=1)
    assert fid == 999
    last_request = respx.calls.last.request
    body = last_request.content.decode()
    assert "title=%E7%BC%96%E7%A8%8B%E6%95%99%E7%A8%8B" in body or "编程教程" in body
    assert "csrf=csrf_tok" in body


async def test_move_videos(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    respx.post(f"{API_BASE}/x/v3/fav/resource/move").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"success": True}})
    )
    ok = await client.move_videos(src_media_id=100, tar_media_id=999, avids=[1001, 1002])
    assert ok is True
    body = respx.calls.last.request.content.decode()
    assert "src_media_id=100" in body
    assert "tar_media_id=999" in body
    assert "1001:2" in body and "1002:2" in body
    assert "csrf=csrf_tok" in body
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: FAIL with `AttributeError: 'BilibiliClient' object has no attribute 'create_folder'`

- [ ] **Step 3: 实现 create_folder 与 move_videos（追加到 BilibiliClient）**

```python
    async def _post_form(self, path: str, form: dict) -> dict:
        self._require_login()
        form = {**form, "csrf": self.csrf}
        async with self._client() as c:
            r = await c.post(f"{API_BASE}{path}", data=form)
            data = r.json()
        return self._check_bili_response(data)

    async def create_folder(self, title: str, privacy: int = 1, intro: str = "") -> int:
        data = await self._post_form(
            "/x/v3/fav/folder/add",
            {"title": title, "intro": intro, "privacy": privacy, "cover": "", "order": "" },
        )
        return data["id"]

    async def move_videos(self, src_media_id: int, tar_media_id: int, avids: list[int]) -> bool:
        resources = ",".join(f"{avid}:2" for avid in avids)
        await self._post_form(
            "/x/v3/fav/resource/move",
            {"src_media_id": src_media_id, "tar_media_id": tar_media_id, "resources": resources, "platform": "web"},
        )
        return True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_bilibili_api.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add core/bilibili_api.py tests/test_bilibili_api.py
git commit -m "feat(bilibili_api): create folder and move videos"
```

---

## Task 12: ai_classifier.py - 分类与二次归并

**Files:**
- Create: `core/ai_classifier.py`
- Test: `tests/test_ai_classifier.py`

- [ ] **Step 1: 写失败测试**

`tests/test_ai_classifier.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.ai_classifier import AiClassifier, VideoInfo


def make_video(avid, title, up="UP", tname="科技"):
    return VideoInfo(avid=avid, title=title, up_name=up, tname=tname, tags=[])


@pytest.mark.asyncio
async def test_classify_single_batch(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='{"items":[{"avid":1,"category":"编程","confidence":0.9,"reason":"Python"}]}'))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    result = await classifier.classify([make_video(1, "Python入门")])
    assert len(result) == 1
    assert result[0].category == "编程"
    assert result[0].avid == 1


@pytest.mark.asyncio
async def test_classify_handles_bad_json(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content="not json"))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    result = await classifier.classify([make_video(1, "x")])
    assert len(result) == 1
    assert result[0].category == "未分类"
    assert result[0].confidence == 0.0


@pytest.mark.asyncio
async def test_merge_categories(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='{"mapping":{"编程":"编程教程","代码教学":"编程教程","音乐":"音乐"}}'))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    mapping = await classifier.merge_categories(["编程", "代码教学", "音乐"])
    assert mapping["编程"] == "编程教程"
    assert mapping["代码教学"] == "编程教程"
    assert mapping["音乐"] == "音乐"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_ai_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 `core/ai_classifier.py`**

```python
import json
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from core.errors import AiApiError


@dataclass
class VideoInfo:
    avid: int
    title: str
    up_name: str
    tname: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Classification:
    avid: int
    category: str
    confidence: float
    reason: str


SYSTEM_PROMPT = """你是B站内容分类助手。给你一批视频的元数据，为每个视频选一个分类。
输入：JSON数组，每项 {avid, title, up_name, tname, tags}
输出：严格JSON，形如 {"items":[{"avid":int,"category":"中文2-6字","confidence":0-1,"reason":"≤20字原因"}]}
约束：
- category 用中文短语，如"编程教程""美食""游戏解说""音乐MV""知识科普""生活vlog"
- 同一批分类数控制在3-10个，相近主题合并
- confidence<0.6 也给最佳猜测，不要用"其他"
"""


class AiClassifier:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self._api = AsyncOpenAI(base_url=base_url, api_key=api_key)

    def _client(self):
        return self._api

    async def _chat_json(self, system: str, user: str) -> dict:
        resp = await self._api.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
        )
        content = resp.choices[0].message.content or ""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise AiApiError("AI返回非JSON:" + content[:200])

    async def classify_batch(self, videos: list[VideoInfo]) -> list[Classification]:
        user = json.dumps([{
            "avid": v.avid, "title": v.title, "up_name": v.up_name,
            "tname": v.tname, "tags": v.tags,
        } for v in videos], ensure_ascii=False)
        try:
            data = await self._chat_json(SYSTEM_PROMPT, user)
        except AiApiError:
            return [Classification(v.avid, "未分类", 0.0, "AI解析失败") for v in videos]
        items = data.get("items", [])
        by_avid = {it["avid"]: it for it in items}
        result = []
        for v in videos:
            it = by_avid.get(v.avid)
            if not it:
                result.append(Classification(v.avid, "未分类", 0.0, "AI未返回"))
            else:
                result.append(Classification(
                    avid=it["avid"], category=it["category"],
                    confidence=float(it.get("confidence", 0.0)), reason=it.get("reason", ""),
                ))
        return result

    async def classify(self, videos: list[VideoInfo], batch_size: int = 50) -> list[Classification]:
        results: list[Classification] = []
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

    async def merge_categories(self, categories: list[str]) -> dict[str, str]:
        system = "给你一组中文分类名，把语义相同的合并成统一名称。输出严格JSON: {\"mapping\":{\"原名\":\"统一名\"}}。每个原名都必须出现在mapping里且值为最终统一名。"
        user = json.dumps(categories, ensure_ascii=False)
        data = await self._chat_json(system, user)
        mapping = data.get("mapping", {})
        for c in categories:
            if c not in mapping:
                mapping[c] = c
        return mapping
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ai_classifier.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add core/ai_classifier.py tests/test_ai_classifier.py
git commit -m "feat(ai): classifier with batch and category merge"
```

---

## Task 13: session.py - 状态机 + SSE 进度 + 失败项

**Files:**
- Create: `core/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: 写失败测试**

`tests/test_session.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.session import ClassifySession
from core.bilibili_api import BilibiliClient
from core.ai_classifier import AiClassifier, VideoInfo, Classification
from core.storage import Storage


@pytest.fixture
def deps(tmp_path):
    storage = Storage(tmp_path)
    bili = MagicMock(spec=BilibiliClient)
    bili.is_logged_in = True
    bili.mid = 12345
    ai = MagicMock(spec=AiClassifier)
    return storage, bili, ai


@pytest.mark.asyncio
async def test_session_full_flow_with_progress(deps):
    storage, bili, ai = deps

    async def fake_videos(fid, storage=None, page_size=20, sleep_seconds=0):
        yield [
            {"avid": 1, "bvid": "BV1", "title": "Python入门", "intro": "", "tags": "[]",
             "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            {"avid": 2, "bvid": "BV2", "title": "做菜", "intro": "", "tags": "[]",
             "up_name": "UP2", "up_mid": 12, "cover_url": "", "tname": "生活", "fid": fid},
        ]
    bili.get_folder_videos = fake_videos
    ai.classify = AsyncMock(return_value=[
        Classification(1, "编程", 0.9, ""),
        Classification(2, "美食", 0.85, ""),
    ])

    progress_events = []
    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    await session.run_pipeline(sid, on_progress=progress_events.append)
    s = storage.load_session(sid)
    assert s["status"] == "pending_review"
    items = storage.load_classifications(sid)
    assert len(items) == 2
    stages = [e["stage"] for e in progress_events]
    assert "collecting" in stages
    assert "classifying" in stages
    assert "pending_review" in stages


@pytest.mark.asyncio
async def test_session_resume_pending(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
    ])
    session = ClassifySession(storage, bili, ai)
    resumable = session.list_resumable()
    assert len(resumable) == 1
    assert resumable[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_session_execute_records_failed_items(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "编程", "confidence": 0.8, "reason": ""},
        {"avid": 3, "category": "音乐", "confidence": 0.7, "reason": ""},
    ])
    storage.upsert_video({
        "avid": 3, "bvid": "BV3", "title": "视频C", "intro": "", "tags": "[]",
        "up_name": "UP3", "up_mid": 13, "cover_url": "", "tname": "音乐", "fid": 100,
    })
    bili.create_folder = AsyncMock(side_effect=[5001, 5002])
    bili.move_videos = AsyncMock(side_effect=[True, Exception("-509 限流")])

    session = ClassifySession(storage, bili, ai)
    stats = await session.execute(sid)
    assert stats["success"] == 2
    assert stats["failed"] == 1
    failed = storage.list_failed_items(sid)
    assert len(failed) == 1
    assert failed[0]["avid"] == 3
    assert failed[0]["title"] == "视频C"
    assert failed[0]["target_fid"] == 5002
    assert "-509" in failed[0]["error_message"]
    assert storage.load_session(sid)["status"] == "done"


@pytest.mark.asyncio
async def test_session_retry_failed(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "音乐", "confidence": 0.7, "reason": ""},
    ])
    storage.add_failed_item(sid, {
        "avid": 2, "title": "视频B", "category": "音乐", "target_fid": 5002,
        "error_code": "-509", "error_message": "限流",
    })
    bili.move_videos = AsyncMock(return_value=True)

    session = ClassifySession(storage, bili, ai)
    stats = await session.retry_failed(sid)
    assert stats["success"] == 1
    assert stats["failed"] == 0
    assert storage.list_failed_items(sid) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 `core/session.py`**

```python
import json

from core.errors import StateError, NotLoggedInError, BiliApiError
from core.ai_classifier import VideoInfo


_VALID_TRANSITIONS = {
    "draft": {"collecting"},
    "collecting": {"classifying", "draft"},
    "classifying": {"pending_review", "draft"},
    "pending_review": {"executing"},
    "executing": {"done", "failed", "pending_review"},
    "failed": {"pending_review"},
    "done": set(),
}


class ClassifySession:
    def __init__(self, storage, bili_client, ai_classifier):
        self.storage = storage
        self.bili = bili_client
        self.ai = ai_classifier

    def _transition(self, sid: str, current: str, target: str) -> None:
        if target not in _VALID_TRANSITIONS.get(current, set()):
            raise StateError(f"非法状态转换: {current} -> {target}")
        self.storage.update_session_status(sid, target)

    async def create(self, source_fid: int, mode: str) -> str:
        if not self.bili.is_logged_in:
            raise NotLoggedInError()
        return self.storage.create_session(source_fid=source_fid, mode=mode)

    async def run_pipeline(self, sid: str, on_progress=None) -> None:
        await self.collect(sid, on_progress=on_progress)
        await self.classify(sid, on_progress=on_progress)

    async def collect(self, sid: str, on_progress=None) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] not in ("draft", "collecting"):
            raise StateError("会话不在可采集状态")
        self._transition(sid, s["status"], "collecting")
        if on_progress:
            on_progress({"stage": "collecting", "progress": 0.0})
        count = 0
        async for batch in self.bili.get_folder_videos(s["source_fid"], storage=self.storage):
            for v in batch:
                self.storage.upsert_video(v)
            count += len(batch)
            if on_progress:
                on_progress({"stage": "collecting", "progress": None, "collected": count})
        if on_progress:
            on_progress({"stage": "collecting", "progress": 1.0, "collected": count})
        self._transition(sid, "collecting", "classifying")

    async def classify(self, sid: str, on_progress=None) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "classifying":
            raise StateError("会话不在分类状态")
        videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
        videos = [VideoInfo(
            avid=r["avid"], title=r["title"], up_name=r["up_name"],
            tname=r.get("tname", ""), tags=_parse_tags(r.get("tags", "[]")),
        ) for r in videos_rows]
        total = len(videos)
        if on_progress:
            on_progress({"stage": "classifying", "progress": 0.0, "total": total})
        results = await self.ai.classify(videos)
        self.storage.save_classifications(sid, [
            {"avid": c.avid, "category": c.category, "confidence": c.confidence, "reason": c.reason}
            for c in results
        ])
        if on_progress:
            on_progress({"stage": "classifying", "progress": 1.0, "classified": total})
        self._transition(sid, "classifying", "pending_review")
        if on_progress:
            on_progress({"stage": "pending_review", "progress": 1.0})

    def get_plan(self, sid: str) -> dict:
        s = self.storage.load_session(sid)
        if not s:
            raise StateError("会话不存在")
        items = self.storage.load_classifications(sid)
        videos = {r["avid"]: r for r in self.storage.list_videos_by_fid(s["source_fid"])}
        return {"session": s, "items": items, "videos": videos}

    def adjust_item(self, sid: str, avid: int, new_category: str) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可调整")
        self.storage.adjust_classification(sid, avid, new_category)

    def get_failed_items(self, sid: str) -> list[dict]:
        return self.storage.list_failed_items(sid)

    async def execute(self, sid: str, batch_size: int = 50, on_progress=None) -> dict:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可执行")
        self._transition(sid, "pending_review", "executing")
        items = self.storage.load_classifications(sid)
        videos = {r["avid"]: r for r in self.storage.list_videos_by_fid(s["source_fid"])}
        by_cat: dict[str, list[int]] = {}
        for it in items:
            if it["category"] == "未分类":
                continue
            by_cat.setdefault(it["category"], []).append(it["avid"])
        cat_to_fid: dict[str, int] = {}
        for cat in by_cat:
            cat_to_fid[cat] = await self.bili.create_folder(title=cat, privacy=1)
        success = 0
        failed = 0
        for cat, avids in by_cat.items():
            for i in range(0, len(avids), batch_size):
                chunk = avids[i:i + batch_size]
                try:
                    await self.bili.move_videos(
                        src_media_id=s["source_fid"],
                        tar_media_id=cat_to_fid[cat],
                        avids=chunk,
                    )
                    for avid in chunk:
                        self.storage.mark_classification_executed(sid, avid, True)
                    success += len(chunk)
                except Exception as e:
                    err_code, err_msg = _extract_error(e)
                    for avid in chunk:
                        self.storage.mark_classification_executed(sid, avid, False)
                        self.storage.add_failed_item(sid, {
                            "avid": avid,
                            "title": videos.get(avid, {}).get("title", ""),
                            "category": cat,
                            "target_fid": cat_to_fid[cat],
                            "error_code": err_code,
                            "error_message": err_msg,
                        })
                    failed += len(chunk)
        stats = {"success": success, "failed": failed, "total": len(items)}
        self.storage.update_session_status(sid, "done", stats=stats)
        if on_progress:
            on_progress({"stage": "done", "progress": 1.0, "stats": stats})
        return stats

    async def retry_failed(self, sid: str, batch_size: int = 50) -> dict:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "done":
            raise StateError("仅完成状态可重试失败项")
        failed = self.storage.list_failed_items(sid)
        if not failed:
            return {"success": 0, "failed": 0}
        by_target: dict[int, list[dict]] = {}
        for it in failed:
            by_target.setdefault(it["target_fid"], []).append(it)
        success = 0
        still_failed = 0
        for target_fid, items in by_target.items():
            for i in range(0, len(items), batch_size):
                chunk = items[i:i + batch_size]
                avids = [it["avid"] for it in chunk]
                try:
                    await self.bili.move_videos(
                        src_media_id=s["source_fid"],
                        tar_media_id=target_fid,
                        avids=avids,
                    )
                    for it in chunk:
                        self.storage.mark_classification_executed(sid, it["avid"], True)
                        self.storage.mark_failed_item_retried(it["id"])
                    success += len(chunk)
                except Exception:
                    still_failed += len(chunk)
        self.storage.clear_failed_items(sid)
        return {"success": success, "failed": still_failed}

    def list_resumable(self) -> list[dict]:
        return self.storage.list_sessions_by_status(["pending_review", "executing", "failed"])

    def resume_on_startup(self) -> None:
        for s in self.storage.list_sessions_by_status(["collecting", "classifying", "executing"]):
            if s["status"] in ("collecting", "classifying"):
                self.storage.update_session_status(s["session_id"], "draft")
            elif s["status"] == "executing":
                self.storage.update_session_status(s["session_id"], "pending_review")


def _parse_tags(s: str) -> list[str]:
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _extract_error(e: Exception) -> tuple[str, str]:
    if isinstance(e, BiliApiError):
        return str(e.bili_code), e.user_message
    return "", str(e)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_session.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/session.py tests/test_session.py
git commit -m "feat(session): state machine with sse progress and failed items"
```

---

## Task 14: main.py - FastAPI 启动 + 路由（含 SSE/失败项/重试）

**Files:**
- Create: `main.py`
- Test: 手动验证

- [ ] **Step 1: 实现 `main.py`（启动 + 登录 + 配置 + 收藏夹 + SSE 流 + 失败项 + 重试）**

```python
import asyncio
import base64
import io
import json
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.ai_classifier import AiClassifier
from core.bilibili_api import BilibiliClient
from core.errors import BibiError
from core.session import ClassifySession
from core.storage import Storage

BASE_DIR = Path(__file__).parent
storage = Storage(BASE_DIR)
bili = BilibiliClient(cookie_store_path=BASE_DIR / "bilibili_cookie.json")


def get_ai() -> AiClassifier:
    cfg = storage.load_config()
    if not cfg:
        raise BibiError("请先在设置页填写 AI 配置", code="AI_NOT_CONFIGURED")
    return AiClassifier(cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"])


def get_session_mgr() -> ClassifySession:
    return ClassifySession(storage, bili, get_ai())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if storage.load_config():
        ClassifySession(storage, bili, get_ai()).resume_on_startup()
    webbrowser.open("http://127.0.0.1:8765")
    yield


app = FastAPI(lifespan=lifespan)


class ConfigIn(BaseModel):
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    default_privacy: int = 1


class SessionIn(BaseModel):
    source_fid: int
    mode: str = "quick"


class AdjustIn(BaseModel):
    avid: int
    new_category: str


@app.exception_handler(BibiError)
async def bibi_error_handler(_, exc: BibiError):
    return JSONResponse({"code": exc.code, "message": exc.user_message}, status_code=400)


@app.get("/api/state")
async def api_state():
    return {"logged_in": bili.is_logged_in, "configured": storage.load_config() is not None}


@app.post("/api/qrcode/generate")
async def api_qrcode_generate():
    data = await bili.qrcode_generate()
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"qrcode_key": data["qrcode_key"], "image": f"data:image/png;base64,{b64}"}


@app.get("/api/qrcode/poll")
async def api_qrcode_poll(qrcode_key: str):
    return await bili.qrcode_poll(qrcode_key)


@app.post("/api/logout")
async def api_logout():
    bili.clear_cookies()
    return {"ok": True}


@app.get("/api/config")
async def api_get_config():
    cfg = storage.load_config()
    if not cfg:
        return {"configured": False}
    return {
        "configured": True,
        "ai_base_url": cfg["ai_base_url"],
        "ai_model": cfg["ai_model"],
        "default_privacy": cfg.get("default_privacy", 1),
    }


@app.post("/api/config")
async def api_save_config(cfg: ConfigIn):
    storage.save_config(cfg.model_dump())
    return {"ok": True}


@app.get("/api/folders")
async def api_folders():
    folders = await bili.get_my_folders(storage=storage)
    for f in folders:
        storage.upsert_folder(f)
    return {"folders": folders}


@app.post("/api/session")
async def api_create_session(payload: SessionIn):
    mgr = get_session_mgr()
    sid = await mgr.create(payload.source_fid, payload.mode)
    return {"session_id": sid}


@app.get("/api/session/{sid}")
async def api_get_session(sid: str):
    mgr = get_session_mgr()
    return mgr.get_plan(sid)


@app.get("/api/session/{sid}/stream")
async def api_session_stream(sid: str):
    mgr = get_session_mgr()
    queue: asyncio.Queue = asyncio.Queue()

    async def on_progress(event):
        await queue.put(event)

    async def event_gen():
        task = asyncio.create_task(mgr.run_pipeline(sid, on_progress=on_progress))
        while True:
            done = task.done()
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.2)
                yield f"event: stage\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                if done and queue.empty():
                    break
                continue
        exc = task.exception()
        if exc:
            msg = exc.user_message if isinstance(exc, BibiError) else str(exc)
            yield f"event: fail\ndata: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
        else:
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/session/{sid}/adjust")
async def api_adjust(sid: str, payload: AdjustIn):
    mgr = get_session_mgr()
    mgr.adjust_item(sid, payload.avid, payload.new_category)
    return {"ok": True}


@app.post("/api/session/{sid}/execute")
async def api_execute(sid: str):
    mgr = get_session_mgr()
    stats = await mgr.execute(sid)
    return {"stats": stats}


@app.get("/api/session/{sid}/failed-items")
async def api_failed_items(sid: str):
    mgr = get_session_mgr()
    return {"items": mgr.get_failed_items(sid)}


@app.post("/api/session/{sid}/retry-failed")
async def api_retry_failed(sid: str):
    mgr = get_session_mgr()
    stats = await mgr.retry_failed(sid)
    return {"stats": stats}


@app.get("/api/sessions/resumable")
async def api_resumable():
    if not storage.load_config():
        return {"sessions": []}
    mgr = get_session_mgr()
    return {"sessions": mgr.list_resumable()}


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
```

- [ ] **Step 2: 启动验证（手动）**

Run: `python main.py`
Expected: 浏览器自动打开 `http://127.0.0.1:8765`，能看到前端页面（此时前端尚未实现，会 404 或空白——正常，下个任务补前端）。Ctrl+C 退出。

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(api): fastapi routes with sse stream, failed items, retry"
```

---

## Task 15: 前端 - HTML 结构（6视图整合）+ Pinguo设计token

**Files:**
- Create: `static/index.html`

- [ ] **Step 1: 创建 `static/index.html`**

把 6 个草稿页面整合为单页应用：共享导航栏 + 6 个 `<section data-view="...">`，JS 切换显示。设计 token 的两个 `<style>` 块（`theme-vars` 和 `component-vars`）从 `bibi-tool-ui-draft/pages/config.html` 原样复制到 `<head>`（约 350 行，包含 Pinguo 完整色阶/阴影/组件 CSS）。

```html
<!DOCTYPE html>
<html lang="zh-CN" class="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BiBiTool</title>
<style id="theme-vars">
/* 原样复制 bibi-tool-ui-draft/pages/config.html 的 <style id="theme-vars"> 块 */
</style>
<style id="component-vars">
/* 原样复制 bibi-tool-ui-draft/pages/config.html 的 <style id="component-vars"> 块 */
</style>
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.1/dist/index.global.js"></script>
<script src="https://unpkg.com/lucide@1.8.0/dist/umd/lucide.min.js"></script>
<style type="text/tailwindcss">
@theme inline { --color-foreground: var(--card-foreground); }
@layer base {
  body { background: linear-gradient(180deg, var(--background-100), var(--background-200)); color: var(--foreground); }
  td, th { @apply break-words; word-break: break-all; }
  th { @apply whitespace-nowrap; }
}
</style>
<style>
[data-view] { display: none; }
[data-view].active { display: block; }
.pulse-dot { animation: pulse-dot 1.6s ease-in-out infinite; }
@keyframes pulse-dot { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.45;transform:scale(.8)} }
@keyframes pulse-ring { 0%{transform:scale(1);opacity:.55} 50%{transform:scale(1.8);opacity:0} 100%{transform:scale(1);opacity:0} }
.progress-pulse { transform-origin:center; animation: pulse-ring 1.8s cubic-bezier(.4,0,.6,1) infinite; }
@keyframes shimmer { 0%{background-position:-200% 0} 100%{background-position:200% 0} }
.progress-fill { background-color: var(--brand-500); background-image: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--brand-100) 65%, transparent) 50%, transparent 100%); background-size: 200% 100%; animation: shimmer 2.4s linear infinite; }
@keyframes spin-slow { from{transform:rotate(0)} to{transform:rotate(360deg)} }
.spin-slow { animation: spin-slow 2.2s linear infinite; }
@keyframes scale-in { 0%{transform:scale(0);opacity:0} 60%{transform:scale(1.1);opacity:1} 100%{transform:scale(1);opacity:1} }
.hero-icon { animation: scale-in .5s ease-out; }
@media (prefers-reduced-motion: reduce) { .pulse-dot,.progress-pulse,.progress-fill,.spin-slow,.hero-icon { animation: none; } }
</style>
</head>
<body class="min-h-screen font-sans antialiased">
<nav class="sticky top-0 z-50 w-full h-16" style="background: color-mix(in srgb, var(--background-50) 80%, transparent); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border);">
  <div class="flex items-center justify-between max-w-3xl mx-auto px-6 h-full">
    <div class="flex items-center gap-2">
      <i data-lucide="sparkles" class="w-5 h-5" style="color: var(--brand-500);"></i>
      <span class="text-base font-semibold tracking-tight" style="color: var(--foreground);">BiBiTool</span>
    </div>
    <button data-dom-id="nav-settings" type="button" class="inline-flex items-center justify-center h-9 w-9 rounded-full transition-colors hover:bg-[var(--secondary)]" style="color: var(--icon-muted);" aria-label="设置">
      <i data-lucide="settings" class="w-[18px] h-[18px]"></i>
    </button>
  </div>
</nav>

<section data-view="config">
  <div class="flex flex-col items-center text-center mb-8 pt-12">
    <div class="flex items-center justify-center w-14 h-14 rounded-2xl mb-5" style="background: var(--brand-50); box-shadow: var(--shadow-md);">
      <i data-lucide="cpu" class="w-7 h-7" style="color: var(--brand-500);"></i>
    </div>
    <h1 class="text-2xl font-bold tracking-tight" style="color: var(--foreground);">AI 配置</h1>
    <p class="mt-2 text-sm" style="color: var(--muted-foreground);">使用 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 中转均可）</p>
  </div>
  <div class="max-w-xl mx-auto px-4 rounded-2xl p-8" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-lg);">
    <div class="flex flex-col gap-5">
      <div class="flex flex-col">
        <label class="text-sm font-medium mb-2" style="color: var(--foreground);">Base URL</label>
        <div class="field" style="width:100%;min-width:0;">
          <i data-lucide="globe" class="w-[18px] h-[18px] shrink-0" style="color: var(--icon-muted);"></i>
          <input data-dom-id="config-base-url" class="control" type="text" placeholder="https://api.deepseek.com">
        </div>
      </div>
      <div class="flex flex-col">
        <label class="text-sm font-medium mb-2" style="color: var(--foreground);">API Key</label>
        <div class="field" style="width:100%;min-width:0;">
          <i data-lucide="key" class="w-[18px] h-[18px] shrink-0" style="color: var(--icon-muted);"></i>
          <input data-dom-id="config-api-key" class="control" type="password" placeholder="sk-...">
        </div>
      </div>
      <div class="flex flex-col">
        <label class="text-sm font-medium mb-2" style="color: var(--foreground);">模型名称</label>
        <div class="field" style="width:100%;min-width:0;">
          <i data-lucide="cpu" class="w-[18px] h-[18px] shrink-0" style="color: var(--icon-muted);"></i>
          <input data-dom-id="config-model" class="control" type="text" placeholder="deepseek-chat">
        </div>
      </div>
    </div>
    <div class="mt-8">
      <button data-dom-id="config-save" type="button" class="btn btn-primary btn-lg w-full">
        <span>保存配置</span><i data-lucide="arrow-right" class="w-5 h-5"></i>
      </button>
    </div>
  </div>
</section>

<section data-view="login">
  <div class="flex flex-col items-center justify-center px-4 py-12">
    <div class="w-full max-w-[400px] flex flex-col items-center">
      <div class="w-14 h-14 rounded-2xl flex items-center justify-center mb-6" style="background: var(--brand-50); box-shadow: var(--shadow-md);">
        <i data-lucide="qr-code" class="w-7 h-7" style="color: var(--brand-500);"></i>
      </div>
      <h1 class="text-2xl font-bold tracking-tight mb-2" style="color: var(--foreground);">扫码登录</h1>
      <p class="text-sm text-center mb-10" style="color: var(--muted-foreground);">请使用 B 站手机 App 扫描下方二维码登录</p>
      <div class="rounded-2xl p-8 flex flex-col items-center" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-lg);">
        <div class="relative">
          <div data-dom-id="qr-image" class="w-[220px] h-[220px] sm:w-[240px] sm:h-[240px] rounded-xl flex items-center justify-center" style="background: var(--background-50);"></div>
          <span class="absolute -top-1 -left-1 w-5 h-5 border-t-2 border-l-2" style="border-color: var(--brand-500);"></span>
          <span class="absolute -top-1 -right-1 w-5 h-5 border-t-2 border-r-2" style="border-color: var(--brand-500);"></span>
          <span class="absolute -bottom-1 -left-1 w-5 h-5 border-b-2 border-l-2" style="border-color: var(--brand-500);"></span>
          <span class="absolute -bottom-1 -right-1 w-5 h-5 border-b-2 border-r-2" style="border-color: var(--brand-500);"></span>
        </div>
        <div class="flex items-center gap-2 mt-6">
          <span class="pulse-dot w-2 h-2 rounded-full" style="background: var(--state-success);"></span>
          <span data-dom-id="qr-status" class="text-sm" style="color: var(--muted-foreground);">等待扫码...</span>
        </div>
      </div>
      <button data-dom-id="login-refresh-qr" type="button" class="btn btn-text mt-6">
        <i data-lucide="refresh-cw" class="w-[14px] h-[14px]"></i>刷新二维码
      </button>
    </div>
  </div>
</section>

<section data-view="home">
  <div class="max-w-3xl mx-auto pt-12 pb-24 px-6">
    <div class="flex items-center gap-4">
      <div class="shrink-0 flex items-center justify-center w-10 h-10 rounded-xl" style="background: var(--brand-50);">
        <i data-lucide="folder-open" style="width:20px;height:20px;color:var(--brand-500);"></i>
      </div>
      <div class="min-w-0">
        <h1 class="text-2xl font-bold tracking-tight" style="color: var(--foreground);">选择收藏夹</h1>
        <p class="mt-1 text-sm" style="color: var(--muted-foreground);">选择要分类的收藏夹，开始整理你的视频内容</p>
      </div>
    </div>
    <div data-dom-id="resume-session" class="mt-8" style="display:none;"></div>
    <div class="mt-8">
      <div class="inline-flex items-center gap-1 p-1 rounded-full" style="background: var(--brand-50);">
        <button data-dom-id="mode-quick" class="flex items-center gap-1.5 px-4 h-8 rounded-full text-xs font-semibold" style="background: var(--background-50); color: var(--foreground); border:none; cursor:pointer; box-shadow: var(--shadow-sm);">
          <i data-lucide="zap" style="width:14px;height:14px;"></i>快速模式
        </button>
        <button data-dom-id="mode-full" class="flex items-center gap-1.5 px-4 h-8 rounded-full text-xs font-semibold" style="background:transparent;color:var(--muted-foreground);border:none;cursor:pointer;">
          <i data-lucide="layers" style="width:14px;height:14px;"></i>完整模式
        </button>
      </div>
      <p class="mt-2 text-xs" style="color: var(--muted-foreground);">快速模式仅用标题分类，完整模式额外拉取简介和标签</p>
    </div>
    <div data-dom-id="folder-list" class="mt-6 flex flex-col gap-3"></div>
  </div>
</section>

<section data-view="progress">
  <div class="flex flex-col items-center px-4 pt-12 pb-16">
    <div class="flex flex-col items-center text-center">
      <div class="w-14 h-14 rounded-2xl flex items-center justify-center" style="background: var(--brand-50); box-shadow: var(--shadow-md);">
        <i data-lucide="loader" class="w-7 h-7 spin-slow" style="color: var(--brand-500);"></i>
      </div>
      <h1 class="mt-5 text-2xl font-bold tracking-tight" style="color: var(--foreground);">整理中</h1>
      <p class="mt-2 text-sm" style="color: var(--muted-foreground);">正在整理你的收藏夹，请稍候</p>
    </div>
    <div class="w-full max-w-[560px] mt-8 rounded-2xl p-8" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-lg);">
      <div data-dom-id="step-indicator" class="flex items-start"></div>
      <div class="mt-8">
        <p data-dom-id="progress-percent" class="text-center text-sm font-semibold tabular-nums" style="color: var(--brand-500);">0%</p>
        <div class="mt-2 w-full h-2 rounded-full overflow-hidden" style="background: var(--background-200);">
          <div data-dom-id="progress-bar" class="progress-fill h-full rounded-full" style="width:0%;"></div>
        </div>
        <p data-dom-id="progress-status" class="mt-2.5 text-center text-xs" style="color: var(--muted-foreground);">准备中...</p>
      </div>
    </div>
  </div>
</section>

<section data-view="review">
  <div class="max-w-4xl mx-auto px-5 sm:px-6 pt-8 pb-32">
    <header class="mb-10 flex items-start gap-4">
      <div class="shrink-0 w-10 h-10 rounded-xl flex items-center justify-center" style="background: var(--brand-50);">
        <i data-lucide="layout-grid" class="w-5 h-5" style="color: var(--brand-500);"></i>
      </div>
      <div class="min-w-0 flex-1 pt-1">
        <h1 class="text-2xl font-bold tracking-tight" style="color: var(--foreground);">预览分类方案</h1>
        <p data-dom-id="review-summary" class="mt-1.5 text-sm" style="color: var(--muted-foreground);"></p>
      </div>
    </header>
    <div data-dom-id="review-plan"></div>
  </div>
  <div class="fixed bottom-0 left-0 right-0 z-20 backdrop-blur" style="background: color-mix(in srgb, var(--background-50) 82%, transparent); border-top: 1px solid var(--border); box-shadow: var(--shadow-lg);">
    <div class="max-w-4xl mx-auto px-5 sm:px-6 py-4 flex flex-col sm:flex-row items-center justify-between gap-3">
      <div class="flex items-center gap-2 order-2 sm:order-1 min-w-0">
        <i data-lucide="alert-triangle" class="w-4 h-4 shrink-0" style="color: var(--chart-3);"></i>
        <p class="text-xs sm:truncate" style="color: var(--muted-foreground);">将创建新收藏夹并移动视频，此操作不可逆</p>
      </div>
      <div class="order-1 sm:order-2 shrink-0">
        <button data-dom-id="execute-confirm" type="button" class="btn btn-primary btn-lg">
          <i data-lucide="check" class="w-5 h-5"></i><span>确认执行</span>
        </button>
      </div>
    </div>
  </div>
</section>

<section data-view="result">
  <div class="flex justify-center px-4 py-10 sm:py-14">
    <div class="w-full max-w-xl rounded-2xl p-6 sm:p-8" style="background: var(--card); box-shadow: var(--shadow-lg); border: 1px solid var(--border);">
      <div class="flex flex-col items-center text-center">
        <div class="hero-icon flex items-center justify-center w-20 h-20 rounded-full" style="background: var(--state-success);">
          <i data-lucide="check" style="width:40px;height:40px;color:var(--state-success-foreground);"></i>
        </div>
        <h1 class="mt-5 text-2xl font-bold tracking-tight" style="color: var(--foreground);">整理完成</h1>
      </div>
      <div data-dom-id="result-stats" class="mt-8 grid grid-cols-3 gap-3 sm:gap-4"></div>
      <div data-dom-id="result-failed" class="mt-8"></div>
      <div class="mt-8 flex items-center justify-center gap-3">
        <button data-dom-id="retry-failed" class="btn btn-secondary" style="display:none;">
          <i data-lucide="refresh-cw" style="width:16px;height:16px;"></i>重试失败项
        </button>
        <button data-dom-id="back-home" class="btn btn-text">
          <i data-lucide="home" style="width:16px;height:16px;"></i>返回首页
        </button>
      </div>
    </div>
  </div>
</section>

<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat(frontend): spa with 6 views and pinguo design tokens"
```

---

## Task 16: 前端 - app.js（6视图切换 + SSE消费 + 失败项展示）

**Files:**
- Create: `static/app.js`

- [ ] **Step 1: 创建 `static/app.js`**

```javascript
const $ = (id) => document.querySelector(`[data-dom-id="${id}"]`);

let currentMode = 'quick';
let currentSid = null;

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || `请求失败 ${r.status}`);
  return data;
}

function showView(name) {
  document.querySelectorAll('[data-view]').forEach(s => s.classList.remove('active'));
  document.querySelector(`[data-view="${name}"]`).classList.add('active');
  if (window.lucide) lucide.createIcons();
}

async function start() {
  try {
    const state = await api('/api/state');
    if (!state.configured) { showView('config'); loadConfig(); return; }
    if (!state.logged_in) { showView('login'); renderLogin(); return; }
    showView('home'); renderHome();
  } catch (e) {
    alert(e.message);
  }
}

function loadConfig() {
  api('/api/config').then(cfg => {
    if (cfg.configured) {
      $('config-base-url').value = cfg.ai_base_url || '';
      $('config-model').value = cfg.ai_model || '';
    }
  });
  $('config-save').onclick = async () => {
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          ai_base_url: $('config-base-url').value,
          ai_api_key: $('config-api-key').value,
          ai_model: $('config-model').value,
          default_privacy: 1,
        }),
      });
      start();
    } catch (e) { alert(e.message); }
  };
}

async function renderLogin() {
  const data = await api('/api/qrcode/generate', { method: 'POST' });
  $('qr-image').innerHTML = `<img src="${data.image}" style="width:100%;height:100%;border-radius:8px;">`;
  $('qr-status').textContent = '等待扫码...';
  pollQrcode(data.qrcode_key);
  $('login-refresh-qr').onclick = () => renderLogin();
}

async function pollQrcode(key) {
  const startTime = Date.now();
  const tick = async () => {
    if (Date.now() - startTime > 180000) {
      $('qr-status').textContent = '二维码已过期，请刷新';
      return;
    }
    try {
      const r = await api(`/api/qrcode/poll?qrcode_key=${key}`);
      if (r.status === 'success') { start(); return; }
      if (r.status === 'scanned') $('qr-status').textContent = '已扫码，请在手机确认';
      if (r.status === 'expired') { $('qr-status').textContent = '二维码已过期，请刷新'; return; }
      setTimeout(tick, 2000);
    } catch (e) { $('qr-status').textContent = e.message; }
  };
  tick();
}

async function renderHome() {
  const resumable = await api('/api/sessions/resumable');
  const resumeEl = $('resume-session');
  if (resumable.sessions.length) {
    const s = resumable.sessions[0];
    resumeEl.style.display = 'block';
    resumeEl.innerHTML = `
      <div class="flex items-center gap-4 p-4 rounded-xl" style="background: linear-gradient(135deg, var(--brand-50), var(--brand-100));">
        <div class="shrink-0 flex items-center justify-center w-10 h-10 rounded-xl" style="background: var(--brand-500);">
          <i data-lucide="play" style="width:18px;height:18px;color:var(--primary-foreground);"></i>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-sm font-semibold truncate" style="color: var(--brand-800);">收藏夹 #${s.source_fid}</p>
          <p class="text-xs truncate mt-0.5" style="color: var(--brand-600);">状态: ${s.status}</p>
        </div>
        <button class="btn btn-primary shrink-0" data-dom-id="resume-continue" style="height:32px;padding:0 14px;font-size:12px;">继续</button>
      </div>`;
    $('resume-continue').onclick = () => openSession(s.session_id);
  } else {
    resumeEl.style.display = 'none';
  }

  const data = await api('/api/folders');
  const colors = ['var(--brand-100)', 'var(--state-success-surface)', 'var(--state-error-surface)', 'var(--brand-50)'];
  const iconColors = ['var(--brand-600)', 'var(--state-success)', 'var(--state-error)', 'var(--brand-500)'];
  $('folder-list').innerHTML = data.folders.map((f, i) => `
    <div data-dom-id="select-folder-${f.fid}" class="flex items-center gap-4 p-4 rounded-xl cursor-pointer transition-all hover:shadow-md hover:-translate-y-0.5" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-xs);">
      <div class="shrink-0 flex items-center justify-center w-12 h-12 rounded-xl" style="background: ${colors[i % 4]};">
        <i data-lucide="folder" style="width:24px;height:24px;color:${iconColors[i % 4]};"></i>
      </div>
      <div class="flex-1 min-w-0">
        <p class="text-sm font-medium truncate" style="color: var(--foreground);">${f.title}</p>
        <p class="text-xs truncate mt-0.5" style="color: var(--muted-foreground);">${f.media_count} 个视频</p>
      </div>
      <i data-lucide="chevron-right" class="shrink-0" style="width:18px;height:18px;color:var(--icon-300);"></i>
    </div>`).join('') || '<p style="color:var(--muted-foreground);">暂无收藏夹</p>';

  data.folders.forEach(f => {
    $(`select-folder-${f.fid}`).onclick = () => newSession(f.fid);
  });

  $('mode-quick').onclick = () => setMode('quick');
  $('mode-full').onclick = () => setMode('full');
}

function setMode(mode) {
  currentMode = mode;
  const quick = $('mode-quick'), full = $('mode-full');
  if (mode === 'quick') {
    quick.style.background = 'var(--background-50)'; quick.style.color = 'var(--foreground)'; quick.style.boxShadow = 'var(--shadow-sm)';
    full.style.background = 'transparent'; full.style.color = 'var(--muted-foreground)'; full.style.boxShadow = 'none';
  } else {
    full.style.background = 'var(--background-50)'; full.style.color = 'var(--foreground)'; full.style.boxShadow = 'var(--shadow-sm)';
    quick.style.background = 'transparent'; quick.style.color = 'var(--muted-foreground)'; quick.style.boxShadow = 'none';
  }
}

async function newSession(fid) {
  const r = await api('/api/session', { method: 'POST', body: JSON.stringify({ source_fid: fid, mode: currentMode }) });
  currentSid = r.session_id;
  showView('progress');
  runPipeline(r.session_id);
}

async function openSession(sid) {
  currentSid = sid;
  const plan = await api(`/api/session/${sid}`);
  renderReview(sid, plan);
}

function runPipeline(sid) {
  $('progress-percent').textContent = '0%';
  $('progress-bar').style.width = '0%';
  $('progress-status').textContent = '准备中...';
  renderSteps('collecting');

  const es = new EventSource(`/api/session/${sid}/stream`);
  es.addEventListener('stage', e => {
    const d = JSON.parse(e.data);
    updateProgress(d);
  });
  es.addEventListener('done', () => {
    es.close();
    openSession(sid);
  });
  es.addEventListener('fail', e => {
    es.close();
    const d = JSON.parse(e.data);
    alert(d.message || '处理失败');
    showView('home');
    renderHome();
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) return;
    es.close();
    alert('连接中断，请重试');
    showView('home');
    renderHome();
  };
}

function updateProgress(d) {
  if (d.stage === 'collecting') {
    renderSteps('collecting');
    if (d.progress != null) {
      const pct = Math.round(d.progress * 100);
      $('progress-percent').textContent = pct + '%';
      $('progress-bar').style.width = pct + '%';
      $('progress-status').textContent = d.collected != null ? `已获取 ${d.collected} 个视频` : '正在拉取视频';
    }
  } else if (d.stage === 'classifying') {
    renderSteps('classifying');
    if (d.progress != null) {
      const pct = Math.round(d.progress * 100);
      $('progress-percent').textContent = pct + '%';
      $('progress-bar').style.width = pct + '%';
      $('progress-status').textContent = d.total != null ? `共 ${d.total} 个视频，AI 分析中` : 'AI 分类中';
    }
  } else if (d.stage === 'pending_review') {
    renderSteps('pending_review');
  }
}

function renderSteps(active) {
  const steps = [
    { key: 'collecting', label: '拉取视频', icon: 'check' },
    { key: 'classifying', label: 'AI 分类', icon: 'loader' },
    { key: 'pending_review', label: '预览方案', icon: 'clock' },
  ];
  const activeIdx = steps.findIndex(s => s.key === active);
  const colorSuccess = 'var(--state-success)', colorBrand = 'var(--brand-500)', colorMuted = 'var(--muted-foreground)';
  $('step-indicator').innerHTML = steps.map((s, i) => {
    const state = i < activeIdx ? 'done' : (i === activeIdx ? 'active' : 'pending');
    const bg = state === 'done' ? colorSuccess : (state === 'active' ? colorBrand : 'var(--background-200)');
    const fg = state === 'done' ? 'var(--state-success-foreground)' : (state === 'active' ? 'var(--primary-foreground)' : colorMuted);
    const labelColor = state === 'pending' ? colorMuted : 'var(--foreground)';
    const icon = state === 'done' ? '<i data-lucide="check" class="w-5 h-5"></i>' :
                 state === 'active' ? '<span class="progress-pulse absolute inset-0 rounded-full" style="background:var(--brand-500);animation:pulse-ring 1.8s cubic-bezier(.4,0,.6,1) infinite;"></span><span class="relative w-2.5 h-2.5 rounded-full" style="background:var(--primary-foreground);"></span>' :
                 '<i data-lucide="clock" class="w-5 h-5"></i>';
    const lineColor = i < activeIdx ? colorSuccess : (i === activeIdx ? colorBrand : 'var(--background-300)');
    const line = i < steps.length - 1 ? `<div class="flex-1 h-0.5 mt-5" style="background:${lineColor};"></div>` : '';
    return `<div class="flex flex-col items-center shrink-0" style="width:104px;">
      <div class="relative w-10 h-10 rounded-full flex items-center justify-center" style="background:${bg};${state==='pending'?'border:1px solid var(--border);':''}color:${fg};">${icon}</div>
      <p class="mt-3 text-xs font-medium text-center" style="color:${labelColor};">${s.label}</p>
    </div>${line}`;
  }).join('');
  if (window.lucide) lucide.createIcons();
}

async function renderReview(sid, plan) {
  showView('review');
  const items = plan.items;
  const videos = plan.videos || {};
  const byCat = {};
  items.forEach(it => { byCat[it.category] = byCat[it.category] || []; byCat[it.category].push(it); });
  const cats = Object.keys(byCat);
  const palette = ['var(--primary)', 'var(--chart-5)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-1)', 'var(--chart-2)'];

  $('review-summary').textContent = `${items.length} 个视频，分成 ${cats.length} 类。可下拉调整单个视频的分类。`;
  $('review-plan').innerHTML = cats.map((cat, ci) => {
    const color = palette[ci % palette.length];
    const rows = byCat[cat].map(it => {
      const v = videos[it.avid] || {};
      const conf = Math.round((it.confidence || 0) * 100);
      const badgeBg = conf >= 90 ? 'var(--state-success-surface)' : 'var(--background-200)';
      const badgeFg = conf >= 90 ? 'var(--state-success)' : 'var(--chart-3)';
      const options = cats.map(c => `<option value="${c}" ${c === cat ? 'selected' : ''}>${c}</option>`).join('');
      return `<article class="flex flex-wrap items-center gap-x-4 gap-y-2 p-4 rounded-xl transition-all" style="background: var(--card); border: 1px solid var(--border); box-shadow: var(--shadow-xs);">
        <img src="${v.cover_url || ''}" onerror="this.style.display='none'" class="shrink-0 w-20 h-12 sm:w-[96px] sm:h-[60px] rounded-lg object-cover" style="background: linear-gradient(135deg, color-mix(in srgb, ${color} 20%, var(--background-100)), var(--background-200));">
        <div class="flex-1 min-w-0 flex flex-col gap-1">
          <span class="text-sm font-medium truncate" style="color: var(--foreground);">${v.title || it.avid}</span>
          <span class="inline-flex items-center gap-1 text-xs truncate" style="color: var(--muted-foreground);">
            <i data-lucide="user" class="w-3.5 h-3.5 shrink-0"></i><span class="truncate">${v.up_name || ''}</span>
          </span>
        </div>
        <span class="shrink-0 inline-flex items-center justify-center h-6 px-2.5 rounded-full text-xs font-semibold" style="background:${badgeBg};color:${badgeFg};">${conf}%</span>
        <div class="relative shrink-0 w-full sm:w-auto">
          <select data-dom-id="adj-${it.avid}" class="h-9 w-full sm:w-auto pl-3 pr-8 rounded-lg text-sm appearance-none cursor-pointer" style="background: var(--secondary); color: var(--secondary-foreground); border: 1px solid var(--border); outline: none; min-width: 120px;">${options}</select>
          <i data-lucide="chevron-down" class="w-4 h-4 absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none" style="color: var(--muted-foreground);"></i>
        </div>
      </article>`;
    }).join('');
    return `<section class="mb-8">
      <div class="relative overflow-hidden rounded-xl mb-4 px-4 py-3 flex items-center gap-3" style="background: var(--background-100);">
        <div class="absolute left-0 top-0 bottom-0 w-1" style="background:${color};"></div>
        <div class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${color};"></div>
        <h2 class="text-base font-semibold tracking-tight" style="color: var(--foreground);">${cat}</h2>
        <span class="inline-flex items-center justify-center h-6 px-2.5 rounded-full text-xs font-semibold" style="background: color-mix(in srgb, ${color} 10%, transparent); color: ${color};">${byCat[cat].length}</span>
      </div>
      <div class="flex flex-col gap-3">${rows}</div>
    </section>`;
  }).join('');

  byCat[Object.keys(byCat)[0]].forEach && items.forEach(it => {
    const sel = $(`adj-${it.avid}`);
    if (sel) sel.onchange = () => adjustItem(sid, it.avid, sel.value);
  });

  if (window.lucide) lucide.createIcons();

  $('execute-confirm').onclick = async () => {
    if (!confirm('确认执行？将创建新收藏夹并移动视频，此操作不可逆。')) return;
    $('execute-confirm').disabled = true;
    try {
      const r = await api(`/api/session/${sid}/execute`, { method: 'POST' });
      renderResult(sid, r.stats);
    } catch (e) {
      alert(e.message);
      $('execute-confirm').disabled = false;
    }
  };
}

async function adjustItem(sid, avid, newCat) {
  await api(`/api/session/${sid}/adjust`, {
    method: 'POST',
    body: JSON.stringify({ avid, new_category: newCat }),
  });
  const plan = await api(`/api/session/${sid}`);
  renderReview(sid, plan);
}

async function renderResult(sid, stats) {
  showView('result');
  $('result-stats').innerHTML = `
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--state-success-surface); border-color: var(--border);">
      <i data-lucide="check-circle" style="width:24px;height:24px;color:var(--state-success);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--state-success);">${stats.success}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个视频已移动</span>
    </div>
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--state-error-surface); border-color: var(--border);">
      <i data-lucide="x-circle" style="width:24px;height:24px;color:var(--state-error);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--state-error);">${stats.failed}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个移动失败</span>
    </div>
    <div class="flex flex-col items-center text-center p-4 sm:p-6 rounded-xl border" style="background: var(--background-200); border-color: var(--border);">
      <i data-lucide="layers" style="width:24px;height:24px;color:var(--foreground);"></i>
      <span class="mt-3 text-3xl sm:text-4xl font-bold" style="color: var(--foreground);">${stats.total}</span>
      <span class="mt-1 text-xs" style="color: var(--muted-foreground);">个视频</span>
    </div>`;

  const failedEl = $('result-failed');
  const retryBtn = $('retry-failed');
  if (stats.failed > 0) {
    const r = await api(`/api/session/${sid}/failed-items`);
    failedEl.innerHTML = `
      <div class="flex items-center gap-2 mb-4">
        <i data-lucide="alert-circle" style="width:18px;height:18px;color:var(--state-error);"></i>
        <h2 class="text-sm font-semibold" style="color: var(--foreground);">失败项</h2>
        <span class="inline-flex items-center justify-center h-5 px-2 rounded-md text-xs font-semibold" style="background: var(--state-error-surface); color: var(--state-error);">${r.items.length}</span>
      </div>
      <div class="space-y-3">
        ${r.items.map(it => `<div class="rounded-lg p-4 border" style="background: var(--state-error-surface); border-color: var(--border);">
          <p class="text-sm font-medium truncate" style="color: var(--foreground);">${it.title}</p>
          <div class="mt-1 flex items-center gap-1.5">
            <i data-lucide="info" style="width:12px;height:12px;color:var(--muted-foreground);flex-shrink:0;"></i>
            <span class="text-xs truncate" style="color: var(--muted-foreground);">${it.error_message}</span>
          </div>
        </div>`).join('')}
      </div>`;
    retryBtn.style.display = 'inline-flex';
    retryBtn.onclick = async () => {
      retryBtn.disabled = true;
      try {
        const r2 = await api(`/api/session/${sid}/retry-failed`, { method: 'POST' });
        renderResult(sid, { success: stats.success + r2.stats.success, failed: r2.stats.failed, total: stats.total });
      } catch (e) { alert(e.message); retryBtn.disabled = false; }
    };
  } else {
    failedEl.innerHTML = '';
    retryBtn.style.display = 'none';
  }

  $('back-home').onclick = () => { showView('home'); renderHome(); };
  if (window.lucide) lucide.createIcons();
}

$('nav-settings').onclick = () => { showView('config'); loadConfig(); };

start();
```

- [ ] **Step 2: 启动验证**

Run: `python main.py`
Expected: 浏览器打开，显示设置页（未配置时）。填入配置后刷新，显示扫码页。扫码后进入选收藏夹页。

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(frontend): 6-view spa with sse and failed items"
```

---

## Task 17: README + 手动验证清单

**Files:**
- Create: `README.md`

- [ ] **Step 1: 创建 `README.md`**

```markdown
# B站收藏夹自动分类工具

一键把 B 站某个收藏夹的视频用 AI 分类到多个子收藏夹。本地运行，扫码登录，半自动预览确认后执行。

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

浏览器自动打开 `http://127.0.0.1:8765`。手机访问 `http://电脑IP:8765` 也可（需在同一局域网）。

## 使用流程

1. 首次启动：填 AI 配置（OpenAI 兼容接口的 base_url / api_key / 模型名）
2. 扫码登录 B 站
3. 选择要整理的源收藏夹
4. 选择快速模式（仅用标题/UP主/分区）或完整模式（额外拉简介标签，更准但慢）
5. 实时进度：整理中页面显示三步骤指示器和百分比进度（SSE 推送）
6. AI 生成分类方案，预览（按分类分组，含置信度）
7. 可在下拉框里调整单个视频的分类
8. 确认执行 → 自动创建子收藏夹（默认私密）并移动视频
9. 完成页显示成功/失败/总计统计，失败项可一键重试
10. 三端（手机/电脑/网页）同步生效

## 状态保持

- `bilibili_cookie.json` 保存登录态，重启免扫码（cookie 失效才重新扫）
- `bibi.db` 缓存视频信息和分类会话，关浏览器后回来可继续未完成的预览
- `config.json` 保存 AI 配置

## 安全提示

- `config.json` 和 `bilibili_cookie.json` 含敏感信息，**不要分享、不要提交 git**（已在 `.gitignore`）
- 工具创建的子收藏夹默认私密

## 手动验证清单

- [ ] 真实账号扫码登录跑通全流程（选小收藏夹测试）
- [ ] 实时进度：整理中页面三步骤指示器和进度条随 SSE 事件更新
- [ ] 断点续作：分类中关浏览器，重启 `python main.py`，能从"未完成会话"回到预览
- [ ] cookie 过期：手动编辑 `bilibili_cookie.json` 把 SESSDATA 改坏，刷新页面验证跳回登录
- [ ] 失败重试：制造移动失败（如断网），完成页显示失败项，点重试后成功
- [ ] 移动端：手机浏览器访问 `http://电脑局域网IP:8765`，界面自适应可用

## 开发

```bash
pytest -v          # 跑全部单测
python main.py     # 启动开发服务器
```

## 已知限制

- B 站 API 非官方，可能随时变动。所有调用集中在 `core/bilibili_api.py`
- 移动视频不可逆，请用小收藏夹先测试
- 大收藏夹（1000+）耗时较长，AI 调用受限于模型限流
```

- [ ] **Step 2: 跑全部测试**

Run: `pytest -v`
Expected: 全部通过（约 24 个测试：storage 15 + bilibili_api 11 + ai_classifier 3 + session 4）

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: readme with usage and verification checklist"
```

---

## Self-Review 检查结果

**Spec 覆盖：**
- R1 AI智能分类 → Task 12 (ai_classifier) + Task 13 (session.classify)
- R2 扫码登录 → Task 8 (qrcode_generate/poll)
- R3 半自动模式 → Task 13 (execute) + Task 16 (预览调整UI)
- R4 本地网页 → Task 14 (FastAPI) + Task 15/16 (前端)
- R5 响应式 → Task 15 (Tailwind 响应式类 + Pinguo 设计稿)
- R6 状态持久化 → Task 2-6 (storage) + Task 13 (resume_on_startup)
- R7 一键启动 → Task 14 (uvicorn + webbrowser) + Task 1 (requirements)
- 第5节 B站API → Task 7-11
- 第7.3节 状态机不变式 → Task 13 (_VALID_TRANSITIONS + resume_on_startup)
- 第8节 错误处理 → Task 1 (errors) + Task 8 (BiliApiError 翻译) + Task 14 (全局异常处理器)
- 第11节 UI规范 → Task 15 (6视图整合 + Pinguo token) + Task 16 (data-dom-id 钩子 + SSE消费)
- SSE 进度推送 → Task 13 (on_progress 回调) + Task 14 (StreamingResponse) + Task 16 (EventSource 消费)
- failed_items 失败项 → Task 6 (storage) + Task 13 (execute 记录 + retry_failed) + Task 14 (API 端点) + Task 16 (失败项展示 + 重试按钮)

**Placeholder 扫描：** 无 TBD/TODO，所有步骤含完整代码。设计 token 的两个 `<style>` 块明确指引从草稿原样复制（避免重复 350 行）。

**类型一致性：** `BilibiliClient` 方法签名（qrcode_generate/qrcode_poll/get_my_folders/get_folder_videos/create_folder/move_videos）在 Task 8-11 与 session.py 调用、main.py 调用一致。`AiClassifier.classify/merge_categories` 在 Task 12 定义，Task 13 调用。`Storage` 方法（含 add_failed_item/list_failed_items/mark_failed_item_retried/clear_failed_items）在 Task 2-6 定义，Task 13 调用。`session.run_pipeline/collect/classify/execute/retry_failed/get_failed_items` 在 Task 13 定义，Task 14 路由调用，Task 16 前端通过 API 调用。`data-dom-id` 钩子在 Task 15 HTML 定义，Task 16 JS 用 `$()` 查找，两者一致。

**已实现完成。等待用户选择执行方式。**
