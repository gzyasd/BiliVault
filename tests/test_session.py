import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.session import ClassifySession
from core.bilibili_api import BilibiliClient
from core.ai_classifier import AiClassifier, VideoInfo, Classification
from core.storage import Storage
from core.errors import BiliApiError, StateError


@pytest.fixture
def deps(tmp_path):
    storage = Storage(tmp_path)
    bili = MagicMock(spec=BilibiliClient)
    bili.is_logged_in = True
    bili.mid = 12345
    bili.account_id = ""
    # 默认让 collect() 回退到 get_folder_video_pages；需要测试 resource_pages 的用例自行覆盖
    bili.get_folder_resource_pages = None
    bili.get_folder_resource_ids = AsyncMock(return_value=[])
    bili.get_my_folders = AsyncMock(return_value=[
        {"fid": 100, "title": "默认收藏夹", "media_count": 0, "cover_url": "", "fav_state": 1},
        {"fid": 200, "title": "舞蹈", "media_count": 0, "cover_url": "", "fav_state": 0},
    ])
    ai = MagicMock(spec=AiClassifier)
    return storage, bili, ai


@pytest.mark.asyncio
async def test_session_full_flow_with_progress(deps):
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "Python入门", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
                {"avid": 2, "bvid": "BV2", "title": "做菜", "intro": "", "tags": "[]",
                 "up_name": "UP2", "up_mid": 12, "cover_url": "", "tname": "生活", "fid": fid},
            ],
            "raw_count": 2, "usable_count": 2, "skipped_count": 0,
            "skipped_reasons": {}, "expected_total": 2, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[
        Classification(1, "编程", 0.9, ""),
        Classification(2, "美食", 0.85, ""),
    ])

    progress_events = []
    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    await session.run_pipeline(sid, on_progress=progress_events.append)
    s = storage.load_session(sid)
    assert s["status"] == "pending_review"
    items = storage.load_classifications(sid)
    assert len(items) == 2
    stages = [e["stage"] for e in progress_events]
    assert "collecting" in stages
    assert "classifying" in stages
    assert "pending_review" in stages


@pytest.mark.asyncio
async def test_session_supports_async_progress_callback(deps):
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "Python入门", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 1,
            "usable_count": 1,
            "skipped_count": 0,
            "skipped_reasons": {},
            "expected_total": 1,
            "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[
        Classification(1, "编程", 0.9, ""),
    ])

    progress_events = []

    async def on_progress(event):
        progress_events.append(event)

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    await session.run_pipeline(sid, on_progress=on_progress)

    stages = [e["stage"] for e in progress_events]
    assert stages[0] == "collecting"
    assert "classifying" in stages
    assert stages[-1] == "pending_review"


@pytest.mark.asyncio
async def test_session_full_mode_enriches_video_before_classification(deps):
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "Python入门", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 1, "usable_count": 1, "skipped_count": 0,
            "skipped_reasons": {}, "expected_total": 1, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    bili.get_video_info = AsyncMock(return_value={
        "intro": "这是 Python 教程简介",
        "tags": ["Python", "教程"],
    })
    captured_videos = []

    async def fake_classify(videos, batch_size=50, on_progress=None, max_categories=14):
        captured_videos.extend(videos)
        return [Classification(1, "编程", 0.9, "")]
    ai.classify = fake_classify

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="full")
    await session.run_pipeline(sid)

    bili.get_video_info.assert_awaited_once_with("BV1")
    assert captured_videos[0].tags == ["Python", "教程"]
    assert captured_videos[0].intro == "这是 Python 教程简介"


@pytest.mark.asyncio
async def test_session_resume_pending(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
    ])
    session = ClassifySession(storage, bili, ai)
    resumable = session.list_resumable()
    assert len(resumable) == 1
    assert resumable[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_create_many_persists_category_limit(deps):
    storage, bili, ai = deps
    session = ClassifySession(storage, bili, ai)

    sid = await session.create_many([100], "quick", category_limit=8)

    assert storage.load_session(sid)["category_limit"] == 8


@pytest.mark.asyncio
async def test_session_execute_records_failed_items(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "编程", "confidence": 0.8, "reason": ""},
        {"avid": 3, "category": "音乐", "confidence": 0.7, "reason": ""},
    ])
    storage.upsert_video({
        "avid": 3, "bvid": "BV3", "title": "视频C", "intro": "", "tags": "[]",
        "up_name": "UP3", "up_mid": 13, "cover_url": "", "tname": "音乐", "fid": 100,
    })
    bili.create_folder = AsyncMock(side_effect=[5001, 5002])
    bili.move_resources = AsyncMock(side_effect=[True, Exception("-509 限流")])

    session = ClassifySession(storage, bili, ai)
    stats = await session.execute(sid)
    assert stats["success"] == 2
    assert stats["failed"] == 1
    failed = storage.list_failed_items(sid)
    assert len(failed) == 1
    assert failed[0]["avid"] == 3
    assert failed[0]["title"] == "视频C"
    assert failed[0]["target_fid"] == 5002
    assert "-509" in failed[0]["error_message"]
    assert storage.load_session(sid)["status"] == "done"


