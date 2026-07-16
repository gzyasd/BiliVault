import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

import main
from core.errors import BibiError
from core.storage import Storage


class _FakeStorage:
    def __init__(self):
        self.status = "collecting"
        self.account_id = "account-a"

    def load_session(self, sid):
        return {"session_id": sid, "status": self.status, "account_id": self.account_id}

    def load_session_for_account(self, sid, account_id):
        session = self.load_session(sid)
        return session if session["account_id"] == account_id else None

    def get_active_account(self):
        return {"account_id": "account-a"}


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
            "account_id": "account-a",
        }

    def load_session_for_account(self, sid, account_id):
        session = self.load_session(sid)
        return session if account_id == "account-a" else None

    def get_active_account(self):
        return {"account_id": "account-a"}


class _FakeExecutionManager:
    def __init__(self, storage, release):
        self.storage = storage
        self.release = release
        self.calls = 0

    async def execute(self, sid, on_progress=None, run_id=None):
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
async def test_execute_stream_does_not_start_missing_job(monkeypatch):
    sid = "sid-execute-stream"
    release = asyncio.Event()
    storage = _FakeExecutionStorage()
    manager = _FakeExecutionManager(storage, release)
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_executions.clear()
    main._execution_progress.clear()

    response = await main.api_execute_stream(sid, job_id="missing-job")
    iterator = response.body_iterator
    first_event = await asyncio.wait_for(anext(iterator), timeout=1)
    assert "event: fail" in first_event
    assert "EXECUTION_NOT_RUNNING" in first_event
    await iterator.aclose()
    assert manager.calls == 0
    assert sid not in main._running_executions


@pytest.mark.asyncio
async def test_execute_post_starts_background_job_and_returns_immediately(monkeypatch):
    sid = "sid-execute-post"
    release = asyncio.Event()
    storage = _FakeExecutionStorage()
    manager = _FakeExecutionManager(storage, release)
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_executions.clear()
    main._execution_progress.clear()
    call = asyncio.create_task(main.api_execute(sid))

    try:
        await asyncio.sleep(0)
        assert call.done()
        response = call.result()
        data = json.loads(response.body)
        assert response.status_code == 202
        assert data["job_id"]
        assert data["reused"] is False
        await asyncio.sleep(0)
        assert manager.calls == 1
        release.set()
        await asyncio.wait_for(main._running_executions[sid], timeout=1)
    finally:
        release.set()
        task = main._running_executions.get(sid)
        if task:
            await asyncio.wait_for(task, timeout=1)
        main._running_executions.clear()
        main._execution_progress.clear()


