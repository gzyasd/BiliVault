import json
import sqlite3
from pathlib import Path

import pytest

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


def test_fav_folders_isolated_by_account_id(tmp_path):
    """不同账号相同 fid 不应互相覆盖。"""
    storage = Storage(tmp_path)
    storage.upsert_folder({"fid": 100, "title": "账号A的收藏夹", "media_count": 10, "cover_url": "", "account_id": "acc1"})
    storage.upsert_folder({"fid": 100, "title": "账号B的收藏夹", "media_count": 20, "cover_url": "", "account_id": "acc2"})
    all_folders = storage.list_folders()
    assert len(all_folders) == 2
    acc1_folders = storage.list_folders(account_id="acc1")
    assert len(acc1_folders) == 1
    assert acc1_folders[0]["title"] == "账号A的收藏夹"
    acc2_folders = storage.list_folders(account_id="acc2")
    assert len(acc2_folders) == 1
    assert acc2_folders[0]["title"] == "账号B的收藏夹"


def test_fav_folders_table_uses_account_id_fid_composite_primary_key(tmp_path):
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(fav_folders)") if row["pk"]]
        assert pk_cols == ["account_id", "fid"]


def test_storage_migrates_legacy_fav_folders_table_to_composite_key(tmp_path):
    """旧库 fav_folders 表是单列 fid 主键，迁移后应为 (account_id, fid) 组合主键。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE fav_folders (fid INTEGER PRIMARY KEY, title TEXT, media_count INTEGER, "
            "cover_url TEXT, account_id TEXT, cached_at TEXT)"
        )
        conn.execute(
            "INSERT INTO fav_folders (fid, title, media_count, cover_url, account_id, cached_at) "
            "VALUES (100, '旧收藏夹', 5, '', 'acc1', 'now')"
        )
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(fav_folders)") if row["pk"]]
        assert pk_cols == ["account_id", "fid"]
        row = conn.execute("SELECT account_id, fid, title FROM fav_folders WHERE fid = 100").fetchone()
        assert row["account_id"] == "acc1"
        assert row["title"] == "旧收藏夹"


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
        {"avid": 1, "resource_id": 1, "resource_type": 2, "category": "编程", "confidence": 0.9, "reason": "Python教程"},
        {"avid": 2, "resource_id": 2, "resource_type": 2, "category": "音乐", "confidence": 0.8, "reason": "MV"},
    ])
    items = storage.load_classifications(sid)
    assert len(items) == 2
    by_avid = {it["avid"]: it for it in items}
    assert by_avid[1]["category"] == "编程"


def test_adjust_classification(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [
        {"avid": 1, "resource_id": 1, "resource_type": 2, "category": "编程", "confidence": 0.9, "reason": ""},
    ])
    storage.adjust_classification(sid, resource_id=1, new_category="工具")
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


def test_save_and_load_wbi_keys(tmp_path):
    storage = Storage(tmp_path)
    assert storage.load_wbi_keys() is None
    storage.save_wbi_keys(img_key="abc", sub_key="def")
    keys = storage.load_wbi_keys()
    assert keys == {"img_key": "abc", "sub_key": "def"}


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


def test_videos_table_uses_account_id_resource_key(tmp_path):
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)") if row["pk"]]
        assert pk_cols == ["account_id", "resource_id", "resource_type"]


def test_wbi_keys_table_isolated_by_account_id(tmp_path):
    storage = Storage(tmp_path)
    storage.save_wbi_keys(img_key="k1", sub_key="s1", account_id="acc1")
    storage.save_wbi_keys(img_key="k2", sub_key="s2", account_id="acc2")
    assert storage.load_wbi_keys(account_id="acc1") == {"img_key": "k1", "sub_key": "s1"}
    assert storage.load_wbi_keys(account_id="acc2") == {"img_key": "k2", "sub_key": "s2"}


def test_storage_migrates_legacy_videos_table_to_resource_key(tmp_path):
    """旧库 videos 表是单列 avid 主键，迁移后应为 (account_id, resource_id, resource_type) 组合主键，且旧数据 account_id 为空字符串。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE videos (avid INTEGER PRIMARY KEY, bvid TEXT, title TEXT, intro TEXT, tags TEXT, up_name TEXT, up_mid INTEGER, cover_url TEXT, tname TEXT, fid INTEGER, cached_at TEXT)")
        conn.execute("INSERT INTO videos (avid, bvid, title, intro, tags, up_name, up_mid, cover_url, tname, fid, cached_at) VALUES (1, 'BV1', 'A', '', '[]', 'UP', 1, '', '科技', 100, 'now')")
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)") if row["pk"]]
        assert pk_cols == ["account_id", "resource_id", "resource_type"]
        row = conn.execute("SELECT account_id, resource_id, resource_type, avid, bvid FROM videos WHERE resource_id = 1").fetchone()
        assert row["account_id"] == ""
        assert row["resource_id"] == 1
        assert row["resource_type"] == 2
        assert row["bvid"] == "BV1"


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