@pytest.mark.asyncio
async def test_session_execute_records_create_folder_failure(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "编程", "confidence": 0.8, "reason": ""},
    ])
    storage.upsert_video({
        "avid": 1, "bvid": "BV1", "title": "视频A", "intro": "", "tags": "[]",
        "up_name": "UP1", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": 100,
    })
    storage.upsert_video({
        "avid": 2, "bvid": "BV2", "title": "视频B", "intro": "", "tags": "[]",
        "up_name": "UP2", "up_mid": 12, "cover_url": "", "tname": "科技", "fid": 100,
    })
    bili.create_folder = AsyncMock(side_effect=BiliApiError(-403, "风控"))
    bili.move_resources = AsyncMock()

    session = ClassifySession(storage, bili, ai)
    stats = await session.execute(sid)

    assert stats == {"success": 0, "failed": 2, "total": 2}
    assert storage.load_session(sid)["status"] == "done"
    failed = storage.list_failed_items(sid)
    assert [it["avid"] for it in failed] == [1, 2]
    assert all(it["target_fid"] == 0 for it in failed)
    bili.move_resources.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_emits_progress_after_each_move_batch(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "Programming", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "Programming", "confidence": 0.8, "reason": ""},
    ])
    bili.create_folder = AsyncMock(return_value=5001)
    bili.move_resources = AsyncMock(return_value=True)
    events = []

    session = ClassifySession(storage, bili, ai)
    stats = await session.execute(sid, batch_size=1, on_progress=events.append)

    moving = [
        event for event in events
        if event.get("stage") == "executing"
        and event.get("phase") == "moving"
        and event.get("processed", 0) > 0
    ]
    assert [event["processed"] for event in moving] == [1, 2]
    assert moving[-1] == {
        "stage": "executing",
        "phase": "moving",
        "progress": 1.0,
        "processed": 2,
        "total": 2,
        "success": 2,
        "failed": 0,
        "folders_created": 1,
        "folders_total": 1,
        "category": "Programming",
        "source_fid": 100,
    }
    assert stats == {"success": 2, "failed": 0, "total": 2}


@pytest.mark.asyncio
async def test_execute_progress_counts_folder_creation_failures(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "Programming", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "Programming", "confidence": 0.8, "reason": ""},
    ])
    bili.create_folder = AsyncMock(side_effect=BiliApiError(-403, "blocked"))
    bili.move_resources = AsyncMock()
    events = []

    session = ClassifySession(storage, bili, ai)
    stats = await session.execute(sid, on_progress=events.append)

    executing = [event for event in events if event.get("stage") == "executing"]
    assert executing[-1]["processed"] == 2
    assert executing[-1]["total"] == 2
    assert executing[-1]["success"] == 0
    assert executing[-1]["failed"] == 2
    assert executing[-1]["progress"] == 1.0
    assert stats == {"success": 0, "failed": 2, "total": 2}


@pytest.mark.asyncio
async def test_session_retry_failed(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "音乐", "confidence": 0.7, "reason": ""},
    ])
    storage.add_failed_item(sid, {
        "avid": 2, "title": "视频B", "category": "音乐", "target_fid": 5002,
        "error_code": "-509", "error_message": "限流",
    })
    bili.move_resources = AsyncMock(return_value=True)

    session = ClassifySession(storage, bili, ai)
    stats = await session.retry_failed(sid)
    assert stats["success"] == 1
    assert stats["failed"] == 0
    assert storage.list_failed_items(sid) == []


