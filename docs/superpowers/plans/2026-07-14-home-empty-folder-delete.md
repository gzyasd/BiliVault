# 首页空收藏夹行内删除实施计划

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在首页收藏夹列表中安全地直接删除空的普通收藏夹。

**Architecture:** FastAPI 删除接口负责重新查询 B 站并执行服务端安全校验，前端只根据列表字段展示行内删除按钮。删除成功后重新调用 `renderHome()`，确保页面以 B 站实际状态为准。

**Tech Stack:** Python 3、FastAPI、pytest、原生 JavaScript、Lucide Icons。

---

### Task 1: 后端删除安全校验

**Files:**
- Modify: `tests/test_main_stream.py`
- Modify: `main.py`

- [ ] 编写 `test_delete_empty_folder_revalidates_and_deletes`，模拟空且非默认收藏夹，断言调用 `delete_folders([200])` 并返回 `ok=True`。
- [ ] 编写参数化拒绝测试，分别覆盖 `media_count > 0` 和 `fav_state == 1`，断言不调用删除接口。
- [ ] 运行目标测试，确认因 `api_delete_folder` 不存在而失败。
- [ ] 新增 `DELETE /api/folders/{fid}`，重新拉取收藏夹并进行存在性、默认收藏夹、空状态及运行任务校验。
- [ ] 删除后再次拉取，只有目标 `fid` 消失才返回成功。
- [ ] 运行后端目标测试并确认通过。

### Task 2: 首页行内删除交互

**Files:**
- Modify: `tests/test_frontend_static.py`
- Modify: `static/app.js`

- [ ] 编写静态测试，要求存在 `delete-empty-folder-*`、`deleteEmptyFolder()`、`event.stopPropagation()`、`method: 'DELETE'` 和删除确认文案。
- [ ] 运行测试并确认因行内删除功能不存在而失败。
- [ ] 在 `renderHome()` 中仅为 `Number(f.media_count) === 0 && Number(f.fav_state) !== 1` 的行渲染垃圾桶按钮。
- [ ] 为按钮单独绑定事件并阻止冒泡；确认后请求删除接口，成功调用 `renderHome()`。
- [ ] 删除期间禁用按钮并显示加载状态，失败后恢复按钮并提示错误。
- [ ] 运行前端静态测试并确认通过。

### Task 3: 回归与视觉验证

**Files:**
- Test: `tests/`

- [ ] 运行 `./.venv/Scripts/python.exe -m pytest -q`，确认全量测试通过。
- [ ] 运行 `node --check static/app.js` 与 Python 编译检查。
- [ ] 使用 Playwright 在桌面和 390px 手机宽度检查空收藏夹删除按钮、标题截断和选择状态。
- [ ] 验证浏览器控制台没有本次功能引入的新错误。
