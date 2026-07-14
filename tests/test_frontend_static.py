from pathlib import Path


APP_JS = Path("static/app.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_frontend_escapes_external_html_data():
    assert "function escapeHtml" in APP_JS
    assert "${escapeHtml(f.title)}" in APP_JS
    assert "${escapeHtml(cat)}" in APP_JS
    assert "${escapeHtml(v.title || rid)}" in APP_JS
    assert "${escapeHtml(it.error_message)}" in APP_JS


def test_frontend_handles_not_logged_in_from_home_fetches():
    assert "err.code = data.code" in APP_JS
    assert "if (e.code === 'NOT_LOGGED_IN')" in APP_JS
    assert "showView('login');" in APP_JS


def test_frontend_review_handles_empty_plan():
    assert "if (items.length)" in APP_JS


def test_frontend_has_cancel_and_back_buttons():
    for dom_id in [
        "progress-back-home",
        "progress-cancel",
        "review-back-home",
        "review-abandon",
        "config-cancel",
        "login-back",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML, f"missing {dom_id}"


def test_frontend_closes_event_source_on_view_switch():
    assert "activeEventSource" in APP_JS
    assert "cleanupPollingAndSSE" in APP_JS
    assert "es.close()" in APP_JS
    assert "activeEventSource = null" in APP_JS


def test_frontend_qr_poll_uses_token_to_abort_stale():
    assert "qrPollToken" in APP_JS
    assert "myToken !== qrPollToken" in APP_JS


def test_frontend_progress_shows_classifiable_wording():
    assert "可整理" in APP_JS
    assert "renderStatsGrid" in APP_JS
    assert "已跳过" in APP_JS


def test_frontend_cancel_calls_cancel_api():
    assert "/cancel" in APP_JS
    assert "CANCELLED" in APP_JS


def test_frontend_review_has_filter_version_refine_and_skipped_regions():
    for dom_id in [
        "review-filter-bar",
        "review-version-bar",
        "review-refine-panel",
        "review-skipped-panel",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML
    assert "renderReviewFilters" in APP_JS
    assert "toggleReviewGroup" in APP_JS
    assert "activeReviewFilter" in APP_JS


def test_frontend_folder_selection_supports_multi_select_start_button():
    assert "selectedSourceFids" in APP_JS
    assert "start-organize" in INDEX_HTML
    assert "toggleFolderSelection" in APP_JS
    assert "source_fids" in APP_JS


def test_frontend_skipped_cleanup_ui():
    assert "renderSkippedPanel" in APP_JS
    assert "remove-skipped" in APP_JS
    assert "/skipped-items/remove" in APP_JS
    assert "此操作不可逆" in APP_JS
    # 跳过项必须渲染明细：标题、原因、来源收藏夹、删除结果/错误
    assert "reason_label" in APP_JS
    assert "source_fid" in APP_JS
    assert "remove_error" in APP_JS


def test_frontend_empty_folder_cleanup_ui():
    assert "empty-source-folders" in INDEX_HTML
    assert "renderEmptySourceFolders" in APP_JS
    assert "/empty-source-folders/delete" in APP_JS
    assert "此操作不可逆" in APP_JS


def test_frontend_account_management_ui():
    assert 'data-dom-id="nav-account"' in INDEX_HTML
    assert 'data-view="accounts"' in INDEX_HTML
    assert "renderAccounts" in APP_JS
    assert "/api/accounts/login/start" in APP_JS
    assert "/api/accounts/login/poll" in APP_JS
    assert "/api/accounts/" in APP_JS
    assert "switch-account-" in APP_JS


def test_frontend_progress_can_derive_percent_from_scanned_source_total():
    assert "deriveProgressPercent" in APP_JS
    assert "d.scanned" in APP_JS
    assert "d.source_total" in APP_JS


def test_frontend_config_supports_ai_batch_size():
    assert "config-ai-batch-size" in INDEX_HTML
    assert "ai_batch_size" in APP_JS


def test_frontend_skipped_panel_supports_collapse():
    assert "collapsedSkippedReasons" in APP_JS
    assert "toggleSkippedReason" in APP_JS
    assert "toggle-skipped-panel" in APP_JS
    assert "toggle-skipped-reason-" in APP_JS


def test_frontend_account_logout_ui():
    assert "account-logout" in INDEX_HTML
    assert "logoutAccount" in APP_JS
    assert "/api/logout" in APP_JS


def test_frontend_skipped_items_fall_back_to_bvid_and_empty_plan_explains_next_step():
    assert "无法访问的视频（BVID：${it.bvid}）" in APP_JS
    assert "无法访问的资源（ID：${it.avid}）" in APP_JS
    assert "没有可整理条目。可在下方查看并处理跳过条目。" in APP_JS


def test_frontend_execution_progress_uses_sse_and_shows_real_counts():
    for dom_id in [
        "review-actions",
        "execution-progress",
        "execution-status",
        "execution-percent",
        "execution-progress-bar",
        "execution-total",
        "execution-processed",
        "execution-success",
        "execution-failed",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML, f"missing {dom_id}"
    assert "function startExecutionProgress" in APP_JS
    assert "function updateExecutionProgress" in APP_JS
    assert "/execute/stream" in APP_JS
    assert "addEventListener('stage'" in APP_JS
    assert "addEventListener('done'" in APP_JS
    assert "addEventListener('fail'" in APP_JS
    assert "EventSource.CLOSED" in APP_JS


def test_frontend_home_allows_direct_delete_for_empty_non_default_folders():
    assert "function deleteEmptyFolder" in APP_JS
    assert "delete-empty-folder-${f.fid}" in APP_JS
    assert "Number(f.media_count) === 0" in APP_JS
    assert "Number(f.fav_state) !== 1" in APP_JS
    assert "event.stopPropagation()" in APP_JS
    assert "method: 'DELETE'" in APP_JS
    assert "/api/folders/${fid}" in APP_JS
    assert "确认删除空收藏夹" in APP_JS


def test_frontend_supports_category_granularity_presets_and_custom_limit():
    for dom_id in [
        "granularity-coarse",
        "granularity-balanced",
        "granularity-detailed",
        "granularity-custom",
        "granularity-custom-limit",
        "category-limit-summary",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML, f"missing {dom_id}"
    assert "let categoryLimit = 14" in APP_JS
    assert "function setCategoryGranularity" in APP_JS
    assert "coarse: 8" in APP_JS
    assert "balanced: 14" in APP_JS
    assert "detailed: 24" in APP_JS
    assert "category_limit: categoryLimit" in APP_JS


def test_frontend_has_read_only_folder_resource_view_and_arrow_navigation():
    assert 'data-view="folder-resources"' in INDEX_HTML
    for dom_id in [
        "folder-resources-back",
        "folder-resources-title",
        "folder-resources-summary",
        "folder-resources-list",
        "folder-resources-load-more",
        "folder-resources-error",
    ]:
        assert f'data-dom-id="{dom_id}"' in INDEX_HTML, f"missing {dom_id}"
    assert "view-folder-resources-${f.fid}" in APP_JS
    assert "function openFolderResources" in APP_JS
    assert "function loadFolderResourcePage" in APP_JS
    assert "function appendInaccessibleResources" in APP_JS
    assert "/api/folders/${folderResourceState.fid}/resources" in APP_JS
    assert "event.stopPropagation()" in APP_JS


def test_folder_resource_covers_avoid_bilibili_hotlink_rejection():
    assert 'referrerpolicy="no-referrer"' in APP_JS
    assert "nextElementSibling.style.display='block'" in APP_JS


def test_frontend_refine_has_visible_progress_and_unclassified_retry():
    assert "refine-progress" in APP_JS
    assert "refine-progress-bar" in APP_JS
    assert "startRefineJob" in APP_JS
    assert "retry-unclassified" in APP_JS
    assert "/retry-unclassified" in APP_JS
    assert "/refine/stream?job_id=" in APP_JS


def test_frontend_has_account_cleanup_view_and_batch_controls():
    assert "open-cleanup" in INDEX_HTML
    assert 'data-view="cleanup"' in INDEX_HTML
    for dom_id in (
        "cleanup-back", "cleanup-progress", "cleanup-filter-bar",
        "cleanup-list", "cleanup-select-all", "cleanup-select-none",
        "cleanup-remove", "cleanup-rescan",
    ):
        assert dom_id in INDEX_HTML
    assert "startCleanupScan" in APP_JS
    assert "renderCleanupResults" in APP_JS
    assert "/api/cleanup/scans" in APP_JS