@pytest.mark.asyncio
async def test_session_retry_failed_preserves_still_failed_items(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_classifications(sid, [
        {"avid": 2, "category": "音乐", "confidence": 0.7, "reason": ""},
    ])
    storage.add_failed_item(sid, {
        "avid": 2, "title": "视频B", "category": "音乐", "target_fid": 5002,
        "error_code": "-509", "error_message": "限流",
    })
    bili.move_resources = AsyncMock(side_effect=Exception("仍然失败"))

    session = ClassifySession(storage, bili, ai)
    stats = await session.retry_failed(sid)

    assert stats["success"] == 0
    assert stats["failed"] == 1
    failed = storage.list_failed_items(sid)
    assert len(failed) == 1
    assert failed[0]["avid"] == 2


@pytest.mark.asyncio
async def test_session_retry_failed_recreates_missing_target_folder(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_classifications(sid, [
        {"avid": 2, "category": "编程", "confidence": 0.7, "reason": ""},
    ])
    storage.add_failed_item(sid, {
        "avid": 2, "title": "视频B", "category": "编程", "target_fid": 0,
        "error_code": "-403", "error_message": "创建收藏夹失败",
    })
    bili.create_folder = AsyncMock(return_value=5002)
    bili.move_resources = AsyncMock(return_value=True)

    session = ClassifySession(storage, bili, ai)
    stats = await session.retry_failed(sid)

    assert stats["success"] == 1
    assert stats["failed"] == 0
    bili.create_folder.assert_awaited_once_with(title="编程", privacy=1)
    bili.move_resources.assert_awaited_once_with(src_media_id=100, tar_media_id=5002, resources="2:2")
    assert storage.list_failed_items(sid) == []


@pytest.mark.asyncio
async def test_session_collect_records_skip_stats(deps):
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 3, "usable_count": 1, "skipped_count": 2,
            "skipped_reasons": {"attr_invalid": 2}, "expected_total": 405, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    await session.run_pipeline(sid)

    s = storage.load_session(sid)
    import json as _json
    st = _json.loads(s["stats"])
    assert st["source_total"] == 405
    assert st["scanned_total"] == 3
    assert st["collected_total"] == 1
    assert st["skipped_total"] == 2
    assert st["skipped_by_reason"]["attr_invalid"] == 2


@pytest.mark.asyncio
async def test_session_collect_persists_skipped_items(deps):
    """collect 应将分页返回的 skipped_items 明细持久化到 skipped_items 表。"""
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "有效", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 3, "usable_count": 1, "skipped_count": 2,
            "skipped_reasons": {"attr_invalid": 1, "non_video_type": 1},
            "skipped_items": [
                {"source_fid": fid, "avid": 2, "bvid": "", "title": "已失效",
                 "resource_type": 2, "raw_attr": 1, "reason_code": "attr_invalid",
                 "reason_label": "失效视频", "detail": "", "removable": True},
                {"source_fid": fid, "avid": 3, "bvid": "BV3", "title": "合集",
                 "resource_type": 11, "raw_attr": 0, "reason_code": "non_video_type",
                 "reason_label": "非视频类型", "detail": "", "removable": False},
            ],
            "expected_total": 3, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    await session.run_pipeline(sid)

    items = storage.list_skipped_items(sid)
    assert len(items) == 2
    by_avid = {it["avid"]: it for it in items}
    assert by_avid[2]["reason_code"] == "attr_invalid"
    assert by_avid[2]["removable"] == 1
    assert by_avid[3]["reason_code"] == "non_video_type"
    assert by_avid[3]["removable"] == 0
    assert by_avid[2]["source_fid"] == 100


@pytest.mark.asyncio
async def test_session_cancel_from_pending_review(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_classifications(sid, [
        {"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""},
    ])
    session = ClassifySession(storage, bili, ai)
    session.cancel(sid)
    assert storage.load_session(sid)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_session_cancel_rejected_for_executing(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "executing")
    session = ClassifySession(storage, bili, ai)
    with pytest.raises(StateError):
        session.cancel(sid)


@pytest.mark.asyncio
async def test_session_cancelled_not_in_resumable(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "cancelled")
    session = ClassifySession(storage, bili, ai)
    resumable = session.list_resumable()
    assert all(r["session_id"] != sid for r in resumable)


@pytest.mark.asyncio
async def test_run_pipeline_does_not_continue_after_collect_cancel(deps):
    storage, bili, ai = deps
    page_yielded = asyncio.Event()
    release_collect = asyncio.Event()

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 1,
            "usable_count": 1,
            "skipped_count": 0,
            "skipped_reasons": {},
            "expected_total": 1,
            "has_more": True,
        }
        page_yielded.set()
        await release_collect.wait()

    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    task = asyncio.create_task(session.run_pipeline(sid))
    await page_yielded.wait()

    session.cancel(sid)
    release_collect.set()
    await task

    assert storage.load_session(sid)["status"] == "cancelled"
    ai.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_from_multiple_sources_dedupes_for_ai(deps):
    """多源采集：同一 avid 来自多个源都记录到 session_video_sources；AI 分类按 avid 去重。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 2, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 2, "selected_order": 1},
    ])

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        if fid == 100:
            yield {
                "page": 1,
                "videos": [
                    {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                    {"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                ],
                "raw_count": 2,
                "usable_count": 2,
                "skipped_count": 0,
                "skipped_reasons": {},
                "skipped_items": [],
                "expected_total": 2,
                "has_more": False,
            }
        if fid == 200:
            yield {
                "page": 1,
                "videos": [
                    {"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": fid},
                    {"avid": 3, "bvid": "BV3", "title": "C", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "舞蹈", "fid": fid},
                ],
                "raw_count": 2,
                "usable_count": 2,
                "skipped_count": 0,
                "skipped_reasons": {},
                "skipped_items": [],
                "expected_total": 2,
                "has_more": False,
            }

    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[
        Classification(1, "动漫", 0.9, ""),
        Classification(2, "动漫", 0.9, ""),
        Classification(3, "舞蹈", 0.9, ""),
    ])

    await ClassifySession(storage, bili, ai).run_pipeline(sid)

    ai.classify.assert_awaited_once()
    classified_videos = ai.classify.await_args.args[0]
    assert [v.avid for v in classified_videos] == [1, 2, 3]
    sources = storage.list_session_video_sources(sid)
    assert {(s["resource_id"], s["source_fid"]) for s in sources} == {(1, 100), (2, 100), (2, 200), (3, 200)}


@pytest.mark.asyncio
async def test_get_plan_returns_sources_and_video_sources(deps):
    """get_plan 应返回 sources 和 video_sources，供前端展示方案来源。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 1, "selected_order": 0},
    ])

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 1, "usable_count": 1, "skipped_count": 0,
            "skipped_reasons": {}, "skipped_items": [], "expected_total": 1, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    await session.run_pipeline(sid)

    plan = session.get_plan(sid)
    assert "sources" in plan
    assert "video_sources" in plan
    assert plan["sources"][0]["source_fid"] == 100
    assert plan["video_sources"][0]["resource_id"] == 1


