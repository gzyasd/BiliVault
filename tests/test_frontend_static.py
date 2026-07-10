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
