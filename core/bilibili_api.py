import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from core.errors import BiliApiError, NotLoggedInError

_WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

_INVALID_TITLES = {"已失效视频", "已失效", "失效视频", ""}


def _is_invalid_resource(m: dict) -> bool:
    """attr 未置 bit0 但表现为失效的资源。

    B站部分失效视频不会设置 attr bit0，而是把标题清空或替换为占位文本。
    仅用 title 作为判据，避免误伤 upper/cover 字段缺失的正常资源。
    """
    title = (m.get("title") or "").strip()
    return title in _INVALID_TITLES


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    img = img_key[:32]
    sub = sub_key[:32]
    raw = img + sub
    return "".join(raw[i] for i in _WBI_TABLE)[:32]


def _wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    mixin = _get_mixin_key(img_key, sub_key)
    params = {"wts": int(time.time()), **params}
    sorted_items = sorted(params.items())
    query = urlencode(sorted_items)
    w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return {**params, "w_rid": w_rid}


def _time_now() -> int:
    return int(time.time())


def _extract_key(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".")[0]


def _guard_page_progress(fid: int, page: int, medias: list[dict], seen: set[tuple]) -> None:
    page_keys = set()
    for media in medias:
        resource_id = media.get("id")
        resource_type = media.get("type", 2)
        fallback = media.get("bvid") or media.get("title") or json.dumps(
            media,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        page_keys.add((resource_id, resource_type, fallback if not resource_id else ""))
    if medias and page_keys and not (page_keys - seen):
        raise BiliApiError(-1, f"收藏夹 {fid} 第 {page} 页分页重复，未发现新增资源")
    seen.update(page_keys)


def _guard_page_limit(fid: int, next_page: int, expected_total, page_size: int) -> None:
    if expected_total is not None:
        declared_pages = max(1, (int(expected_total) + page_size - 1) // page_size)
        if next_page > declared_pages + 5:
            raise BiliApiError(-1, f"收藏夹 {fid} 分页超过声明总数，已停止继续请求")
    elif next_page > 5000:
        raise BiliApiError(-1, f"收藏夹 {fid} 分页超过安全上限，已停止继续请求")


class BilibiliClient:
    def __init__(self, cookie_store_path: Path | str | None = None, account_id: str = ""):
        self.cookie_store_path = Path(cookie_store_path) if cookie_store_path else None
        self.account_id = account_id
        self.cookies: dict[str, str] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._request_semaphore = asyncio.Semaphore(6)
        if self.cookie_store_path and self.cookie_store_path.exists():
            self.cookies = json.loads(self.cookie_store_path.read_text(encoding="utf-8"))

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                cookies=self.cookies,
                headers=_DEFAULT_HEADERS,
                timeout=httpx.Timeout(15.0, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=6),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._request_semaphore:
                    response = await self._client().request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def save_cookies(self) -> None:
        if self.cookie_store_path:
            self.cookie_store_path.write_text(
                json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def clear_cookies(self) -> None:
        self.cookies = {}
        if self._http_client is not None:
            self._http_client.cookies.clear()
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
        r = await self._request(
            "GET",
            f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate",
            params={"returnType": 0},
        )
        data = r.json()
        if data["code"] != 0:
            raise BiliApiError(data["code"], data.get("message", "生成二维码失败"))
        return data["data"]

    async def qrcode_poll(self, qrcode_key: str) -> dict:
        r = await self._request(
            "GET",
            f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
        )
        data = r.json()
        payload = data.get("data") or {}
        code = payload.get("code", data.get("code"))
        if code == 0:
            for cookie in r.headers.get_list("set-cookie"):
                parts = cookie.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    self.cookies[parts[0]] = parts[1]
                    self._client().cookies.set(parts[0], parts[1])
            self.save_cookies()
            return {"status": "success", "mid": payload.get("mid")}
        if code == 86101:
            return {"status": "waiting"}
        if code == 86090:
            return {"status": "scanned"}
        if code in (86038, 86039):
            return {"status": "expired"}
        raise BiliApiError(code, payload.get("message") or data.get("message", "扫码状态未知"))

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

    async def _fetch_wbi_keys(self, storage) -> dict:
        if storage is not None:
            cached = storage.load_wbi_keys(account_id=self.account_id)
            if cached:
                return cached
        r = await self._request("GET", f"{API_BASE}/x/web-interface/nav")
        data = r.json()
        self._check_bili_response(data)
        img_url = data["data"]["wbi_img"]["img_url"]
        sub_url = data["data"]["wbi_img"]["sub_url"]
        keys = {"img_key": _extract_key(img_url), "sub_key": _extract_key(sub_url)}
        if storage is not None:
            storage.save_wbi_keys(keys["img_key"], keys["sub_key"], account_id=self.account_id)
        return keys

    async def _wbi_get(self, path: str, params: dict, storage) -> dict:
        keys = await self._fetch_wbi_keys(storage)
        data = await self._wbi_get_once(path, params, keys)
        if data.get("code") in (-400, -403) and storage is not None:
            storage.clear_wbi_keys(account_id=self.account_id)
            keys = await self._fetch_wbi_keys(storage)
            data = await self._wbi_get_once(path, params, keys)
        return self._check_bili_response(data)

    async def _wbi_get_once(self, path: str, params: dict, keys: dict) -> dict:
        signed = _wbi_sign(params, keys["img_key"], keys["sub_key"])
        r = await self._request("GET", f"{API_BASE}{path}", params=signed)
        return r.json()

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
                "fav_state": f.get("fav_state", 0),
                "raw_attr": f.get("attr"),
                "is_default": f.get("attr") == 0,
            }
            for f in data.get("list", [])
        ]

    async def get_my_profile(self) -> dict:
        """获取当前登录账号资料（mid、uname、face）。"""
        self._require_login()
        r = await self._request("GET", f"{API_BASE}/x/web-interface/nav")
        data = r.json()
        self._check_bili_response(data)
        info = data.get("data", {})
        return {
            "mid": info.get("mid", 0),
            "uname": info.get("name", ""),
            "avatar_url": info.get("face", ""),
        }

    async def get_folder_videos(self, fid: int, storage=None, page_size: int = 20, sleep_seconds: float = 0.3):
        async for page in self.get_folder_video_pages(fid, storage=storage, page_size=page_size, sleep_seconds=sleep_seconds):
            yield page["videos"]

    async def get_folder_video_pages(self, fid: int, storage=None, page_size: int = 20, sleep_seconds: float = 0.3):
        """结构化分页拉取。每页 yield 一个 dict：
        {page, videos, raw_count, usable_count, skipped_count, skipped_reasons, skipped_items, expected_total, has_more}
        分页结束优先用响应 has_more，回退用 info.media_count 与已扫描数，最后才用 len(medias) < page_size。
        skipped_items 中每项含 avid/bvid/title/resource_type/raw_attr/reason_code/reason_label/detail/removable。
        - attr_invalid（attr & 1 失效）：removable=True，可安全删除
        - non_video_type（type != 2 非视频）：removable=False，不删除
        - no_id（无 avid）：removable=False，无法调用删除接口
        """
        self._require_login()
        pn = 1
        scanned = 0
        expected_total = None
        seen_page_resources: set[tuple] = set()
        while True:
            data = await self._wbi_get(
                "/x/v3/fav/resource/list",
                {"media_id": fid, "pn": pn, "ps": page_size, "order": "mtime", "platform": "web"},
                storage,
            )
            medias = data.get("medias") or []
            _guard_page_progress(fid, pn, medias, seen_page_resources)
            info = data.get("info") or {}
            if expected_total is None:
                expected_total = info.get("media_count")
            has_more = data.get("has_more")
            if has_more is None and expected_total is not None:
                has_more = scanned + len(medias) < expected_total
            batch = []
            skipped_reasons: dict[str, int] = {}
            skipped_items: list[dict] = []

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

            for m in medias:
                if m.get("attr", 0) & 1:
                    skip_item(m, "attr_invalid", "失效视频", removable=True)
                    continue
                if _is_invalid_resource(m):
                    skip_item(m, "attr_invalid", "失效视频", removable=True)
                    continue
                if m.get("type", 2) != 2:
                    skip_item(m, "non_video_type", "非视频类型", removable=False)
                    continue
                if not m.get("id"):
                    skip_item(m, "no_id", "无视频ID", removable=False)
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
            scanned += len(medias)
            yield {
                "page": pn,
                "videos": batch,
                "raw_count": len(medias),
                "usable_count": len(batch),
                "skipped_count": len(medias) - len(batch),
                "skipped_reasons": skipped_reasons,
                "skipped_items": skipped_items,
                "expected_total": expected_total,
                "has_more": has_more,
            }
            if has_more is False:
                return
            if not medias:
                return
            if has_more is None and len(medias) < page_size:
                return
            pn += 1
            _guard_page_limit(fid, pn, expected_total, page_size)
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)

    async def get_folder_resource_pages(self, fid: int, storage=None, page_size: int = 20, sleep_seconds: float = 0.3):
        """结构化分页拉取所有可分类资源（视频+非视频）。每页 yield dict：
        {page, videos, resources, raw_count, usable_count, skipped_count, skipped_reasons, skipped_items, expected_total, has_more}
        - videos: 仅视频资源（type==2），向后兼容旧调用
        - resources: 所有可分类资源，每条含 resource_id/resource_type/avid
        skipped_items 仅记录 attr_invalid（失效）和 no_id，不再跳过 non_video_type。
        """
        self._require_login()
        pn = 1
        scanned = 0
        expected_total = None
        seen_page_resources: set[tuple] = set()
        while True:
            data = await self._wbi_get(
                "/x/v3/fav/resource/list",
                {"media_id": fid, "pn": pn, "ps": page_size, "order": "mtime", "platform": "web"},
                storage,
            )
            medias = data.get("medias") or []
            _guard_page_progress(fid, pn, medias, seen_page_resources)
            info = data.get("info") or {}
            if expected_total is None:
                expected_total = info.get("media_count")
            has_more = data.get("has_more")
            if has_more is None and expected_total is not None:
                has_more = scanned + len(medias) < expected_total
            videos_batch = []
            resources_batch = []
            skipped_reasons: dict[str, int] = {}
            skipped_items: list[dict] = []

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

            for m in medias:
                if m.get("attr", 0) & 1:
                    skip_item(m, "attr_invalid", "失效视频", removable=True)
                    continue
                if _is_invalid_resource(m):
                    skip_item(m, "attr_invalid", "失效视频", removable=True)
                    continue
                if not m.get("id"):
                    skip_item(m, "no_id", "无资源ID", removable=False)
                    continue
                resource_type = m.get("type", 2)
                resource_id = m["id"]
                avid = resource_id if resource_type == 2 else 0
                item = {
                    "avid": avid,
                    "resource_id": resource_id,
                    "resource_type": resource_type,
                    "bvid": m.get("bvid", ""),
                    "title": m.get("title", ""),
                    "intro": "",
                    "tags": "[]",
                    "up_name": m.get("upper", {}).get("name", ""),
                    "up_mid": m.get("upper", {}).get("mid", 0),
                    "cover_url": m.get("cover", ""),
                    "tname": m.get("tname", ""),
                    "fid": fid,
                }
                resources_batch.append(item)
                if resource_type == 2:
                    videos_batch.append({
                        "avid": avid, "bvid": item["bvid"], "title": item["title"],
                        "intro": "", "tags": "[]", "up_name": item["up_name"],
                        "up_mid": item["up_mid"], "cover_url": item["cover_url"],
                        "tname": item["tname"], "fid": fid,
                    })
            scanned += len(medias)
            yield {
                "page": pn,
                "videos": videos_batch,
                "resources": resources_batch,
                "raw_count": len(medias),
                "usable_count": len(resources_batch),
                "skipped_count": len(medias) - len(resources_batch),
                "skipped_reasons": skipped_reasons,
                "skipped_items": skipped_items,
                "expected_total": expected_total,
                "has_more": has_more,
            }
            if has_more is False:
                return
            if not medias:
                return
            if has_more is None and len(medias) < page_size:
                return
            pn += 1
            _guard_page_limit(fid, pn, expected_total, page_size)
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)

    async def get_folder_resource_page(
        self,
        fid: int,
        page: int = 1,
        page_size: int = 20,
        storage=None,
    ) -> dict:
        """读取收藏夹指定页的原始资源，供只读列表展示。"""
        self._require_login()
        data = await self._wbi_get(
            "/x/v3/fav/resource/list",
            {"media_id": fid, "pn": page, "ps": page_size, "order": "mtime", "platform": "web"},
            storage,
        )
        medias = data.get("medias") or []
        info = data.get("info") or {}
        total = int(info.get("media_count") or 0)
        has_more = data.get("has_more")
        if has_more is None:
            has_more = page * page_size < total

        items = []
        for media in medias:
            resource_id = int(media.get("id") or 0)
            resource_type = int(media.get("type", 2) or 2)
            invalid = bool(media.get("attr", 0) & 1) or _is_invalid_resource(media)
            items.append({
                "resource_id": resource_id,
                "resource_type": resource_type,
                "bvid": media.get("bvid", ""),
                "title": media.get("title", ""),
                "up_name": media.get("upper", {}).get("name", ""),
                "cover_url": media.get("cover", ""),
                "tname": media.get("tname", ""),
                "raw_attr": int(media.get("attr", 0) or 0),
                "status": "invalid" if invalid else "available",
                "status_label": "已失效" if invalid else "可访问",
            })
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": bool(has_more),
            "items": items,
        }

    async def get_folder_resource_ids(self, fid: int, storage=None) -> list[dict]:
        """返回收藏夹的完整资源键，包含详情接口不返回的权限资源。"""
        self._require_login()
        data = await self._wbi_get(
            "/x/v3/fav/resource/ids",
            {"media_id": fid, "platform": "web"},
            storage,
        )
        items = data if isinstance(data, list) else []
        result = []
        for item in items:
            resource_id = item.get("id")
            if not resource_id:
                continue
            result.append({
                "resource_id": resource_id,
                "resource_type": item.get("type", 2),
                "bvid": item.get("bvid") or item.get("bv_id") or "",
            })
        return result

    async def get_video_info(self, bvid: str) -> dict:
        self._require_login()
        view_resp = await self._request("GET", f"{API_BASE}/x/web-interface/view", params={"bvid": bvid})
        view_data = self._check_bili_response(view_resp.json())
        tag_resp = await self._request("GET", f"{API_BASE}/x/tag/archive/tags", params={"bvid": bvid})
        tag_data = self._check_bili_response(tag_resp.json())
        return {
            "avid": view_data.get("aid", 0),
            "bvid": view_data.get("bvid", bvid),
            "title": view_data.get("title", ""),
            "intro": view_data.get("desc", ""),
            "tags": [t.get("tag_name", "") for t in tag_data if t.get("tag_name")],
            "up_name": view_data.get("owner", {}).get("name", ""),
            "up_mid": view_data.get("owner", {}).get("mid", 0),
            "cover_url": view_data.get("pic", ""),
            "tname": view_data.get("tname", ""),
        }

    async def _post_form(self, path: str, form: dict) -> dict:
        self._require_login()
        form = {**form, "csrf": self.csrf}
        r = await self._request("POST", f"{API_BASE}{path}", data=form)
        data = r.json()
        return self._check_bili_response(data)

    async def create_folder(self, title: str, privacy: int = 1, intro: str = "") -> int:
        data = await self._post_form(
            "/x/v3/fav/folder/add",
            {"title": title, "intro": intro, "privacy": privacy, "cover": "", "order": ""},
        )
        return data["id"]

    async def move_resources(self, src_media_id: int, tar_media_id: int, resources: str) -> bool:
        """移动收藏资源。resources 格式 "id:type,id:type"。"""
        await self._post_form(
            "/x/v3/fav/resource/move",
            {"src_media_id": src_media_id, "tar_media_id": tar_media_id, "resources": resources, "platform": "web"},
        )
        return True

    async def move_videos(self, src_media_id: int, tar_media_id: int, avids: list[int]) -> bool:
        resources = ",".join(f"{avid}:2" for avid in avids)
        return await self.move_resources(src_media_id, tar_media_id, resources)

    async def batch_delete_resources(self, media_id: int, resources: list[dict]) -> bool:
        resource_text = ",".join(f"{r['id']}:{r.get('type', 2)}" for r in resources)
        await self._post_form(
            "/x/v3/fav/resource/batch-del",
            {"media_id": media_id, "resources": resource_text, "platform": "web"},
        )
        return True

    async def delete_folders(self, media_ids: list[int]) -> bool:
        await self._post_form(
            "/x/v3/fav/folder/del",
            {"media_ids": ",".join(str(mid) for mid in media_ids)},
        )
        return True

    async def sort_folders(self, media_ids: list[int]) -> bool:
        await self._post_form(
            "/x/v3/fav/folder/sort",
            {"sort": ",".join(str(mid) for mid in media_ids)},
        )
        return True