@pytest.mark.asyncio
async def test_active_execution_endpoint_returns_running_job(monkeypatch):
    sid = "sid-active-execution"
    release = asyncio.Event()
    storage = _FakeExecutionStorage()
    manager = _FakeExecutionManager(storage, release)
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_executions.clear()
    main._execution_jobs.clear()
    main._execution_job_ids.clear()
    response = await main.api_execute(sid)
    job_id = json.loads(response.body)["job_id"]
    await asyncio.sleep(0)

    try:
        active = await main.api_active_execution(sid)
        assert active["job_id"] == job_id
        assert active["running"] is True
    finally:
        release.set()
        task = main._running_executions.get(sid)
        if task:
            await task


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
    assert main.get_bili() is client


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
async def test_config_api_preserves_existing_key_when_password_input_is_blank(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.save_config({
        "ai_base_url": "https://old.example.com",
        "ai_api_key": "existing-secret",
        "ai_model": "old-model",
        "default_privacy": 1,
        "ai_batch_size": 100,
    })
    monkeypatch.setattr(main, "storage", real_storage)

    await main.api_save_config(main.ConfigIn(
        ai_base_url="https://new.example.com",
        ai_api_key="",
        ai_model="new-model",
        ai_batch_size=120,
    ))

    saved = real_storage.load_config()
    assert saved["ai_api_key"] == "existing-secret"
    assert saved["ai_base_url"] == "https://new.example.com"
    assert saved["ai_model"] == "new-model"


@pytest.mark.asyncio
async def test_config_api_requires_key_when_no_existing_key(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)

    with pytest.raises(BibiError) as exc_info:
        await main.api_save_config(main.ConfigIn(
            ai_base_url="https://api.deepseek.com",
            ai_api_key="",
            ai_model="deepseek-chat",
        ))

    assert exc_info.value.code == "AI_NOT_CONFIGURED"
    assert real_storage.load_config() is None


@pytest.mark.asyncio
async def test_incomplete_config_does_not_block_application_startup(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.save_config({
        "ai_base_url": "",
        "ai_api_key": "",
        "ai_model": "",
        "default_privacy": 1,
        "ai_batch_size": 100,
    })
    get_ai = MagicMock(side_effect=AssertionError("incomplete config must not initialize AI"))
    manager = MagicMock()
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_ai", get_ai)
    monkeypatch.setattr(main, "get_session_mgr", MagicMock(return_value=manager))
    monkeypatch.setattr(main.webbrowser, "open", MagicMock())

    async with main.lifespan(main.app):
        state = await main.api_state()

    assert state["configured"] is False
    get_ai.assert_not_called()
    manager.resume_on_startup.assert_called_once_with()


@pytest.mark.asyncio
async def test_lifespan_can_disable_automatic_browser_for_headless_checks(monkeypatch):
    manager = MagicMock()
    open_browser = MagicMock()
    monkeypatch.setattr(main, "get_session_mgr", MagicMock(return_value=manager))
    monkeypatch.setattr(main.webbrowser, "open", open_browser)
    monkeypatch.setenv("BIBITOOL_NO_BROWSER", "1")

    async with main.lifespan(main.app):
        pass

    open_browser.assert_not_called()


def test_get_session_manager_does_not_require_ai_for_existing_plan(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    sid = real_storage.create_session(100, "quick")
    real_storage.update_session_account(sid, "account-a")
    real_storage.update_session_status(sid, "pending_review")
    client = MagicMock()
    client.account_id = "account-a"
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_bili", lambda: client)

    manager = main.get_session_mgr()

    assert manager.ai is None
    assert manager.get_plan(sid)["session"]["session_id"] == sid


def test_get_ai_reuses_classifier_for_unchanged_configuration(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.save_config({
        "ai_base_url": "https://api.example.com",
        "ai_api_key": "secret",
        "ai_model": "model",
        "ai_batch_size": 100,
    })
    classifier = MagicMock()
    factory = MagicMock(return_value=classifier)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "AiClassifier", factory)
    monkeypatch.setattr(main, "_ai_classifier", None)
    monkeypatch.setattr(main, "_ai_signature", None)

    first = main.get_ai()
    second = main.get_ai()

    assert first is classifier
    assert second is classifier
    factory.assert_called_once_with("https://api.example.com", "secret", "model")


@pytest.mark.asyncio
async def test_session_endpoint_rejects_session_owned_by_another_account(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.upsert_account({
        "account_id": "account-b", "mid": 2, "uname": "B", "avatar_url": "",
        "cookie_path": "accounts/b.json", "is_active": 1,
    })
    real_storage.activate_account("account-b")
    sid = real_storage.create_session(100, "quick")
    real_storage.update_session_account(sid, "account-a")
    manager = MagicMock()
    manager.get_plan.return_value = {"session": {"session_id": sid}}
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)

    with pytest.raises(BibiError) as exc_info:
        await main.api_get_session(sid)

    assert exc_info.value.code == "SESSION_NOT_FOUND"
    manager.get_plan.assert_not_called()


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
async def test_finished_refinement_job_keeps_only_lightweight_result(monkeypatch):
    sid = "sid-lightweight-refine"
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"

    class LargePlanManager:
        async def refine_plan(self, sid, instruction, on_progress=None):
            return {
                "session": {"session_id": sid},
                "items": [{"resource_id": index} for index in range(5000)],
                "versions": [{"version_id": "version-2", "is_active": 1}],
            }

    monkeypatch.setattr(main, "storage", fake_storage)
    main._running_refinements.clear()
    main._refinement_jobs.clear()

    job, _ = main._get_or_start_refinement(sid, LargePlanManager(), "refine", "重新整理")
    result = await job["task"]

    assert result == {"version_id": "version-2"}
    assert job["result"] == {"version_id": "version-2"}
    assert "items" not in job["result"]


@pytest.mark.asyncio
async def test_active_refinement_endpoint_returns_running_job(monkeypatch):
    sid = "sid-active-refine"
    release = asyncio.Event()
    fake_storage = _FakeStorage()
    fake_storage.status = "pending_review"
    manager = _FakeRefineManager(release)
    monkeypatch.setattr(main, "storage", fake_storage)
    monkeypatch.setattr(main, "get_session_mgr", lambda: manager)
    main._running_refinements.clear()
    main._refinement_jobs.clear()
    response = await main.api_refine_plan(sid, main.RefineIn(instruction="继续微调"))
    job_id = json.loads(response.body)["job_id"]
    await asyncio.sleep(0)

    try:
        active = await main.api_active_refinement(sid)
        assert active["job_id"] == job_id
        assert active["running"] is True
        assert active["kind"] == "refine"
    finally:
        release.set()
        await main._refinement_jobs[job_id]["task"]


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
async def test_finished_cleanup_scan_job_keeps_only_lightweight_result():
    class LargeCleanupManager:
        async def scan(self, scan_id, account_id, on_progress=None):
            return {
                "scan": {"scan_id": scan_id, "status": "ready", "problem_total": 5000},
                "items": [{"id": index} for index in range(5000)],
            }

    main._cleanup_jobs.clear()
    job = main._start_cleanup_job(
        "large-scan",
        "account-a",
        LargeCleanupManager(),
        "scan",
    )

    result = await job["task"]

    assert result == {
        "scan_id": "large-scan",
        "status": "ready",
        "problem_total": 5000,
    }
    assert "items" not in job["result"]


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
    monkeypatch.setattr(main, "_active_account_id", lambda: "account-a")
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
async def test_switch_account_is_blocked_by_running_refinement(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    for account_id in ("account-a", "account-b"):
        real_storage.upsert_account({
            "account_id": account_id, "mid": 1 if account_id == "account-a" else 2,
            "uname": account_id, "avatar_url": "", "cookie_path": f"accounts/{account_id}.json",
            "is_active": 1 if account_id == "account-a" else 0,
        })
    real_storage.activate_account("account-a")
    sid = real_storage.create_session(100, "quick")
    real_storage.update_session_account(sid, "account-a")
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    monkeypatch.setattr(main, "storage", real_storage)
    main._running_refinements[sid] = "refine-job"
    main._refinement_jobs["refine-job"] = {"sid": sid, "task": task}

    try:
        with pytest.raises(BibiError) as exc_info:
            await main.api_switch_account("account-b")
        assert exc_info.value.code == "PIPELINE_RUNNING"
        assert real_storage.get_active_account()["account_id"] == "account-a"
    finally:
        release.set()
        await task
        main._running_refinements.clear()
        main._refinement_jobs.clear()


@pytest.mark.asyncio
async def test_logout_is_blocked_by_running_cleanup_job(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    real_storage.upsert_account({
        "account_id": "account-a", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a.json", "is_active": 1,
    })
    real_storage.activate_account("account-a")
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    monkeypatch.setattr(main, "storage", real_storage)
    main._cleanup_jobs["scan-1"] = {"account_id": "account-a", "task": task}

    try:
        with pytest.raises(BibiError) as exc_info:
            await main.api_logout()
        assert exc_info.value.code == "PIPELINE_RUNNING"
        assert real_storage.get_active_account()["account_id"] == "account-a"
    finally:
        release.set()
        await task
        main._cleanup_jobs.clear()


@pytest.mark.asyncio
async def test_finished_job_registries_are_pruned_after_retention(monkeypatch):
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    main._execution_jobs["old-job"] = {
        "sid": "sid", "task": task, "finished_at": 1.0,
    }

    main._prune_finished_jobs(now=main.JOB_RETENTION_SECONDS + 2.0)

    assert "old-job" not in main._execution_jobs


@pytest.mark.asyncio
async def test_expired_pending_login_removes_client_temp_cookie_and_closes_http(tmp_path):
    login_id = "expired-login"
    cookie_path = tmp_path / "pending.json"
    cookie_path.write_text("{}", encoding="utf-8")
    client = MagicMock()
    client.cookie_store_path = cookie_path
    client.aclose = AsyncMock()
    main._pending_logins[login_id] = client
    main._pending_login_started[login_id] = 1.0

    main._prune_pending_logins(now=main.LOGIN_SESSION_TTL_SECONDS + 2.0)
    await asyncio.sleep(0)

    assert login_id not in main._pending_logins
    assert login_id not in main._pending_login_started
    assert not cookie_path.exists()
    client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pending_account_login_can_be_cancelled_explicitly(tmp_path):
    login_id = "cancel-login"
    cookie_path = tmp_path / "pending.json"
    cookie_path.write_text("{}", encoding="utf-8")
    client = MagicMock()
    client.cookie_store_path = cookie_path
    client.aclose = AsyncMock()
    main._pending_logins[login_id] = client
    main._pending_login_started[login_id] = 1.0

    result = await main.api_cancel_account_login(login_id)
    await asyncio.sleep(0)

    assert result == {"ok": True, "cancelled": True}
    assert not cookie_path.exists()
    client.aclose.assert_awaited_once_with()


def test_pending_login_registration_schedules_real_expiry(monkeypatch):
    client = MagicMock()
    handle = MagicMock()
    loop = MagicMock()
    loop.call_later.return_value = handle
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: loop)
    main._pending_logins.clear()
    main._pending_login_started.clear()
    main._pending_login_expiry_handles.clear()

    main._register_pending_login("login-with-ttl", client)

    assert main._pending_logins["login-with-ttl"] is client
    loop.call_later.assert_called_once_with(
        main.LOGIN_SESSION_TTL_SECONDS,
        main._discard_pending_login,
        "login-with-ttl",
    )
    assert main._pending_login_expiry_handles["login-with-ttl"] is handle


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_tasks_closes_clients_and_clears_registries(monkeypatch):
    task = asyncio.create_task(asyncio.Event().wait())
    default_client = MagicMock()
    default_client.aclose = AsyncMock()
    account_client = MagicMock()
    account_client.aclose = AsyncMock()
    pending_client = MagicMock()
    pending_client.cookie_store_path = None
    pending_client.aclose = AsyncMock()
    monkeypatch.setattr(main, "bili", default_client)
    monkeypatch.setattr(main, "_bili_clients", {("a", "x"): account_client})
    monkeypatch.setattr(main, "_pending_logins", {"login": pending_client})
    monkeypatch.setattr(main, "_pending_login_started", {"login": 1.0})
    monkeypatch.setattr(main, "_running_pipelines", {"sid": task})
    monkeypatch.setattr(main, "_running_executions", {})
    monkeypatch.setattr(main, "_execution_jobs", {})
    monkeypatch.setattr(main, "_execution_job_ids", {})
    monkeypatch.setattr(main, "_execution_progress", {})
    monkeypatch.setattr(main, "_running_refinements", {})
    monkeypatch.setattr(main, "_refinement_jobs", {})
    monkeypatch.setattr(main, "_cleanup_jobs", {})

    await main._shutdown_runtime()

    assert task.cancelled()
    default_client.aclose.assert_awaited_once_with()
    account_client.aclose.assert_awaited_once_with()
    pending_client.aclose.assert_awaited_once_with()
    assert main._running_pipelines == {}
    assert main._pending_logins == {}


@pytest.mark.asyncio
async def test_runtime_shutdown_ignores_completed_tasks_from_closed_event_loop(monkeypatch):
    foreign_loop = asyncio.new_event_loop()
    foreign_future = foreign_loop.create_future()
    foreign_future.set_result(None)
    foreign_loop.close()
    monkeypatch.setattr(main, "_running_pipelines", {"old-sid": foreign_future})
    monkeypatch.setattr(main, "_running_executions", {})
    monkeypatch.setattr(main, "_execution_jobs", {})
    monkeypatch.setattr(main, "_execution_job_ids", {})
    monkeypatch.setattr(main, "_execution_progress", {})
    monkeypatch.setattr(main, "_running_refinements", {})
    monkeypatch.setattr(main, "_refinement_jobs", {})
    monkeypatch.setattr(main, "_cleanup_jobs", {})
    monkeypatch.setattr(main, "_pending_logins", {})
    monkeypatch.setattr(main, "_pending_login_started", {})
    monkeypatch.setattr(main, "_bili_clients", {})

    await main._shutdown_runtime()

    assert main._running_pipelines == {}


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
    ({"fid": 200, "title": "Default", "media_count": 0, "fav_state": 0, "is_default": True}, "FOLDER_DELETE_PROTECTED"),
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
    client.get_my_folders = AsyncMock(side_effect=[[folder]] * 5)
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    with pytest.raises(BibiError) as exc_info:
        await main.api_delete_folder(200)

    assert exc_info.value.code == "FOLDER_DELETE_NOT_CONFIRMED"
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_delete_folder_accepts_delayed_remote_confirmation(monkeypatch):
    client = MagicMock()
    folder = {"fid": 200, "title": "Empty", "media_count": 0, "fav_state": 0}
    client.get_my_folders = AsyncMock(side_effect=[[folder], [folder], []])
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    result = await main.api_delete_folder(200)

    assert result == {"ok": True, "fid": 200}
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_delete_folder_distinguishes_confirmation_transport_failure(monkeypatch):
    client = MagicMock()
    folder = {"fid": 200, "title": "Empty", "media_count": 0, "fav_state": 0}
    client.get_my_folders = AsyncMock(side_effect=[
        [folder],
        RuntimeError("network unavailable"),
        RuntimeError("network unavailable"),
        RuntimeError("network unavailable"),
        RuntimeError("network unavailable"),
    ])
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    with pytest.raises(BibiError) as exc_info:
        await main.api_delete_folder(200)

    assert exc_info.value.code == "FOLDER_STATE_CONFIRM_FAILED"
    assert "操作已经提交" in exc_info.value.user_message
    client.delete_folders.assert_awaited_once_with([200])


@pytest.mark.asyncio
async def test_batch_delete_empty_folders_uses_one_delete_and_one_confirmation(monkeypatch):
    client = MagicMock()
    folders = [
        {"fid": 200, "title": "Empty A", "media_count": 0, "fav_state": 0},
        {"fid": 201, "title": "Empty B", "media_count": 0, "fav_state": 0},
    ]
    client.get_my_folders = AsyncMock(side_effect=[folders, []])
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})

    result = await main.api_batch_delete_folders(main.DeleteFoldersIn(fids=[200, 201, 200]))

    assert result == {
        "stats": {"total": 2, "success": 2, "failed": 0},
        "deleted_fids": [200, 201],
        "failed_fids": [],
    }
    client.delete_folders.assert_awaited_once_with([200, 201])
    assert client.get_my_folders.await_count == 2


@pytest.mark.asyncio
async def test_batch_delete_empty_folders_rejects_non_empty_before_delete(monkeypatch):
    client = MagicMock()
    client.get_my_folders = AsyncMock(return_value=[
        {"fid": 200, "title": "Empty", "media_count": 0, "fav_state": 0},
        {"fid": 201, "title": "Changed", "media_count": 1, "fav_state": 0},
    ])
    client.delete_folders = AsyncMock()
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})

    with pytest.raises(BibiError) as exc_info:
        await main.api_batch_delete_folders(main.DeleteFoldersIn(fids=[200, 201]))

    assert exc_info.value.code == "FOLDER_NOT_EMPTY"
    client.delete_folders.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_delete_empty_folders_reports_unconfirmed_items(monkeypatch):
    client = MagicMock()
    folders = [
        {"fid": 200, "title": "Empty A", "media_count": 0, "fav_state": 0},
        {"fid": 201, "title": "Empty B", "media_count": 0, "fav_state": 0},
    ]
    client.get_my_folders = AsyncMock(side_effect=[folders] + [[folders[1]]] * 4)
    client.delete_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main, "_running_pipelines", {})
    monkeypatch.setattr(main, "_running_executions", {})
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    result = await main.api_batch_delete_folders(main.DeleteFoldersIn(fids=[200, 201]))

    assert result["stats"] == {"total": 2, "success": 1, "failed": 1}
    assert result["deleted_fids"] == [200]
    assert result["failed_fids"] == [201]


