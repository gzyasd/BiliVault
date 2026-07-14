import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
import uuid
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
for _vendor in (BASE_DIR / ".libs", BASE_DIR / "_libs"):
    if _vendor.is_dir():
        sys.path.insert(0, str(_vendor))

import qrcode
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.ai_classifier import AiClassifier
from core.bilibili_api import BilibiliClient
from core.cleanup import CleanupManager
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
_running_executions: dict[str, asyncio.Task] = {}
_execution_progress: dict[str, dict] = {}
_running_refinements: dict[str, str] = {}
_refinement_jobs: dict[str, dict] = {}
_cleanup_jobs: dict[str, dict] = {}
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


def _forget_execution_when_done(sid: str, task: asyncio.Task) -> None:
    if _running_executions.get(sid) is task:
        _running_executions.pop(sid, None)
    _execution_progress.pop(sid, None)
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


def _get_or_start_execution(sid: str, mgr: ClassifySession) -> tuple[asyncio.Task, bool]:
    existing = _running_executions.get(sid)
    if existing and not existing.done():
        return existing, True

    async def on_progress(event):
        _execution_progress[sid] = dict(event)

    task = asyncio.create_task(mgr.execute(sid, on_progress=on_progress))
    _running_executions[sid] = task
    task.add_done_callback(lambda done_task: _forget_execution_when_done(sid, done_task))
    return task, False


def _get_or_start_refinement(
    sid: str,
    mgr: ClassifySession,
    kind: str,
    instruction: str = "",
) -> tuple[dict, bool]:
    active_job_id = _running_refinements.get(sid)
    active_job = _refinement_jobs.get(active_job_id or "")
    if active_job and not active_job["task"].done():
        return active_job, True

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id": job_id,
        "sid": sid,
        "kind": kind,
        "progress": {
            "stage": "analyzing",
            "processed": 0,
            "total": 0,
            "progress": 0.0,
            "retry_count": 0,
        },
        "result": None,
    }

    async def on_progress(event):
        job["progress"] = dict(event)

    async def run():
        if kind == "unclassified_retry":
            result = await mgr.retry_unclassified(sid, on_progress=on_progress)
        else:
            result = await mgr.refine_plan(sid, instruction, on_progress=on_progress)
        job["result"] = result
        return result

    task = asyncio.create_task(run())
    job["task"] = task
    _refinement_jobs[job_id] = job
    _running_refinements[sid] = job_id

    def forget(done_task: asyncio.Task) -> None:
        if _running_refinements.get(sid) == job_id:
            _running_refinements.pop(sid, None)
        if not done_task.cancelled():
            try:
                done_task.exception()
            except Exception:
                pass

    task.add_done_callback(forget)
    return job, False


def _start_cleanup_job(
    scan_id: str,
    account_id: str,
    manager: CleanupManager,
    kind: str,
    item_ids: list[int] | None = None,
) -> dict:
    job: dict = {
        "scan_id": scan_id,
        "account_id": account_id,
        "kind": kind,
        "progress": {"stage": "queued", "progress": 0.0},
        "result": None,
    }

    async def on_progress(event):
        job["progress"] = dict(event)

    async def run():
        if kind == "remove":
            result = await manager.remove(scan_id, account_id, item_ids or [], on_progress=on_progress)
        else:
            result = await manager.scan(scan_id, account_id, on_progress=on_progress)
        job["result"] = result
        return result

    task = asyncio.create_task(run())
    job["task"] = task
    _cleanup_jobs[scan_id] = job

    def consume(done_task: asyncio.Task) -> None:
        if not done_task.cancelled():
            try:
                done_task.exception()
            except Exception:
                pass

    task.add_done_callback(consume)
    return job