@pytest.mark.asyncio
async def test_remove_skipped_items_groups_by_source_and_marks_removed(deps):
    """remove_skipped_items 应按 source_fid 分组调用 batch_delete，并标记 removed 状态。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.add_skipped_items(sid, "", [
        {"source_fid": 100, "avid": 2, "bvid": "", "title": "失效1",
         "resource_type": 2, "raw_attr": 1, "reason_code": "attr_invalid",
         "reason_label": "失效视频", "detail": "", "removable": True},
        {"source_fid": 100, "avid": 3, "bvid": "", "title": "失效2",
         "resource_type": 2, "raw_attr": 1, "reason_code": "attr_invalid",
         "reason_label": "失效视频", "detail": "", "removable": True},
        {"source_fid": 200, "avid": 4, "bvid": "", "title": "失效3",
         "resource_type": 2, "raw_attr": 1, "reason_code": "attr_invalid",
         "reason_label": "失效视频", "detail": "", "removable": True},
        {"source_fid": 100, "avid": 5, "bvid": "BV5", "title": "合集",
         "resource_type": 11, "raw_attr": 0, "reason_code": "non_video_type",
         "reason_label": "非视频类型", "detail": "", "removable": False},
    ])
    items = storage.list_skipped_items(sid)
    item_ids = [it["id"] for it in items]

    bili.batch_delete_resources = AsyncMock(return_value=True)

    session = ClassifySession(storage, bili, ai)
    stats = await session.remove_skipped_items(sid, item_ids)

    # 3 个 removable=True 被处理，1 个 removable=False 被过滤
    assert stats["total"] == 3
    assert stats["success"] == 3
    assert stats["failed"] == 0
    # 按 source 分组：source_fid=100 有 2 个，source_fid=200 有 1 个
    calls = bili.batch_delete_resources.await_args_list
    media_ids = sorted([c.kwargs["media_id"] for c in calls])
    assert media_ids == [100, 200]
    # removable=False 的条目 removed 仍为 0
    non_video = [it for it in storage.list_skipped_items(sid) if it["reason_code"] == "non_video_type"][0]
    assert non_video["removed"] == 0
    # removable=True 的条目 removed=1
    removed_items = [it for it in storage.list_skipped_items(sid) if it["removable"] == 1]
    assert all(it["removed"] == 1 for it in removed_items)


@pytest.mark.asyncio
async def test_remove_skipped_items_records_failure(deps):
    """删除失败时应标记 removed=0 并记录错误信息。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.add_skipped_items(sid, "", [
        {"source_fid": 100, "avid": 2, "bvid": "", "title": "失效",
         "resource_type": 2, "raw_attr": 1, "reason_code": "attr_invalid",
         "reason_label": "失效视频", "detail": "", "removable": True},
    ])
    item_id = storage.list_skipped_items(sid)[0]["id"]

    bili.batch_delete_resources = AsyncMock(side_effect=Exception("-509 限流"))

    session = ClassifySession(storage, bili, ai)
    stats = await session.remove_skipped_items(sid, [item_id])

    assert stats["success"] == 0
    assert stats["failed"] == 1
    assert stats["total"] == 1
    it = storage.list_skipped_items(sid)[0]
    assert it["removed"] == 0
    assert "-509" in it["remove_error"]


@pytest.mark.asyncio
async def test_execute_uses_active_plan_version(deps):
    """execute 应使用激活版本 items，而非旧 classifications 表。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100})
    v1 = storage.create_plan_version(sid, None, "初始", [{"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""}], activate=False)
    storage.create_plan_version(sid, v1, "官方单独放", [{"avid": 1, "category": "官方作品", "confidence": 0.95, "reason": ""}], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_videos = AsyncMock(return_value=True)

    stats = await ClassifySession(storage, bili, ai).execute(sid)

    assert stats["success"] == 1
    bili.create_folder.assert_awaited_once_with(title="官方作品", privacy=1)


@pytest.mark.asyncio
async def test_execute_groups_moves_by_source_folder(deps):
    """多源时 execute 应按 (category, source_fid) 分组移动，统计来源实例数。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 2, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 1, "selected_order": 1},
    ])
    storage.upsert_video({"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100})
    storage.upsert_video({"avid": 2, "bvid": "BV2", "title": "B", "intro": "", "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 200})
    storage.add_session_video_source(sid, resource_id=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=2, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=2, source_fid=200, resource_type=2)
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_resources = AsyncMock(return_value=True)
    bili.get_my_folders = AsyncMock(return_value=[])

    stats = await ClassifySession(storage, bili, ai).execute(sid)

    # avid=1 来自源100（1实例），avid=2 来自源100+200（2实例），共3个来源实例
    assert stats["success"] == 3
    bili.move_resources.assert_any_await(src_media_id=100, tar_media_id=9001, resources="1:2,2:2")
    bili.move_resources.assert_any_await(src_media_id=200, tar_media_id=9001, resources="2:2")


@pytest.mark.asyncio
async def test_retry_failed_retries_by_original_source_folder(deps):
    """多源失败重试应按原始来源收藏夹分组重试，统计来源实例数。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 1, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 1, "selected_order": 1},
    ])
    storage.add_session_video_source(sid, resource_id=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=1, source_fid=200, resource_type=2)
    storage.mark_session_video_source_moved(sid, 1, 100, False, "timeout", resource_type=2)
    storage.mark_session_video_source_moved(sid, 1, 200, False, "timeout", resource_type=2)
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_resources = AsyncMock(return_value=True)

    stats = await ClassifySession(storage, bili, ai).retry_failed(sid)

    assert stats["success"] == 2
    bili.move_resources.assert_any_await(src_media_id=100, tar_media_id=9001, resources="1:2")
    bili.move_resources.assert_any_await(src_media_id=200, tar_media_id=9001, resources="1:2")


@pytest.mark.asyncio
async def test_adjust_item_writes_active_plan_version(deps):
    """有激活方案版本时，adjust_item 必须写入 plan_items，get_plan 与 execute 都使用新分类。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.upsert_video({
        "avid": 1, "bvid": "BV1", "title": "视频A", "intro": "", "tags": "[]",
        "up_name": "UP1", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": 100,
    })
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)

    ClassifySession(storage, bili, ai).adjust_item(sid, 1, "编程")

    plan = ClassifySession(storage, bili, ai).get_plan(sid)
    assert plan["items"][0]["category"] == "编程"
    # 旧 classifications 表不应被作为 execute 数据源
    legacy = storage.load_classifications(sid)
    assert all(it["category"] != "编程" for it in legacy)


@pytest.mark.asyncio
async def test_collect_source_total_not_doubled_when_media_count_present(deps):
    """session_sources.media_count 已有值时，不应再累加第一页 expected_total。"""
    storage, bili, ai = deps

    async def fake_pages(fid, storage=None, page_size=20, sleep_seconds=0):
        yield {
            "page": 1,
            "videos": [
                {"avid": 1, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 11, "cover_url": "", "tname": "科技", "fid": fid},
            ],
            "raw_count": 1, "usable_count": 1, "skipped_count": 0,
            "skipped_reasons": {}, "expected_total": 405, "has_more": False,
        }
    bili.get_folder_video_pages = fake_pages
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(source_fid=100, mode="quick")
    # 手动设置 session_sources.media_count=405
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 405, "selected_order": 0},
    ])
    await session.run_pipeline(sid)

    s = storage.load_session(sid)
    import json as _json
    st = _json.loads(s["stats"])
    assert st["source_total"] == 405


