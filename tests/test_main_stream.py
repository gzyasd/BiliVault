import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from core.errors import BibiError
from core.storage import Storage


class _FakeStorage:
    def __init__(self):
        self.status = "collecting"

    def load_session(self, sid):
        return {"session_id": sid, "status": self.status}


class _FakeManager:
    def __init__(self, release):
        self.release = release

    async def run_pipeline(self, sid, on_progress=None):
        if on_progress:
            await on_progress({"stage": "collecting", "progress": None})
        await self.release.wait()


class _FakeExecutionStorage:
    def __init__(self):
        self.status = "pending_review"
        self.stats = "{}"

    def load_session(self, sid):
        return {
            "session_id": sid,
            "status": self.status,
            "stats": self.stats,
        }


class _FakeExecutionManager:
    def __init__(self, storage, release):
        self.storage = storage
        self.release = release
        self.calls = 0

    async def execute(self, sid, on_progress=None):
        self.calls += 1
        self.storage.status = "executing"
        if on_progress:
            await on_progress({
                "stage": "executing",
                "phase": "moving",
                "progress": 0.5,
                "processed": 1,
                "total": 2,
                "success": 1,
                "failed": 0,
            })
        await self.release.wait()
        stats = {"success": 2, "failed": 0, "total": 2}
        self.storage.status = "done"
        self.storage.stats = json.dumps(stats)
        if on_progress:
            await on_progress({"stage": "done", "progress": 1.0, "stats": stats})
        return stats


class _FakeRefineManager:
    def __init__(self, release=None):
        self.release = release
        self.calls = 0

    async def refine_plan(self, sid, instruction, on_progress=None):
        self.calls += 1
        if on_progress:
            await on_progress({
                "stage": "refining", "processed": 2, "total": 4,
                "progress": 0.5, "retry_count": 1,
            })
        if self.release:
            await self.release.wait()
        return {"session": {"session_id": sid}, "items": [], "versions": []}

    async def retry_unclassified(self, sid, on_progress=None):
        if on_progress:
            await on_progress({
                "stage": "refining", "processed": 1, "total": 1,
                "progress": 1.0, "retry_count": 0,
            })
        return {"plan": {"session": {"session_id": sid}, "items": []}, "recovered": 1, "remaining": 0}


class _FakeCleanupManager:
    def __init__(self, storage):
        self.storage = storage

    async def scan(self, scan_id, account_id, on_progress=None):
        self.storage.update_cleanup_scan(
            scan_id, status="ready", folders_total=1, folders_scanned=1,
            resources_scanned=3, problem_total=1,
        )
        if on_progress:
            await on_progress({
                "stage": "ready", "progress": 1.0, "folders_scanned": 1,
                "folders_total": 1, "resources_scanned": 3, "problem_total": 1,
            })
        return self.get_scan(scan_id, account_id)

    def get_scan(self, scan_id, account_id):
        return {
            "scan": self.storage.get_cleanup_scan(scan_id, account_id),
            "items": self.storage.list_cleanup_items(scan_id),
        }

    async def remove(self, scan_id, account_id, item_ids, on_progress=None):
        self.storage.update_cleanup_scan(scan_id, status="completed")
        result = {"total": len(item_ids), "success": len(item_ids), "failed": 0}
        if on_progress:
            await on_progress({"stage": "completed", "progress": 1.0, **result})
        return result


@pytest.mark.asyncio
async def test_stream_disconnect_keeps_running_pipeline_registered(monkeypatch):
    sid = "sid-stream"
    release = asyncio.Event()
    storage = _FakeStorage()
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: _FakeManager(release))
    main._running_pipelines.clear()

    response = await main.api_session_stream(sid)
    iterator = response.body_iterator

    first_event = await anext(iterator)
    assert "event: stage" in first_event
    await iterator.aclose()

    try:
        assert sid in main._running_pipelines
        assert not main._running_pipelines[sid].done()
    finally:
        release.set()
        task = main._running_pipelines.get(sid)
        if task:
            await asyncio.wait_for(task, timeout=1)
        main._running_pipelines.clear()