def _load_session_stats(session: dict) -> dict:
    raw = session.get("stats")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def get_ai() -> AiClassifier:
    cfg = storage.load_config()
    if not cfg:
        raise BibiError("请先在设置页填写 AI 配置", code="AI_NOT_CONFIGURED")
    return AiClassifier(cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"])


def get_session_mgr() -> ClassifySession:
    return ClassifySession(storage, get_bili(), get_ai())


def get_cleanup_manager() -> CleanupManager:
    return CleanupManager(storage, get_bili())


def _active_account_id() -> str:
    account = storage.get_active_account()
    if not account:
        raise BibiError("请先登录 B 站账号", code="NOT_LOGGED_IN")
    return str(account["account_id"])


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
    category_limit: int = Field(default=14, ge=3, le=50)

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


class RemoveCleanupIn(BaseModel):
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
        "running_executions": len(_running_executions),
        "running_refinements": len(_running_refinements),
        "running_cleanup_jobs": sum(1 for job in _cleanup_jobs.values() if not job["task"].done()),
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
    if _running_pipelines or _running_executions:
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


@app.delete("/api/folders/{fid}")
async def api_delete_folder(fid: int):
    if _running_pipelines or _running_executions:
        raise BibiError("有整理任务正在运行，请等待任务完成后再删除收藏夹", code="PIPELINE_RUNNING")

    client = get_bili()
    folders = await client.get_my_folders(storage=storage)
    folder = next((item for item in folders if int(item["fid"]) == fid), None)
    if not folder:
        raise BibiError("收藏夹不存在或已被删除", code="FOLDER_NOT_FOUND")
    if int(folder.get("fav_state", 0)) == 1:
        raise BibiError("默认收藏夹不能删除", code="FOLDER_DELETE_PROTECTED")
    if int(folder.get("media_count", 0)) != 0:
        raise BibiError("该收藏夹不为空，不能直接删除", code="FOLDER_NOT_EMPTY")

    await client.delete_folders([fid])
    latest = await client.get_my_folders(storage=storage)
    if any(int(item["fid"]) == fid for item in latest):
        raise BibiError("B站尚未确认删除该收藏夹，请稍后重试", code="FOLDER_DELETE_NOT_CONFIRMED")
    return {"ok": True, "fid": fid}


@app.get("/api/folders/{fid}/resources")
async def api_folder_resources(
    fid: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=50),
):
    client = get_bili()
    folders = await client.get_my_folders(storage=storage)
    folder = next((item for item in folders if int(item["fid"]) == fid), None)
    if not folder:
        raise BibiError("收藏夹不存在或不属于当前账号", code="FOLDER_NOT_FOUND")

    result = dict(await client.get_folder_resource_page(
        fid,
        page=page,
        page_size=page_size,
        storage=storage,
    ))
    result["folder"] = folder
    result["resource_ids"] = (
        await client.get_folder_resource_ids(fid, storage=storage)
        if page == 1
        else []
    )
    return result


@app.post("/api/cleanup/scans")
async def api_start_cleanup_scan():
    account_id = _active_account_id()
    latest = storage.get_latest_cleanup_scan(account_id)
    if latest and latest["status"] in ("queued", "scanning", "removing"):
        existing = _cleanup_jobs.get(latest["scan_id"])
        if existing and not existing["task"].done():
            return JSONResponse(
                {"scan_id": latest["scan_id"], "reused": True, "kind": existing["kind"]},
                status_code=202,
            )
    scan_id = storage.create_cleanup_scan(account_id, folders_total=0)
    job = _start_cleanup_job(scan_id, account_id, get_cleanup_manager(), "scan")
    return JSONResponse(
        {"scan_id": scan_id, "reused": False, "kind": job["kind"]},
        status_code=202,
    )


@app.get("/api/cleanup/scans/latest")
async def api_latest_cleanup_scan():
    account_id = _active_account_id()
    latest = storage.get_latest_cleanup_scan(account_id)
    if not latest:
        return {"scan": None, "items": []}
    return get_cleanup_manager().get_scan(latest["scan_id"], account_id)


@app.get("/api/cleanup/scans/{scan_id}")
async def api_get_cleanup_scan(scan_id: str):
    return get_cleanup_manager().get_scan(scan_id, _active_account_id())