@pytest.mark.asyncio
async def test_delete_empty_source_folders_rechecks_after_exception(deps):
    """delete_folders 抛异常后仍应重新拉取收藏夹核验，已删除的标记为已删除。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_session_sources(sid, [
        {"source_fid": 200, "title": "空夹", "media_count": 0, "selected_order": 0, "delete_protected": 0},
    ])
    call_count = {"n": 0}

    async def fake_get_my_folders(storage=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [{"fid": 200, "title": "空夹", "media_count": 0, "cover_url": "", "fav_state": 0}]
        return []

    bili.get_my_folders = fake_get_my_folders
    bili.delete_folders = AsyncMock(side_effect=Exception("-403 风控"))

    stats = await ClassifySession(storage, bili, ai).delete_empty_source_folders(sid, [200])

    assert 200 in stats["deleted"]
    assert stats["success"] == 1
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_retry_failed_sources_clears_failed_items_per_source(deps):
    """同一 avid 来自多个源且都失败时，重试成功后 failed_items 不应残留。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "done")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 1, "selected_order": 0},
        {"source_fid": 200, "title": "舞蹈", "media_count": 1, "selected_order": 1},
    ])
    storage.add_session_video_source(sid, resource_id=1, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=1, source_fid=200, resource_type=2)
    storage.mark_session_video_source_moved(sid, 1, 100, False, "timeout", resource_type=2)
    storage.mark_session_video_source_moved(sid, 1, 200, False, "timeout", resource_type=2)
    storage.create_plan_version(sid, None, "初始", [
        {"avid": 1, "category": "动漫", "confidence": 0.9, "reason": ""},
    ], activate=True)
    # execute 时会为每个失败来源创建 failed_item，这里模拟两条
    storage.add_failed_item(sid, {
        "avid": 1, "title": "视频A", "category": "动漫", "target_fid": 9001,
        "error_code": "", "error_message": "timeout",
    })
    storage.add_failed_item(sid, {
        "avid": 1, "title": "视频A", "category": "动漫", "target_fid": 9001,
        "error_code": "", "error_message": "timeout",
    })
    bili.create_folder = AsyncMock(return_value=9001)
    bili.move_videos = AsyncMock(return_value=True)

    stats = await ClassifySession(storage, bili, ai).retry_failed(sid)

    assert stats["success"] == 2
    # failed_items 应全部被清理
    assert storage.list_failed_items(sid) == []


@pytest.mark.asyncio
async def test_create_many_delete_protected_uses_fav_state_not_title(tmp_path):
    """默认收藏夹保护应依赖 fav_state 字段，不靠标题推断。"""
    storage = Storage(tmp_path)
    bili = MagicMock(spec=BilibiliClient)
    bili.is_logged_in = True
    bili.mid = 12345
    bili.account_id = ""
    # fid=300 标题是"默认收藏夹"但 fav_state=0 → 不应保护
    # fid=400 标题是"我的收藏"但 fav_state=1 → 应保护
    bili.get_my_folders = AsyncMock(return_value=[
        {"fid": 300, "title": "默认收藏夹", "media_count": 0, "cover_url": "", "fav_state": 0},
        {"fid": 400, "title": "我的收藏", "media_count": 0, "cover_url": "", "fav_state": 1},
    ])
    ai = MagicMock(spec=AiClassifier)
    mgr = ClassifySession(storage, bili, ai)
    sid = await mgr.create_many([300, 400], "quick")
    sources = storage.list_session_sources(sid)
    by_fid = {s["source_fid"]: s for s in sources}
    assert by_fid[300]["delete_protected"] == 0  # fav_state=0 不保护，即使标题是"默认收藏夹"
    assert by_fid[400]["delete_protected"] == 1  # fav_state=1 保护，即使标题不是"默认收藏夹"