def test_session_sources_upsert_preserves_collected_counts(tmp_path):
    """重复 save 不能清空 collected_count/skipped_count（验证 ON CONFLICT 修复）。"""
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 405, "selected_order": 0},
    ])
    storage.update_session_source_counts(sid, 100, collected_count=380, skipped_count=25)

    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹(改名)", "media_count": 405, "selected_order": 0},
    ])

    src = storage.list_session_sources(sid)[0]
    assert src["title"] == "默认收藏夹(改名)"
    assert src["collected_count"] == 380
    assert src["skipped_count"] == 25


def test_session_video_sources_preserve_multiple_origins(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")

    storage.add_session_video_source(sid, resource_id=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=1, source_fid=200, resource_type=2)

    rows = storage.list_session_video_sources(sid)

    assert {(r["resource_id"], r["source_fid"]) for r in rows} == {(1, 100), (1, 200)}


def test_list_videos_by_avids_filters_by_account(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                          "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "科技", "fid": 100,
                          "account_id": "acc1"})
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A-acc2", "intro": "", "tags": "[]",
                          "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "科技", "fid": 100,
                          "account_id": "acc2"})

    rows = storage.list_videos_by_avids([1], account_id="acc1")
    assert len(rows) == 1
    assert rows[0]["account_id"] == "acc1"


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
        instruction="把官方的作品单独放在一个收藏夹",
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


def test_activate_plan_version_switches_active(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    v1 = storage.create_plan_version(sid, None, "v1", [{"avid": 1, "category": "A", "confidence": 0.9, "reason": ""}], activate=True)
    v2 = storage.create_plan_version(sid, v1, "v2", [{"avid": 1, "category": "B", "confidence": 0.9, "reason": ""}], activate=True)
    assert storage.get_active_plan_version(sid)["version_id"] == v2

    storage.activate_plan_version(sid, v1)

    assert storage.get_active_plan_version(sid)["version_id"] == v1


def test_mark_plan_item_executed(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    vid = storage.create_plan_version(sid, None, "v1", [{"avid": 1, "category": "A", "confidence": 0.9, "reason": ""}], activate=True)

    storage.mark_plan_item_executed(vid, 1, True)

    items = storage.load_plan_items(vid)
    assert items[0]["executed"] == 1


def test_migrate_legacy_classifications_to_version(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "音乐", "confidence": 0.8, "reason": ""},
    ])

    vid = storage.migrate_legacy_classifications_to_version(sid)

    assert vid is not None
    active = storage.get_active_plan_version(sid)
    assert active["version_id"] == vid
    items = storage.load_plan_items(vid)
    assert [it["avid"] for it in items] == [1, 2]
    assert items[0]["category"] == "编程"


