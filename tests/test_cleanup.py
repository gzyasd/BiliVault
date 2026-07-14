import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cleanup import CleanupManager
from core.storage import Storage


@pytest.mark.asyncio
async def test_scan_finds_invalid_and_inaccessible_resources(tmp_path):
    storage = Storage(tmp_path)
    bili = MagicMock()
    bili.get_my_folders = AsyncMock(return_value=[
        {"fid": 100, "title": "默认收藏夹", "media_count": 3, "fav_state": 1},
        {"fid": 200, "title": "其他收藏夹", "media_count": 1, "fav_state": 0},
    ])
    requested_page_sizes = []

    async def resource_page(fid, page=1, page_size=50, storage=None):
        requested_page_sizes.append(page_size)
        if fid == 100:
            return {
                "page": 1, "total": 3, "has_more": False,
                "items": [
                    {"resource_id": 1, "resource_type": 2, "bvid": "BV1", "title": "正常", "status": "available"},
                    {"resource_id": 2, "resource_type": 2, "bvid": "BV2", "title": "失效", "status": "invalid"},
                ],
            }
        return {
            "page": 1, "total": 1, "has_more": False,
            "items": [{"resource_id": 3, "resource_type": 2, "bvid": "BV3", "title": "另一处正常", "status": "available"}],
        }

    async def resource_ids(fid, storage=None):
        if fid == 100:
            return [
                {"resource_id": 1, "resource_type": 2},
                {"resource_id": 2, "resource_type": 2},
                {"resource_id": 3, "resource_type": 2},
            ]
        return [{"resource_id": 3, "resource_type": 2}]

    bili.get_folder_resource_page = resource_page
    bili.get_folder_resource_ids = resource_ids
    events = []
    scan_id = storage.create_cleanup_scan("account-a", 0)

    await CleanupManager(storage, bili, sleep=AsyncMock()).scan(scan_id, "account-a", on_progress=events.append)

    scan = storage.get_cleanup_scan(scan_id, "account-a")
    items = storage.list_cleanup_items(scan_id)
    assert scan["status"] == "ready"
    assert scan["folders_scanned"] == 2
    # Count every favorite location from the complete ID list, including
    # resources omitted by the detail endpoint.
    assert scan["resources_scanned"] == 4
    assert {(item["source_fid"], item["resource_id"], item["problem_type"]) for item in items} == {
        (100, 2, "invalid"),
        (100, 3, "inaccessible"),
    }
    assert events[-1]["stage"] == "ready"
    assert set(requested_page_sizes) == {20}


@pytest.mark.asyncio
async def test_remove_groups_by_folder_in_chunks_and_verifies_ids(tmp_path):
    storage = Storage(tmp_path)
    bili = MagicMock()
    bili.batch_delete_resources = AsyncMock(return_value=True)
    id_calls = 0

    async def resource_ids(fid, storage=None):
        nonlocal id_calls
        id_calls += 1
        if id_calls == 1:
            return [{"resource_id": 1, "resource_type": 2}]
        return []

    bili.get_folder_resource_ids = resource_ids
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    scan_id = storage.create_cleanup_scan("account-a", 1)
    storage.update_cleanup_scan(scan_id, status="ready")
    storage.add_cleanup_items(scan_id, [
        {
            "source_fid": 100, "source_title": "默认收藏夹", "resource_id": rid,
            "resource_type": 2, "problem_type": "invalid", "problem_label": "已失效",
        }
        for rid in range(1, 52)
    ])
    item_ids = [item["id"] for item in storage.list_cleanup_items(scan_id)]

    stats = await CleanupManager(storage, bili, sleep=fake_sleep).remove(
        scan_id, "account-a", item_ids
    )

    assert bili.batch_delete_resources.await_count == 2
    assert [len(call.kwargs["resources"]) for call in bili.batch_delete_resources.await_args_list] == [50, 1]
    assert sleeps == [0.5, 1.0]
    assert stats == {"total": 51, "success": 51, "failed": 0}
    assert all(item["removed"] for item in storage.list_cleanup_items(scan_id))


@pytest.mark.asyncio
async def test_cancelled_scan_is_persisted_as_cancelled(tmp_path):
    storage = Storage(tmp_path)
    release = asyncio.Event()
    bili = MagicMock()

    async def folders(storage=None):
        await release.wait()
        return []

    bili.get_my_folders = folders
    scan_id = storage.create_cleanup_scan("account-a", 0)
    task = asyncio.create_task(CleanupManager(storage, bili).scan(scan_id, "account-a"))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert storage.get_cleanup_scan(scan_id, "account-a")["status"] == "cancelled"
