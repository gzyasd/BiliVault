import asyncio
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
    "failed": {"draft", "classifying", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ClassifySession:
    def __init__(self, storage, bili_client, ai_classifier=None):
        self.storage = storage
        self.bili = bili_client
        self.ai = ai_classifier
        self.account_id = str(getattr(bili_client, "account_id", "") or "")

    def _require_ai(self):
        if self.ai is None:
            raise BibiError("请先在设置页填写完整的 AI 配置", code="AI_NOT_CONFIGURED")
        return self.ai

    def _assert_bound_session(self, sid: str) -> dict | None:
        session = self.storage.load_session(sid)
        if session and self.account_id:
            session_account = str(session.get("account_id") or "")
            if session_account != self.account_id:
                raise StateError("整理会话不属于当前 B 站账号")
        return session

    def _transition(self, sid: str, current: str, target: str) -> None:
        self._assert_bound_session(sid)
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

    async def create(self, source_fid: int, mode: str, category_limit: int = 14) -> str:
        return await self.create_many([source_fid], mode, category_limit=category_limit)

    async def create_many(self, source_fids: list[int], mode: str, category_limit: int = 14) -> str:
        if not self.bili.is_logged_in:
            raise NotLoggedInError()
        active_account = self.storage.get_active_account()
        account_id = active_account["account_id"] if active_account else ""
        if self.account_id and str(account_id or "") != self.account_id:
            raise StateError("当前 B 站客户端与已激活账号不一致")
        unique_fids: list[int] = []
        for fid in source_fids:
            if fid not in unique_fids:
                unique_fids.append(fid)
        if not unique_fids:
            raise BibiError("请至少选择一个收藏夹", code="SOURCE_FOLDER_REQUIRED")
        folders = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
        missing = [fid for fid in unique_fids if fid not in folders]
        if missing:
            raise BibiError(f"收藏夹不存在或不属于当前账号: {missing}", code="SOURCE_FOLDER_NOT_FOUND")
        sid = self.storage.create_session(
            source_fid=unique_fids[0],
            mode=mode,
            category_limit=category_limit,
        )
        if account_id:
            self.storage.update_session_account(sid, account_id)
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
        self._assert_bound_session(sid)
        session = self.storage.load_session(sid)
        if not session:
            raise StateError("会话不存在")
        if session["status"] == "failed":
            retry_status = "classifying" if session.get("failed_stage") == "classifying" else "draft"
            self.storage.clear_session_failure(sid, retry_status)
            session = self.storage.load_session(sid)
        try:
            if session["status"] in ("draft", "collecting"):
                await self.collect(sid, on_progress=on_progress)
            session = self.storage.load_session(sid)
            if not session or session["status"] == "cancelled":
                return
            if session["status"] == "classifying":
                await self.classify(sid, on_progress=on_progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.storage.load_session(sid) or {}
            stage = current.get("status") or "pipeline"
            code = exc.code if isinstance(exc, BibiError) else "INTERNAL"
            self.storage.mark_session_failed(sid, stage, str(code), str(exc))
            raise

    def _is_cancelled(self, sid: str) -> bool:
        s = self.storage.load_session(sid)
        return bool(s and s["status"] == "cancelled")

    async def collect(self, sid: str, on_progress=None) -> None:
        self._assert_bound_session(sid)
        s = self.storage.load_session(sid)
        if not s or s["status"] not in ("draft", "collecting"):
            raise StateError("会话不在可采集状态")
        if s["status"] == "draft":
            await asyncio.to_thread(self.storage.reset_session_collection, sid)
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
        enrichment_tasks: dict[str, asyncio.Task] = {}
        enrichment_semaphore = asyncio.Semaphore(5)
        for src in sources:
            source_fid = src["source_fid"]
            source_collected = 0
            source_skipped = 0
            source_scanned = 0
            source_expected_initialized = False
            seen_resource_keys: set[tuple[int, int]] = set()
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
                normalized_resources = []
                for raw_resource in resources:
                    if self._is_cancelled(sid):
                        return
                    v = dict(raw_resource)
                    v["fid"] = source_fid
                    resource_id = v.get("resource_id") or v.get("avid")
                    resource_type = v.get("resource_type", 2)
                    if resource_id:
                        seen_resource_keys.add((resource_id, resource_type))
                    v["avid"] = resource_id
                    v["resource_type"] = resource_type
                    normalized_resources.append(v)
                if s["mode"] == "full":
                    normalized_resources = list(await asyncio.gather(*[
                        self._enrich_video(
                            resource,
                            cache=enrichment_tasks,
                            semaphore=enrichment_semaphore,
                        ) if resource.get("bvid") else asyncio.sleep(0, result=resource)
                        for resource in normalized_resources
                    ]))
                if self._is_cancelled(sid):
                    return
                for v in normalized_resources:
                    resource_id = v.get("resource_id") or v.get("avid")
                    resource_type = v.get("resource_type", 2)
                    v["fid"] = source_fid
                    v["avid"] = resource_id
                    v["resource_type"] = resource_type
                    v["account_id"] = s.get("account_id") or ""
                    v.setdefault("intro", "")
                    v.setdefault("tags", "[]")
                page_skipped_items = page.get("skipped_items", [])
                if normalized_resources or page_skipped_items:
                    await asyncio.to_thread(
                        self.storage.save_collected_page,
                        sid,
                        s.get("account_id"),
                        source_fid,
                        normalized_resources,
                        page_skipped_items,
                    )
                if page_skipped_items:
                    for item in page_skipped_items:
                        resource_id = item.get("resource_id", item.get("avid"))
                        resource_type = item.get("resource_type", 2)
                        if resource_id:
                            seen_resource_keys.add((resource_id, resource_type))
                source_collected += page["usable_count"]
                source_skipped += page["skipped_count"]
                source_scanned += page["raw_count"]
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
            try:
                resource_ids = await self.bili.get_folder_resource_ids(source_fid, storage=self.storage)
            except Exception as exc:
                logger.warning("无法交叉核验收藏夹 %s 的资源清单: %s", source_fid, exc)
            else:
                unique_resource_ids: list[dict] = []
                id_keys: set[tuple[int, int]] = set()
                for resource in resource_ids:
                    resource_id = resource.get("resource_id")
                    resource_type = resource.get("resource_type", 2)
                    if not resource_id or (resource_id, resource_type) in id_keys:
                        continue
                    id_keys.add((resource_id, resource_type))
                    unique_resource_ids.append(resource)
                hidden_resources = [
                    resource for resource in unique_resource_ids
                    if (resource["resource_id"], resource.get("resource_type", 2)) not in seen_resource_keys
                ]
                if hidden_resources:
                    await asyncio.to_thread(self.storage.add_skipped_items, sid, s.get("account_id"), [
                        {
                            "source_fid": source_fid,
                            "avid": resource["resource_id"],
                            "bvid": resource.get("bvid", ""),
                            "title": "",
                            "resource_type": resource.get("resource_type", 2),
                            "raw_attr": 0,
                            "reason_code": "inaccessible",
                            "reason_label": "无访问权限",
                            "detail": "B站未返回资源详情，可能仅UP主可见或受权限限制",
                            "removable": True,
                        }
                        for resource in hidden_resources
                    ])
                    hidden_count = len(hidden_resources)
                    source_skipped += hidden_count
                    skipped += hidden_count
                    skipped_by_reason["inaccessible"] = skipped_by_reason.get("inaccessible", 0) + hidden_count
                corrected_source_scanned = max(source_scanned, len(unique_resource_ids))
                scanned += corrected_source_scanned - source_scanned
                source_scanned = corrected_source_scanned
                self.storage.update_session_source_counts(sid, source_fid, source_collected, source_skipped)
                progress = min(0.99, scanned / source_total) if source_total else None
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
        self._assert_bound_session(sid)
        ai = self._require_ai()
        s = self.storage.load_session(sid)
        if not s or s["status"] != "classifying":
            raise StateError("会话不在分类状态")
        video_sources = self.storage.list_session_video_sources(sid)
        # 按会话来源的组合键精确查询，避免同 ID 其他类型缓存被误带入
        resource_keys = sorted({(row["resource_id"], row.get("resource_type", 2)) for row in video_sources})
        videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
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

        results = []
        if videos:
            results = await ai.classify(
                videos,
                batch_size=self._ai_batch_size(),
                on_progress=ai_progress,
                max_categories=int(s.get("category_limit") or 14),
            )
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
        self._assert_bound_session(sid)
        s = self.storage.load_session(sid)
        if not s:
            raise StateError("会话不存在")
        if s["status"] in ("executing", "done", "cancelled"):
            raise StateError(f"当前状态 {s['status']} 不支持取消")
        self._transition(sid, s["status"], "cancelled")

    async def _enrich_video(
        self,
        video: dict,
        cache: dict[str, asyncio.Task] | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> dict:
        async def fetch_info() -> dict:
            try:
                if semaphore is None:
                    return await self.bili.get_video_info(video["bvid"])
                async with semaphore:
                    return await self.bili.get_video_info(video["bvid"])
            except Exception:
                return {}

        if cache is None:
            info = await fetch_info()
        else:
            task = cache.get(video["bvid"])
            if task is None:
                task = asyncio.create_task(fetch_info())
                cache[video["bvid"]] = task
            info = await task
        if not info:
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
        self._assert_bound_session(sid)
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
        self._assert_bound_session(sid)
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可调整")
        active = self.storage.get_active_plan_version(sid)
        if active:
            self.storage.adjust_plan_item(active["version_id"], resource_id, new_category, resource_type=resource_type)
        else:
            self.storage.adjust_classification(sid, resource_id, new_category, resource_type=resource_type)

    async def refine_plan(self, sid: str, instruction: str, on_progress=None) -> dict:
        self._assert_bound_session(sid)
        ai = self._require_ai()
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
        latest_retry_count = 0

        async def ai_progress(event: dict):
            nonlocal latest_retry_count
            latest_retry_count = int(event.get("retry_count") or latest_retry_count)
            await _emit_progress(on_progress, event)

        refined = await ai.refine_plan(
            videos,
            current,
            instruction,
            max_categories=int(s.get("category_limit") or 14),
            batch_size=self._ai_batch_size(),
            on_progress=ai_progress,
        )
        await _emit_progress(on_progress, {
            "stage": "saving",
            "processed": len(current_items),
            "total": len(current_items),
            "progress": 1.0,
            "retry_count": latest_retry_count,
        })
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

    async def retry_unclassified(self, sid: str, on_progress=None) -> dict:
        self._assert_bound_session(sid)
        ai = self._require_ai()
        s = self.storage.load_session(sid)
        if not s or s["status"] != "pending_review":
            raise StateError("仅预览状态可重试未分类条目")
        active = self.storage.get_active_plan_version(sid)
        if not active:
            self.storage.migrate_legacy_classifications_to_version(sid)
            active = self.storage.get_active_plan_version(sid)
        if not active:
            raise StateError("当前没有可重试的方案版本")

        current_items = self.storage.load_plan_items(active["version_id"])
        targets = [item for item in current_items if item["category"] == "未分类"]
        if not targets:
            return {"plan": self.get_plan(sid), "recovered": 0, "remaining": 0}
        resource_keys = [
            (item.get("resource_id", item.get("avid")), item.get("resource_type", 2))
            for item in targets
        ]
        rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
        videos = [VideoInfo(
            avid=row["resource_id"],
            title=row["title"],
            up_name=row["up_name"],
            tname=row.get("tname", ""),
            intro=row.get("intro", ""),
            tags=_parse_tags(row.get("tags", "[]")),
            resource_type=row.get("resource_type", 2),
        ) for row in rows]

        async def ai_progress(event: dict):
            await _emit_progress(on_progress, {**event, "stage": "refining"})

        retried = await ai.classify(
            videos,
            batch_size=self._ai_batch_size(),
            on_progress=ai_progress,
            max_categories=int(s.get("category_limit") or 14),
        ) if videos else []
        recovered_by_key = {
            (item.avid, item.resource_type): item
            for item in retried
            if item.category != "未分类"
        }
        recovered = len(recovered_by_key)
        if recovered:
            merged_items = []
            for item in current_items:
                key = (item.get("resource_id", item.get("avid")), item.get("resource_type", 2))
                replacement = recovered_by_key.get(key)
                merged_items.append({
                    "avid": key[0],
                    "resource_id": key[0],
                    "resource_type": key[1],
                    "category": replacement.category if replacement else item["category"],
                    "confidence": replacement.confidence if replacement else item["confidence"],
                    "reason": replacement.reason if replacement else item["reason"],
                })
            await _emit_progress(on_progress, {
                "stage": "saving", "processed": len(targets), "total": len(targets),
                "progress": 1.0, "retry_count": 0,
            })
            self.storage.create_plan_version(
                sid,
                active["version_id"],
                "重试未分类",
                merged_items,
                activate=True,
            )
        remaining = len(targets) - recovered
        return {"plan": self.get_plan(sid), "recovered": recovered, "remaining": remaining}

    def get_failed_items(self, sid: str) -> list[dict]:
        self._assert_bound_session(sid)
        return self.storage.list_failed_items(sid)

    async def remove_skipped_items(self, sid: str, item_ids: list[int]) -> dict:
        self._assert_bound_session(sid)
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

    async def execute(
        self,
        sid: str,
        batch_size: int = 50,
        on_progress=None,
        run_id: str | None = None,
    ) -> dict:
        self._assert_bound_session(sid)
        session = self.storage.load_session(sid)
        if not session or session["status"] != "pending_review":
            raise StateError("仅预览状态可执行")
        active = self.storage.get_active_plan_version(sid)
        version_id = active["version_id"] if active else None
        existing_run = self.storage.get_execution_run(run_id) if run_id else None
        if existing_run and existing_run["session_id"] != sid:
            raise StateError("执行任务不属于当前会话")
        if not existing_run:
            run_id = self.storage.create_execution_run(
                sid,
                version_id=version_id,
                account_id=session.get("account_id") or self.account_id,
                run_id=run_id,
            )
        self.storage.update_execution_run(run_id, status="running", error="")
        try:
            stats = await self._execute_once(sid, batch_size, on_progress, run_id)
        except asyncio.CancelledError:
            self.storage.update_execution_run(run_id, status="cancelled", error="执行已取消")
            current = self.storage.load_session(sid)
            if current and current["status"] == "executing":
                self.storage.update_session_status(sid, "pending_review")
            raise
        except Exception as exc:
            self.storage.update_execution_run(run_id, status="failed", error=str(exc))
            current = self.storage.load_session(sid)
            if current and current["status"] == "executing":
                self.storage.update_session_status(sid, "pending_review")
            raise
        self.storage.update_execution_run(
            run_id,
            status="completed",
            processed=stats["total"],
            total=stats["total"],
            success=stats["success"],
            failed=stats["failed"],
            error="",
        )
        return stats

    async def _recover_saved_execution_targets(
        self,
        sid: str,
        version_id: str | None,
        categories: set[str],
    ) -> dict[str, int]:
        recovered: dict[str, int] = {}
        unresolved: list[tuple[str, dict, set[int]]] = []
        for category in sorted(categories):
            target = self.storage.get_execution_target(sid, version_id, category)
            if not target:
                continue
            if target.get("target_fid") and target.get("status") == "ready":
                recovered[category] = int(target["target_fid"])
                continue
            try:
                baseline = {int(fid) for fid in json.loads(target.get("baseline_fids") or "[]")}
            except (TypeError, ValueError, json.JSONDecodeError):
                baseline = set()
            if baseline and target.get("status") in {"creating", "failed"}:
                unresolved.append((category, target, baseline))
        if not unresolved:
            return recovered
        try:
            folders = await self.bili.get_my_folders(storage=self.storage)
        except Exception as exc:
            logger.warning("无法核验中断前创建的目标收藏夹: %s", exc)
            return recovered
        for category, _, baseline in unresolved:
            candidates = [
                int(folder["fid"])
                for folder in folders
                if int(folder["fid"]) not in baseline
                and str(folder.get("title") or "").strip() == category
            ]
            if len(candidates) == 1:
                target_fid = candidates[0]
                self.storage.save_execution_target(
                    sid,
                    version_id,
                    category,
                    target_fid,
                    status="ready",
                    error="",
                    baseline_fids=sorted(baseline),
                )
                recovered[category] = target_fid
            elif len(candidates) > 1:
                self.storage.save_execution_target(
                    sid,
                    version_id,
                    category,
                    0,
                    status="ambiguous",
                    error="发现多个同名新收藏夹，无法安全判断执行目标",
                    baseline_fids=sorted(baseline),
                )
        return recovered

    async def _reconcile_remote_execution_positions(
        self,
        sid: str,
        version_id: str | None,
        items_by_key: dict[tuple[int, int], dict],
        target_by_category: dict[str, int],
        sources: list[dict],
        on_progress=None,
    ) -> int:
        candidates = []
        remote_fids: set[int] = set()
        for source in sources:
            if source.get("moved"):
                continue
            key = (int(source["resource_id"]), int(source.get("resource_type", 2)))
            item = items_by_key.get(key)
            if not item:
                continue
            target_fid = target_by_category.get(item.get("category", ""))
            if not target_fid:
                continue
            source_fid = int(source["source_fid"])
            candidates.append((source, item, key, source_fid, target_fid))
            remote_fids.update((source_fid, target_fid))
        if not candidates:
            return 0

        await _emit_progress(on_progress, {
            "stage": "executing",
            "phase": "reconciling",
            "progress": 0.0,
            "processed": 0,
            "total": len(candidates),
            "success": 0,
            "failed": 0,
        })

        semaphore = asyncio.Semaphore(5)

        async def load_remote(fid: int):
            try:
                async with semaphore:
                    rows = await self.bili.get_folder_resource_ids(fid, storage=self.storage)
                return fid, {
                    (int(row.get("resource_id") or 0), int(row.get("resource_type") or 2))
                    for row in rows
                    if row.get("resource_id")
                }
            except Exception as exc:
                logger.warning("执行恢复时无法核验收藏夹 %s: %s", fid, exc)
                return fid, None

        remote_by_fid = dict(await asyncio.gather(*[
            load_remote(fid) for fid in sorted(remote_fids)
        ]))
        reconciled = 0
        for source, item, key, source_fid, target_fid in candidates:
            source_keys = remote_by_fid.get(source_fid)
            target_keys = remote_by_fid.get(target_fid)
            if source_keys is None or target_keys is None:
                continue
            if key in source_keys or key not in target_keys:
                continue
            resource_id, resource_type = key
            self.storage.mark_session_video_source_moved(
                sid,
                resource_id,
                source_fid,
                True,
                "",
                resource_type=resource_type,
            )
            item_complete = self.storage.are_all_session_video_sources_moved(
                sid,
                resource_id,
                resource_type=resource_type,
            )
            if version_id:
                self.storage.mark_plan_item_executed(
                    version_id,
                    resource_id,
                    item_complete,
                    resource_type=resource_type,
                )
            else:
                self.storage.mark_classification_executed(
                    sid,
                    resource_id,
                    item_complete,
                    resource_type=resource_type,
                )
            self.storage.delete_one_failed_item(
                sid,
                resource_id,
                item.get("category", ""),
                resource_type=resource_type,
            )
            reconciled += 1
        return reconciled

    async def _execute_once(self, sid: str, batch_size: int, on_progress, run_id: str) -> dict:
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
        items_by_key = {
            (int(item.get("resource_id", item.get("avid"))), int(item.get("resource_type", 2))): item
            for item in items
            if item.get("category") != "未分类"
        }
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
        existing_targets = await self._recover_saved_execution_targets(
            sid,
            version_id,
            {str(item["category"]) for item in items_by_key.values()},
        )
        reconciled = await self._reconcile_remote_execution_positions(
            sid,
            version_id,
            items_by_key,
            existing_targets,
            sources,
            on_progress=on_progress,
        )
        if reconciled:
            sources = self.storage.list_session_video_sources(sid)
        sources_by_key: dict[tuple[int, int], list[dict]] = {}
        known_source_keys: set[tuple[int, int]] = set()
        for src in sources:
            key = (src["resource_id"], src.get("resource_type", 2))
            known_source_keys.add(key)
            if not src.get("moved"):
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
            if key in known_source_keys:
                srcs = sources_by_key.get(key, [])
            else:
                srcs = [{"source_fid": s["source_fid"], "resource_type": resource_type}]
            for src in srcs:
                gkey = (cat, src["source_fid"])
                move_groups.setdefault(gkey, []).append((resource_id, src.get("resource_type", resource_type)))
        # 为每个 category 创建目标收藏夹
        cat_to_fid: dict[str, int] = {
            category: target_fid
            for category, target_fid in existing_targets.items()
            if any(category == pending_category for pending_category, _ in move_groups)
        }
        categories = sorted({cat for cat, _ in move_groups.keys()})
        total = reconciled + sum(len(res_list) for res_list in move_groups.values())
        processed = reconciled
        success = reconciled
        failed = 0
        folders_created = 0
        folders_total = len(categories)

        async def emit_execution_progress(
            phase: str,
            category: str = "",
            source_fid: int | None = None,
        ) -> None:
            progress = processed / total if total else 1.0
            await _emit_progress(on_progress, {
                "stage": "executing",
                "phase": phase,
                "progress": progress,
                "processed": processed,
                "total": total,
                "success": success,
                "failed": failed,
                "folders_created": folders_created,
                "folders_total": folders_total,
                "category": category,
                "source_fid": source_fid,
            })
            self.storage.update_execution_run(
                run_id,
                status="running",
                processed=processed,
                total=total,
                success=success,
                failed=failed,
            )

        await emit_execution_progress("creating_folders")
        folder_snapshot: list[dict] | None = None
        for cat in categories:
            saved_target = self.storage.get_execution_target(sid, version_id, cat)
            if saved_target and saved_target.get("target_fid") and saved_target.get("status") == "ready":
                cat_to_fid[cat] = int(saved_target["target_fid"])
                folders_created += 1
                await emit_execution_progress("creating_folders", category=cat)
                continue
            try:
                if saved_target and saved_target.get("status") == "ambiguous":
                    raise BibiError(
                        saved_target.get("error") or "目标收藏夹状态不明确，请人工核验后重试",
                        code="EXECUTION_TARGET_AMBIGUOUS",
                    )
                if folder_snapshot is None:
                    folder_snapshot = await self.bili.get_my_folders(storage=self.storage)
                baseline_fids = [int(folder["fid"]) for folder in folder_snapshot]
                self.storage.save_execution_target(
                    sid,
                    version_id,
                    cat,
                    0,
                    status="creating",
                    error="",
                    baseline_fids=baseline_fids,
                )
                cat_to_fid[cat] = await self.bili.create_folder(title=cat, privacy=1)
                self.storage.save_execution_target(
                    sid,
                    version_id,
                    cat,
                    cat_to_fid[cat],
                    status="ready",
                    error="",
                    baseline_fids=baseline_fids,
                )
                folder_snapshot.append({"fid": cat_to_fid[cat], "title": cat})
                folders_created += 1
                await emit_execution_progress("creating_folders", category=cat)
            except Exception as e:
                err_code, err_msg = _extract_error(e)
                self.storage.save_execution_target(
                    sid, version_id, cat, 0, status="failed", error=err_msg
                )
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
                    processed += len(res_list)
                    await emit_execution_progress(
                        "creating_folders",
                        category=cat,
                        source_fid=sf,
                    )
        # 按分组移动
        await emit_execution_progress("moving")
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
                        self.storage.mark_session_video_source_moved(sid, resource_id, sf, True, "", resource_type=resource_type)
                        item_complete = self.storage.are_all_session_video_sources_moved(
                            sid, resource_id, resource_type=resource_type
                        )
                        if version_id:
                            self.storage.mark_plan_item_executed(version_id, resource_id, item_complete, resource_type=resource_type)
                        else:
                            self.storage.mark_classification_executed(sid, resource_id, item_complete, resource_type=resource_type)
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
                processed += len(chunk)
                await emit_execution_progress(
                    "moving",
                    category=cat,
                    source_fid=sf,
                )
        stats = {"success": success, "failed": failed, "total": success + failed}
        await self.refresh_empty_source_candidates(sid)
        self.storage.update_session_status(sid, "done", stats=stats)
        await _emit_progress(on_progress, {"stage": "done", "progress": 1.0, "stats": stats})
        return stats

    async def refresh_empty_source_candidates(self, sid: str) -> None:
        self._assert_bound_session(sid)
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
        self._assert_bound_session(sid)
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
            latest = folders
            confirm_error = ""
            confirmed = False
            for delay in (0, 0.5, 1, 2):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    latest = {f["fid"]: f for f in await self.bili.get_my_folders(storage=self.storage)}
                    confirmed = True
                except Exception as exc:
                    confirm_error = str(exc)
                    continue
                if all(fid not in latest for fid in deletable):
                    break
            for fid in deletable:
                if confirmed and fid not in latest:
                    self.storage.mark_session_source_deleted(sid, fid, True, "")
                    deleted.append(fid)
                else:
                    error = delete_error or confirm_error or "B站返回成功但收藏夹仍存在"
                    self.storage.mark_session_source_deleted(sid, fid, False, error)
                    rejected.append(fid)
        return {"success": len(deleted), "failed": len(rejected), "deleted": deleted, "rejected": rejected}

    async def retry_failed(self, sid: str, batch_size: int = 50) -> dict:
        self._assert_bound_session(sid)
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
        return self.storage.list_sessions_by_status(
            ["draft", "collecting", "classifying", "failed", "pending_review", "executing"],
            account_id=self.account_id or None,
        )

    def resume_on_startup(self) -> None:
        for s in self.storage.list_sessions_by_status(
            ["collecting", "classifying", "executing"],
            account_id=self.account_id or None,
        ):
            if s["status"] == "collecting":
                self.storage.update_session_status(s["session_id"], "draft")
            elif s["status"] == "classifying":
                self.storage.mark_session_failed(
                    s["session_id"], "classifying", "PROCESS_RESTARTED", "程序重启，等待继续 AI 分类"
                )
            elif s["status"] == "executing":
                self.storage.mark_execution_runs_interrupted(s["session_id"])
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