@pytest.mark.asyncio
async def test_execute_stream_disconnect_keeps_task_and_reconnect_reuses_it(monkeypatch):
    sid = "sid-execute-stream"
    release = asyncio.Event()
    storage = _FakeExecutionStorage()
    manager = _FakeExecutionManager(storage, release)
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_executions.clear()
    main._execution_progress.clear()

    response = await main.api_execute_stream(sid)
    iterator = response.body_iterator
    first_event = await asyncio.wait_for(anext(iterator), timeout=1)
    assert "event: stage" in first_event
    assert '"processed": 1' in first_event
    await iterator.aclose()

    try:
        assert sid in main._running_executions
        assert not main._running_executions[sid].done()

        reconnect = await main.api_execute_stream(sid)
        reconnect_iterator = reconnect.body_iterator
        reconnect_event = await asyncio.wait_for(anext(reconnect_iterator), timeout=1)
        assert "event: stage" in reconnect_event
        assert '"processed": 1' in reconnect_event
        assert manager.calls == 1

        release.set()
        done_event = await asyncio.wait_for(anext(reconnect_iterator), timeout=1)
        assert "event: done" in done_event
        assert '"success": 2' in done_event
        await reconnect_iterator.aclose()
    finally:
        release.set()
        task = main._running_executions.get(sid)
        if task:
            await asyncio.wait_for(task, timeout=1)
        main._running_executions.clear()
        main._execution_progress.clear()


@pytest.mark.asyncio
async def test_save_account_from_login_persists_cookie_and_activates(monkeypatch, tmp_path):
    """扫码登录成功后应把 cookie 复制到账号目录并激活账号。"""
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    src_cookie = tmp_path / "bilibili_cookie.json"
    src_cookie.write_text('{"SESSDATA":"abc","DedeUserID":"12345"}', encoding="utf-8")

    client = MagicMock()
    client.cookie_store_path = src_cookie
    client.get_my_profile = AsyncMock(return_value={
        "mid": 12345, "uname": "测试用户", "avatar_url": "http://x/avatar.jpg",
    })

    await main._save_account_from_login(client)

    active = real_storage.get_active_account()
    assert active["account_id"] == "12345"
    assert active["uname"] == "测试用户"
    assert active["cookie_path"] == "accounts/12345/bilibili_cookie.json"

    final_cookie = tmp_path / "accounts/12345/bilibili_cookie.json"
    assert final_cookie.exists()
    assert "SESSDATA" in final_cookie.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_get_bili_returns_active_account_client(monkeypatch, tmp_path):
    """有活跃账号时 get_bili 返回该账号客户端，否则返回全局 bili。"""
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    # 无活跃账号，返回全局 bili
    assert main.get_bili() is main.bili

    # 有活跃账号，返回新客户端
    cookie_path = tmp_path / "accounts/a1/bilibili_cookie.json"
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text('{"SESSDATA":"xyz","DedeUserID":"1"}', encoding="utf-8")
    real_storage.upsert_account({
        "account_id": "a1", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a1/bilibili_cookie.json",
    })
    real_storage.activate_account("a1")

    client = main.get_bili()
    assert client is not main.bili
    assert client.account_id == "a1"
    assert client.is_logged_in


@pytest.mark.asyncio
async def test_account_login_start_uses_temp_cookie_not_active_account(monkeypatch, tmp_path):
    """已登录账号 A 时添加账号 B，扫码必须用临时 cookie，不能写入 A 的 cookie 文件。"""
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    # 准备已激活账号 A
    cookie_a = tmp_path / "accounts/a1/bilibili_cookie.json"
    cookie_a.parent.mkdir(parents=True, exist_ok=True)
    cookie_a.write_text('{"SESSDATA":"a-sess","DedeUserID":"1"}', encoding="utf-8")
    real_storage.upsert_account({
        "account_id": "a1", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a1/bilibili_cookie.json",
    })
    real_storage.activate_account("a1")
    cookie_a_mtime_before = cookie_a.stat().st_mtime_ns

    # 启动新账号登录
    captured_clients = []

    async def fake_qrcode_generate(self_client):
        captured_clients.append(self_client)
        return {"qrcode_key": "qk-1", "url": "http://x/qr"}

    monkeypatch.setattr(main.BilibiliClient, "qrcode_generate", fake_qrcode_generate)

    res = await main.api_account_login_start()

    assert "login_id" in res
    login_id = res["login_id"]
    assert res["qrcode_key"] == "qk-1"
    # 临时客户端的 cookie 路径不能指向账号 A
    temp_client = captured_clients[0]
    temp_path = str(temp_client.cookie_store_path)
    assert "_pending" in temp_path
    assert "accounts/a1" not in temp_path
    # 账号 A 的 cookie 文件未被修改
    assert cookie_a.stat().st_mtime_ns == cookie_a_mtime_before
    # 临时客户端已在 _pending_logins 注册
    assert login_id in main._pending_logins