@pytest.mark.asyncio
async def test_sort_folders_validates_complete_order_and_confirms_result(monkeypatch):
    client = MagicMock()
    original = [
        {"fid": 100, "title": "默认收藏夹"},
        {"fid": 200, "title": "动画"},
        {"fid": 300, "title": "科技"},
    ]
    confirmed = [original[2], original[0], original[1]]
    client.get_my_folders = AsyncMock(side_effect=[original, confirmed])
    client.sort_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)

    result = await main.api_sort_folders(main.SortFoldersIn(fids=[300, 100, 200]))

    assert result == {"ok": True, "fids": [300, 100, 200]}
    client.sort_folders.assert_awaited_once_with([300, 100, 200])
    assert client.get_my_folders.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [
    [100, 200],
    [100, 200, 999],
    [100, 100, 200],
])
async def test_sort_folders_rejects_incomplete_foreign_or_duplicate_ids(monkeypatch, requested):
    client = MagicMock()
    client.get_my_folders = AsyncMock(return_value=[
        {"fid": 100}, {"fid": 200}, {"fid": 300},
    ])
    client.sort_folders = AsyncMock()
    monkeypatch.setattr(main, "get_bili", lambda: client)

    with pytest.raises(BibiError) as exc_info:
        await main.api_sort_folders(main.SortFoldersIn(fids=requested))

    assert exc_info.value.code == "FOLDER_SORT_INVALID"
    client.sort_folders.assert_not_awaited()


