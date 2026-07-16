import asyncio
import base64
import hmac
import io
import inspect
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).parent
for _vendor in (BASE_DIR / ".libs", BASE_DIR / "_libs"):
    if _vendor.is_dir():
        sys.path.insert(0, str(_vendor))

import qrcode
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
logger = logging.getLogger(__name__)

STARTED_AT = datetime.now().isoformat(timespec="seconds")

storage = Storage(BASE_DIR)
bili = BilibiliClient(cookie_store_path=BASE_DIR / "bilibili_cookie.json")
_bili_clients: dict[tuple[str, str], BilibiliClient] = {}
_ai_classifier: AiClassifier | None = None
_ai_signature: tuple[str, str, str] | None = None
_running_pipelines: dict[str, asyncio.Task] = {}
_running_executions: dict[str, asyncio.Task] = {}
_execution_progress: dict[str, dict] = {}
_execution_job_ids: dict[str, str] = {}
_execution_jobs: dict[str, dict] = {}
_running_refinements: dict[str, str] = {}
_refinement_jobs: dict[str, dict] = {}
_cleanup_jobs: dict[str, dict] = {}
_pending_logins: dict[str, "BilibiliClient"] = {}
_pending_login_started: dict[str, float] = {}
_pending_login_expiry_handles: dict[str, asyncio.TimerHandle] = {}

JOB_RETENTION_SECONDS = 30 * 60
LOGIN_SESSION_TTL_SECONDS = 10 * 60
MAX_RETAINED_JOBS = 100
ACCESS_COOKIE_NAME = "bibitool_access"


@dataclass(frozen=True)
class BindSettings:
    host: str
    port: int
    lan_auth_enabled: bool
    token: str
    allowed_hosts: frozenset[str]


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _resolve_bind_settings() -> BindSettings:
    host = (os.getenv("BIBITOOL_HOST") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("BIBITOOL_PORT") or "8765")
    except ValueError as exc:
        raise RuntimeError("BIBITOOL_PORT 必须是有效端口号") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("BIBITOOL_PORT 必须在 1 到 65535 之间")

    token = (os.getenv("BIBITOOL_LAN_TOKEN") or "").strip()
    configured_hosts = {
        item.strip().strip("[]").lower()
        for item in (os.getenv("BIBITOOL_ALLOWED_HOSTS") or "").split(",")
        if item.strip()
    }
    is_lan_bind = not _is_loopback_host(host)
    if is_lan_bind and len(token) < 16:
        raise RuntimeError("非本机监听必须设置至少 16 位的 BIBITOOL_LAN_TOKEN")
    if is_lan_bind and not configured_hosts:
        raise RuntimeError("非本机监听必须设置 BIBITOOL_ALLOWED_HOSTS")

    allowed_hosts = {"127.0.0.1", "localhost", "::1", *configured_hosts}
    if host not in {"0.0.0.0", "::"}:
        allowed_hosts.add(host.strip("[]").lower())
    return BindSettings(
        host=host,
        port=port,
        lan_auth_enabled=is_lan_bind or bool(token),
        token=token,
        allowed_hosts=frozenset(allowed_hosts),
    )