def test_migrate_legacy_classifications_idempotent(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [{"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""}])

    vid1 = storage.migrate_legacy_classifications_to_version(sid)
    vid2 = storage.migrate_legacy_classifications_to_version(sid)

    assert vid1 == vid2


def test_accounts_create_switch_and_active(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_account({"account_id": "a1", "mid": 1, "uname": "账号1", "avatar_url": "", "cookie_path": "accounts/a1.json"})
    storage.upsert_account({"account_id": "a2", "mid": 2, "uname": "账号2", "avatar_url": "", "cookie_path": "accounts/a2.json"})

    storage.activate_account("a2")

    assert storage.get_active_account()["account_id"] == "a2"
    accounts = storage.list_accounts()
    assert [a["is_active"] for a in accounts if a["account_id"] == "a2"] == [1]
    assert [a["is_active"] for a in accounts if a["account_id"] == "a1"] == [0]


def test_activate_account_rejects_unknown_keeps_current(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_account({"account_id": "a1", "mid": 1, "uname": "账号1", "avatar_url": "", "cookie_path": "accounts/a1.json"})
    storage.activate_account("a1")
    with pytest.raises(Exception) as ei:
        storage.activate_account("not-exist")
    assert "ACCOUNT_NOT_FOUND" in str(ei.value) or "不存在" in str(ei.value)
    # 当前活跃账号未被清空
    assert storage.get_active_account()["account_id"] == "a1"


def test_activate_plan_version_rejects_unknown_keeps_current(tmp_path):
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    v1_id = storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    assert storage.get_active_plan_version(sid)["version_id"] == v1_id
    with pytest.raises(Exception) as ei:
        storage.activate_plan_version(sid, "non-existent-version-id")
    assert "PLAN_VERSION_NOT_FOUND" in str(ei.value) or "不存在" in str(ei.value)
    # 当前激活版本未被清空
    assert storage.get_active_plan_version(sid)["version_id"] == v1_id


def test_plan_items_table_uses_resource_id_resource_type_composite_key(tmp_path):
    """classification_plan_items 表主键应为 (version_id, resource_id, resource_type)。"""
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)") if row["pk"]]
        assert pk_cols == ["version_id", "resource_id", "resource_type"]


def test_storage_migrates_legacy_plan_items_to_resource_key(tmp_path):
    """旧 classification_plan_items 表主键是 (version_id, avid)，迁移后应变成 (version_id, resource_id, resource_type)，旧数据 avid 复制到 resource_id，resource_type 默认 2。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE classification_plan_versions (version_id TEXT PRIMARY KEY, session_id TEXT, "
            "version_no INTEGER, parent_version_id TEXT, instruction TEXT, status TEXT, "
            "is_active INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE classification_plan_items (version_id TEXT, avid INTEGER, category TEXT, "
            "confidence REAL, reason TEXT, adjusted INTEGER DEFAULT 0, executed INTEGER DEFAULT 0, "
            "PRIMARY KEY (version_id, avid))"
        )
        conn.execute(
            "INSERT INTO classification_plan_versions (version_id, session_id, version_no, parent_version_id, "
            "instruction, status, is_active, created_at, updated_at) VALUES "
            "('v1', 's1', 1, NULL, '', 'ready', 1, 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO classification_plan_items (version_id, avid, category, confidence, reason, adjusted, executed) "
            "VALUES ('v1', 100, '编程', 0.9, '', 0, 0)"
        )
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)") if row["pk"]]
        assert pk_cols == ["version_id", "resource_id", "resource_type"]
        row = conn.execute(
            "SELECT resource_id, resource_type, category FROM classification_plan_items WHERE version_id = 'v1'"
        ).fetchone()
        assert row["resource_id"] == 100
        assert row["resource_type"] == 2
        assert row["category"] == "编程"


def test_create_plan_version_writes_resource_id_and_type(tmp_path):
    """create_plan_version 应同时写入 resource_id/resource_type，且支持只有 avid 的旧格式（视为视频 resource_type=2）。"""
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    vid = storage.create_plan_version(sid, None, "v1", [
        {"avid": 1, "category": "视频", "confidence": 0.9, "reason": ""},
        {"avid": 2, "resource_id": 2, "resource_type": 2, "category": "视频2", "confidence": 0.9, "reason": ""},
        {"avid": 0, "resource_id": 1001, "resource_type": 11, "category": "合集", "confidence": 0.9, "reason": ""},
    ], activate=True)
    items = storage.load_plan_items(vid)
    by_key = {(it["resource_id"], it["resource_type"]): it for it in items}
    assert (1, 2) in by_key
    assert (2, 2) in by_key
    assert (1001, 11) in by_key
    assert by_key[(1, 2)]["category"] == "视频"
    assert by_key[(1001, 11)]["category"] == "合集"


def test_adjust_plan_item_by_resource_id_and_type(tmp_path):
    """adjust_plan_item 应按 (version_id, resource_id, resource_type) 更新。"""
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    vid = storage.create_plan_version(sid, None, "v1", [
        {"resource_id": 1001, "resource_type": 11, "avid": 0, "category": "旧分类", "confidence": 0.9, "reason": ""},
    ], activate=True)
    storage.adjust_plan_item(vid, resource_id=1001, resource_type=11, new_category="新分类")
    items = storage.load_plan_items(vid)
    assert items[0]["category"] == "新分类"
    assert items[0]["adjusted"] == 1


def test_failed_items_table_has_resource_id_and_resource_type(tmp_path):
    """failed_items 表应包含 resource_id/resource_type 列。"""
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(failed_items)")}
        assert "resource_id" in cols
        assert "resource_type" in cols


def test_storage_migrates_legacy_failed_items_to_resource_columns(tmp_path):
    """旧 failed_items 表无 resource_id/resource_type 列，迁移后旧数据 resource_id = avid, resource_type = 2，avid 保留。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE classify_sessions (session_id TEXT PRIMARY KEY, source_fid INTEGER, mode TEXT, "
            "status TEXT, stats TEXT, account_id TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE failed_items (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, avid INTEGER, "
            "title TEXT, category TEXT, target_fid INTEGER, error_code TEXT, error_message TEXT, "
            "retried INTEGER DEFAULT 0, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO failed_items (session_id, avid, title, category, target_fid, error_code, error_message, retried, created_at) "
            "VALUES ('s1', 1001, '视频A', '编程', 5001, '-509', '限流', 0, 'now')"
        )
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(failed_items)")}
        assert "resource_id" in cols
        assert "resource_type" in cols
        row = conn.execute(
            "SELECT resource_id, resource_type, avid FROM failed_items WHERE session_id = 's1'"
        ).fetchone()
        assert row["resource_id"] == 1001
        assert row["resource_type"] == 2
        assert row["avid"] == 1001


def test_classifications_table_supports_same_id_different_type(tmp_path):
    """classifications 表按 (session_id, resource_id, resource_type) 主键，同 ID 不同类型不覆盖。"""
    storage = Storage(tmp_path)
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_classifications(sid, [
        {"resource_id": 123, "resource_type": 2, "avid": 123, "category": "视频类", "confidence": 0.9, "reason": ""},
        {"resource_id": 123, "resource_type": 11, "avid": 0, "category": "合集类", "confidence": 0.9, "reason": ""},
    ])
    items = storage.load_classifications(sid)
    assert len(items) == 2
    by_key = {(it["resource_id"], it["resource_type"]): it for it in items}
    assert by_key[(123, 2)]["category"] == "视频类"
    assert by_key[(123, 11)]["category"] == "合集类"


def test_storage_migrates_legacy_classifications_to_resource_key(tmp_path):
    """旧 classifications 表（session_id, avid）主键迁移为 (session_id, resource_id, resource_type)。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE classifications (session_id TEXT, avid INTEGER, category TEXT, "
            "confidence REAL, reason TEXT, adjusted INTEGER DEFAULT 0, executed INTEGER DEFAULT 0, "
            "PRIMARY KEY (session_id, avid))"
        )
        conn.execute(
            "INSERT INTO classifications (session_id, avid, category, confidence, reason, adjusted, executed) "
            "VALUES ('s1', 1001, '编程', 0.9, 'Python', 0, 0)"
        )
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classifications)") if row["pk"]]
        assert pk_cols == ["session_id", "resource_id", "resource_type"]
        row = conn.execute(
            "SELECT resource_id, resource_type, avid, category FROM classifications WHERE session_id = 's1'"
        ).fetchone()
        assert row["resource_id"] == 1001
        assert row["resource_type"] == 2
        assert row["avid"] == 1001
        assert row["category"] == "编程"


def test_storage_migrates_half_migrated_videos_preserves_resource_type(tmp_path):
    """半迁移状态：videos 表已有 resource_id/resource_type 列但主键未更新，迁移应保留已有类型信息。"""
    db_path = tmp_path / "bibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE videos (account_id TEXT, resource_id INTEGER, resource_type INTEGER, "
            "avid INTEGER, bvid TEXT, title TEXT, intro TEXT, tags TEXT, up_name TEXT, up_mid INTEGER, "
            "cover_url TEXT, tname TEXT, fid INTEGER, cached_at TEXT, "
            "PRIMARY KEY (account_id, avid))"
        )
        conn.execute(
            "INSERT INTO videos (account_id, resource_id, resource_type, avid, bvid, title, intro, tags, "
            "up_name, up_mid, cover_url, tname, fid, cached_at) "
            "VALUES ('', 123, 11, 0, '', '合集半迁移', '', '[]', 'UP', 1, '', '合集', 100, 'now')"
        )
    storage = Storage(tmp_path)
    with storage._conn() as conn:
        pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(videos)") if row["pk"]]
        assert pk_cols == ["account_id", "resource_id", "resource_type"]
        row = conn.execute(
            "SELECT resource_id, resource_type, avid, title FROM videos"
        ).fetchone()
        assert row["resource_id"] == 123
        assert row["resource_type"] == 11
        assert row["avid"] == 0
        assert row["title"] == "合集半迁移"
