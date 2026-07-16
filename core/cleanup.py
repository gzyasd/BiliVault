import asyncio
import inspect
from collections import defaultdict

from core.errors import BibiError


MAX_CLEANUP_PAGES_WITHOUT_TOTAL = 5000


class CleanupManager:
    def __init__(self, storage, bili_client, sleep=asyncio.sleep):
        self.storage = storage
        self.bili = bili_client
        self._sleep = sleep

    async def _emit(self, callback, event: dict) -> None:
        if not callback:
            return
        value = callback(event)
        if inspect.isawaitable(value):
            await value

    async def scan(self, scan_id: str, account_id: str, on_progress=None) -> dict:
        try:
            folders = await self.bili.get_my_folders(storage=self.storage)
            self.storage.update_cleanup_scan(
                scan_id,
                status="scanning",
                folders_total=len(folders),
                folders_scanned=0,
                resources_scanned=0,
                problem_total=0,
                error="",
            )
            resources_scanned = 0
            problem_total = 0
            for folder_index, folder in enumerate(folders, start=1):
                fid = int(folder["fid"])
                title = str(folder.get("title") or fid)
                seen_keys: set[tuple[int, int]] = set()
                folder_detail_scanned = 0
                page = 1
                while True:
                    data = await self.bili.get_folder_resource_page(
                        fid,
                        page=page,
                        page_size=20,
                        storage=self.storage,
                    )
                    page_items = data.get("items", [])
                    page_keys = {
                        (int(item.get("resource_id") or 0), int(item.get("resource_type") or 2))
                        for item in page_items
                        if item.get("resource_id")
                    }
                    if page_items and data.get("has_more") and not (page_keys - seen_keys):
                        raise BibiError(
                            f"收藏夹 {title} 第 {page} 页分页重复，未发现新增资源",
                            code="BILI_PAGINATION_STALLED",
                        )
                    problem_items = []
                    for item in page_items:
                        resource_id = int(item.get("resource_id") or 0)
                        resource_type = int(item.get("resource_type") or 2)
                        if not resource_id:
                            continue
                        seen_keys.add((resource_id, resource_type))
                        if item.get("status") == "invalid":
                            problem_items.append({
                                "source_fid": fid,
                                "source_title": title,
                                "resource_id": resource_id,
                                "resource_type": resource_type,
                                "bvid": item.get("bvid", ""),
                                "title": item.get("title", ""),
                                "problem_type": "invalid",
                                "problem_label": "已失效",
                            })
                    if problem_items:
                        self.storage.add_cleanup_items(scan_id, problem_items)
                    page_item_count = len(page_items)
                    folder_detail_scanned += page_item_count
                    resources_scanned += page_item_count
                    problem_total = self.storage.count_cleanup_items(scan_id)
                    await self._emit(on_progress, {
                        "stage": "scanning",
                        "folders_scanned": folder_index - 1,
                        "folders_total": len(folders),
                        "resources_scanned": resources_scanned,
                        "problem_total": problem_total,
                        "current_folder_title": title,
                        "progress": (folder_index - 1) / len(folders) if folders else 1.0,
                    })
                    if not data.get("has_more"):
                        break
                    declared_total = int(data.get("total") or folder.get("media_count") or 0)
                    if declared_total:
                        max_pages = max(1, (declared_total + 19) // 20) + 5
                        if page + 1 > max_pages:
                            raise BibiError(
                                f"收藏夹 {title} 分页超过声明总数，已停止扫描",
                                code="BILI_PAGINATION_LIMIT",
                            )
                    elif page + 1 > MAX_CLEANUP_PAGES_WITHOUT_TOTAL:
                        raise BibiError(
                            f"收藏夹 {title} 分页超过安全上限，已停止扫描",
                            code="BILI_PAGINATION_LIMIT",
                        )
                    page += 1
                    await self._sleep(0.3)

                all_ids = await self.bili.get_folder_resource_ids(fid, storage=self.storage)
                inaccessible = []
                id_items_by_key = {}
                for item in all_ids:
                    resource_id = int(item.get("resource_id") or 0)
                    resource_type = int(item.get("resource_type") or 2)
                    if not resource_id:
                        continue
                    id_items_by_key[(resource_id, resource_type)] = item
                resources_scanned += max(0, len(id_items_by_key) - folder_detail_scanned)
                for (resource_id, resource_type), item in id_items_by_key.items():
                    if (resource_id, resource_type) in seen_keys:
                        continue
                    inaccessible.append({
                        "source_fid": fid,
                        "source_title": title,
                        "resource_id": resource_id,
                        "resource_type": resource_type,
                        "bvid": item.get("bvid", ""),
                        "title": "",
                        "problem_type": "inaccessible",
                        "problem_label": "无法访问",
                    })
                if inaccessible:
                    self.storage.add_cleanup_items(scan_id, inaccessible)
                problem_total = self.storage.count_cleanup_items(scan_id)
                self.storage.update_cleanup_scan(
                    scan_id,
                    folders_scanned=folder_index,
                    resources_scanned=resources_scanned,
                    problem_total=problem_total,
                    current_folder_title=title,
                )
                await self._emit(on_progress, {
                    "stage": "scanning",
                    "folders_scanned": folder_index,
                    "folders_total": len(folders),
                    "resources_scanned": resources_scanned,
                    "problem_total": problem_total,
                    "current_folder_title": title,
                    "progress": folder_index / len(folders) if folders else 1.0,
                })
                if folder_index < len(folders):
                    await self._sleep(0.3)

            self.storage.update_cleanup_scan(
                scan_id,
                status="ready",
                folders_scanned=len(folders),
                resources_scanned=resources_scanned,
                problem_total=problem_total,
                current_folder_title="",
            )
            result = self.get_scan(scan_id, account_id)
            await self._emit(on_progress, {"stage": "ready", "progress": 1.0, **result["scan"]})
            return result
        except asyncio.CancelledError:
            self.storage.update_cleanup_scan(scan_id, status="cancelled", current_folder_title="")
            raise
        except Exception as exc:
            self.storage.update_cleanup_scan(scan_id, status="failed", error=str(exc), current_folder_title="")
            raise

    def get_scan(self, scan_id: str, account_id: str) -> dict:
        scan = self.storage.get_cleanup_scan(scan_id, account_id=account_id)
        if not scan:
            raise BibiError("清理任务不存在或不属于当前账号", code="CLEANUP_SCAN_NOT_FOUND")
        return {"scan": scan, "items": self.storage.list_cleanup_items(scan_id)}

    async def remove(self, scan_id: str, account_id: str, item_ids: list[int], on_progress=None) -> dict:
        try:
            return await self._remove_once(scan_id, account_id, item_ids, on_progress=on_progress)
        except asyncio.CancelledError:
            self.storage.update_cleanup_scan(
                scan_id, status="cancelled", error="删除任务已取消，未确认条目将在重试时重新核验",
                current_folder_title="",
            )
            self._mark_unconfirmed_items(scan_id, item_ids, "删除任务已取消，状态未知，请重新核验")
            raise
        except Exception as exc:
            self.storage.update_cleanup_scan(
                scan_id, status="failed", error=str(exc), current_folder_title=""
            )
            self._mark_unconfirmed_items(
                scan_id, item_ids, f"删除后确认失败，状态未知：{exc}"
            )
            raise

    def _mark_unconfirmed_items(self, scan_id: str, item_ids: list[int], error: str) -> None:
        for item in self.storage.list_cleanup_items_by_ids(scan_id, item_ids):
            if not item["removed"] and not item.get("remove_error"):
                self.storage.mark_cleanup_item_removed(item["id"], False, error)

    async def _remove_once(self, scan_id: str, account_id: str, item_ids: list[int], on_progress=None) -> dict:
        scan = self.storage.get_cleanup_scan(scan_id, account_id=account_id)
        if not scan:
            raise BibiError("清理任务不存在或不属于当前账号", code="CLEANUP_SCAN_NOT_FOUND")
        items = [
            item for item in self.storage.list_cleanup_items_by_ids(scan_id, item_ids)
            if not item["removed"]
        ]
        is_retry = scan.get("status") in {"failed", "cancelled", "completed"}
        self.storage.update_cleanup_scan(scan_id, status="removing", error="")
        by_folder: dict[int, list[dict]] = defaultdict(list)
        for item in items:
            by_folder[int(item["source_fid"])].append(item)
        success = 0
        failed = 0
        processed = 0
        for source_fid, source_items in by_folder.items():
            pending_items = source_items
            if is_retry:
                current_keys = {
                    (int(item.get("resource_id") or 0), int(item.get("resource_type") or 2))
                    for item in await self.bili.get_folder_resource_ids(source_fid, storage=self.storage)
                }
                pending_items = []
                for item in source_items:
                    key = (item["resource_id"], item.get("resource_type") or 2)
                    if key in current_keys:
                        pending_items.append(item)
                        continue
                    self.storage.mark_cleanup_item_removed(item["id"], True, "")
                    success += 1
                    processed += 1
            submitted = []
            for start in range(0, len(pending_items), 50):
                chunk = pending_items[start:start + 50]
                try:
                    await self.bili.batch_delete_resources(
                        media_id=source_fid,
                        resources=[
                            {"id": item["resource_id"], "type": item.get("resource_type") or 2}
                            for item in chunk
                        ],
                    )
                    submitted.extend(chunk)
                except Exception as exc:
                    for item in chunk:
                        self.storage.mark_cleanup_item_removed(item["id"], False, str(exc))
                    failed += len(chunk)
                    processed += len(chunk)
            if submitted:
                remaining = {
                    (item["resource_id"], item.get("resource_type") or 2)
                    for item in submitted
                }
                for delay in (0.5, 1.0, 2.0):
                    await self._sleep(delay)
                    current = {
                        (int(item.get("resource_id") or 0), int(item.get("resource_type") or 2))
                        for item in await self.bili.get_folder_resource_ids(source_fid, storage=self.storage)
                    }
                    remaining &= current
                    if not remaining:
                        break
                for item in submitted:
                    key = (item["resource_id"], item.get("resource_type") or 2)
                    ok = key not in remaining
                    self.storage.mark_cleanup_item_removed(
                        item["id"], ok, "B站仍返回该资源，请稍后重试" if not ok else ""
                    )
                    success += 1 if ok else 0
                    failed += 0 if ok else 1
                    processed += 1
            await self._emit(on_progress, {
                "stage": "removing",
                "processed": processed,
                "total": len(items),
                "success": success,
                "failed": failed,
                "progress": processed / len(items) if items else 1.0,
                "current_folder_title": source_items[0].get("source_title", ""),
            })
        self.storage.update_cleanup_scan(scan_id, status="completed")
        stats = {"total": len(items), "success": success, "failed": failed}
        await self._emit(on_progress, {"stage": "completed", "progress": 1.0, **stats})
        return stats