def _prune_finished_jobs(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    for registry in (_execution_jobs, _refinement_jobs, _cleanup_jobs):
        expired = [
            key for key, job in registry.items()
            if job.get("task") is not None
            and job["task"].done()
            and now - float(job.get("finished_at", now)) > JOB_RETENTION_SECONDS
        ]
        for key in expired:
            registry.pop(key, None)
        terminal = sorted(
            (
                (float(job.get("finished_at", now)), key)
                for key, job in registry.items()
                if job.get("task") is not None and job["task"].done()
            ),
            key=lambda item: item[0],
        )
        for _, key in terminal[:-MAX_RETAINED_JOBS]:
            registry.pop(key, None)


def _discard_pending_login(login_id: str) -> None:
    expiry_handle = _pending_login_expiry_handles.pop(login_id, None)
    if expiry_handle is not None:
        expiry_handle.cancel()
    client = _pending_logins.pop(login_id, None)
    _pending_login_started.pop(login_id, None)
    if not client:
        return
    close = getattr(client, "aclose", None)
    if close:
        result = close()
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()
    if not client.cookie_store_path:
        return
    try:
        if client.cookie_store_path.exists():
            client.cookie_store_path.unlink()
    except OSError:
        logger.warning("无法清理过期登录临时文件: %s", client.cookie_store_path)


def _register_pending_login(login_id: str, client: BilibiliClient) -> None:
    if login_id in _pending_logins:
        _discard_pending_login(login_id)
    _pending_logins[login_id] = client
    _pending_login_started[login_id] = time.monotonic()
    _pending_login_expiry_handles[login_id] = asyncio.get_running_loop().call_later(
        LOGIN_SESSION_TTL_SECONDS,
        _discard_pending_login,
        login_id,
    )


def _prune_pending_logins(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        login_id for login_id, started_at in _pending_login_started.items()
        if now - started_at > LOGIN_SESSION_TTL_SECONDS
    ]
    for login_id in expired:
        _discard_pending_login(login_id)


def _job_belongs_to_account(sid: str | None, job_account_id: str | None, account_id: str | None) -> bool:
    if account_id is None:
        return True
    if job_account_id:
        return str(job_account_id) == account_id
    if not sid:
        return True
    try:
        session = storage.load_session(sid)
    except (AttributeError, TypeError):
        return True
    return bool(session and str(session.get("account_id") or "") == account_id)


def _has_running_tasks(account_id: str | None = None) -> bool:
    for sid, task in _running_pipelines.items():
        if not task.done() and _job_belongs_to_account(sid, None, account_id):
            return True
    for sid, task in _running_executions.items():
        if not task.done() and _job_belongs_to_account(sid, None, account_id):
            return True
    for job in _refinement_jobs.values():
        task = job.get("task")
        if task and not task.done() and _job_belongs_to_account(job.get("sid"), job.get("account_id"), account_id):
            return True
    for job in _cleanup_jobs.values():
        task = job.get("task")
        if task and not task.done() and _job_belongs_to_account(None, job.get("account_id"), account_id):
            return True
    return False


def _ensure_account_tasks_idle(account_id: str | None) -> None:
    if _has_running_tasks(account_id):
        raise BibiError("有后台任务正在运行，请完成或取消后再切换或退出账号", code="PIPELINE_RUNNING")


async def _close_bili_client(client) -> None:
    close = getattr(client, "aclose", None)
    if not close:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _shutdown_runtime() -> None:
    global _ai_classifier, _ai_signature
    for handle in _pending_login_expiry_handles.values():
        handle.cancel()
    _pending_login_expiry_handles.clear()
    task_candidates = [
        *_running_pipelines.values(),
        *_running_executions.values(),
        *(job.get("task") for job in _execution_jobs.values()),
        *(job.get("task") for job in _refinement_jobs.values()),
        *(job.get("task") for job in _cleanup_jobs.values()),
    ]
    current = asyncio.current_task()
    current_loop = asyncio.get_running_loop()
    tasks = {
        task for task in task_candidates
        if task is not None
        and task is not current
        and getattr(task, "get_loop", lambda: current_loop)() is current_loop
    }
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    clients_by_id = {id(bili): bili}
    for client in _bili_clients.values():
        clients_by_id[id(client)] = client
    for client in _pending_logins.values():
        clients_by_id[id(client)] = client
        cookie_path = getattr(client, "cookie_store_path", None)
        if cookie_path:
            try:
                path = Path(cookie_path)
                if path.exists():
                    path.unlink()
            except OSError:
                logger.warning("无法清理登录临时文件: %s", cookie_path)
    await asyncio.gather(*[
        _close_bili_client(client) for client in clients_by_id.values()
    ], return_exceptions=True)
    if _ai_classifier is not None:
        await _close_bili_client(_ai_classifier)
        _ai_classifier = None
        _ai_signature = None

    _running_pipelines.clear()
    _running_executions.clear()
    _execution_progress.clear()
    _execution_job_ids.clear()
    _execution_jobs.clear()
    _running_refinements.clear()
    _refinement_jobs.clear()
    _cleanup_jobs.clear()
    _pending_logins.clear()
    _pending_login_started.clear()
    _bili_clients.clear()


def get_bili() -> BilibiliClient:
    """根据活跃账号返回对应的 B 站客户端；无活跃账号时返回全局默认客户端。"""
    account = storage.get_active_account()
    if account and account.get("cookie_path"):
        cookie_path = (BASE_DIR / account["cookie_path"]).resolve()
        key = (str(account["account_id"]), str(cookie_path))
        client = _bili_clients.get(key)
        if client is None:
            client = BilibiliClient(cookie_store_path=cookie_path, account_id=account["account_id"])
            _bili_clients[key] = client
        return client
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
    active = storage.get_active_account()
    if active and str(active["account_id"]) != account_id:
        _ensure_account_tasks_idle(str(active["account_id"]))
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
    cache_key = (account_id, str(final_cookie_path.resolve()))
    _bili_clients[cache_key] = BilibiliClient(
        cookie_store_path=final_cookie_path,
        account_id=account_id,
    )


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


def _forget_execution_when_done(sid: str, job_id: str, task: asyncio.Task) -> None:
    if _running_executions.get(sid) is task:
        _running_executions.pop(sid, None)
    if _execution_job_ids.get(sid) == job_id:
        _execution_job_ids.pop(sid, None)
    _execution_progress.pop(sid, None)
    job = _execution_jobs.get(job_id)
    if job is not None:
        job["finished_at"] = time.monotonic()
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


def _get_or_start_execution(sid: str, mgr: ClassifySession) -> tuple[dict, bool]:
    _prune_finished_jobs()
    existing = _running_executions.get(sid)
    if existing and not existing.done():
        job_id = _execution_job_ids[sid]
        return _execution_jobs[job_id], True

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id": job_id,
        "sid": sid,
        "account_id": (storage.load_session(sid) or {}).get("account_id", ""),
        "progress": {"stage": "queued", "progress": 0.0},
        "result": None,
    }

    async def on_progress(event):
        _execution_progress[sid] = dict(event)
        job["progress"] = dict(event)

    async def run():
        result = await mgr.execute(sid, on_progress=on_progress, run_id=job_id)
        job["result"] = result
        return result

    task = asyncio.create_task(run())
    job["task"] = task
    _running_executions[sid] = task
    _execution_job_ids[sid] = job_id
    _execution_jobs[job_id] = job
    task.add_done_callback(lambda done_task: _forget_execution_when_done(sid, job_id, done_task))
    return job, False