@pytest.mark.asyncio
async def test_account_login_poll_saves_to_new_account_and_cleans_temp(monkeypatch, tmp_path):
    """扫码成功后 cookie 写入新账号目录，临时文件被清理，新账号激活。"""
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    # 先启动登录会话
    async def fake_qrcode_generate(self_client):
        return {"qrcode_key": "qk-1", "url": "http://x/qr"}
    monkeypatch.setattr(main.BilibiliClient, "qrcode_generate", fake_qrcode_generate)

    start_res = await main.api_account_login_start()
    login_id = start_res["login_id"]
    temp_client = main._pending_logins[login_id]
    # 模拟扫码成功写入临时 cookie
    temp_client.cookie_store_path.parent.mkdir(parents=True, exist_ok=True)
    temp_client.cookie_store_path.write_text('{"SESSDATA":"b-sess","DedeUserID":"2"}', encoding="utf-8")

    async def fake_qrcode_poll(self_client, qrcode_key):
        return {"status": "success"}
    monkeypatch.setattr(main.BilibiliClient, "qrcode_poll", fake_qrcode_poll)

    async def fake_get_my_profile(self_client):
        return {"mid": 2, "uname": "B", "avatar_url": ""}
    monkeypatch.setattr(main.BilibiliClient, "get_my_profile", fake_get_my_profile)

    result = await main.api_account_login_poll(login_id=login_id, qrcode_key="qk-1")

    assert result["status"] == "success"
    # 新账号 cookie 已写入
    new_cookie = tmp_path / "accounts/2/bilibili_cookie.json"
    assert new_cookie.exists()
    assert "b-sess" in new_cookie.read_text(encoding="utf-8")
    # 临时文件已清理
    assert not temp_client.cookie_store_path.exists()
    # 临时客户端已从 _pending_logins 移除
    assert login_id not in main._pending_logins
    # 新账号已激活
    active = real_storage.get_active_account()
    assert active["account_id"] == "2"


@pytest.mark.asyncio
async def test_runtime_endpoint_reports_process_state():
    data = await main.api_runtime()
    assert data["pid"] > 0
    assert "started_at" in data
    assert "running_pipelines" in data
    assert "running_executions" in data
    assert "pending_logins" in data


@pytest.mark.asyncio
async def test_config_api_accepts_ai_batch_size(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)

    await main.api_save_config(main.ConfigIn(
        ai_base_url="http://x",
        ai_api_key="k",
        ai_model="m",
        ai_batch_size=120,
    ))

    cfg = await main.api_get_config()
    assert cfg["ai_batch_size"] == 120