@pytest.mark.asyncio
async def test_sort_folders_requires_remote_confirmation(monkeypatch):
    client = MagicMock()
    original = [{"fid": 100}, {"fid": 200}, {"fid": 300}]
    client.get_my_folders = AsyncMock(side_effect=[original] * 5)
    client.sort_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    with pytest.raises(BibiError) as exc_info:
        await main.api_sort_folders(main.SortFoldersIn(fids=[300, 100, 200]))

    assert exc_info.value.code == "FOLDER_SORT_NOT_CONFIRMED"


@pytest.mark.asyncio
async def test_sort_folders_accepts_delayed_remote_confirmation(monkeypatch):
    client = MagicMock()
    original = [{"fid": 100}, {"fid": 200}, {"fid": 300}]
    confirmed = [original[2], original[0], original[1]]
    client.get_my_folders = AsyncMock(side_effect=[original, original, confirmed])
    client.sort_folders = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "get_bili", lambda: client)
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    result = await main.api_sort_folders(main.SortFoldersIn(fids=[300, 100, 200]))

    assert result == {"ok": True, "fids": [300, 100, 200]}
    sleep.assert_awaited_once_with(0.5)


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


def test_server_binds_to_loopback_by_default(monkeypatch):
    monkeypatch.delenv("BIBITOOL_HOST", raising=False)
    monkeypatch.delenv("BIBITOOL_LAN_TOKEN", raising=False)
    monkeypatch.delenv("BIBITOOL_ALLOWED_HOSTS", raising=False)

    settings = main._resolve_bind_settings()

    assert settings.host == "127.0.0.1"
    assert settings.lan_auth_enabled is False