@pytest.mark.asyncio
async def test_collect_emits_numeric_progress_from_scanned_total(deps):
    """采集阶段应根据 scanned/source_total 发送可计算进度，而不是一直 None。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 266, "selected_order": 0},
    ])

    async def pages(fid, storage=None):
        yield {
            "page": 1,
            "videos": [
                {"avid": i, "bvid": f"BV{i}", "title": f"条目{i}", "intro": "", "tags": "[]",
                 "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100}
                for i in range(1, 34)
            ],
            "raw_count": 39,
            "usable_count": 33,
            "skipped_count": 6,
            "skipped_reasons": {"attr_invalid": 6},
            "skipped_items": [],
            "expected_total": 266,
            "has_more": False,
        }

    bili.get_folder_video_pages = pages
    events = []

    await ClassifySession(storage, bili, ai).collect(sid, on_progress=events.append)

    numeric = [e for e in events if e["stage"] == "collecting" and isinstance(e.get("progress"), float)]
    assert any(0.14 <= e["progress"] <= 0.15 for e in numeric)


@pytest.mark.asyncio
async def test_classify_emits_batch_progress(deps):
    """AI 分类阶段应按批次发送进度，而不是只在 0% 和 100% 跳变。"""
    storage, bili, ai = deps
    storage.save_config({
        "ai_base_url": "http://x", "ai_api_key": "k", "ai_model": "m",
        "default_privacy": 1, "ai_batch_size": 50,
    })
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    for avid in range(1, 121):
        storage.upsert_video({
            "avid": avid, "bvid": f"BV{avid}", "title": f"条目{avid}", "intro": "",
            "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
        })
        storage.add_session_video_source(sid, resource_id=avid, source_fid=100)

    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def classify_batch(videos, max_categories=14):
        return [Classification(v.avid, "动画", 0.9, "测试") for v in videos]

    classifier.classify_batch = classify_batch
    classifier.merge_categories = AsyncMock(return_value={"动画": "动画"})
    events = []

    await ClassifySession(storage, bili, classifier).classify(sid, on_progress=events.append)

    progresses = [e["progress"] for e in events if e["stage"] == "classifying" and isinstance(e.get("progress"), float)]
    assert progresses[0] == 0.0
    assert any(0.40 <= p <= 0.43 for p in progresses)
    assert any(0.82 <= p <= 0.84 for p in progresses)
    assert progresses[-1] == 1.0


@pytest.mark.asyncio
async def test_classify_uses_configured_ai_batch_size(deps):
    """会话应读取配置中的 ai_batch_size 并传给 AiClassifier.classify()。"""
    storage, bili, ai = deps
    storage.save_config({
        "ai_base_url": "http://x", "ai_api_key": "k", "ai_model": "m",
        "default_privacy": 1, "ai_batch_size": 120,
    })
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    for avid in range(1, 4):
        storage.upsert_video({
            "avid": avid, "bvid": f"BV{avid}", "title": f"条目{avid}", "intro": "",
            "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
        })
        storage.add_session_video_source(sid, resource_id=avid, source_fid=100)

    seen = {}

    async def classify(videos, batch_size=50, on_progress=None, max_categories=14):
        seen["batch_size"] = batch_size
        seen["max_categories"] = max_categories
        if on_progress:
            await on_progress({"stage": "classifying", "progress": 1.0, "classified": len(videos), "total": len(videos)})
        return [Classification(v.avid, "动画", 0.9, "测试") for v in videos]

    ai.classify = classify

    await ClassifySession(storage, bili, ai).classify(sid, on_progress=lambda e: None)

    assert seen["batch_size"] == 120


@pytest.mark.asyncio
async def test_classify_uses_session_category_limit(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick", category_limit=8)
    storage.update_session_status(sid, "classifying")
    storage.upsert_video({
        "avid": 1, "bvid": "BV1", "title": "条目1", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
    })
    storage.add_session_video_source(sid, resource_id=1, source_fid=100)
    seen = {}

    async def classify(videos, batch_size=50, on_progress=None, max_categories=14):
        seen["max_categories"] = max_categories
        return [Classification(1, "动画", 0.9, "")]

    ai.classify = classify
    await ClassifySession(storage, bili, ai).classify(sid)

    assert seen["max_categories"] == 8


@pytest.mark.asyncio
async def test_refine_plan_uses_session_category_limit(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick", category_limit=8)
    storage.update_session_status(sid, "pending_review")
    storage.upsert_video({
        "avid": 1, "bvid": "BV1", "title": "条目1", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
    })
    storage.create_plan_version(sid, None, "初始分类", [{
        "avid": 1, "resource_id": 1, "resource_type": 2,
        "category": "动画", "confidence": 0.9, "reason": "",
    }], activate=True)
    seen = {}

    async def refine_plan(videos, current, instruction, max_categories=14, batch_size=100, on_progress=None):
        seen["max_categories"] = max_categories
        seen["batch_size"] = batch_size
        if on_progress:
            await on_progress({"stage": "refining", "processed": 1, "total": 1, "progress": 1.0, "retry_count": 0})
        return current

    ai.refine_plan = refine_plan
    await ClassifySession(storage, bili, ai).refine_plan(sid, "保持不变")

    assert seen["max_categories"] == 8
    assert seen["batch_size"] == 100


@pytest.mark.asyncio
async def test_refine_plan_forwards_progress_and_emits_saving(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.upsert_video({
        "avid": 1, "bvid": "BV1", "title": "条目1", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
    })
    storage.create_plan_version(sid, None, "初始分类", [{
        "avid": 1, "resource_id": 1, "resource_type": 2,
        "category": "动画", "confidence": 0.9, "reason": "",
    }], activate=True)

    async def refine(videos, current, instruction, max_categories=14, batch_size=100, on_progress=None):
        await on_progress({"stage": "refining", "processed": 1, "total": 1, "progress": 1.0, "retry_count": 2})
        return [Classification(1, "官方作品", 0.95, "官方", resource_type=2)]

    ai.refine_plan = refine
    events = []
    result = await ClassifySession(storage, bili, ai).refine_plan(sid, "官方单独分类", on_progress=events.append)

    assert [event["stage"] for event in events] == ["refining", "saving"]
    assert events[-1]["retry_count"] == 2
    assert result["items"][0]["category"] == "官方作品"


@pytest.mark.asyncio
async def test_retry_unclassified_only_updates_unclassified_items(deps):
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    for avid in (1, 2):
        storage.upsert_video({
            "avid": avid, "bvid": f"BV{avid}", "title": f"条目{avid}", "intro": "", "tags": "[]",
            "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
        })
    storage.create_plan_version(sid, None, "初始分类", [
        {"avid": 1, "resource_id": 1, "resource_type": 2, "category": "未分类", "confidence": 0, "reason": "AI解析失败"},
        {"avid": 2, "resource_id": 2, "resource_type": 2, "category": "动画", "confidence": 0.9, "reason": "原分类"},
    ], activate=True)
    ai.classify = AsyncMock(return_value=[Classification(1, "编程", 0.9, "重试成功", resource_type=2)])

    result = await ClassifySession(storage, bili, ai).retry_unclassified(sid)

    assert result["recovered"] == 1
    assert result["remaining"] == 0
    categories = {item["resource_id"]: item["category"] for item in result["plan"]["items"]}
    assert categories == {1: "编程", 2: "动画"}
    assert len(storage.list_plan_versions(sid)) == 2
    classified_videos = ai.classify.await_args.args[0]
    assert [video.avid for video in classified_videos] == [1]


@pytest.mark.asyncio
async def test_collect_includes_non_video_resources(deps):
    """采集阶段应纳入非视频资源（合集/音频等），不再跳过 non_video_type。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.save_session_sources(sid, [
        {"source_fid": 100, "title": "默认收藏夹", "media_count": 3, "selected_order": 0},
    ])

    async def pages(fid, storage=None):
        yield {
            "page": 1,
            "videos": [],
            "resources": [
                {"resource_id": 101, "resource_type": 2, "bvid": "BV1", "title": "视频A",
                 "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100},
                {"resource_id": 201, "resource_type": 11, "bvid": "", "title": "合集B",
                 "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 100},
            ],
            "raw_count": 2,
            "usable_count": 2,
            "skipped_count": 0,
            "skipped_reasons": {},
            "skipped_items": [],
            "expected_total": 3,
            "has_more": False,
        }

    bili.get_folder_resource_pages = pages
    bili.get_folder_video_pages = pages  # 兼容回退

    await ClassifySession(storage, bili, ai).collect(sid, on_progress=lambda e: None)

    # 非视频资源 201 应该被入库
    videos = storage.list_videos_by_avids([101, 201])
    resource_ids = [v["resource_id"] for v in videos]
    assert 101 in resource_ids
    assert 201 in resource_ids
    # session_video_sources 应记录两条
    sources = storage.list_session_video_sources(sid)
    by_rid = {s["resource_id"]: s for s in sources}
    assert by_rid[101]["resource_type"] == 2
    assert by_rid[201]["resource_type"] == 11


@pytest.mark.asyncio
async def test_execute_moves_resources_with_correct_type(deps):
    """执行阶段应按资源类型构造 resources 字符串，视频为 avid:2，合集中为 id:11。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    # 两条 plan items：一条视频，一条合集
    v_id = storage.create_plan_version(sid, None, "初始", [
        {"resource_id": 101, "resource_type": 2, "avid": 101, "category": "动画", "confidence": 0.9, "reason": ""},
        {"resource_id": 201, "resource_type": 11, "avid": 201, "category": "动画", "confidence": 0.9, "reason": ""},
    ], activate=True)
    storage.upsert_video({"avid": 101, "bvid": "BV1", "title": "A", "intro": "", "tags": "[]",
                          "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100})
    storage.upsert_video({"avid": 201, "bvid": "", "title": "B", "intro": "", "tags": "[]",
                          "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 100})
    storage.add_session_video_source(sid, 101, 100, 2)
    storage.add_session_video_source(sid, 201, 100, 11)

    moved_calls = []

    async def move_resources(src_media_id, tar_media_id, resources):
        moved_calls.append({"src": src_media_id, "tar": tar_media_id, "resources": resources})
        return True

    bili.move_resources = move_resources
    bili.create_folder = AsyncMock(return_value=999)

    await ClassifySession(storage, bili, ai).execute(sid)

    # 应该有一次移动调用，resources 包含 "101:2,201:11"
    assert len(moved_calls) == 1
    assert "101:2" in moved_calls[0]["resources"]
    assert "201:11" in moved_calls[0]["resources"]


@pytest.mark.asyncio
async def test_adjust_item_uses_resource_id_and_type(deps):
    """adjust_item 应支持按 (resource_id, resource_type) 调整分类。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "pending_review")
    storage.create_plan_version(sid, None, "初始", [
        {"resource_id": 201, "resource_type": 11, "category": "动画", "confidence": 0.9, "reason": ""},
    ], activate=True)

    mgr = ClassifySession(storage, bili, ai)
    mgr.adjust_item(sid, resource_id=201, resource_type=11, new_category="合集")

    active = storage.get_active_plan_version(sid)
    items = storage.load_plan_items(active["version_id"])
    by_key = {(it["resource_id"], it["resource_type"]): it for it in items}
    assert by_key[(201, 11)]["category"] == "合集"
    assert by_key[(201, 11)]["adjusted"] == 1


@pytest.mark.asyncio
async def test_classify_distinguishes_same_resource_id_different_type(deps):
    """同 ID 不同类型的资源应分别分类，不因单 ID 映射互相覆盖。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    # 同 resource_id=123，但 resource_type 不同（视频 2 / 合集 11）
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 2, "avid": 123,
        "bvid": "BV123", "title": "视频123", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "视频", "fid": 100,
    })
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 11, "avid": 0,
        "bvid": "", "title": "合集123", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 100,
    })
    storage.add_session_video_source(sid, resource_id=123, source_fid=100, resource_type=2)
    storage.add_session_video_source(sid, resource_id=123, source_fid=100, resource_type=11)

    async def classify(videos, batch_size=50, on_progress=None, max_categories=14):
        return [
            Classification(v.avid, f"类型{v.resource_type}", 0.9, "", resource_type=v.resource_type)
            for v in videos
        ]

    ai.classify = classify
    await ClassifySession(storage, bili, ai).classify(sid)

    active = storage.get_active_plan_version(sid)
    items = storage.load_plan_items(active["version_id"])
    by_key = {(it["resource_id"], it["resource_type"]): it for it in items}
    assert by_key[(123, 2)]["category"] == "类型2"
    assert by_key[(123, 11)]["category"] == "类型11"
    assert len(items) == 2


@pytest.mark.asyncio
async def test_classify_does_not_include_cached_same_id_other_type(deps):
    """缓存中存在同 ID 其他类型，但当前会话只包含其中一个类型时，不应误带入。"""
    storage, bili, ai = deps
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 2, "avid": 123,
        "bvid": "BV123", "title": "视频123", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "视频", "fid": 100,
    })
    storage.upsert_video({
        "account_id": "", "resource_id": 123, "resource_type": 11, "avid": 0,
        "bvid": "", "title": "缓存中的其他合集", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "fid": 999,
    })
    storage.add_session_video_source(sid, resource_id=123, source_fid=100, resource_type=2)

    seen = []

    async def classify(videos, batch_size=50, on_progress=None, max_categories=14):
        seen.extend((v.avid, v.resource_type) for v in videos)
        return [Classification(v.avid, "视频类", 0.9, "", resource_type=v.resource_type) for v in videos]

    ai.classify = classify
    await ClassifySession(storage, bili, ai).classify(sid)

    assert seen == [(123, 2)]
    active = storage.get_active_plan_version(sid)
    items = storage.load_plan_items(active["version_id"])
    assert [(it["resource_id"], it["resource_type"]) for it in items] == [(123, 2)]


@pytest.mark.asyncio
async def test_collect_records_detail_hidden_resources_and_skips_ai(deps):
    storage, bili, ai = deps

    async def empty_pages(fid, storage=None):
        yield {
            "page": 1, "videos": [], "resources": [], "raw_count": 0,
            "usable_count": 0, "skipped_count": 0, "skipped_reasons": {},
            "skipped_items": [], "expected_total": 2, "has_more": False,
        }

    bili.get_folder_resource_pages = empty_pages
    bili.get_folder_resource_ids = AsyncMock(return_value=[
        {"resource_id": 11, "resource_type": 2, "bvid": "BV1hiddenA"},
        {"resource_id": 12, "resource_type": 2, "bvid": "BV1hiddenB"},
    ])
    ai.classify = AsyncMock()

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(100, "quick")
    await session.run_pipeline(sid)

    skipped = storage.list_skipped_items(sid)
    assert [(item["avid"], item["bvid"], item["reason_code"], item["removable"]) for item in skipped] == [
        (11, "BV1hiddenA", "inaccessible", 1),
        (12, "BV1hiddenB", "inaccessible", 1),
    ]
    assert storage.load_session(sid)["status"] == "pending_review"
    ai.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_classifies_visible_resource_and_skips_only_missing_id(deps):
    storage, bili, ai = deps

    async def partial_pages(fid, storage=None):
        yield {
            "page": 1, "videos": [],
            "resources": [{
                "resource_id": 11, "resource_type": 2, "avid": 11,
                "bvid": "BV1visible", "title": "可见", "intro": "", "tags": "[]",
                "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "", "fid": fid,
            }],
            "raw_count": 1, "usable_count": 1, "skipped_count": 0,
            "skipped_reasons": {}, "skipped_items": [], "expected_total": 2, "has_more": False,
        }

    bili.get_folder_resource_pages = partial_pages
    bili.get_folder_resource_ids = AsyncMock(return_value=[
        {"resource_id": 11, "resource_type": 2, "bvid": "BV1visible"},
        {"resource_id": 12, "resource_type": 2, "bvid": "BV1hidden"},
    ])
    ai.classify = AsyncMock(return_value=[Classification(11, "分类", 0.9, "")])

    session = ClassifySession(storage, bili, ai)
    sid = await session.create(100, "quick")
    await session.run_pipeline(sid)

    assert [item["resource_id"] for item in storage.load_classifications(sid)] == [11]
    assert [item["avid"] for item in storage.list_skipped_items(sid)] == [12]
    ai.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_empty_session_never_uses_fid_cache_or_calls_ai(deps):
    storage, bili, ai = deps
    storage.upsert_video({
        "avid": 999, "bvid": "BV1cached", "title": "历史缓存", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "", "fid": 100,
    })
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    ai.classify = AsyncMock()

    await ClassifySession(storage, bili, ai).classify(sid)

    assert storage.load_classifications(sid) == []
    assert storage.load_session(sid)["status"] == "pending_review"
    ai.classify.assert_not_awaited()
