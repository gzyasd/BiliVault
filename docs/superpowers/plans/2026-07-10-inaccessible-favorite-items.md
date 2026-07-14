# Inaccessible Favorite Items Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent historical-cache contamination, list favorite resources whose details are inaccessible to the active account, and allow the user to remove selected inaccessible resources from their source folders.

**Architecture:** `BilibiliClient` will expose the complete favorite-resource ID list. `ClassifySession.collect()` will compare that list with resources received from the detail-list endpoint and save missing resources as removable skipped items. Classification and preview retrieval will rely only on the current session's resource keys, never on a `fid`-only cache fallback.

**Tech Stack:** Python 3, FastAPI, SQLite, httpx, pytest/respx, vanilla JavaScript.

---

## File Structure

- Modify: `core/bilibili_api.py` - normalize the favorite-resource ID endpoint.
- Modify: `core/session.py` - reconcile visible resources with ID-only resources; remove stale-cache fallbacks; avoid AI calls for empty sessions.
- Modify: `static/app.js` - display a BVID or resource ID when skipped items have no title; provide an accurate empty-plan summary.
- Modify: `tests/test_bilibili_api.py` - verify ID endpoint normalization.
- Modify: `tests/test_session.py` - reproduce inaccessible-resource collection and stale-cache contamination.
- Modify: `tests/test_frontend_static.py` - lock in the BVID fallback and empty-plan wording.

The workspace has no `.git` directory. Do not attempt commit commands; use test commands as each task's checkpoint.

### Task 1: Add the Favorite Resource ID Client API

**Files:**
- Modify: `tests/test_bilibili_api.py`
- Modify: `core/bilibili_api.py`

- [ ] **Step 1: Write the failing client test**

Append this test to `tests/test_bilibili_api.py`. It establishes the method contract without involving session logic.

```python
@pytest.mark.asyncio
@respx.mock
async def test_get_folder_resource_ids_normalizes_resource_keys(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {
            "img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png",
            "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png",
        }},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/ids").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": [
            {"id": 101, "type": 2, "bvid": "BV1visible"},
            {"id": 202, "type": 11, "bv_id": ""},
            {"id": 0, "type": 2, "bvid": "BV1ignore"},
        ],
    }))

    assert await client.get_folder_resource_ids(100, storage=None) == [
        {"resource_id": 101, "resource_type": 2, "bvid": "BV1visible"},
        {"resource_id": 202, "resource_type": 11, "bvid": ""},
    ]
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_bilibili_api.py::test_get_folder_resource_ids_normalizes_resource_keys -q
```

Expected: fail with `AttributeError` because `BilibiliClient.get_folder_resource_ids` does not exist.

- [ ] **Step 3: Add the minimal client method**

Add this method after `get_folder_resource_pages()` in `core/bilibili_api.py`:

```python
    async def get_folder_resource_ids(self, fid: int, storage=None) -> list[dict]:
        """返回收藏夹中的完整资源键，包含详情接口不返回的权限资源。"""
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
```

- [ ] **Step 4: Run the client test and verify GREEN**

Run the command from Step 2.

Expected: `1 passed`.

### Task 2: Reconcile Inaccessible Resources During Collection

**Files:**
- Modify: `tests/test_session.py`
- Modify: `core/session.py`

- [ ] **Step 1: Write failing session tests**

Append these tests to `tests/test_session.py`.

```python
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

    sid = await ClassifySession(storage, bili, ai).create(100, "quick")
    await ClassifySession(storage, bili, ai).run_pipeline(sid)

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
            "resources": [{"resource_id": 11, "resource_type": 2, "avid": 11,
                           "bvid": "BV1visible", "title": "可见", "intro": "", "tags": "[]",
                           "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "", "fid": fid}],
            "raw_count": 1, "usable_count": 1, "skipped_count": 0,
            "skipped_reasons": {}, "skipped_items": [], "expected_total": 2, "has_more": False,
        }

    bili.get_folder_resource_pages = partial_pages
    bili.get_folder_resource_ids = AsyncMock(return_value=[
        {"resource_id": 11, "resource_type": 2, "bvid": "BV1visible"},
        {"resource_id": 12, "resource_type": 2, "bvid": "BV1hidden"},
    ])
    ai.classify = AsyncMock(return_value=[Classification(11, "分类", 0.9, "")])

    sid = await ClassifySession(storage, bili, ai).create(100, "quick")
    await ClassifySession(storage, bili, ai).run_pipeline(sid)

    assert [item["resource_id"] for item in storage.load_classifications(sid)] == [11]
    assert [item["avid"] for item in storage.list_skipped_items(sid)] == [12]
    ai.classify.assert_awaited_once()
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py::test_collect_records_detail_hidden_resources_and_skips_ai tests/test_session.py::test_collect_classifies_visible_resource_and_skips_only_missing_id -q
```

Expected: fail because `collect()` never calls the resource-ID endpoint or saves `inaccessible` skipped items, and the empty case still calls AI.

- [ ] **Step 3: Reconcile `resource/ids` after each source's detail pages**

In `ClassifySession.collect()` in `core/session.py`:

1. Before each source's page loop, initialize `seen_resource_keys: set[tuple[int, int]] = set()` and `source_scanned = 0`.
2. During each page, add keys from `resources` and from `page_skipped_items` that contain an ID, so an already-reported invalid item is not duplicated as inaccessible.
3. After the page loop, call `await self.bili.get_folder_resource_ids(source_fid, storage=self.storage)` in a `try` block. On exception, log `logger.warning("无法交叉核验收藏夹 %s 的资源清单: %s", source_fid, exc)` and continue with the detail-list results.
4. For each returned ID record not in `seen_resource_keys`, append this dictionary to a `hidden_items` list and save it with `self.storage.add_skipped_items(...)`:

```python
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
```

5. Increase `source_skipped`, `skipped`, and `skipped_by_reason["inaccessible"]` by `len(hidden_items)`. Set `source_scanned = max(source_scanned, len(resource_ids))`, add only the positive delta to global `scanned`, then update `session_sources` and emit a collecting progress event with the corrected totals.

Keep all normal resources in `session_video_sources`; the new code only adds skipped rows for keys absent from the detail response.

- [ ] **Step 4: Run both tests and verify GREEN**

Run the command from Step 2.

Expected: `2 passed`.

### Task 3: Remove Historical Cache Fallbacks and Skip AI for Empty Sessions

**Files:**
- Modify: `tests/test_session.py`
- Modify: `core/session.py`

- [ ] **Step 1: Write the stale-cache regression test**

Append this test to `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py::test_classify_empty_session_never_uses_fid_cache_or_calls_ai -q
```

Expected: fail because the existing `list_videos_by_fid(s["source_fid"])` fallback passes the cached row to AI.

- [ ] **Step 3: Make session resources the only classification and preview source**

In `ClassifySession.classify()`:

```python
video_sources = self.storage.list_session_video_sources(sid)
resource_keys = sorted({(row["resource_id"], row.get("resource_type", 2)) for row in video_sources})
videos_rows = self.storage.list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))
```

Delete both `list_videos_by_fid(...)` fallback branches. After emitting initial classification progress, use an empty `results` list when `videos` is empty; call `self.ai.classify(...)` only when `videos` is non-empty. Preserve `save_classifications(...)`, `create_plan_version(...)`, progress completion, and the transition to `pending_review` for both paths.

In `ClassifySession.get_plan()`, remove the `list_videos_by_fid(s["source_fid"])` fallback. Build the `videos` dictionary exclusively from `list_videos_by_resource_keys(resource_keys, account_id=s.get("account_id"))`.

- [ ] **Step 4: Run the regression and collection tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py::test_classify_empty_session_never_uses_fid_cache_or_calls_ai tests/test_session.py::test_collect_records_detail_hidden_resources_and_skips_ai tests/test_session.py::test_collect_classifies_visible_resource_and_skips_only_missing_id -q
```

Expected: `3 passed`.

### Task 4: Present Titleless Inaccessible Items Clearly in the Preview

**Files:**
- Modify: `tests/test_frontend_static.py`
- Modify: `static/app.js`

- [ ] **Step 1: Write failing frontend static assertions**

Append this test to `tests/test_frontend_static.py`:

```python
def test_frontend_skipped_items_fall_back_to_bvid_and_empty_plan_explains_next_step():
    assert "无法访问的视频（BVID：${it.bvid}）" in APP_JS
    assert "无法访问的资源（ID：${it.avid}）" in APP_JS
    assert "没有可整理条目。可在下方查看并处理跳过条目。" in APP_JS
```

- [ ] **Step 2: Run the frontend test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_frontend_static.py::test_frontend_skipped_items_fall_back_to_bvid_and_empty_plan_explains_next_step -q
```

Expected: fail because the current skipped-row fallback only displays `avid` and empty plans use the generic category summary.

- [ ] **Step 3: Add the display fallbacks**

In `renderSkippedPanelFromItems()` in `static/app.js`, compute the display name before returning each row:

```javascript
const itemName = it.title || (it.bvid
  ? `无法访问的视频（BVID：${it.bvid}）`
  : `无法访问的资源（ID：${it.avid}）`);
```

Use `${escapeHtml(itemName)}` in the title node. Keep the existing checkbox, reason group, confirmation, POST request, and refresh behavior unchanged.

In `renderReview()`, replace the unconditional summary initialization with:

```javascript
let summaryText = items.length
  ? `${items.length} 个可整理条目，分成 ${cats.length} 类。可下拉调整单个条目的分类。`
  : '没有可整理条目。可在下方查看并处理跳过条目。';
```

- [ ] **Step 4: Run the frontend test and verify GREEN**

Run the command from Step 2.

Expected: `1 passed`.

### Task 5: Full Verification and Manual Safety Check

**Files:**
- Verify only: `core/bilibili_api.py`, `core/session.py`, `static/app.js`, `tests/`

- [ ] **Step 1: Run the complete automated suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass with no failures.

- [ ] **Step 2: Run the isolated inaccessible-session regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py::test_collect_records_detail_hidden_resources_and_skips_ai tests/test_session.py::test_classify_empty_session_never_uses_fid_cache_or_calls_ai -q
```

Expected: both tests pass. They prove that an empty detail list produces removable BVID-tagged skipped entries, AI is not called, and an existing `videos` cache row cannot enter the plan.

- [ ] **Step 3: Confirm the destructive action remains opt-in**

Inspect `static/app.js` and `core/session.py` to confirm the UI still requires `confirm(...)`, and `remove_skipped_items()` still filters on `removable`, `removed`, resource ID, and source folder before calling `batch_delete_resources`.

Expected: opening a preview never calls B 站's delete endpoint; deletion occurs only after the user explicitly confirms the selected rows.