def _get_or_start_refinement(
    sid: str,
    mgr: ClassifySession,
    kind: str,
    instruction: str = "",
) -> tuple[dict, bool]:
    _prune_finished_jobs()
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
            full_result = await mgr.retry_unclassified(sid, on_progress=on_progress)
            result = {
                "recovered": int(full_result.get("recovered") or 0),
                "remaining": int(full_result.get("remaining") or 0),
            }
        else:
            full_result = await mgr.refine_plan(sid, instruction, on_progress=on_progress)
            active_version = next(
                (
                    version for version in full_result.get("versions", [])
                    if version.get("is_active")
                ),
                None,
            )
            result = {
                "version_id": active_version.get("version_id") if active_version else None,
            }
        job["result"] = result
        return result

    task = asyncio.create_task(run())
    job["task"] = task
    _refinement_jobs[job_id] = job
    _running_refinements[sid] = job_id

    def forget(done_task: asyncio.Task) -> None:
        if _running_refinements.get(sid) == job_id:
            _running_refinements.pop(sid, None)
        job["finished_at"] = time.monotonic()
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
    _prune_finished_jobs()
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
            full_result = await manager.scan(scan_id, account_id, on_progress=on_progress)
            scan = full_result.get("scan") or {}
            result = {
                "scan_id": scan_id,
                "status": scan.get("status", "ready"),
                "problem_total": int(scan.get("problem_total") or 0),
            }
        job["result"] = result
        return result

    task = asyncio.create_task(run())
    job["task"] = task
    _cleanup_jobs[scan_id] = job

    def consume(done_task: asyncio.Task) -> None:
        job["finished_at"] = time.monotonic()
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