@app.get("/api/cleanup/scans/{scan_id}/stream")
async def api_cleanup_stream(scan_id: str):
    account_id = _active_account_id()
    get_cleanup_manager().get_scan(scan_id, account_id)

    async def event_gen():
        job = _cleanup_jobs.get(scan_id)
        if not job:
            scan = storage.get_cleanup_scan(scan_id, account_id)
            if scan and scan["status"] in ("ready", "completed"):
                yield f"event: done\ndata: {json.dumps({'scan_id': scan_id, 'status': scan['status']}, ensure_ascii=False)}\n\n"
            else:
                if scan and scan["status"] in ("queued", "scanning", "removing"):
                    message = "任务因程序重启而中断，请重新扫描或重试删除"
                    storage.update_cleanup_scan(scan_id, status="failed", error=message, current_folder_title="")
                else:
                    message = scan.get("error") if scan else "清理任务不存在"
                yield f"event: failed\ndata: {json.dumps({'message': message, 'code': 'CLEANUP_NOT_RUNNING'}, ensure_ascii=False)}\n\n"
            return
        task = job["task"]
        last_event = None
        while True:
            latest_event = job.get("progress")
            if latest_event is not None and latest_event != last_event:
                last_event = dict(latest_event)
                yield f"event: stage\ndata: {json.dumps(latest_event, ensure_ascii=False)}\n\n"
            if task.done():
                break
            await asyncio.sleep(0.1)
        if task.cancelled():
            yield f"event: cancelled\ndata: {json.dumps({'message': '扫描已取消', 'code': 'CANCELLED'}, ensure_ascii=False)}\n\n"
            return
        exc = task.exception()
        if exc:
            message = exc.user_message if isinstance(exc, BibiError) else str(exc)
            code = exc.code if isinstance(exc, BibiError) else "INTERNAL"
            yield f"event: failed\ndata: {json.dumps({'message': message, 'code': code}, ensure_ascii=False)}\n\n"
            return
        payload = {"scan_id": scan_id, "kind": job["kind"], "status": "done"}
        yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/cleanup/scans/{scan_id}/remove")
async def api_remove_cleanup_items(scan_id: str, payload: RemoveCleanupIn):
    account_id = _active_account_id()
    manager = get_cleanup_manager()
    manager.get_scan(scan_id, account_id)
    existing = _cleanup_jobs.get(scan_id)
    if existing and not existing["task"].done():
        raise BibiError("当前清理任务仍在运行", code="CLEANUP_JOB_RUNNING")
    job = _start_cleanup_job(scan_id, account_id, manager, "remove", payload.item_ids)
    return JSONResponse(
        {"scan_id": scan_id, "reused": False, "kind": job["kind"]},
        status_code=202,
    )


@app.post("/api/cleanup/scans/{scan_id}/cancel")
async def api_cancel_cleanup_scan(scan_id: str):
    account_id = _active_account_id()
    if not storage.get_cleanup_scan(scan_id, account_id):
        raise BibiError("清理任务不存在或不属于当前账号", code="CLEANUP_SCAN_NOT_FOUND")
    job = _cleanup_jobs.get(scan_id)
    if not job or job["task"].done():
        return {"ok": True, "cancelled": False}
    job["task"].cancel()
    storage.update_cleanup_scan(scan_id, status="cancelled", current_folder_title="")
    return {"ok": True, "cancelled": True}


@app.post("/api/session")
async def api_create_session(payload: SessionIn):
    source_fids = payload.normalized_source_fids()
    if not source_fids:
        raise BibiError("请至少选择一个收藏夹", code="NO_SOURCE_FOLDER")
    mgr = get_session_mgr()
    sid = await mgr.create_many(source_fids, payload.mode, category_limit=payload.category_limit)
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