def test_api_input_models_validate_modes_ids_and_trim_text():
    with pytest.raises(ValidationError):
        main.SessionIn(source_fids=[100], mode="unexpected")
    with pytest.raises(ValidationError):
        main.SessionIn(source_fids=[0], mode="quick")
    with pytest.raises(ValidationError):
        main.RefineIn(instruction="   ")
    with pytest.raises(ValidationError):
        main.RemoveCleanupIn(item_ids=list(range(1, 5002)))

    session = main.SessionIn(source_fids=[100, 100, 200], mode="full")
    adjust = main.AdjustIn(resource_id=1, resource_type=2, new_category="  科技数码  ")
    assert session.normalized_source_fids() == [100, 200]
    assert adjust.new_category == "科技数码"


def test_default_privacy_is_not_exposed_as_an_ineffective_config_option():
    config = main.ConfigIn(ai_base_url="http://x", ai_api_key="k", ai_model="m")
    assert "default_privacy" not in config.model_dump()


def test_non_loopback_bind_requires_token_and_allowed_hosts(monkeypatch):
    monkeypatch.setenv("BIBITOOL_HOST", "0.0.0.0")
    monkeypatch.delenv("BIBITOOL_LAN_TOKEN", raising=False)
    monkeypatch.delenv("BIBITOOL_ALLOWED_HOSTS", raising=False)

    with pytest.raises(RuntimeError, match="BIBITOOL_LAN_TOKEN"):
        main._resolve_bind_settings()

    monkeypatch.setenv("BIBITOOL_LAN_TOKEN", "0123456789abcdef")
    with pytest.raises(RuntimeError, match="BIBITOOL_ALLOWED_HOSTS"):
        main._resolve_bind_settings()