def _has_complete_ai_config(cfg: dict | None) -> bool:
    if not cfg:
        return False
    return all(str(cfg.get(key, "")).strip() for key in ("ai_base_url", "ai_api_key", "ai_model"))


def get_ai() -> AiClassifier:
    global _ai_classifier, _ai_signature
    cfg = storage.load_config()
    if not _has_complete_ai_config(cfg):
        raise BibiError("请先在设置页填写完整的 AI 配置", code="AI_NOT_CONFIGURED")
    signature = (cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"])
    if _ai_classifier is None or _ai_signature != signature:
        previous = _ai_classifier
        _ai_classifier = AiClassifier(*signature)
        _ai_signature = signature
        if previous is not None:
            close_result = previous.aclose()
            if inspect.isawaitable(close_result):
                try:
                    asyncio.get_running_loop().create_task(close_result)
                except RuntimeError:
                    close_result.close()
    return _ai_classifier


def get_session_mgr() -> ClassifySession:
    cfg = storage.load_config()
    ai = get_ai() if _has_complete_ai_config(cfg) else None
    return ClassifySession(storage, get_bili(), ai)


def get_cleanup_manager() -> CleanupManager:
    return CleanupManager(storage, get_bili())


def _active_account_id() -> str:
    account = storage.get_active_account()
    if not account:
        raise BibiError("请先登录 B 站账号", code="NOT_LOGGED_IN")
    return str(account["account_id"])


def _require_owned_session(sid: str) -> dict:
    account_id = _active_account_id()
    session = storage.load_session_for_account(sid, account_id)
    if not session:
        raise BibiError("会话不存在或不属于当前账号", code="SESSION_NOT_FOUND")
    return session


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_session_mgr().resume_on_startup()
    settings = _resolve_bind_settings()
    browser_url = f"http://127.0.0.1:{settings.port}"
    if settings.lan_auth_enabled:
        browser_url += f"/?access_token={settings.token}"
    if os.getenv("BIBITOOL_NO_BROWSER") != "1":
        webbrowser.open(browser_url)
    try:
        yield
    finally:
        await _shutdown_runtime()


app = FastAPI(lifespan=lifespan)


def _with_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
    )
    return response


@app.middleware("http")
async def enforce_local_access(request: Request, call_next):
    settings = _resolve_bind_settings()
    request_host = (request.url.hostname or "").strip("[]").lower()
    if request_host not in settings.allowed_hosts:
        return _with_security_headers(PlainTextResponse("Host 不在允许列表中", status_code=400))

    if settings.lan_auth_enabled:
        query_token = request.query_params.get("access_token", "")
        if request.method == "GET" and request.url.path == "/" and query_token:
            if not hmac.compare_digest(query_token, settings.token):
                return _with_security_headers(PlainTextResponse("访问令牌无效", status_code=401))
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie(
                ACCESS_COOKIE_NAME,
                settings.token,
                max_age=24 * 60 * 60,
                httponly=True,
                samesite="strict",
            )
            return _with_security_headers(response)

        cookie_token = request.cookies.get(ACCESS_COOKIE_NAME, "")
        if not cookie_token or not hmac.compare_digest(cookie_token, settings.token):
            if request.url.path.startswith("/api/"):
                return _with_security_headers(JSONResponse(
                    {"code": "UNAUTHORIZED", "message": "请先使用配对链接访问 BiBiTool"},
                    status_code=401,
                ))
            return _with_security_headers(PlainTextResponse("请先使用配对链接访问 BiBiTool", status_code=401))

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin:
            origin_parts = urlsplit(origin)
            if (origin_parts.hostname or "").lower() != request_host:
                return _with_security_headers(JSONResponse(
                    {"code": "ORIGIN_FORBIDDEN", "message": "拒绝跨站写操作"},
                    status_code=403,
                ))

    return _with_security_headers(await call_next(request))


class ApiInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


def _dedupe_positive_ids(values: list[int]) -> list[int]:
    if any(int(value) <= 0 for value in values):
        raise ValueError("ID 必须是正整数")
    return list(dict.fromkeys(int(value) for value in values))


