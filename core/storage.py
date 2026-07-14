import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from core.errors import BibiError


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fav_folders (
  account_id TEXT,
  fid INTEGER,
  title TEXT,
  media_count INTEGER,
  cover_url TEXT,
  cached_at TEXT,
  PRIMARY KEY (account_id, fid)
);
CREATE TABLE IF NOT EXISTS videos (
  account_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
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
  PRIMARY KEY (account_id, resource_id, resource_type)
);
CREATE TABLE IF NOT EXISTS classify_sessions (
  session_id TEXT PRIMARY KEY,
  source_fid INTEGER,
  status TEXT,
  mode TEXT,
  category_limit INTEGER NOT NULL DEFAULT 14,
  account_id TEXT,
  created_at TEXT,
  updated_at TEXT,
  stats TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
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
);
CREATE TABLE IF NOT EXISTS wbi_keys (
  account_id TEXT PRIMARY KEY,
  img_key TEXT,
  sub_key TEXT,
  cached_at TEXT
);
CREATE TABLE IF NOT EXISTS failed_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  avid INTEGER,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
  title TEXT,
  category TEXT,
  target_fid INTEGER,
  error_code TEXT,
  error_message TEXT,
  retried INTEGER DEFAULT 0,
  created_at TEXT
);
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
CREATE TABLE IF NOT EXISTS session_video_sources (
  session_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
  source_fid INTEGER,
  moved INTEGER DEFAULT 0,
  move_error TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (session_id, resource_id, resource_type, source_fid)
);
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
CREATE TABLE IF NOT EXISTS classification_plan_items (
  version_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
  avid INTEGER,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,
  executed INTEGER DEFAULT 0,
  PRIMARY KEY (version_id, resource_id, resource_type)
);
CREATE TABLE IF NOT EXISTS cleanup_scans (
  scan_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  status TEXT NOT NULL,
  folders_total INTEGER DEFAULT 0,
  folders_scanned INTEGER DEFAULT 0,
  resources_scanned INTEGER DEFAULT 0,
  problem_total INTEGER DEFAULT 0,
  current_folder_title TEXT DEFAULT '',
  error TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS cleanup_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id TEXT NOT NULL,
  source_fid INTEGER NOT NULL,
  source_title TEXT DEFAULT '',
  resource_id INTEGER NOT NULL,
  resource_type INTEGER DEFAULT 2,
  bvid TEXT DEFAULT '',
  title TEXT DEFAULT '',
  problem_type TEXT NOT NULL,
  problem_label TEXT DEFAULT '',
  removed INTEGER DEFAULT 0,
  remove_error TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT,
  UNIQUE (scan_id, source_fid, resource_id, resource_type)
);
"""


class Storage:
    def __init__(self, base_dir: Path | str = "."):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self.base_dir / "config.json"
        self._cookie_path = self.base_dir / "bilibili_cookie.json"
        self._db_path = self.base_dir / "bibi.db"
        self._init_db()
        self._migrate_schema()

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
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _has_column(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        return column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _has_table(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None

    def _migrate_schema(self) -> None:
        with self._conn() as conn:
            if self._has_table(conn, "classify_sessions") and not self._has_column(conn, "classify_sessions", "account_id"):
                conn.execute("ALTER TABLE classify_sessions ADD COLUMN account_id TEXT")
            if self._has_table(conn, "classify_sessions") and not self._has_column(conn, "classify_sessions", "category_limit"):
                conn.execute("ALTER TABLE classify_sessions ADD COLUMN category_limit INTEGER NOT NULL DEFAULT 14")
            if self._has_table(conn, "fav_folders") and not self._has_column(conn, "fav_folders", "account_id"):
                conn.execute("ALTER TABLE fav_folders ADD COLUMN account_id TEXT")
            if self._has_table(conn, "session_sources") and not self._has_column(conn, "session_sources", "account_id"):
                conn.execute("ALTER TABLE session_sources ADD COLUMN account_id TEXT")
            if self._has_table(conn, "session_sources") and not self._has_column(conn, "session_sources", "delete_protected"):
                conn.execute("ALTER TABLE session_sources ADD COLUMN delete_protected INTEGER DEFAULT 0")
            self._migrate_wbi_keys_to_account_key(conn)
            self._migrate_videos_to_account_key(conn)
            self._migrate_fav_folders_to_account_key(conn)
            self._migrate_plan_items_to_resource_key(conn)
            self._migrate_failed_items_to_resource_columns(conn)
            self._migrate_session_video_sources_to_resource_key(conn)
            self._migrate_classifications_to_resource_key(conn)

    def _migrate_wbi_keys_to_account_key(self, conn: sqlite3.Connection) -> None:
        """旧 wbi_keys 表是 id INTEGER PRIMARY KEY CHECK (id=1) 单行结构，迁移为 account_id PRIMARY KEY。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(wbi_keys)")}
        if "account_id" in cols:
            return
        legacy_rows = conn.execute("SELECT img_key, sub_key, cached_at FROM wbi_keys").fetchall()
        conn.execute("DROP TABLE wbi_keys")
        conn.execute(
            "CREATE TABLE wbi_keys (account_id TEXT PRIMARY KEY, img_key TEXT, sub_key TEXT, cached_at TEXT)"
        )
        for row in legacy_rows:
            conn.execute(
                "INSERT INTO wbi_keys (account_id, img_key, sub_key, cached_at) VALUES ('', ?, ?, ?)",
                (row["img_key"], row["sub_key"], row["cached_at"]),
            )

    def _migrate_videos_to_account_key(self, conn: sqlite3.Connection) -> None:
        """旧 videos 表主键是单列 avid，迁移为 (account_id, avid) 组合主键。

        注意：后续 _migrate_videos_to_resource_key 会进一步迁移到 (account_id, resource_id, resource_type)。
        """
        if not self._has_table(conn, "videos"):
            return
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)") if row["pk"]]
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
        # 已经是 resource_key 结构则跳过
        if pk_cols == ["account_id", "resource_id", "resource_type"]:
            return
        # 已有 resource_id/resource_type 列但主键未更新 → 交给 resource_key 迁移保留已有类型信息
        if "resource_id" in cols or "resource_type" in cols:
            self._migrate_videos_to_resource_key(conn)
            return
        # 已经是 (account_id, avid) 但缺 resource_id/resource_type 列 → 交给 resource_key 迁移
        if pk_cols == ["account_id", "avid"] and "resource_id" not in cols:
            self._migrate_videos_to_resource_key(conn)
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        legacy_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)")]
        has_account_id = "account_id" in legacy_cols
        conn.execute("ALTER TABLE videos RENAME TO videos_legacy")
        conn.execute(
            "CREATE TABLE videos (account_id TEXT, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
            "avid INTEGER, bvid TEXT, title TEXT, intro TEXT, tags TEXT, up_name TEXT, up_mid INTEGER, "
            "cover_url TEXT, tname TEXT, fid INTEGER, cached_at TEXT, "
            "PRIMARY KEY (account_id, resource_id, resource_type))"
        )
        col_list = "account_id, resource_id, resource_type, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at"
        if has_account_id:
            conn.execute(f"INSERT OR REPLACE INTO videos ({col_list}) SELECT account_id, avid, 2, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at FROM videos_legacy")
        else:
            conn.execute(f"INSERT OR REPLACE INTO videos ({col_list}) SELECT '', avid, 2, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at FROM videos_legacy")
        conn.execute("DROP TABLE videos_legacy")

    def _migrate_videos_to_resource_key(self, conn: sqlite3.Connection) -> None:
        """videos 表从 (account_id, avid) 主键迁移为 (account_id, resource_id, resource_type) 主键。

        处理半迁移状态：已有 resource_id 但缺 resource_type 时，保留 resource_id 并补默认类型。
        """
        if not self._has_table(conn, "videos"):
            return
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)") if row["pk"]]
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
        if pk_cols == ["account_id", "resource_id", "resource_type"]:
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        has_resource_id = "resource_id" in cols
        has_resource_type = "resource_type" in cols
        has_avid = "avid" in cols
        conn.execute("ALTER TABLE videos RENAME TO videos_legacy")
        conn.execute(
            "CREATE TABLE videos (account_id TEXT, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
            "avid INTEGER, bvid TEXT, title TEXT, intro TEXT, tags TEXT, up_name TEXT, up_mid INTEGER, "
            "cover_url TEXT, tname TEXT, fid INTEGER, cached_at TEXT, "
            "PRIMARY KEY (account_id, resource_id, resource_type))"
        )
        if has_resource_id and has_resource_type:
            # 已有完整组合键列，保留原值；avid 缺失时按类型回填
            conn.execute(
                "INSERT OR REPLACE INTO videos (account_id, resource_id, resource_type, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) "
                "SELECT account_id, resource_id, resource_type, "
                "COALESCE(avid, CASE WHEN resource_type = 2 THEN resource_id ELSE 0 END), "
                "bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at FROM videos_legacy"
            )
        elif has_resource_id:
            # 只有 resource_id，缺 resource_type：保留 resource_id，补默认类型 2
            conn.execute(
                "INSERT OR REPLACE INTO videos (account_id, resource_id, resource_type, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) "
                "SELECT account_id, resource_id, 2, "
                "COALESCE(avid, resource_id), "
                "bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at FROM videos_legacy"
            )
        else:
            # 旧表只有 avid，复制为 resource_id
            conn.execute(
                "INSERT OR REPLACE INTO videos (account_id, resource_id, resource_type, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) "
                "SELECT account_id, avid, 2, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at FROM videos_legacy"
            )
        conn.execute("DROP TABLE videos_legacy")

    def _migrate_fav_folders_to_account_key(self, conn: sqlite3.Connection) -> None:
        """旧 fav_folders 表主键是单列 fid，迁移为 (account_id, fid) 组合主键。"""
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(fav_folders)") if row["pk"]]
        if pk_cols == ["account_id", "fid"]:
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        legacy_cols = [row["name"] for row in conn.execute("PRAGMA table_info(fav_folders)")]
        has_account_id = "account_id" in legacy_cols
        conn.execute("ALTER TABLE fav_folders RENAME TO fav_folders_legacy")
        conn.execute(
            "CREATE TABLE fav_folders (account_id TEXT, fid INTEGER, title TEXT, media_count INTEGER, "
            "cover_url TEXT, cached_at TEXT, PRIMARY KEY (account_id, fid))"
        )
        col_list = "account_id, fid, title, media_count, cover_url, cached_at"
        if has_account_id:
            conn.execute(f"INSERT OR REPLACE INTO fav_folders ({col_list}) SELECT account_id, fid, title, media_count, cover_url, cached_at FROM fav_folders_legacy")
        else:
            conn.execute(f"INSERT OR REPLACE INTO fav_folders ({col_list}) SELECT '', fid, title, media_count, cover_url, cached_at FROM fav_folders_legacy")
        conn.execute("DROP TABLE fav_folders_legacy")

    def _migrate_plan_items_to_resource_key(self, conn: sqlite3.Connection) -> None:
        """旧 classification_plan_items 表主键是 (version_id, avid)，迁移为 (version_id, resource_id, resource_type) 复合主键。

        旧数据 avid 复制到 resource_id，resource_type 默认 2（视频），avid 列保留作为兼容字段。
        处理半迁移状态：有 resource_id 但缺 resource_type 或主键未更新时也需修复。
        """
        if not self._has_table(conn, "classification_plan_items"):
            return
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)")}
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)") if row["pk"]]
        if pk_cols == ["version_id", "resource_id", "resource_type"] and {"resource_id", "resource_type"} <= cols:
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        select_cols = ["version_id", "avid", "category", "confidence", "reason", "adjusted", "executed"]
        if "resource_id" in cols:
            select_cols.insert(1, "resource_id")
        if "resource_type" in cols:
            select_cols.insert(2, "resource_type")
        legacy_rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM classification_plan_items"
        ).fetchall()
        conn.execute("DROP TABLE classification_plan_items")
        conn.execute(
            "CREATE TABLE classification_plan_items (version_id TEXT, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
            "avid INTEGER, category TEXT, confidence REAL, reason TEXT, adjusted INTEGER DEFAULT 0, executed INTEGER DEFAULT 0, "
            "PRIMARY KEY (version_id, resource_id, resource_type))"
        )
        for row in legacy_rows:
            row_dict = dict(row)
            resource_id = row_dict.get("resource_id", row_dict["avid"])
            resource_type = row_dict.get("resource_type", 2)
            conn.execute(
                "INSERT INTO classification_plan_items (version_id, resource_id, resource_type, avid, category, confidence, reason, adjusted, executed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_dict["version_id"], resource_id, resource_type, row_dict["avid"], row_dict["category"],
                 row_dict["confidence"], row_dict["reason"], row_dict["adjusted"], row_dict["executed"]),
            )

    def _migrate_failed_items_to_resource_columns(self, conn: sqlite3.Connection) -> None:
        """旧 failed_items 表无 resource_id/resource_type 列，迁移后 resource_id = avid, resource_type = 2。

        处理半迁移状态：有 resource_id 但缺 resource_type 时补齐列并回填。
        """
        if not self._has_table(conn, "failed_items"):
            return
        if not self._has_column(conn, "failed_items", "resource_id"):
            conn.execute("ALTER TABLE failed_items ADD COLUMN resource_id INTEGER")
        if not self._has_column(conn, "failed_items", "resource_type"):
            conn.execute("ALTER TABLE failed_items ADD COLUMN resource_type INTEGER DEFAULT 2")
        conn.execute("UPDATE failed_items SET resource_id = avid WHERE resource_id IS NULL")
        conn.execute("UPDATE failed_items SET resource_type = 2 WHERE resource_type IS NULL")

    def _migrate_session_video_sources_to_resource_key(self, conn: sqlite3.Connection) -> None:
        """session_video_sources 表从 (session_id, avid, source_fid) 主键迁移为 (session_id, resource_id, resource_type, source_fid)。

        旧表列名为 avid，迁移后改名为 resource_id，并新增 resource_type 列（默认 2）。
        """
        if not self._has_table(conn, "session_video_sources"):
            return
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_video_sources)")}
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(session_video_sources)") if row["pk"]]
        if pk_cols == ["session_id", "resource_id", "resource_type", "source_fid"]:
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        has_resource_id = "resource_id" in cols
        has_resource_type = "resource_type" in cols
        legacy_rows = conn.execute("SELECT * FROM session_video_sources").fetchall()
        conn.execute("DROP TABLE session_video_sources")
        conn.execute(
            "CREATE TABLE session_video_sources (session_id TEXT, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
            "source_fid INTEGER, moved INTEGER DEFAULT 0, move_error TEXT, created_at TEXT, updated_at TEXT, "
            "PRIMARY KEY (session_id, resource_id, resource_type, source_fid))"
        )
        for row in legacy_rows:
            rd = dict(row)
            resource_id = rd.get("resource_id", rd.get("avid", 0))
            resource_type = rd.get("resource_type", 2)
            conn.execute(
                "INSERT OR IGNORE INTO session_video_sources (session_id, resource_id, resource_type, source_fid, moved, move_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rd["session_id"], resource_id, resource_type, rd["source_fid"],
                 rd.get("moved", 0), rd.get("move_error", ""), rd.get("created_at", ""), rd.get("updated_at", "")),
            )

    def _migrate_classifications_to_resource_key(self, conn: sqlite3.Connection) -> None:
        """classifications 表从 (session_id, avid) 主键迁移为 (session_id, resource_id, resource_type) 主键。

        旧数据 avid 复制到 resource_id，resource_type 默认 2（视频），avid 列保留。
        处理半迁移状态：有 resource_id 但缺 resource_type 或主键未更新时也需修复。
        """
        if not self._has_table(conn, "classifications"):
            return
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classifications)") if row["pk"]]
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(classifications)")}
        if pk_cols == ["session_id", "resource_id", "resource_type"] and "resource_type" in cols:
            return
        need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
        if need_backup:
            self._backup_database()
        has_resource_id = "resource_id" in cols
        has_resource_type = "resource_type" in cols
        legacy_rows = conn.execute("SELECT * FROM classifications").fetchall()
        conn.execute("DROP TABLE classifications")
        conn.execute(
            "CREATE TABLE classifications (session_id TEXT, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
            "avid INTEGER, category TEXT, confidence REAL, reason TEXT, "
            "adjusted INTEGER DEFAULT 0, executed INTEGER DEFAULT 0, "
            "PRIMARY KEY (session_id, resource_id, resource_type))"
        )
        for row in legacy_rows:
            rd = dict(row)
            if has_resource_id and has_resource_type:
                resource_id = rd.get("resource_id", rd.get("avid", 0))
                resource_type = rd.get("resource_type", 2)
                avid = rd.get("avid", resource_id if resource_type == 2 else 0)
            else:
                resource_id = rd.get("avid", 0)
                resource_type = 2
                avid = rd.get("avid", 0)
            conn.execute(
                "INSERT OR REPLACE INTO classifications (session_id, resource_id, resource_type, avid, category, confidence, reason, adjusted, executed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rd["session_id"], resource_id, resource_type, avid,
                 rd.get("category", ""), rd.get("confidence", 0.0), rd.get("reason", ""),
                 rd.get("adjusted", 0), rd.get("executed", 0)),
            )

    def _backup_database(self) -> None:
        """破坏性表重建前备份当前数据库文件。"""
        if not self._db_path.exists():
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = self._db_path.with_name(f"bibi.db.backup-{ts}")
        backup_path.write_bytes(self._db_path.read_bytes())

    def upsert_folder(self, folder: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fav_folders (account_id, fid, title, media_count, cover_url, cached_at) "
                "VALUES (:account_id, :fid, :title, :media_count, :cover_url, datetime('now'))",
                {**folder, "account_id": folder.get("account_id", "")},
            )

    def list_folders(self, account_id: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if account_id is not None:
                rows = conn.execute(
                    "SELECT * FROM fav_folders WHERE account_id = ? ORDER BY fid", (account_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM fav_folders ORDER BY fid").fetchall()
            return [dict(r) for r in rows]

    def upsert_video(self, video: dict) -> None:
        account_id = video.get("account_id", "")
        resource_id = video.get("resource_id", video.get("avid"))
        resource_type = video.get("resource_type", 2)
        avid = video.get("avid", resource_id if resource_type == 2 else 0)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO videos (account_id, resource_id, resource_type, avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) "
                "VALUES (:account_id, :resource_id, :resource_type, :avid, :bvid, :title, :intro, :tags, :up_name, :up_mid, :cover_url, :tname, :fid, datetime('now'))",
                {**video, "account_id": account_id, "resource_id": resource_id, "resource_type": resource_type, "avid": avid},
            )

    def list_videos_by_fid(self, fid: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE fid = ? ORDER BY resource_id, resource_type", (fid,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_videos_by_avids(self, avids: list[int], account_id: str | None = None) -> list[dict]:
        """按 avid 列表查询视频。兼容旧调用：avid 即 resource_id（视频类型）。

        注意：此方法只按 resource_id 过滤，会把同 ID 不同类型的缓存行都返回。
        新会话流程应使用 list_videos_by_resource_keys() 按组合键精确查询。
        """
        if not avids:
            return []
        placeholders = ",".join("?" * len(avids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM videos WHERE account_id = ? AND resource_id IN ({placeholders}) ORDER BY resource_id, resource_type",
                [account_id or "", *avids],
            ).fetchall()
            return [dict(r) for r in rows]

    def list_videos_by_resource_keys(self, keys: list[tuple[int, int]], account_id: str | None = None) -> list[dict]:
        """按 (resource_id, resource_type) 组合键列表精确查询视频缓存。

        避免同 ID 不同类型的缓存行被误带入当前会话。
        """
        if not keys:
            return []
        clauses = " OR ".join(["(resource_id = ? AND resource_type = ?)"] * len(keys))
        params: list = [account_id or ""]
        for rid, rtype in keys:
            params.extend([rid, rtype])
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM videos WHERE account_id = ? AND ({clauses}) ORDER BY resource_id, resource_type",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def create_session(self, source_fid: int, mode: str, category_limit: int = 14) -> str:
        session_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO classify_sessions (session_id, source_fid, status, mode, category_limit, account_id, created_at, updated_at, stats) "
                "VALUES (?, ?, 'draft', ?, ?, '', datetime('now'), datetime('now'), '{}')",
                (session_id, source_fid, mode, category_limit),
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

    def update_session_account(self, session_id: str, account_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classify_sessions SET account_id = ?, updated_at = datetime('now') WHERE session_id = ?",
                (account_id, session_id),
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
                resource_id = it.get("resource_id", it.get("avid"))
                resource_type = it.get("resource_type", 2)
                avid = it.get("avid", resource_id if resource_type == 2 else 0)
                conn.execute(
                    "INSERT OR REPLACE INTO classifications (session_id, resource_id, resource_type, avid, category, confidence, reason, adjusted, executed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                    (session_id, resource_id, resource_type, avid, it["category"], it["confidence"], it["reason"]),
                )

    def load_classifications(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM classifications WHERE session_id = ? ORDER BY resource_id, resource_type", (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def adjust_classification(self, session_id: str, resource_id: int, new_category: str, resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classifications SET category = ?, adjusted = 1 WHERE session_id = ? AND resource_id = ? AND resource_type = ?",
                (new_category, session_id, resource_id, resource_type),
            )

    def adjust_plan_item(self, version_id: str, resource_id: int, new_category: str, resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classification_plan_items SET category = ?, adjusted = 1 "
                "WHERE version_id = ? AND resource_id = ? AND resource_type = ?",
                (new_category, version_id, resource_id, resource_type),
            )

    def mark_classification_executed(self, session_id: str, resource_id: int, ok: bool, resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classifications SET executed = ? WHERE session_id = ? AND resource_id = ? AND resource_type = ?",
                (1 if ok else 0, session_id, resource_id, resource_type),
            )

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
                resource_id = it.get("resource_id", it.get("avid"))
                resource_type = it.get("resource_type", 2)
                avid = it.get("avid", resource_id if resource_type == 2 else 0)
                conn.execute(
                    "INSERT INTO classification_plan_items (version_id, resource_id, resource_type, avid, category, confidence, reason, adjusted, executed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (version_id, resource_id, resource_type, avid, it["category"],
                     it.get("confidence", 0.0), it.get("reason", ""), it.get("adjusted", 0), it.get("executed", 0)),
                )
        return version_id

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
            row = conn.execute(
                "SELECT 1 FROM classification_plan_versions WHERE session_id = ? AND version_id = ?",
                (session_id, version_id),
            ).fetchone()
            if not row:
                raise BibiError(f"方案版本不存在: {version_id}", code="PLAN_VERSION_NOT_FOUND")
            conn.execute("UPDATE classification_plan_versions SET is_active = 0 WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE classification_plan_versions SET is_active = 1, updated_at = datetime('now') WHERE session_id = ? AND version_id = ?",
                (session_id, version_id),
            )

    def load_plan_items(self, version_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM classification_plan_items WHERE version_id = ? ORDER BY resource_id, resource_type", (version_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_plan_item_executed(self, version_id: str, resource_id: int, ok: bool, resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE classification_plan_items SET executed = ? WHERE version_id = ? AND resource_id = ? AND resource_type = ?",
                (1 if ok else 0, version_id, resource_id, resource_type),
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
                    "resource_id": it.get("resource_id", it.get("avid")),
                    "resource_type": it.get("resource_type", 2),
                    "avid": it.get("avid", it.get("resource_id", 0) if it.get("resource_type", 2) == 2 else 0),
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

    def compute_session_progress(self, session_id: str) -> dict:
        """从 session_sources 和 classify_sessions.stats 汇总当前进度，用于 SSE 重连恢复。"""
        sources = self.list_session_sources(session_id)
        collected = sum(s.get("collected_count", 0) or 0 for s in sources)
        skipped = sum(s.get("skipped_count", 0) or 0 for s in sources)
        source_total = sum(s.get("media_count", 0) or 0 for s in sources)
        s = self.load_session(session_id)
        stats = {}
        if s and s.get("stats"):
            try:
                stats = json.loads(s["stats"]) if isinstance(s["stats"], str) else (s["stats"] or {})
            except json.JSONDecodeError:
                stats = {}
        if not source_total and stats.get("source_total"):
            source_total = stats["source_total"]
        scanned = stats.get("scanned_total", collected + skipped)
        return {
            "source_total": source_total,
            "scanned": scanned,
            "collected": collected,
            "skipped": skipped,
        }

    def update_session_source_counts(self, session_id: str, source_fid: int, collected_count: int, skipped_count: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE session_sources SET collected_count = ?, skipped_count = ?, updated_at = datetime('now') WHERE session_id = ? AND source_fid = ?",
                (collected_count, skipped_count, session_id, source_fid),
            )

    def add_session_video_source(self, session_id: str, resource_id: int, source_fid: int, resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_video_sources (session_id, resource_id, resource_type, source_fid, moved, move_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, '', datetime('now'), datetime('now'))",
                (session_id, resource_id, resource_type, source_fid),
            )

    def list_session_video_sources(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_video_sources WHERE session_id = ? ORDER BY source_fid, resource_id, resource_type",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_session_video_source_moved(self, session_id: str, resource_id: int, source_fid: int, ok: bool, error: str = "", resource_type: int = 2) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE session_video_sources SET moved = ?, move_error = ?, updated_at = datetime('now') "
                "WHERE session_id = ? AND resource_id = ? AND resource_type = ? AND source_fid = ?",
                (1 if ok else 0, error, session_id, resource_id, resource_type, source_fid),
            )

    def list_failed_session_video_sources(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_video_sources WHERE session_id = ? AND moved = 0 AND COALESCE(move_error, '') <> ''",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_session_source_empty_candidate(self, session_id: str, source_fid: int, delete_candidate: bool, emptied_after_execute: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE session_sources SET delete_candidate = ?, emptied_after_execute = ?, updated_at = datetime('now') WHERE session_id = ? AND source_fid = ?",
                (1 if delete_candidate else 0, 1 if emptied_after_execute else 0, session_id, source_fid),
            )

    def list_empty_source_candidates(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_sources WHERE session_id = ? AND delete_candidate = 1 AND deleted = 0 ORDER BY selected_order",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_session_source_deleted(self, session_id: str, source_fid: int, ok: bool, error: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE session_sources SET deleted = ?, delete_error = ?, updated_at = datetime('now') WHERE session_id = ? AND source_fid = ?",
                (1 if ok else 0, error, session_id, source_fid),
            )

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

    def list_skipped_items(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM skipped_items WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_skipped_items_by_ids(self, session_id: str, item_ids: list[int]) -> list[dict]:
        if not item_ids:
            return []
        placeholders = ",".join("?" * len(item_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM skipped_items WHERE session_id = ? AND id IN ({placeholders}) ORDER BY id",
                [session_id, *item_ids],
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_skipped_item_removed(self, item_id: int, ok: bool, error: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE skipped_items SET removed = ?, remove_error = ?, updated_at = datetime('now') WHERE id = ?",
                (1 if ok else 0, error, item_id),
            )

    def create_cleanup_scan(self, account_id: str, folders_total: int) -> str:
        scan_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cleanup_scans (scan_id, account_id, status, folders_total, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, datetime('now'), datetime('now'))",
                (scan_id, account_id, folders_total),
            )
        return scan_id

    def update_cleanup_scan(self, scan_id: str, **values) -> None:
        allowed = {
            "status", "folders_total", "folders_scanned", "resources_scanned",
            "problem_total", "current_folder_title", "error",
        }
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        params = [value for _, value in updates]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE cleanup_scans SET {assignments}, updated_at = datetime('now') WHERE scan_id = ?",
                [*params, scan_id],
            )

    def get_cleanup_scan(self, scan_id: str, account_id: str | None = None) -> dict | None:
        with self._conn() as conn:
            if account_id is None:
                row = conn.execute("SELECT * FROM cleanup_scans WHERE scan_id = ?", (scan_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM cleanup_scans WHERE scan_id = ? AND account_id = ?",
                    (scan_id, account_id),
                ).fetchone()
            return dict(row) if row else None

    def get_latest_cleanup_scan(self, account_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cleanup_scans WHERE account_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            return dict(row) if row else None

    def add_cleanup_items(self, scan_id: str, items: list[dict]) -> None:
        with self._conn() as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO cleanup_items (scan_id, source_fid, source_title, resource_id, resource_type, "
                    "bvid, title, problem_type, problem_label, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now')) "
                    "ON CONFLICT(scan_id, source_fid, resource_id, resource_type) DO UPDATE SET "
                    "source_title=excluded.source_title, bvid=excluded.bvid, title=excluded.title, "
                    "problem_type=excluded.problem_type, problem_label=excluded.problem_label, updated_at=datetime('now')",
                    (
                        scan_id, item["source_fid"], item.get("source_title", ""), item["resource_id"],
                        item.get("resource_type", 2), item.get("bvid", ""), item.get("title", ""),
                        item["problem_type"], item.get("problem_label", ""),
                    ),
                )

    def list_cleanup_items(self, scan_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cleanup_items WHERE scan_id = ? ORDER BY source_fid, id",
                (scan_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_cleanup_items_by_ids(self, scan_id: str, item_ids: list[int]) -> list[dict]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM cleanup_items WHERE scan_id = ? AND id IN ({placeholders}) ORDER BY id",
                [scan_id, *item_ids],
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_cleanup_item_removed(self, item_id: int, ok: bool, error: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cleanup_items SET removed = ?, remove_error = ?, updated_at = datetime('now') WHERE id = ?",
                (1 if ok else 0, error, item_id),
            )

    def upsert_account(self, account: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accounts (account_id, mid, uname, avatar_url, cookie_path, is_active, created_at, updated_at) "
                "VALUES (:account_id, :mid, :uname, :avatar_url, :cookie_path, COALESCE(:is_active, 0), datetime('now'), datetime('now'))",
                {**account, "is_active": account.get("is_active", 0)},
            )

    def activate_account(self, account_id: str) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if not row:
                raise BibiError(f"账号不存在: {account_id}", code="ACCOUNT_NOT_FOUND")
            conn.execute("UPDATE accounts SET is_active = 0")
            conn.execute("UPDATE accounts SET is_active = 1, updated_at = datetime('now') WHERE account_id = ?", (account_id,))

    def deactivate_account(self, account_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE accounts SET is_active = 0, updated_at = datetime('now') WHERE account_id = ?", (account_id,))

    def get_active_account(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE is_active = 1").fetchone()
            return dict(row) if row else None

    def list_accounts(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def load_wbi_keys(self, account_id: str = "") -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT img_key, sub_key FROM wbi_keys WHERE account_id = ?", (account_id,)).fetchone()
            if not row:
                return None
            return {"img_key": row["img_key"], "sub_key": row["sub_key"]}

    def save_wbi_keys(self, img_key: str, sub_key: str, account_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO wbi_keys (account_id, img_key, sub_key, cached_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (account_id, img_key, sub_key),
            )

    def clear_wbi_keys(self, account_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM wbi_keys WHERE account_id = ?", (account_id,))

    def add_failed_item(self, session_id: str, item: dict) -> int:
        with self._conn() as conn:
            avid = item.get("avid", 0)
            resource_id = item.get("resource_id", avid)
            resource_type = item.get("resource_type", 2)
            cur = conn.execute(
                "INSERT INTO failed_items (session_id, avid, resource_id, resource_type, title, category, target_fid, error_code, error_message, retried, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (session_id, avid, resource_id, resource_type, item["title"], item["category"],
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

    def update_failed_item_target(self, item_id: int, target_fid: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE failed_items SET target_fid = ? WHERE id = ?",
                (target_fid, item_id),
            )

    def delete_failed_item(self, item_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM failed_items WHERE id = ?", (item_id,))

    def delete_one_failed_item(self, session_id: str, resource_id: int, category: str, resource_type: int = 2) -> None:
        """删除指定 (resource_id, resource_type, category) 下最早的一条 failed_item，用于多源重试按来源清理。"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM failed_items WHERE id = ("
                "SELECT id FROM failed_items WHERE session_id = ? AND resource_id = ? AND resource_type = ? AND category = ? "
                "ORDER BY id LIMIT 1)",
                (session_id, resource_id, resource_type, category),
            )

    def clear_failed_items(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM failed_items WHERE session_id = ?", (session_id,))
