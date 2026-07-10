import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
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