class ConfigIn(ApiInput):
    ai_base_url: str = Field(max_length=2048)
    ai_api_key: str = Field(max_length=4096)
    ai_model: str = Field(max_length=200)
    ai_batch_size: int = Field(default=100, ge=10, le=200)


class SessionIn(ApiInput):
    source_fids: list[int] | None = Field(default=None, max_length=100)
    source_fid: int | None = Field(default=None, gt=0)
    mode: Literal["quick", "full"] = "quick"
    category_limit: int = Field(default=14, ge=3, le=50)

    @field_validator("source_fids")
    @classmethod
    def validate_source_fids(cls, value):
        return _dedupe_positive_ids(value) if value is not None else None

    @model_validator(mode="after")
    def require_source(self):
        if not self.source_fids and self.source_fid is None:
            raise ValueError("请至少选择一个收藏夹")
        return self

    def normalized_source_fids(self) -> list[int]:
        if self.source_fids:
            return list(self.source_fids)
        if self.source_fid is not None:
            return [self.source_fid]
        return []


class AdjustIn(ApiInput):
    resource_id: int | None = Field(default=None, gt=0)
    avid: int | None = Field(default=None, gt=0)
    resource_type: int = Field(default=2, gt=0, le=1000)
    new_category: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_resource_id(self):
        if self.resource_id is None and self.avid is None:
            raise ValueError("resource_id or avid is required")
        return self

    def normalized_resource_id(self) -> int:
        if self.resource_id is not None:
            return self.resource_id
        if self.avid is not None:
            return self.avid
        raise ValueError("resource_id or avid is required")


class RemoveSkippedIn(ApiInput):
    item_ids: list[int] = Field(min_length=1, max_length=5000)

    @field_validator("item_ids")
    @classmethod
    def validate_item_ids(cls, value):
        return _dedupe_positive_ids(value)


class RemoveCleanupIn(RemoveSkippedIn):
    pass


class RefineIn(ApiInput):
    instruction: str = Field(min_length=1, max_length=2000)


class DeleteEmptyFoldersIn(ApiInput):
    source_fids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("source_fids")
    @classmethod
    def validate_source_fids(cls, value):
        return _dedupe_positive_ids(value)


class DeleteFoldersIn(ApiInput):
    fids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("fids")
    @classmethod
    def validate_fids(cls, value):
        return _dedupe_positive_ids(value)


class SortFoldersIn(ApiInput):
    fids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("fids")
    @classmethod
    def validate_fids(cls, value):
        if any(int(item) <= 0 for item in value):
            raise ValueError("ID 必须是正整数")
        return [int(item) for item in value]


@app.exception_handler(BibiError)
async def bibi_error_handler(_, exc: BibiError):
    return JSONResponse({"code": exc.code, "message": exc.user_message}, status_code=400)


@app.get("/api/state")
async def api_state():
    client = get_bili()
    return {"logged_in": client.is_logged_in, "configured": _has_complete_ai_config(storage.load_config())}


@app.get("/api/runtime")
async def api_runtime():
    _prune_finished_jobs()
    _prune_pending_logins()
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
    active = storage.get_active_account()
    if active:
        _ensure_account_tasks_idle(str(active["account_id"]))
    client = get_bili()
    client.clear_cookies()
    await _close_bili_client(client)
    for key, cached in list(_bili_clients.items()):
        if cached is client:
            _bili_clients.pop(key, None)
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
    active = storage.get_active_account()
    if active and str(active["account_id"]) != account_id:
        _ensure_account_tasks_idle(str(active["account_id"]))
    storage.activate_account(account_id)
    return {"ok": True}


@app.post("/api/accounts/login/start")
async def api_account_login_start():
    """新增账号扫码登录：使用独立临时 cookie 文件，避免覆盖当前活跃账号。"""
    _prune_pending_logins()
    import uuid
    login_id = uuid.uuid4().hex
    temp_cookie_path = BASE_DIR / f"accounts/_pending/{login_id}.json"
    temp_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    temp_client = BilibiliClient(cookie_store_path=temp_cookie_path)
    data = await temp_client.qrcode_generate()
    _register_pending_login(login_id, temp_client)
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {
        "login_id": login_id,
        "qrcode_key": data["qrcode_key"],
        "image": f"data:image/png;base64,{b64}",
    }