@pytest.mark.asyncio
async def test_lan_mode_requires_pairing_cookie_and_rejects_cross_origin_writes(monkeypatch):
    monkeypatch.setenv("BIBITOOL_HOST", "0.0.0.0")
    monkeypatch.setenv("BIBITOOL_LAN_TOKEN", "0123456789abcdef")
    monkeypatch.setenv("BIBITOOL_ALLOWED_HOSTS", "lan.example")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://lan.example") as client:
        unauthorized = await client.get("/api/runtime")
        assert unauthorized.status_code == 401

        paired = await client.get("/?access_token=0123456789abcdef")
        assert paired.status_code == 303
        assert client.cookies.get("bibitool_access") == "0123456789abcdef"

        runtime = await client.get("/api/runtime")
        assert runtime.status_code == 200

        cross_origin = await client.post(
            "/api/logout",
            headers={"Origin": "http://attacker.example"},
        )
        assert cross_origin.status_code == 403


@pytest.mark.asyncio
async def test_local_mode_rejects_untrusted_host_and_cross_origin_writes(monkeypatch):
    monkeypatch.delenv("BIBITOOL_HOST", raising=False)
    monkeypatch.delenv("BIBITOOL_LAN_TOKEN", raising=False)
    monkeypatch.delenv("BIBITOOL_ALLOWED_HOSTS", raising=False)
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://attacker.example") as client:
        invalid_host = await client.get("/api/runtime")
        assert invalid_host.status_code == 400

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        cross_origin = await client.post(
            "/api/logout",
            headers={"Origin": "http://attacker.example"},
        )
        assert cross_origin.status_code == 403
