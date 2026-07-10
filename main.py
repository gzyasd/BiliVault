import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
for _vendor in (BASE_DIR / ".libs", BASE_DIR / "_libs"):
    if _vendor.is_dir():
        sys.path.insert(0, str(_vendor))

import qrcode
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.ai_classifier import AiClassifier
from core.bilibili_api import BilibiliClient
from core.errors import BibiError
from core.session import ClassifySession
from core.storage import Storage


class UvicornAccessFilter(logging.Filter):
    """过滤高频轮询/SSE 路径的访问日志，降低控制台刷屏与 conhost CPU 占用。"""

    HIGH_FREQ_PATHS = (
        re.compile(r"GET /api/session/.*/stream"),
        re.compile(r"GET /api/accounts/login/poll"),
        re.compile(r"GET /api/qrcode/poll"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(pattern.search(message) for pattern in self.HIGH_FREQ_PATHS)


logging.getLogger("uvicorn.access").addFilter(UvicornAccessFilter())

STARTED_AT = datetime.now().isoformat(timespec="seconds")

storage = Storage(BASE_DIR)
bili = BilibiliClient(cookie_store_path=BASE_DIR / "bilibili_cookie.json")
_running_pipelines: dict[str, asyncio.Task] = {}
_pending_logins: dict[str, "BilibiliClient"] = {}


def get_bili() -> BilibiliClient:
    """根据活跃账号返回对应的 B 站客户端；无活跃账号时返回全局默认客户端。"""
    account = storage.get_active_account()
    if account and account.get("cookie_path"):
        return BilibiliClient(
            cookie_store_path=BASE_DIR / account["cookie_path"],
            account_id=account["account_id"],
        )
    return bili


async def _save_account_from_login(client: BilibiliClient) -> None:
    """扫码登录成功后，读取账号资料并把 Cookie 持久化到账号目录。"""
    try:
        profile = await client.get_my_profile()
    except Exception:
        return
    if not profile.get("mid"):
        return
    account_id = str(profile["mid"])
    cookie_path = f"accounts/{account_id}/bilibili_cookie.json"
    final_cookie_path = BASE_DIR / cookie_path
    final_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    if client.cookie_store_path:
        src = client.cookie_store_path.resolve()
        dst = final_cookie_path.resolve()
        if src != dst:
            if src.exists():
                shutil.copy2(str(src), str(dst))
            else:
                client.cookie_store_path = final_cookie_path
                client.save_cookies()
    storage.upsert_account({
        "account_id": account_id,
        "mid": profile["mid"],
        "uname": profile["uname"],
        "avatar_url": profile.get("avatar_url", ""),
        "cookie_path": cookie_path,
        "is_active": 1,
    })
    storage.activate_account(account_id)


def _forget_pipeline_when_done(sid: str, task: asyncio.Task) -> None:
    if _running_pipelines.get(sid) is task:
        _running_pipelines.pop(sid, None)
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


def _get_or_start_pipeline(sid: str, mgr: ClassifySession, on_progress) -> tuple[asyncio.Task, bool]:
    existing = _running_pipelines.get(sid)
    if existing and not existing.done():
        return existing, True
    task = asyncio.create_task(mgr.run_pipeline(sid, on_progress=on_progress))
    _running_pipelines[sid] = task
    task.add_done_callback(lambda done_task: _forget_pipeline_when_done(sid, done_task))
    return task, False


def get_ai() -> AiClassifier:
    cfg = storage.load_config()
    if not cfg:
        raise BibiError("请先在设置页填写 AI 配置", code="AI_NOT_CONFIGURED")
    return AiClassifier(cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"])


def get_session_mgr() -> ClassifySession:
    return ClassifySession(storage, get_bili(), get_ai())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if storage.load_config():
        ClassifySession(storage, get_bili(), get_ai()).resume_on_startup()
    webbrowser.open("http://127.0.0.1:8765")
    yield


app = FastAPI(lifespan=lifespan)


class ConfigIn(BaseModel):
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    default_privacy: int = 1
    ai_batch_size: int = Field(default=100, ge=10, le=200)


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


class RemoveSkippedIn(BaseModel):
    item_ids: list[int]


class RefineIn(BaseModel):
    instruction: str


class DeleteEmptyFoldersIn(BaseModel):
    source_fids: list[int]


@app.exception_handler(BibiError)
async def bibi_error_handler(_, exc: BibiError):
    return JSONResponse({"code": exc.code, "message": exc.user_message}, status_code=400)


@app.get("/api/state")
async def api_state():
    client = get_bili()
    return {"logged_in": client.is_logged_in, "configured": storage.load_config() is not None}


@app.get("/api/runtime")
async def api_runtime():
    return {
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "running_pipelines": len(_running_pipelines),
        "pending_logins": len(_pending_logins),
    }


@app.post("/api/qrcode/generate")
async def api_qrcode_generate():
    client = get_bili()
    data = await client.qrcode_generate()
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"qrcode_key": data["qrcode_key"], "image": f"data:image/png;base64,{b64}"}


@app.get("/api/qrcode/poll")
async def api_qrcode_poll(qrcode_key: str):
    client = get_bili()
    result = await client.qrcode_poll(qrcode_key)
    if result.get("status") == "success":
        await _save_account_from_login(client)
    return result


@app.post("/api/logout")
async def api_logout():
    client = get_bili()
    active = storage.get_active_account()
    client.clear_cookies()
    if active:
        if active.get("cookie_path"):
            cookie_file = BASE_DIR / active["cookie_path"]
            if cookie_file.exists():
                cookie_file.unlink()
        storage.deactivate_account(active["account_id"])
    return {"ok": True}


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
    """新增账号扫码登录：使用独立临时 cookie 文件，避免覆盖当前活跃账号。"""
    import uuid
    login_id = uuid.uuid4().hex
    temp_cookie_path = BASE_DIR / f"accounts/_pending/{login_id}.json"
    temp_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    temp_client = BilibiliClient(cookie_store_path=temp_cookie_path)
    data = await temp_client.qrcode_generate()
    _pending_logins[login_id] = temp_client
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {
        "login_id": login_id,
        "qrcode_key": data["qrcode_key"],
        "image": f"data:image/png;base64,{b64}",
    }


@app.get("/api/accounts/login/poll")
async def api_account_login_poll(login_id: str, qrcode_key: str):
    """新增账号扫码轮询：使用临时客户端，成功后保存到新账号目录并清理临时文件。"""
    temp_client = _pending_logins.get(login_id)
    if not temp_client:
        raise BibiError("登录会话不存在或已过期，请重新扫码", code="LOGIN_SESSION_NOT_FOUND")
    result = await temp_client.qrcode_poll(qrcode_key)
    if result.get("status") == "success":
        try:
            await _save_account_from_login(temp_client)
        finally:
            # 清理临时 cookie 文件和会话
            try:
                if temp_client.cookie_store_path and temp_client.cookie_store_path.exists():
                    temp_client.cookie_store_path.unlink()
            except Exception:
                pass
            _pending_logins.pop(login_id, None)
    return result


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
        "ai_batch_size": int(cfg.get("ai_batch_size", 100)),
    }


@app.post("/api/config")
async def api_save_config(cfg: ConfigIn):
    storage.save_config(cfg.model_dump())
    return {"ok": True}


@app.get("/api/folders")
async def api_folders():
    client = get_bili()
    folders = await client.get_my_folders(storage=storage)
    active = storage.get_active_account()
    account_id = active["account_id"] if active else ""
    for f in folders:
        f["account_id"] = account_id
        storage.upsert_folder(f)
    return {"folders": folders}


@app.post("/api/session")
async def api_create_session(payload: SessionIn):
    source_fids = payload.normalized_source_fids()
    if not source_fids:
        raise BibiError("请至少选择一个收藏夹", code="NO_SOURCE_FOLDER")
    mgr = get_session_mgr()
    sid = await mgr.create_many(source_fids, payload.mode)
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
        s = storage.load_session(sid)
        if not s:
            yield f"event: fail\ndata: {json.dumps({'message': '会话不存在', 'code': 'NOT_FOUND'}, ensure_ascii=False)}\n\n"
            return
        if s["status"] == "cancelled":
            yield f"event: fail\ndata: {json.dumps({'message': '已取消', 'code': 'CANCELLED'}, ensure_ascii=False)}\n\n"
            return
        if s["status"] in ("pending_review", "done"):
            yield f"event: done\ndata: {json.dumps({'stage': s['status']}, ensure_ascii=False)}\n\n"
            return
        task, reused = _get_or_start_pipeline(sid, mgr, on_progress)
        while not task.done():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield f"event: stage\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                cur = storage.load_session(sid)
                if cur and cur["status"] == "cancelled":
                    task.cancel()
                    yield f"event: fail\ndata: {json.dumps({'message': '已取消', 'code': 'CANCELLED'}, ensure_ascii=False)}\n\n"
                    return
                if reused and cur:
                    prog = storage.compute_session_progress(sid)
                    progress = None
                    if prog["source_total"]:
                        progress = min(0.99, prog["scanned"] / prog["source_total"])
                    yield f"event: stage\ndata: {json.dumps({'stage': cur['status'], 'progress': progress, 'reused': True, 'source_total': prog['source_total'], 'scanned': prog['scanned'], 'collected': prog['collected'], 'skipped': prog['skipped']}, ensure_ascii=False)}\n\n"
                continue
        if task.cancelled():
            yield f"event: fail\ndata: {json.dumps({'message': '已取消', 'code': 'CANCELLED'}, ensure_ascii=False)}\n\n"
            return
        exc = task.exception()
        if exc:
            msg = exc.user_message if isinstance(exc, BibiError) else str(exc)
            code = exc.code if isinstance(exc, BibiError) else "INTERNAL"
            yield f"event: fail\ndata: {json.dumps({'message': msg, 'code': code}, ensure_ascii=False)}\n\n"
        else:
            cur = storage.load_session(sid)
            if cur and cur["status"] == "cancelled":
                yield f"event: fail\ndata: {json.dumps({'message': '已取消', 'code': 'CANCELLED'}, ensure_ascii=False)}\n\n"
            else:
                yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/session/{sid}/cancel")
async def api_cancel_session(sid: str):
    mgr = get_session_mgr()
    mgr.cancel(sid)
    task = _running_pipelines.get(sid)
    if task and not task.done():
        task.cancel()
    return {"ok": True}


@app.post("/api/session/{sid}/adjust")
async def api_adjust(sid: str, payload: AdjustIn):
    mgr = get_session_mgr()
    mgr.adjust_item(sid, payload.normalized_resource_id(), payload.new_category, resource_type=payload.resource_type)
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


@app.get("/api/session/{sid}/skipped-items")
async def api_skipped_items(sid: str):
    return {"items": storage.list_skipped_items(sid)}


@app.post("/api/session/{sid}/skipped-items/remove")
async def api_remove_skipped_items(sid: str, payload: RemoveSkippedIn):
    mgr = get_session_mgr()
    return {"stats": await mgr.remove_skipped_items(sid, payload.item_ids)}


@app.post("/api/session/{sid}/retry-failed")
async def api_retry_failed(sid: str):
    mgr = get_session_mgr()
    stats = await mgr.retry_failed(sid)
    return {"stats": stats}


@app.post("/api/session/{sid}/refine")
async def api_refine_plan(sid: str, payload: RefineIn):
    mgr = get_session_mgr()
    return await mgr.refine_plan(sid, payload.instruction)


@app.post("/api/session/{sid}/versions/{version_id}/activate")
async def api_activate_version(sid: str, version_id: str):
    storage.activate_plan_version(sid, version_id)
    return get_session_mgr().get_plan(sid)


@app.get("/api/session/{sid}/empty-source-folders")
async def api_empty_source_folders(sid: str):
    return {"items": storage.list_empty_source_candidates(sid)}


@app.post("/api/session/{sid}/empty-source-folders/delete")
async def api_delete_empty_source_folders(sid: str, payload: DeleteEmptyFoldersIn):
    mgr = get_session_mgr()
    return {"stats": await mgr.delete_empty_source_folders(sid, payload.source_fids)}


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
    uvicorn.run(app, host="0.0.0.0", port=8765, access_log=True)