@app.post("/api/accounts/login/{login_id}/cancel")
async def api_cancel_account_login(login_id: str):
    existed = login_id in _pending_logins
    _discard_pending_login(login_id)
    return {"ok": True, "cancelled": existed}


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
            _discard_pending_login(login_id)
    elif result.get("status") in {"expired", "cancelled", "failed"}:
        _discard_pending_login(login_id)
    return result


@app.get("/api/config")
async def api_get_config():
    cfg = storage.load_config()
    if not _has_complete_ai_config(cfg):
        return {"configured": False}
    return {
        "configured": True,
        "ai_base_url": cfg["ai_base_url"],
        "ai_model": cfg["ai_model"],
        "ai_batch_size": int(cfg.get("ai_batch_size", 100)),
    }


@app.post("/api/config")
async def api_save_config(cfg: ConfigIn):
    global _ai_classifier, _ai_signature
    saved = cfg.model_dump()
    existing = storage.load_config() or {}
    saved["ai_base_url"] = saved["ai_base_url"].strip()
    saved["ai_model"] = saved["ai_model"].strip()
    saved["ai_api_key"] = saved["ai_api_key"].strip() or str(existing.get("ai_api_key", "")).strip()
    if not _has_complete_ai_config(saved):
        raise BibiError(
            "请填写完整的 AI 配置（Base URL、API Key 和模型名称）",
            code="AI_NOT_CONFIGURED",
        )
    new_signature = (saved["ai_base_url"], saved["ai_api_key"], saved["ai_model"])
    if _ai_classifier is not None and _ai_signature != new_signature:
        previous = _ai_classifier
        _ai_classifier = None
        _ai_signature = None
        await _close_bili_client(previous)
    storage.save_config(saved)
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


