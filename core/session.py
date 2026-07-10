import inspect
import json
import logging

from core.errors import StateError, NotLoggedInError, BiliApiError, BibiError
from core.ai_classifier import VideoInfo, Classification

logger = logging.getLogger(__name__)


_VALID_TRANSITIONS = {
    "draft": {"collecting", "cancelled"},
    "collecting": {"classifying", "draft", "cancelled"},
    "classifying": {"pending_review", "draft", "cancelled"},
    "pending_review": {"executing", "cancelled"},
    "executing": {"done", "pending_review"},
    "done": set(),
    "cancelled": set(),
}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ClassifySession:
    def __init__(self, storage, bili_client, ai_classifier):
        self.storage = storage
        self.bili = bili_client
        self.ai = ai_classifier

    def _transition(self, sid: str, current: str, target: str) -> None:
        if target not in _VALID_TRANSITIONS.get(current, set()):
            raise StateError(f"非法状态转换: {current} -> {target}")
        self.storage.update_session_status(sid, target)

    def _ai_batch_size(self) -> int:
        cfg = self.storage.load_config() or {}
        try:
            value = int(cfg.get("ai_batch_size", 100))
        except (TypeError, ValueError):
            value = 100
        return max(10, min(200, value))

    async def create(self, source_fid: int, mode: str) -> str:
        return await self.create_many([source_fid], mode)

    async def create_many(self, source_fids: list[int], mode: str) -> str:
        if not self.bili.is_logged_in:
            raise NotLoggedInError()
        active_account = self.storage.get_active_account()
        account_id = active_account["account_id"] if active_account else ""
        unique_fids: list[int] = []
        for fid in source_fids:
            if fid not in unique_fids:
                unique_fids.append(fid)
        sid = self.storage.create_session(source_fid=unique_fids[0], mode=mode)
        if account_id:
            self.storage.update_session_account(sid, account_id)
        folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
        missing = [fid for fid in unique_fids if fid not in folders]
        if missing:
            raise BibiError(f"收藏夹不存在或不属于当前账号: {missing}", code="SOURCE_FOLDER_NOT_FOUND")
        self.storage.save_session_sources(sid, [
            {
                "account_id": account_id,
                "source_fid": fid,
                "title": folders[fid]["title"],
                "media_count": folders[fid].get("media_count", 0),
                "selected_order": idx,
                "delete_protected": folders[fid].get("fav_state") == 1,
            }
            for idx, fid in enumerate(unique_fids)
        ])
        return sid

    async def run_pipeline(self, sid: str, on_progress=None) -> None:
        await self.collect(sid, on_progress=on_progress)
        s = self.storage.load_session(sid)
        if not s or s["status"] == "cancelled":
            return
        await self.classify(sid, on_progress=on_progress)

    def _is_cancelled(self, sid: str) -> bool:
        s = self.storage.load_session(sid)
        return bool(s and s["status"] == "cancelled")

    async def collect(self, sid: str, on_progress=None) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] not in ("draft", "collecting"):
            raise StateError("会话不在可采集状态")
        self._transition(sid, s["status"], "collecting")
        await _emit_progress(on_progress, {"stage": "collecting", "progress": 0.0})
        sources = self.storage.list_session_sources(sid)
        if not sources and s.get("source_fid"):
            sources = [{"source_fid": s["source_fid"], "title": f"收藏夹 {s['source_fid']}", "media_count": 0}]
        source_total = sum(src.get("media_count", 0) for src in sources)
        if source_total == 0:
            source_total = None
        source_total_known: dict[int, bool] = {src["source_fid"]: bool(src.get("media_count")) for src in sources}
        collected = 0
        scanned = 0
        skipped = 0
        skipped_by_reason: dict[str, int] = {}
        for src in sources:
            source_fid = src["source_fid"]
            source_collected = 0
            source_skipped = 0
            source_expected_initialized = False
            if self._is_cancelled(sid):
                return
            pages_method = getattr(self.bili, "get_folder_resource_pages", None) or self.bili.get_folder_video_pages
            async for page in pages_method(source_fid, storage=self.storage):
                if self._is_cancelled(sid):
                    return
                if not source_expected_initialized:
                    source_expected_initialized = True
                    page_expected = page.get("expected_total") or 0
                    # 仅在该源的 media_count 缺失时用第一页 expected_total 补齐
                    if not source_total_known.get(source_fid):
                        if source_total is None:
                            source_total = page_expected
                        elif page_expected:
                            source_total += page_expected
                # 优先使用 resources（含非视频），回退到 videos（旧 API）
                resources = page.get("resources")
                if resources is None:
                    resources = [{**v, "resource_id": v["avid"], "resource_type": 2} for v in page["videos"]]
                for v in resources:
                    if self._is_cancelled(sid):
                        return
                    v["fid"] = source_fid
                    resource_id = v.get("resource_id") or v.get("avid")
                    resource_type = v.get("resource_type", 2)
                    v["avid"] = resource_id
                    v["resource_type"] = resource_type
                    if s["mode"] == "full" and v.get("bvid"):
                        v = await self._enrich_video(v)
                        if self._is_cancelled(sid):
                            return
                        v["fid"] = source_fid
                        v["avid"] = resource_id
                        v["resource_type"] = resource_type
                    v["account_id"] = s.get("account_id") or ""
                    v.setdefault("intro", "")
                    v.setdefault("tags", "[]")
                    self.storage.upsert_video(v)
                    self.storage.add_session_video_source(sid, resource_id=resource_id, source_fid=source_fid, resource_type=resource_type)
                page_skipped_items = page.get("skipped_items", [])
                if page_skipped_items:
                    self.storage.add_skipped_items(sid, s.get("account_id"), page_skipped_items)
                source_collected += page["usable_count"]
                source_skipped += page["skipped_count"]
                self.storage.update_session_source_counts(sid, source_fid, source_collected, source_skipped)
                collected += page["usable_count"]
                scanned += page["raw_count"]
                skipped += page["skipped_count"]
                for reason, cnt in page["skipped_reasons"].items():
                    skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + cnt
                progress = None
                if source_total:
                    progress = min(0.99, scanned / source_total)
                await _emit_progress(on_progress, {
                    "stage": "collecting", "progress": progress,
                    "source_fid": source_fid,
                    "collected": collected, "scanned": scanned, "skipped": skipped,
                    "source_total": source_total,
                })
                if self._is_cancelled(sid):
                    return
        collect_stats = {
            "source_total": source_total,
            "scanned_total": scanned,
            "collected_total": collected,
            "skipped_total": skipped,
            "skipped_by_reason": skipped_by_reason,
        }
        existing = s.get("stats")
        try:
            existing_stats = json.loads(existing) if isinstance(existing, str) and existing else {}
        except json.JSONDecodeError:
            existing_stats = {}
        merged_stats = {**existing_stats, **collect_stats}
        if self._is_cancelled(sid):
            return
        self.storage.update_session_status(sid, "collecting", stats=merged_stats)
        await _emit_progress(on_progress, {
            "stage": "collecting", "progress": 1.0,
            "collected": collected, "scanned": scanned, "skipped": skipped,
            "source_total": source_total, "skipped_by_reason": skipped_by_reason,
        })
        if self._is_cancelled(sid):
            return
        self._transition(sid, "collecting", "classifying")

    async def classify(self, sid: str, on_progress=None) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "classifying":
            raise StateError("会话不在分类状态")
        video_sources = self.storage.list_session_video_sources(sid)
        # 按会话来源的组合键精确查询，避免同 ID 其他类型缓存被误带入
        resource_keys = sorted({(row["resource_id"], row.get("resource_type", 2)) for row in video_sources})
        if resource_keys:
            videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
        else:
            videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
        if not videos_rows:
            videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
        videos = [VideoInfo(
            avid=r["resource_id"], title=r["title"], up_name=r["up_name"],
            tname=r.get("tname", ""), intro=r.get("intro", ""),
            tags=_parse_tags(r.get("tags", "[]")),
            resource_type=r.get("resource_type", 2),
        ) for r in videos_rows]
        total = len(videos)
        try:
            prior_stats = json.loads(s["stats"]) if s.get("stats") else {}
        except json.JSONDecodeError:
            prior_stats = {}
        await _emit_progress(on_progress, {
            "stage": "classifying", "progress": 0.0, "total": total,
            "source_total": prior_stats.get("source_total"),
            "skipped": prior_stats.get("skipped_total", 0),
        })
        if self._is_cancelled(sid):
            return

        async def ai_progress(event: dict):
            await _emit_progress(on_progress, {
                **event,
                "source_total": prior_stats.get("source_total"),
                "skipped": prior_stats.get("skipped_total", 0),
            })

        results = await self.ai.classify(videos, batch_size=self._ai_batch_size(), on_progress=ai_progress)
        if self._is_cancelled(sid):
            return
        # 写回时使用 Classification 自身的 resource_type，避免同 ID 不同类型互相覆盖
        classification_items = [
            {
                "avid": c.avid,
                "resource_id": c.avid,
                "resource_type": c.resource_type,
                "category": c.category,
                "confidence": c.confidence,
                "reason": c.reason,
            }
            for c in results
        ]
        self.storage.save_classifications(sid, classification_items)
        self.storage.create_plan_version(
            session_id=sid,
            parent_version_id=None,
            instruction="初始分类",
            items=classification_items,
            activate=True,
        )
        await _emit_progress(on_progress, {"stage": "classifying", "progress": 1.0, "classified": total})
        if self._is_cancelled(sid):
            return
        self._transition(sid, "classifying", "pending_review")
        await _emit_progress(on_progress, {"stage": "pending_review", "progress": 1.0})

    def cancel(self, sid: str) -> None:
        s = self.storage.load_session(sid)
        if not s:
            raise StateError("会话不存在")
        if s["status"] in ("executing", "done", "cancelled"):
            raise StateError(f"当前状态 {s['status']} 不支持取消")
        self._transition(sid, s["status"], "cancelled")

    async def _enrich_video(self, video: dict) -> dict:
        try:
            info = await self.bili.get_video_info(video["bvid"])
        except Exception:
            return video
        enriched = dict(video)
        for key in ("title", "intro", "up_name", "up_mid", "cover_url", "tname"):
            if info.get(key):
                enriched[key] = info[key]
        tags = info.get("tags")
        if isinstance(tags, list):
            enriched["tags"] = json.dumps(tags, ensure_ascii=False)
        return enriched

    def get_plan(self, sid: str) -> dict:
        s = self.storage.load_session(sid)
        if not s:
            raise StateError("会话不存在")
        active = self.storage.get_active_plan_version(sid)
        if active:
            items = self.storage.load_plan_items(active["version_id"])
        else:
            items = self.storage.load_classifications(sid)
        # 按方案项的组合键精确查询，避免同 ID 其他类型缓存被误带入
        resource_keys = [
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
            for it in items
        ]
        videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
        if not videos_rows and s.get("source_fid"):
            videos_rows = self.storage.list_videos_by_fid(s["source_fid"])
        # 用 "resource_id:resource_type" 组合键，避免同 ID 不同类型互相覆盖
        videos = {f"{r['resource_id']}:{r.get('resource_type', 2)}": r for r in videos_rows}
        return {
            "session": s,
            "sources": self.storage.list_session_sources(sid),
            "video_sources": self.storage.list_session_video_sources(sid),
            "items": items,
            "videos": videos,
            "versions": self.storage.list_plan_versions(sid),
        }

    def adjust_item(self, sid: str, resource_id: int, new_category: str, resource_type: int = 2) -> None:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可调整")
        active = self.storage.get_active_plan_version(sid)
        if active:
            self.storage.adjust_plan_item(active["version_id"], resource_id, new_category, resource_type=resource_type)
        else:
            self.storage.adjust_classification(sid, resource_id, new_category, resource_type=resource_type)

    async def refine_plan(self, sid: str, instruction: str) -> dict:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可微调方案")
        active = self.storage.get_active_plan_version(sid)
        if not active:
            self.storage.migrate_legacy_classifications_to_version(sid)
            active = self.storage.get_active_plan_version(sid)
        if not active:
            raise StateError("当前没有可微调的方案版本")
        current_items = self.storage.load_plan_items(active["version_id"])
        # 按当前方案项的组合键精确查询，避免同 ID 其他类型缓存被误带入
        resource_keys = sorted({
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
            for it in current_items
        })
        videos_by_key = {
            (r["resource_id"], r.get("resource_type", 2)): r
            for r in self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
        }
        videos = []
        for it in current_items:
            rid = it.get("resource_id", it.get("avid"))
            rtype = it.get("resource_type", 2)
            v = videos_by_key.get((rid, rtype))
            if not v:
                continue
            videos.append(VideoInfo(
                avid=rid, title=v["title"], up_name=v["up_name"],
                tname=v.get("tname", ""), intro=v.get("intro", ""),
                tags=_parse_tags(v.get("tags", "[]")),
                resource_type=rtype,
            ))
        current = [Classification(
            it.get("resource_id", it.get("avid")),
            it["category"], it["confidence"], it["reason"],
            resource_type=it.get("resource_type", 2),
        ) for it in current_items]
        refined = await self.ai.refine_plan(videos, current, instruction)
        self.storage.create_plan_version(
            sid,
            active["version_id"],
            instruction,
            [
                {
                    "avid": c.avid,
                    "resource_id": c.avid,
                    "resource_type": c.resource_type,
                    "category": c.category,
                    "confidence": c.confidence,
                    "reason": c.reason,
                }
                for c in refined
            ],
            activate=True,
        )
        return self.get_plan(sid)

    def get_failed_items(self, sid: str) -> list[dict]:
        return self.storage.list_failed_items(sid)

    async def remove_skipped_items(self, sid: str, item_ids: list[int]) -> dict:
        s = self.storage.load_session(sid)
        if not s:
            raise StateError("会话不存在")
        items = self.storage.list_skipped_items_by_ids(sid, item_ids)
        removable = [it for it in items if it["removable"] and not it["removed"] and it["avid"] and it["source_fid"]]
        success = 0
        failed = 0
        by_source: dict[int, list[dict]] = {}
        for it in removable:
            by_source.setdefault(int(it["source_fid"]), []).append(it)
        for source_fid, source_items in by_source.items():
            for chunk in _chunks(source_items, 50):
                try:
                    await self.bili.batch_delete_resources(
                        media_id=source_fid,
                        resources=[{"id": it["avid"], "type": it.get("resource_type") or 2} for it in chunk],
                    )
                    for it in chunk:
                        self.storage.mark_skipped_item_removed(it["id"], True, "")
                    success += len(chunk)
                except Exception as e:
                    for it in chunk:
                        self.storage.mark_skipped_item_removed(it["id"], False, str(e))
                    failed += len(chunk)
        return {"success": success, "failed": failed, "total": len(removable)}

    async def execute(self, sid: str, batch_size: int = 50, on_progress=None) -> dict:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可执行")
        self._transition(sid, "pending_review", "executing")
        # 优先使用激活版本 items，回退到旧 classifications 表
        active = self.storage.get_active_plan_version(sid)
        if active:
            version_id = active["version_id"]
            items = self.storage.load_plan_items(version_id)
        else:
            version_id = None
            items = self.storage.load_classifications(sid)
        # 用 (resource_id, resource_type) 组合键映射 videos，避免同 ID 不同类型互相覆盖
        # 按方案项组合键查询标题，多源会话也能正确取到
        resource_keys = [
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2))
            for it in items
            if it.get("category") != "未分类"
        ]
        videos = {
            (r["resource_id"], r.get("resource_type", 2)): r
            for r in self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
        }
        # 构建 sources_by_key 映射；无记录时回退到会话 source_fid
        sources = self.storage.list_session_video_sources(sid)
        sources_by_key: dict[tuple[int, int], list[dict]] = {}
        for src in sources:
            key = (src["resource_id"], src.get("resource_type", 2))
            sources_by_key.setdefault(key, []).append(src)
        # 按 (category, source_fid) 分组，每项为 (resource_id, resource_type)
        move_groups: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for it in items:
            cat = it["category"]
            if cat == "未分类":
                continue
            resource_id = it.get("resource_id", it.get("avid"))
            resource_type = it.get("resource_type", 2)
            key = (resource_id, resource_type)
            srcs = sources_by_key.get(key, [{"source_fid": s["source_fid"], "resource_type": resource_type}])
            for src in srcs:
                gkey = (cat, src["source_fid"])
                move_groups.setdefault(gkey, []).append((resource_id, src.get("resource_type", resource_type)))
        # 为每个 category 创建目标收藏夹
        cat_to_fid: dict[str, int] = {}
        categories = sorted({cat for cat, _ in move_groups.keys()})
        success = 0
        failed = 0
        for cat in categories:
            try:
                cat_to_fid[cat] = await self.bili.create_folder(title=cat, privacy=1)
            except Exception as e:
                err_code, err_msg = _extract_error(e)
                for (c, sf), res_list in move_groups.items():
                    if c != cat:
                        continue
                    for resource_id, resource_type in res_list:
                        if version_id:
                            self.storage.mark_plan_item_executed(version_id, resource_id, False, resource_type=resource_type)
                        else:
                            self.storage.mark_classification_executed(sid, resource_id, False, resource_type=resource_type)
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, False, err_msg, resource_type=resource_type)
                        self.storage.add_failed_item(sid, {
                            "avid": resource_id,
                            "resource_id": resource_id,
                            "resource_type": resource_type,
                            "title": videos.get((resource_id, resource_type), {}).get("title", ""),
                            "category": cat,
                            "target_fid": 0,
                            "error_code": err_code,
                            "error_message": err_msg,
                        })
                    failed += len(res_list)
        # 按分组移动
        for (cat, sf), res_list in move_groups.items():
            if cat not in cat_to_fid:
                continue
            target_fid = cat_to_fid[cat]
            for chunk in _chunks(res_list, batch_size):
                try:
                    resources_str = ",".join(f"{rid}:{rtype}" for rid, rtype in chunk)
                    await self.bili.move_resources(
                        src_media_id=sf,
                        tar_media_id=target_fid,
                        resources=resources_str,
                    )
                    for resource_id, resource_type in chunk:
                        if version_id:
                            self.storage.mark_plan_item_executed(version_id, resource_id, True, resource_type=resource_type)
                        else:
                            self.storage.mark_classification_executed(sid, resource_id, True, resource_type=resource_type)
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, True, "", resource_type=resource_type)
                    success += len(chunk)
                except Exception as e:
                    err_code, err_msg = _extract_error(e)
                    for resource_id, resource_type in chunk:
                        if version_id:
                            self.storage.mark_plan_item_executed(version_id, resource_id, False, resource_type=resource_type)
                        else:
                            self.storage.mark_classification_executed(sid, resource_id, False, resource_type=resource_type)
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, False, err_msg, resource_type=resource_type)
                        self.storage.add_failed_item(sid, {
                            "avid": resource_id,
                            "resource_id": resource_id,
                            "resource_type": resource_type,
                            "title": videos.get((resource_id, resource_type), {}).get("title", ""),
                            "category": cat,
                            "target_fid": target_fid,
                            "error_code": err_code,
                            "error_message": err_msg,
                        })
                    failed += len(chunk)
        stats = {"success": success, "failed": failed, "total": success + failed}
        await self.refresh_empty_source_candidates(sid)
        self.storage.update_session_status(sid, "done", stats=stats)
        await _emit_progress(on_progress, {"stage": "done", "progress": 1.0, "stats": stats})
        return stats

    async def refresh_empty_source_candidates(self, sid: str) -> None:
        sources = self.storage.list_session_sources(sid)
        if not sources:
            return
        try:
            folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
        except Exception as e:
            logger.warning("移动已完成，但刷新空源收藏夹候选失败: %s", e)
            return
        for src in sources:
            fid = src["source_fid"]
            folder = folders.get(fid)
            if not folder:
                continue
            is_empty = int(folder.get("media_count", 0)) == 0
            self.storage.mark_session_source_empty_candidate(
                sid,
                fid,
                delete_candidate=is_empty and not src.get("delete_protected"),
                emptied_after_execute=is_empty,
            )

    async def delete_empty_source_folders(self, sid: str, source_fids: list[int]) -> dict:
        sources = {s["source_fid"]: s for s in self.storage.list_session_sources(sid)}
        folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
        deletable = []
        rejected = []
        for fid in source_fids:
            src = sources.get(fid)
            folder = folders.get(fid)
            if not src or not folder:
                rejected.append(fid)
                continue
            if src.get("delete_protected"):
                rejected.append(fid)
                continue
            if int(folder.get("media_count", 0)) != 0:
                rejected.append(fid)
                continue
            deletable.append(fid)
        deleted = []
        if deletable:
            delete_error = ""
            try:
                await self.bili.delete_folders(deletable)
            except Exception as e:
                delete_error = str(e)
            # 无论成功失败都重新拉取核验实际状态
            latest = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
            for fid in deletable:
                if fid not in latest:
                    self.storage.mark_session_source_deleted(sid, fid, True, "")
                    deleted.append(fid)
                else:
                    self.storage.mark_session_source_deleted(sid, fid, False, delete_error or "B站返回成功但收藏夹仍存在")
                    rejected.append(fid)
        return {"success": len(deleted), "failed": len(rejected), "deleted": deleted, "rejected": rejected}

    async def retry_failed(self, sid: str, batch_size: int = 50) -> dict:
        s = self.storage.load_session(sid)
        if not s or s["status"] != "done":
            raise StateError("仅完成状态可重试失败项")
        # 优先按来源实例重试（多源场景）
        failed_sources = self.storage.list_failed_session_video_sources(sid)
        if failed_sources:
            return await self._retry_failed_sources(sid, failed_sources, batch_size)
        # 回退到旧 failed_items 逻辑（单源兼容）
        failed = self.storage.list_failed_items(sid)
        if not failed:
            return {"success": 0, "failed": 0}
        by_target: dict[int, list[dict]] = {}
        missing_target_by_cat: dict[str, list[dict]] = {}
        for it in failed:
            if it["target_fid"]:
                by_target.setdefault(it["target_fid"], []).append(it)
            else:
                missing_target_by_cat.setdefault(it["category"], []).append(it)
        success = 0
        still_failed = 0
        for cat, items in missing_target_by_cat.items():
            try:
                target_fid = await self.bili.create_folder(title=cat, privacy=1)
            except Exception:
                for it in items:
                    self.storage.mark_failed_item_retried(it["id"])
                still_failed += len(items)
                continue
            for it in items:
                it["target_fid"] = target_fid
                self.storage.update_failed_item_target(it["id"], target_fid)
            by_target.setdefault(target_fid, []).extend(items)
        for target_fid, items in by_target.items():
            for i in range(0, len(items), batch_size):
                chunk = items[i:i + batch_size]
                resources_str = ",".join(
                    f"{it.get('resource_id', it['avid'])}:{it.get('resource_type', 2)}" for it in chunk
                )
                try:
                    await self.bili.move_resources(
                        src_media_id=s["source_fid"],
                        tar_media_id=target_fid,
                        resources=resources_str,
                    )
                    for it in chunk:
                        self.storage.mark_classification_executed(sid, it.get("resource_id", it["avid"]), True, resource_type=it.get("resource_type", 2))
                        self.storage.delete_failed_item(it["id"])
                    success += len(chunk)
                except Exception:
                    for it in chunk:
                        self.storage.mark_failed_item_retried(it["id"])
                    still_failed += len(chunk)
        return {"success": success, "failed": still_failed}

    async def _retry_failed_sources(self, sid: str, failed_sources: list[dict], batch_size: int) -> dict:
        # 获取 (resource_id, resource_type) -> item 映射
        active = self.storage.get_active_plan_version(sid)
        if active:
            version_id = active["version_id"]
            plan_items = self.storage.load_plan_items(version_id)
        else:
            version_id = None
            plan_items = self.storage.load_classifications(sid)
        items_by_key = {
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2)): it
            for it in plan_items
        }
        # 按 (category, source_fid) 分组，每项为 (resource_id, resource_type)
        move_groups: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for src in failed_sources:
            resource_id = src["resource_id"]
            resource_type = src.get("resource_type", 2)
            it = items_by_key.get((resource_id, resource_type))
            if not it or it["category"] == "未分类":
                continue
            key = (it["category"], src["source_fid"])
            move_groups.setdefault(key, []).append((resource_id, resource_type))
        if not move_groups:
            return {"success": 0, "failed": 0}
        # 为每个 category 确定 target_fid（优先复用 failed_items 中已有 target_fid）
        failed_items_by_key = {
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2)): it
            for it in self.storage.list_failed_items(sid)
        }
        cat_to_fid: dict[str, int] = {}
        categories = sorted({cat for cat, _ in move_groups.keys()})
        for cat in categories:
            target_fid = 0
            for it in failed_items_by_key.values():
                if it["category"] == cat and it["target_fid"]:
                    target_fid = it["target_fid"]
                    break
            if not target_fid:
                try:
                    target_fid = await self.bili.create_folder(title=cat, privacy=1)
                except Exception:
                    target_fid = 0
            if target_fid:
                cat_to_fid[cat] = target_fid
        # 按分组重试
        success = 0
        failed = 0
        for (cat, sf), res_list in move_groups.items():
            target_fid = cat_to_fid.get(cat, 0)
            if not target_fid:
                failed += len(res_list)
                continue
            for chunk in _chunks(res_list, batch_size):
                try:
                    resources_str = ",".join(f"{rid}:{rtype}" for rid, rtype in chunk)
                    await self.bili.move_resources(
                        src_media_id=sf,
                        tar_media_id=target_fid,
                        resources=resources_str,
                    )
                    for resource_id, resource_type in chunk:
                        if version_id:
                            self.storage.mark_plan_item_executed(version_id, resource_id, True, resource_type=resource_type)
                        else:
                            self.storage.mark_classification_executed(sid, resource_id, True, resource_type=resource_type)
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, True, "", resource_type=resource_type)
                        # 按来源实例清理一条 failed_item，避免多源残留
                        self.storage.delete_one_failed_item(sid, resource_id, cat, resource_type=resource_type)
                    success += len(chunk)
                except Exception as e:
                    err_msg = str(e)
                    for resource_id, resource_type in chunk:
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, False, err_msg, resource_type=resource_type)
                    failed += len(chunk)
        return {"success": success, "failed": failed}

    def list_resumable(self) -> list[dict]:
        return self.storage.list_sessions_by_status(["pending_review", "executing"])

    def resume_on_startup(self) -> None:
        for s in self.storage.list_sessions_by_status(["collecting", "classifying", "executing"]):
            if s["status"] in ("collecting", "classifying"):
                self.storage.update_session_status(s["session_id"], "draft")
            elif s["status"] == "executing":
                self.storage.update_session_status(s["session_id"], "pending_review")


def _parse_tags(s: str) -> list[str]:
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _extract_error(e: Exception) -> tuple[str, str]:
    if isinstance(e, BiliApiError):
        return str(e.bili_code), e.user_message
    return "", str(e)


async def _emit_progress(on_progress, event: dict) -> None:
    if not on_progress:
        return
    result = on_progress(event)
    if inspect.isawaitable(result):
        await result