@pytest.mark.asyncio
async def test_refine_endpoint_starts_background_job_and_reuses_running_task(monkeypatch):
    sid = "sid-refine"
    release = asyncio.Event()
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"
    manager = _FakeRefineManager(release)
    monkeypatch.setattr(main, "storage", fake_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_refinements.clear()
    main._refinement_jobs.clear()

    first = await main.api_refine_plan(sid, main.RefineIn(instruction="官方单独分类"))
    second = await main.api_refine_plan(sid, main.RefineIn(instruction="官方单独分类"))
    first_data = json.loads(first.body)
    second_data = json.loads(second.body)

    assert first.status_code == 202
    assert first_data["job_id"] == second_data["job_id"]
    assert first_data["reused"] is False
    assert second_data["reused"] is True
    release.set()
    await main._refinement_jobs[first_data["job_id"]]["task"]
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_refine_stream_reports_progress_and_done(monkeypatch):
    sid = "sid-refine-stream"
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"
    manager = _FakeRefineManager()
    monkeypatch.setattr(main, "storage", fake_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_refinements.clear()
    main._refinement_jobs.clear()

    response = await main.api_refine_plan(sid, main.RefineIn(instruction="官方单独分类"))
    job_id = json.loads(response.body)["job_id"]
    stream = await main.api_refine_stream(sid, job_id)
    chunks = []
    async for chunk in stream.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    body = "".join(chunks)
    assert "event: stage" in body
    assert '"retry_count": 1' in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_retry_unclassified_endpoint_uses_same_background_job(monkeypatch):
    sid = "sid-retry-unclassified"
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"
    manager = _FakeRefineManager()
    monkeypatch.setattr(main, "storage", fake_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_refinements.clear()
    main._refinement_jobs.clear()

    response = await main.api_retry_unclassified(sid)
    data = json.loads(response.body)
    await main._refinement_jobs[data["job_id"]]["task"]

    assert response.status_code == 202
    assert main._refinement_jobs[data["job_id"]]["kind"] == "unclassified_retry"


@pytest.mark.asyncio
async def test_refine_cancel_stops_background_job_and_stream_reports_cancelled(monkeypatch):
    sid = "sid-refine-cancel"
    release = asyncio.Event()
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"
    manager = _FakeRefineManager(release)
    monkeypatch.setattr(main, "storage", fake_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_refinements.clear()
    main._refinement_jobs.clear()

    response = await main.api_refine_plan(sid, main.RefineIn(instruction="测试取消"))
    job_id = json.loads(response.body)["job_id"]
    await asyncio.sleep(0)
    cancelled = await main.api_cancel_refinement(sid)
    stream = await main.api_refine_stream(sid, job_id)
    chunks = []
    async for chunk in stream.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    assert cancelled == {"ok": True, "cancelled": True}
    assert "event: cancelled" in "".join(chunks)


@pytest.mark.asyncio
async def test_cleanup_scan_api_persists_and_streams_progress(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.upsert_account({
        "account_id": "account-a", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a.json", "is_active": 1,
    })
    real_storage.activate_account("account-a")
    manager = _FakeCleanupManager(real_storage)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_cleanup_manager", lambda: manager)
    main._cleanup_jobs.clear()

    response = await main.api_start_cleanup_scan()
    data = json.loads(response.body)
    await main._cleanup_jobs[data["scan_id"]]["task"]
    latest = await main.api_latest_cleanup_scan()
    stream = await main.api_cleanup_stream(data["scan_id"])
    chunks = []
    async for chunk in stream.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    assert response.status_code == 202
    assert latest["scan"]["status"] == "ready"
    assert "event: stage" in "".join(chunks)
    assert "event: done" in "".join(chunks)


@pytest.mark.asyncio
async def test_cleanup_remove_api_starts_background_removal(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.upsert_account({
        "account_id": "account-a", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a.json", "is_active": 1,
    })
    real_storage.activate_account("account-a")
    scan_id = real_storage.create_cleanup_scan("account-a", 1)
    real_storage.update_cleanup_scan(scan_id, status="ready")
    manager = _FakeCleanupManager(real_storage)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_cleanup_manager", lambda: manager)
    main._cleanup_jobs.clear()

    response = await main.api_remove_cleanup_items(scan_id, main.RemoveCleanupIn(item_ids=[1, 2]))
    data = json.loads(response.body)
    await main._cleanup_jobs[scan_id]["task"]

    assert response.status_code == 202
    assert data["scan_id"] == scan_id
    assert main._cleanup_jobs[scan_id]["kind"] == "remove"


@pytest.mark.asyncio
async def test_cleanup_stream_marks_orphaned_running_scan_failed(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.upsert_account({
        "account_id": "account-a", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a.json", "is_active": 1,
    })
    real_storage.activate_account("account-a")
    scan_id = real_storage.create_cleanup_scan("account-a", 1)
    real_storage.update_cleanup_scan(scan_id, status="scanning")
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_cleanup_manager", lambda: _FakeCleanupManager(real_storage))
    main._cleanup_jobs.clear()

    stream = await main.api_cleanup_stream(scan_id)
    chunks = []
    async for chunk in stream.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    body = "".join(chunks)
    assert "event: failed" in body
    assert real_storage.get_cleanup_scan(scan_id, "account-a")["status"] == "failed"
    assert "\u91cd\u542f" in real_storage.get_cleanup_scan(scan_id, "account-a")["error"]


@pytest.mark.asyncio
async def test_create_session_passes_category_limit(monkeypatch):
    manager = MagicMock()
    manager.create_many = AsyncMock(return_value="sid-limit")
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    payload = main.SessionIn(source_fids=[100, 200], mode="quick", category_limit=8)

    result = await main.api_create_session(payload)

    assert result == {"session_id": "sid-limit"}
    manager.create_many.assert_awaited_once_with([100, 200], "quick", category_limit=8)


@pytest.mark.asyncio
async def test_logout_clears_active_account_cookie_and_state(monkeypatch, tmp_path):
    """退出当前账号：删除 cookie 文件、清除 is_active、客户端 is_logged_in 变为 False。"""
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)

    # 预置活跃账号
    cookie_rel = "accounts/1/bilibili_cookie.json"
    cookie_abs = tmp_path / cookie_rel
    cookie_abs.parent.mkdir(parents=True, exist_ok=True)
    cookie_abs.write_text('{"SESSDATA":"a","DedeUserID":"1"}', encoding="utf-8")
    real_storage.upsert_account({
        "account_id": "1", "mid": 1, "uname": "A",
        "avatar_url": "", "cookie_path": cookie_rel, "is_active": 1,
    })

    # get_bili 返回真实客户端（基于 cookie 文件）
    from core.bilibili_api import BilibiliClient
    monkeypatch.setattr(main, "get_bili", lambda: BilibiliClient(cookie_store_path=cookie_abs))

    await main.api_logout()

    assert not cookie_abs.exists()
    assert real_storage.get_active_account() is None
    assert main.get_bili().is_logged_in is False


@pytest.mark.asyncio
async def test_delete_empty_folder_revalidates_and_deletes(monkeypatch):
    client = MagicMock()
    folder = {"fid": 200, "title": "Empty", "media_count": 0, "fav_state": 0}
    client.get_my_folders = AsyncMock(side_effect=[[folder], []])
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})

    result = await main.api_delete_folder(200)

    assert result == {"ok": True, "fid": 200}
    client.delete_folders.assert_awaited_once_with([200])
    assert client.get_my_folders.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("folder", "expected_code"), [
    ({"fid": 200, "title": "Not empty", "media_count": 1, "fav_state": 0}, "FOLDER_NOT_EMPTY"),
    ({"fid": 200, "title": "Default", "media_count": 0, "fav_state": 1}, "FOLDER_DELETE_PROTECTED"),
])
async def test_delete_folder_rejects_non_empty_or_default(monkeypatch, folder, expected_code):
    client = MagicMock()
    client.get_my_folders = AsyncMock(return_value=[folder])
    client.delete_folders = AsyncMock()
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})

    with pytest.raises(BibiError) as exc_info:
        await main.api_delete_folder(200)

    assert exc_info.value.code == expected_code
    client.delete_folders.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_folder_requires_post_delete_confirmation(monkeypatch):
    client = MagicMock()
    folder = {"fid": 200, "title": "Empty", "media_count": 0, "fav_state": 0}
    client.get_my_folders = AsyncMock(side_effect=[[folder], [folder]])
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})

    with pytest.raises(BibiError) as exc_info:
        await main.api_delete_folder(200)

    assert exc_info.value.code == "FOLDER_DELETE_NOT_CONFIRMED"