async def _confirm_folder_state(client: BilibiliClient, predicate) -> list[dict]:
    latest: list[dict] | None = None
    last_error: Exception | None = None
    for delay in (0, 0.5, 1.0, 2.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            latest = await client.get_my_folders(storage=storage)
        except Exception as exc:
            last_error = exc
            continue
        if predicate(latest):
            return latest
    if latest is None:
        raise BibiError(
            f"操作已经提交，但暂时无法从 B 站确认结果：{last_error}",
            code="FOLDER_STATE_CONFIRM_FAILED",
        )
    return latest


@app.post("/api/folders/sort")
async def api_sort_folders(payload: SortFoldersIn):
    requested = [int(fid) for fid in payload.fids]
    client = get_bili()
    current = await client.get_my_folders(storage=storage)
    current_ids = [int(item["fid"]) for item in current]

    if len(set(requested)) != len(requested) or set(requested) != set(current_ids):
        raise BibiError(
            "收藏夹排序必须包含当前账号全部收藏夹，且不能重复",
            code="FOLDER_SORT_INVALID",
        )

    await client.sort_folders(requested)
    latest = await _confirm_folder_state(
        client,
        lambda items: [int(item["fid"]) for item in items] == requested,
    )
    confirmed_ids = [int(item["fid"]) for item in latest]
    if confirmed_ids != requested:
        raise BibiError(
            "B站尚未确认新的收藏夹顺序，请刷新后重试",
            code="FOLDER_SORT_NOT_CONFIRMED",
        )
    return {"ok": True, "fids": confirmed_ids}


@app.post("/api/folders/batch-delete")
async def api_batch_delete_folders(payload: DeleteFoldersIn):
    if _has_running_tasks():
        raise BibiError("有整理任务正在运行，请等待任务完成后再删除收藏夹", code="PIPELINE_RUNNING")

    fids = list(dict.fromkeys(int(fid) for fid in payload.fids))
    client = get_bili()
    folders = await client.get_my_folders(storage=storage)
    folders_by_id = {int(item["fid"]): item for item in folders}

    missing = [fid for fid in fids if fid not in folders_by_id]
    if missing:
        raise BibiError("部分收藏夹不存在或已被删除，请刷新后重试", code="FOLDER_NOT_FOUND")
    if any(
        int(folders_by_id[fid].get("fav_state", 0)) == 1
        or bool(folders_by_id[fid].get("is_default"))
        for fid in fids
    ):
        raise BibiError("默认收藏夹不能删除", code="FOLDER_DELETE_PROTECTED")
    if any(int(folders_by_id[fid].get("media_count", 0)) != 0 for fid in fids):
        raise BibiError("部分收藏夹已不为空，请刷新后重新选择", code="FOLDER_NOT_EMPTY")

    await client.delete_folders(fids)
    target_ids = set(fids)
    latest = await _confirm_folder_state(
        client,
        lambda items: target_ids.isdisjoint(int(item["fid"]) for item in items),
    )
    remaining = {int(item["fid"]) for item in latest}
    deleted_fids = [fid for fid in fids if fid not in remaining]
    failed_fids = [fid for fid in fids if fid in remaining]
    return {
        "stats": {
            "total": len(fids),
            "success": len(deleted_fids),
            "failed": len(failed_fids),
        },
        "deleted_fids": deleted_fids,
        "failed_fids": failed_fids,
    }


@app.delete("/api/folders/{fid}")
async def api_delete_folder(fid: int):
    if _has_running_tasks():
        raise BibiError("有整理任务正在运行，请等待任务完成后再删除收藏夹", code="PIPELINE_RUNNING")

    client = get_bili()
    folders = await client.get_my_folders(storage=storage)
    folder = next((item for item in folders if int(item["fid"]) == fid), None)
    if not folder:
        raise BibiError("收藏夹不存在或已被删除", code="FOLDER_NOT_FOUND")
    if int(folder.get("fav_state", 0)) == 1 or bool(folder.get("is_default")):
        raise BibiError("默认收藏夹不能删除", code="FOLDER_DELETE_PROTECTED")
    if int(folder.get("media_count", 0)) != 0:
        raise BibiError("该收藏夹不为空，不能直接删除", code="FOLDER_NOT_EMPTY")

    await client.delete_folders([fid])
    latest = await _confirm_folder_state(
        client,
        lambda items: all(int(item["fid"]) != fid for item in items),
    )
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
    _active_account_id()
    source_fids = payload.normalized_source_fids()
    if not source_fids:
        raise BibiError("请至少选择一个收藏夹", code="NO_SOURCE_FOLDER")
    mgr = get_session_mgr()
    sid = await mgr.create_many(source_fids, payload.mode, category_limit=payload.category_limit)
    return {"session_id": sid}


@app.get("/api/session/{sid}")
async def api_get_session(sid: str):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    return mgr.get_plan(sid)


@app.get("/api/session/{sid}/stream")
async def api_session_stream(sid: str):
    _require_owned_session(sid)
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
    _require_owned_session(sid)
    mgr = get_session_mgr()
    mgr.cancel(sid)
    task = _running_pipelines.get(sid)
    if task and not task.done():
        task.cancel()
    return {"ok": True}


@app.post("/api/session/{sid}/adjust")
async def api_adjust(sid: str, payload: AdjustIn):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    mgr.adjust_item(sid, payload.normalized_resource_id(), payload.new_category, resource_type=payload.resource_type)
    return {"ok": True}


@app.get("/api/session/{sid}/execute/active")
async def api_active_execution(sid: str):
    _require_owned_session(sid)
    job_id = _execution_job_ids.get(sid)
    job = _execution_jobs.get(job_id or "")
    if not job or job["task"].done():
        return {"job_id": None, "running": False}
    return {
        "job_id": job_id,
        "running": True,
        "progress": job.get("progress") or {},
    }


@app.get("/api/session/{sid}/execute/stream")
async def api_execute_stream(sid: str, job_id: str = Query(...)):
    _require_owned_session(sid)

    async def event_gen():
        job = _execution_jobs.get(job_id)
        if not job or job.get("sid") != sid:
            payload = {
                "message": "执行任务不存在或未运行",
                "code": "EXECUTION_NOT_RUNNING",
            }
            yield f"event: fail\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        task = job["task"]

        last_event = None
        while not task.done():
            latest = job.get("progress")
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
    session = _require_owned_session(sid)
    if session["status"] not in ("pending_review", "executing"):
        raise BibiError("当前会话状态不可执行", code="INVALID_SESSION_STATE")
    mgr = get_session_mgr()
    job, reused = _get_or_start_execution(sid, mgr)
    return JSONResponse(
        {"job_id": job["job_id"], "reused": reused},
        status_code=202,
    )


@app.get("/api/session/{sid}/failed-items")
async def api_failed_items(sid: str):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    return {"items": mgr.get_failed_items(sid)}


@app.get("/api/session/{sid}/skipped-items")
async def api_skipped_items(sid: str):
    _require_owned_session(sid)
    return {"items": storage.list_skipped_items(sid)}


@app.post("/api/session/{sid}/skipped-items/remove")
async def api_remove_skipped_items(sid: str, payload: RemoveSkippedIn):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    return {"stats": await mgr.remove_skipped_items(sid, payload.item_ids)}


@app.post("/api/session/{sid}/retry-failed")
async def api_retry_failed(sid: str):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    stats = await mgr.retry_failed(sid)
    return {"stats": stats}


@app.post("/api/session/{sid}/refine")
async def api_refine_plan(sid: str, payload: RefineIn):
    session = _require_owned_session(sid)
    mgr = get_session_mgr()
    if not session or session["status"] != "pending_review":
        raise BibiError("仅预览状态可微调方案", code="INVALID_SESSION_STATE")
    job, reused = _get_or_start_refinement(sid, mgr, "refine", payload.instruction)
    return JSONResponse(
        {"job_id": job["job_id"], "reused": reused, "kind": job["kind"]},
        status_code=202,
    )


@app.post("/api/session/{sid}/retry-unclassified")
async def api_retry_unclassified(sid: str):
    session = _require_owned_session(sid)
    mgr = get_session_mgr()
    if not session or session["status"] != "pending_review":
        raise BibiError("仅预览状态可重试未分类条目", code="INVALID_SESSION_STATE")
    job, reused = _get_or_start_refinement(sid, mgr, "unclassified_retry")
    return JSONResponse(
        {"job_id": job["job_id"], "reused": reused, "kind": job["kind"]},
        status_code=202,
    )


@app.get("/api/session/{sid}/refine/active")
async def api_active_refinement(sid: str):
    _require_owned_session(sid)
    job_id = _running_refinements.get(sid)
    job = _refinement_jobs.get(job_id or "")
    if not job or job["task"].done():
        return {"job_id": None, "running": False}
    return {
        "job_id": job_id,
        "running": True,
        "kind": job["kind"],
        "progress": job.get("progress") or {},
    }


@app.get("/api/session/{sid}/refine/stream")
async def api_refine_stream(sid: str, job_id: str = Query(...)):
    _require_owned_session(sid)
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
    _require_owned_session(sid)
    job_id = _running_refinements.get(sid)
    job = _refinement_jobs.get(job_id or "")
    if not job or job["task"].done():
        return {"ok": True, "cancelled": False}
    job["task"].cancel()
    return {"ok": True, "cancelled": True}


@app.post("/api/session/{sid}/versions/{version_id}/activate")
async def api_activate_version(sid: str, version_id: str):
    _require_owned_session(sid)
    storage.activate_plan_version(sid, version_id)
    return get_session_mgr().get_plan(sid)


@app.get("/api/session/{sid}/empty-source-folders")
async def api_empty_source_folders(sid: str):
    _require_owned_session(sid)
    return {"items": storage.list_empty_source_candidates(sid)}


@app.post("/api/session/{sid}/empty-source-folders/delete")
async def api_delete_empty_source_folders(sid: str, payload: DeleteEmptyFoldersIn):
    _require_owned_session(sid)
    mgr = get_session_mgr()
    return {"stats": await mgr.delete_empty_source_folders(sid, payload.source_fids)}


@app.get("/api/sessions/resumable")
async def api_resumable():
    _active_account_id()
    mgr = get_session_mgr()
    return {"sessions": mgr.list_resumable()}


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    bind_settings = _resolve_bind_settings()
    uvicorn.run(app, host=bind_settings.host, port=bind_settings.port, access_log=True)
