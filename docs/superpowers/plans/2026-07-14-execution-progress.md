# Execution Progress Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show real-time, item-based progress while the final classification plan creates target folders and moves favorite resources.

**Architecture:** `ClassifySession.execute()` emits terminal progress after folder-creation failures and every move batch. FastAPI owns one background execution task per session and exposes its latest event through SSE. The review footer switches from confirmation controls to a live progress band and reconnects to an already-running task.

**Tech Stack:** Python 3, FastAPI `StreamingResponse`, asyncio, pytest, vanilla JavaScript/EventSource.

---

Commit steps are intentionally omitted because this implementation was not requested as a separate commit.

### Task 1: Emit Real Execution Progress

**Files:**
- Modify: `tests/test_session.py`
- Modify: `core/session.py`

- [ ] Add `test_execute_emits_progress_after_each_move_batch` with two plan items, `batch_size=1`, and an async progress collector. Assert moving events end at `processed=2`, `total=2`, `success=2`, `failed=0`, `progress=1.0`.
- [ ] Run `./.venv/Scripts/python.exe -m pytest tests/test_session.py::test_execute_emits_progress_after_each_move_batch -q` and verify it fails because only the final `done` event exists.
- [ ] In `execute()`, compute `total = sum(len(resources) for resources in move_groups.values())`, emit an initial `creating_folders` event, count folder-creation failures as processed failures, and emit after each move batch.
- [ ] Run the targeted test and verify it passes.
- [ ] Add `test_execute_progress_counts_folder_creation_failures` and assert two resources under a failed category produce `processed=2`, `failed=2`, `progress=1.0` without a move call.
- [ ] Run both execution progress tests and verify they pass.

Progress events use this exact shape:

```python
{
    "stage": "executing",
    "phase": "moving",
    "progress": processed / total if total else 1.0,
    "processed": processed,
    "total": total,
    "success": success,
    "failed": failed,
    "category": category,
    "source_fid": source_fid,
    "folders_created": folders_created,
    "folders_total": len(categories),
}
```

### Task 2: Add an Idempotent Execution SSE Endpoint

**Files:**
- Modify: `tests/test_main_stream.py`
- Modify: `main.py`

- [ ] Add `test_execute_stream_emits_progress_and_done` using a fake manager whose `execute()` emits one progress event and returns stats after an `asyncio.Event` is released.
- [ ] Run `./.venv/Scripts/python.exe -m pytest tests/test_main_stream.py::test_execute_stream_emits_progress_and_done -q` and verify it fails because `api_execute_stream` does not exist.
- [ ] Add `_running_executions`, `_execution_progress`, `_get_or_start_execution()`, and `/api/session/{sid}/execute/stream`.
- [ ] The stream starts execution only from `pending_review`, reuses the task while `executing`, emits the latest distinct progress event, emits `done` with returned stats, and emits `fail` for invalid or orphaned states.
- [ ] Ensure closing the SSE iterator does not cancel the background task.
- [ ] Run the targeted stream test and the existing pipeline stream test.

The task starter follows this contract:

```python
def _get_or_start_execution(sid: str, mgr: ClassifySession) -> tuple[asyncio.Task, bool]:
    existing = _running_executions.get(sid)
    if existing and not existing.done():
        return existing, True

    async def on_progress(event: dict):
        _execution_progress[sid] = event

    task = asyncio.create_task(mgr.execute(sid, on_progress=on_progress))
    _running_executions[sid] = task
    return task, False
```

### Task 3: Render the Live Progress Band

**Files:**
- Modify: `tests/test_frontend_static.py`
- Modify: `static/index.html`
- Modify: `static/app.js`

- [ ] Add a failing static test asserting `execution-progress`, `execution-progress-bar`, `execution-processed`, `/execute/stream`, and `startExecutionProgress` are present.
- [ ] Run `./.venv/Scripts/python.exe -m pytest tests/test_frontend_static.py::test_frontend_final_execution_has_live_progress -q` and verify it fails.
- [ ] Add a hidden progress band to the review footer with a stable progress bar, `processed/total`, success, failed, percentage, and current-stage text.
- [ ] Add `startExecutionProgress(sid)` and `updateExecutionProgress(event)` using `EventSource`.
- [ ] Replace the synchronous POST call in the execute button handler with the SSE starter after confirmation.
- [ ] When `plan.session.status === 'executing'`, reconnect automatically without another confirmation.
- [ ] On `done`, close SSE and call `renderResult`; on `fail`, restore the confirmation controls and show the error.
- [ ] Run the frontend static test and verify it passes.

### Task 4: Verify the Whole Change

**Files:**
- Verify: `core/session.py`, `main.py`, `static/index.html`, `static/app.js`, `tests/`

- [ ] Run `./.venv/Scripts/python.exe -m pytest -q` and require zero failures.
- [ ] Run the execution progress and SSE tests together to confirm item counts and stream completion.
- [ ] Inspect the final frontend path to confirm no request to `/execute` is sent in parallel with `/execute/stream`.
- [ ] Confirm no execution cancel action was introduced and SSE disconnect does not cancel `_running_executions[sid]`.