@pytest.mark.asyncio
async def test_folder_resources_returns_ids_on_first_page_only(monkeypatch):
    client = MagicMock()
    client.get_my_folders = AsyncMock(return_value=[
        {"fid": 200, "title": "AI教程", "media_count": 2, "fav_state": 0},
    ])
    client.get_folder_resource_page = AsyncMock(return_value={
        "page": 1,
        "page_size": 20,
        "total": 2,
        "has_more": False,
        "items": [{"resource_id": 1, "resource_type": 2, "status": "available"}],
    })
    client.get_folder_resource_ids = AsyncMock(return_value=[
        {"resource_id": 1, "resource_type": 2, "bvid": "BV1"},
        {"resource_id": 2, "resource_type": 2, "bvid": "BV2"},
    ])
    monkeypatch.setattr(main, "get_bili", lambda: client)

    first = await main.api_folder_resources(200, page=1, page_size=20)
    second = await main.api_folder_resources(200, page=2, page_size=20)

    assert first["folder"]["title"] == "AI教程"
    assert len(first["resource_ids"]) == 2
    assert second["resource_ids"] == []
    client.get_folder_resource_ids.assert_awaited_once_with(200, storage=main.storage)
    assert client.get_folder_resource_page.await_args_list[1].kwargs["page"] == 2
