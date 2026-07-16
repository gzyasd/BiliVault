from pathlib import Path


APP_JS = Path("static/app.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_frontend_escapes_external_html_data():
    assert "function escapeHtml" in APP_JS
    assert "${escapeHtml(f.title)}" in APP_JS
    assert "${escapeHtml(cat)}" in APP_JS
    assert "${escapeHtml(v.title || rid)}" in APP_JS
    assert "${escapeHtml(it.error_message)}" in APP_JS


def test_frontend_static_assets_are_local_and_precompiled():
    assert "https://cdn.jsdelivr.net" not in INDEX_HTML
    assert "https://unpkg.com" not in INDEX_HTML
    assert "text/tailwindcss" not in INDEX_HTML
    assert 'href="/static/app.css?v=' in INDEX_HTML
    assert 'src="/static/vendor/lucide-1.8.0.min.js"' in INDEX_HTML
    assert Path("static/app.css").stat().st_size > 10_000
    assert Path("static/vendor/lucide-1.8.0.min.js").stat().st_size > 10_000


def test_home_sticky_action_bar_is_opaque_and_above_folder_rows():
    assert 'data-dom-id="home-action-bar"' in INDEX_HTML
    assert (
        'data-dom-id="home-action-bar" class="sticky bottom-0 z-20 mt-6 py-4"'
        in INDEX_HTML
    )
    assert 'background: var(--background-50);' in INDEX_HTML


def test_frontend_does_not_submit_ineffective_default_privacy_setting():
    assert "default_privacy" not in APP_JS


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


def test_utility_pages_return_to_the_originating_session_view():
    """设置和账号管理必须能返回进入前的整理页面，而不是固定跳回首页。"""
    assert "let currentView = 'home'" in APP_JS
    assert "let utilityReturnContext = null" in APP_JS
    assert "function openUtilityView" in APP_JS
    assert "async function returnFromUtilityView" in APP_JS
    assert "view: currentView" in APP_JS
    assert "sid: currentSid" in APP_JS
    assert "case 'progress':" in APP_JS
    assert "runPipeline(context.sid, { reset: false })" in APP_JS
    assert "case 'review':" in APP_JS
    assert "await openSession(context.sid)" in APP_JS
    assert "openUtilityView('config')" in APP_JS
    assert "openUtilityView('accounts')" in APP_JS
    assert "$('config-cancel').onclick = returnFromUtilityView" in APP_JS
    assert "$('accounts-back').onclick = async () =>" in APP_JS
    assert "await returnFromUtilityView()" in APP_JS


def test_utility_back_buttons_are_at_the_top_of_long_pages():
    accounts = INDEX_HTML.index('<section data-view="accounts">')
    accounts_title = INDEX_HTML.index('账号管理', accounts)
    accounts_back = INDEX_HTML.index('data-dom-id="accounts-back"', accounts)
    config = INDEX_HTML.index('<section data-view="config">')
    config_title = INDEX_HTML.index('AI 配置', config)
    config_back = INDEX_HTML.index('data-dom-id="config-cancel"', config)
    assert accounts_back < accounts_title
    assert config_back < config_title


def test_frontend_closes_event_source_on_view_switch():
    assert "const eventSources" in APP_JS
    for slot in ("pipeline", "execution", "refine", "cleanup"):
        assert f"{slot}: null" in APP_JS
    assert "function replaceEventSource" in APP_JS
    assert "function closeEventSource" in APP_JS
    assert "cleanupPollingAndSSE" in APP_JS
    assert "source.close()" in APP_JS


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
    assert "cancelAddAccountLogin" in APP_JS
    assert "accounts/login/${encodeURIComponent(loginId)}/cancel" in APP_JS


def test_frontend_progress_can_derive_percent_from_scanned_source_total():
    assert "deriveProgressPercent" in APP_JS
    assert "d.scanned" in APP_JS
    assert "d.source_total" in APP_JS


def test_frontend_resumes_failed_pipeline_instead_of_opening_empty_review():
    assert "function resumeSession" in APP_JS
    assert "['draft', 'collecting', 'classifying', 'failed'].includes(session.status)" in APP_JS
    assert "runPipeline(session.session_id, { reset: false })" in APP_JS


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
    assert "data.phase === 'reconciling'" in APP_JS
    assert "正在核验上次执行进度" in APP_JS


def test_frontend_execution_uses_post_start_and_job_scoped_stream():
    assert "await api(`/api/session/${sid}/execute`, { method: 'POST' })" in APP_JS
    assert "execute/stream?job_id=${encodeURIComponent(jobId)}" in APP_JS
    assert "/execute/active" in APP_JS


def test_frontend_home_allows_direct_delete_for_empty_non_default_folders():
    assert "function deleteEmptyFolder" in APP_JS
    assert "delete-empty-folder-${f.fid}" in APP_JS
    assert "Number(f.media_count) === 0" in APP_JS
    assert "Number(f.fav_state) !== 1" in APP_JS
    assert "event.stopPropagation()" in APP_JS
    assert "method: 'DELETE'" in APP_JS
    assert "/api/folders/${fid}" in APP_JS
    assert "确认删除空收藏夹" in APP_JS


def test_frontend_home_supports_batch_selecting_and_deleting_empty_folders():
    assert 'data-dom-id="empty-folder-batch-bar"' in INDEX_HTML
    assert "批量选择空收藏夹并删除" in APP_JS
    assert "selectedEmptyFolderFids" in APP_JS
    assert "emptyFolderSelectionMode" in APP_JS
    assert "function renderEmptyFolderBatchControls" in APP_JS
    assert "function toggleEmptyFolderSelection" in APP_JS
    assert "function deleteSelectedEmptyFolders" in APP_JS
    assert 'data-role="empty-folder-batch-select"' in APP_JS
    assert "empty-folder-select-all" in APP_JS
    assert "empty-folder-select-none" in APP_JS
    assert "empty-folder-delete-selected" in APP_JS
    assert "/api/folders/batch-delete" in APP_JS
    assert "确认批量删除" in APP_JS
    assert "deleted_fids" in APP_JS
    assert "!Boolean(f.is_default)" in APP_JS


def test_frontend_home_supports_touch_drag_folder_sorting_with_explicit_save():
    assert 'data-dom-id="folder-sort-bar"' in INDEX_HTML
    assert "folderSortMode" in APP_JS
    assert "folderSortOriginalIds" in APP_JS
    assert 'data-role="folder-drag-handle"' in APP_JS
    assert "pointerdown" in APP_JS
    assert "pointermove" in APP_JS
    assert "pointerup" in APP_JS
    assert "draggable = folderSortMode" in APP_JS
    assert "dragstart" in APP_JS
    assert "dragover" in APP_JS
    assert "dragend" in APP_JS
    assert "touchAction" in APP_JS
    assert "window.scrollBy" in APP_JS
    assert "keydown" in APP_JS
    assert "ArrowUp" in APP_JS
    assert "ArrowDown" in APP_JS
    assert "getCurrentFolderOrder" in APP_JS
    assert "bindFolderDragSurface(row, row)" in APP_JS
    assert "cancelFolderSort" in APP_JS
    assert "saveFolderSort" in APP_JS
    assert "/api/folders/sort" in APP_JS
    assert "保存排序" in APP_JS
    assert '<script src="/static/app.js?v=' in INDEX_HTML


def test_frontend_home_shows_animated_folder_loading_and_retry_states():
    assert "function renderFolderLoadingState" in APP_JS
    assert "function renderFolderLoadError" in APP_JS
    assert 'data-dom-id="folder-loading-status"' in APP_JS
    assert 'data-dom-id="folder-load-retry"' in APP_JS
    assert "正在从 B 站加载收藏夹" in APP_JS
    assert "正在加载收藏夹" in APP_JS
    assert "skeleton-pulse" in INDEX_HTML
    assert "skeleton-pulse" in APP_JS
    assert "prefers-reduced-motion" in INDEX_HTML
    assert APP_JS.index("renderFolderLoadingState();") < APP_JS.index("await api('/api/sessions/resumable')")


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


def test_frontend_review_filter_preserves_running_refine_progress():
    """筛选或折叠预览内容时，不得断开微调 SSE 或覆盖进度面板。"""
    assert "let activeRefineKind = null" in APP_JS
    assert "let lastRefineProgress = null" in APP_JS
    assert "if (currentView !== name) cleanupPollingAndSSE();" in APP_JS
    assert "activeRefineKind = kind" in APP_JS
    assert "lastRefineProgress = { ...event }" in APP_JS
    assert "if (activeRefineJob)" in APP_JS
    assert "renderRefineProgress(sid, activeRefineKind, lastRefineProgress)" in APP_JS


def test_frontend_reconnects_refine_progress_after_navigation_or_refresh():
    assert "function connectRefineStream" in APP_JS
    assert "async function restoreRefineJob" in APP_JS
    assert "/refine/active" in APP_JS
    assert "await openSession(sid)" in APP_JS
    assert "connectRefineStream(sid, active.job_id" in APP_JS


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