@app.get("/api/session/{sid}/execute/stream")
async def api_execute_stream(sid: str):
    mgr = get_session_mgr()

    async def event_gen():
        session = storage.load_session(sid)
        if not session:
            payload = {"message": "会话不存在", "code": "NOT_FOUND"}
            yield f"event: fail\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        status = session["status"]
        if status == "done":
            payload = {"stage": "done", "stats": _load_session_stats(session)}
            yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        if status == "pending_review":
            task, _ = _get_or_start_execution(sid, mgr)
        elif status == "executing":
            task = _running_executions.get(sid)
            if not task or task.done():
                payload = {
                    "message": "执行任务已中断，请重启程序后检查该任务状态",
                    "code": "EXECUTION_NOT_RUNNING",
                }
                yield f"event: fail\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
        else:
            payload = {
                "message": "当前会话状态不可执行",
                "code": "INVALID_SESSION_STATE",
            }
            yield f"event: fail\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        last_event = None
        while not task.done():
            latest = _execution_progress.get(sid)
            if latest is not None and latest != last_event:
                last_event = dict(latest)
                yield f"event: stage\ndata: {json.dumps(latest, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.1)

        if task.cancelled():
            payload = {"message": "执行已取消", "code": "CANCELLED"}
            yield f"event: fail\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        exc = task.exception()
        if exc:
            message = exc.user_message if isinstance(exc, BibiError) else str(exc)
            code = exc.code if isinstance(exc, BibiError) else "INTERNAL"
            yield f"event: fail\ndata: {json.dumps({'message': message, 'code': code}, ensure_ascii=False)}\n\n"
            return

        stats = task.result()
        payload = {"stage": "done", "progress": 1.0, "stats": stats}
        yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/session/{sid}/execute")
async def api_execute(sid: str):
    mgr = get_session_mgr()
    task, _ = _get_or_start_execution(sid, mgr)
    stats = await asyncio.shield(task)
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
    session = storage.load_session(sid)
    if not session or session["status"] != "pending_review":
        raise BibiError("仅预览状态可微调方案", code="INVALID_SESSION_STATE")
    job, reused = _get_or_start_refinement(sid, mgr, "refine", payload.instruction)
    return JSONResponse(
        {"job_id": job["job_id"], "reused": reused, "kind": job["kind"]},
        status_code=202,
    )


@app.post("/api/session/{sid}/retry-unclassified")
async def api_retry_unclassified(sid: str):
    mgr = get_session_mgr()
    session = storage.load_session(sid)
    if not session or session["status"] != "pending_review":
        raise BibiError("仅预览状态可重试未分类条目", code="INVALID_SESSION_STATE")
    job, reused = _get_or_start_refinement(sid, mgr, "unclassified_retry")
    return JSONResponse(
        {"job_id": job["job_id"], "reused": reused, "kind": job["kind"]},
        status_code=202,
    )


@app.get("/api/session/{sid}/refine/stream")
async def api_refine_stream(sid: str, job_id: str = Query(...)):
    async def event_gen():
        job = _refinement_jobs.get(job_id)
        if not job or job["sid"] != sid:
            payload = {"message": "微调任务不存在", "code": "NOT_FOUND"}
            yield f"event: failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        task = job["task"]
        last_event = None
        while True:
            latest = job.get("progress")
            if latest is not None and latest != last_event:
                last_event = dict(latest)
                yield f"event: stage\ndata: {json.dumps(latest, ensure_ascii=False)}\n\n"
            if task.done():
                break
            await asyncio.sleep(0.1)
        if task.cancelled():
            payload = {"message": "生成新方案已取消", "code": "CANCELLED"}
            yield f"event: cancelled\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        exc = task.exception()
        if exc:
            message = exc.user_message if isinstance(exc, BibiError) else str(exc)
            code = exc.code if isinstance(exc, BibiError) else "INTERNAL"
            payload = {"message": message, "code": code}
            yield f"event: failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        payload = {"stage": "done", "progress": 1.0, "kind": job["kind"], "result": task.result()}
        yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/session/{sid}/refine/cancel")
async def api_cancel_refinement(sid: str):
    job_id = _running_refinements.get(sid)
    job = _refinement_jobs.get(job_id or "")
    if not job or job["task"].done():
        return {"ok": True, "cancelled": False}
    job["task"].cancel()
    return {"ok": True, "cancelled": True}


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
