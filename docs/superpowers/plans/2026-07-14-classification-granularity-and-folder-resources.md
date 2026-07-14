# 分类精细度与收藏夹资源列表实施计划

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在开始整理前控制最终分类数量，并通过独立只读页面查看收藏夹的完整资源列表。

**Architecture:** 分类上限随会话持久化，AI 批量分类后执行受约束的全局归并。资源详情使用指定页 API 渐进加载，前端在最后一页将完整 ID 清单与详情键做差，补齐不可访问资源。

**Tech Stack:** Python 3、SQLite、FastAPI、OpenAI 兼容接口、pytest、原生 JavaScript/EventSource。

---

### Task 1: 分类上限存储与会话链路

**Files:**
- Modify: `core/storage.py`
- Modify: `core/session.py`
- Modify: `main.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_session.py`
- Test: `tests/test_main_stream.py`

- [x] 先写失败测试：新建会话传入 `category_limit=8` 后，`load_session()` 返回 `8`。
- [x] 先写迁移测试：缺少该列的旧 `classify_sessions` 初始化后新增列，旧行值为 `14`。
- [x] 在 `_SCHEMA` 和 `_migrate_schema()` 增加 `category_limit`，将 `create_session()` 默认参数设为 `14`。
- [x] 给 `SessionIn` 增加 `Field(default=14, ge=3, le=50)`，并贯通 `api_create_session()`、`create_many()` 与 `create()`。
- [x] 运行存储、会话和主接口目标测试。

### Task 2: AI 全局分类上限

**Files:**
- Modify: `core/ai_classifier.py`
- Modify: `core/session.py`
- Test: `tests/test_ai_classifier.py`
- Test: `tests/test_session.py`

- [x] 写失败测试：12 个不同分类在 `max_categories=3` 时调用全局归并，并将最终唯一分类限制为 3。
- [x] 写失败测试：归并映射缺少原分类或最终仍超过上限时抛出 `AI_CATEGORY_LIMIT_FAILED`。
- [x] `classify_batch()` 接收 `max_categories` 并把限制加入系统提示词。
- [x] `classify()` 接收 `max_categories=14`；超过时调用 `merge_categories(categories, max_categories)`。
- [x] 校验映射覆盖和最终唯一值数量，失败时不返回超限结果。
- [x] `ClassifySession.classify()` 从会话读取 `category_limit` 并传给 AI。
- [x] 运行 AI 与会话目标测试。

### Task 3: 指定页资源读取 API

**Files:**
- Modify: `core/bilibili_api.py`
- Modify: `main.py`
- Test: `tests/test_bilibili_api.py`
- Test: `tests/test_main_stream.py`

- [x] 写失败测试：指定 `page=2` 时请求参数使用 `pn=2`，并保留正常、失效和非视频资源的展示字段。
- [x] 实现 `get_folder_resource_page()`，返回 `items/page/page_size/total/has_more`。
- [x] 写失败测试：资源接口第一页返回完整 `resource_ids`，后续页不重复请求 ID 接口。
- [x] 实现 `GET /api/folders/{fid}/resources`，校验收藏夹归属及分页参数。
- [x] 运行 B 站客户端和主接口目标测试。

### Task 4: 首页精细度控件

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Test: `tests/test_frontend_static.py`

- [x] 写失败测试，要求存在粗略/均衡/精细/自定义控件和 `category_limit` 创建请求字段。
- [x] 实现预设值 `8/14/24`、自定义范围 `3-50`，默认均衡。
- [x] 切换档位时更新说明和请求值，自定义值在失焦与创建前进行范围归一化。
- [x] 运行前端静态测试和 `node --check static/app.js`。

### Task 5: 独立只读资源列表

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Test: `tests/test_frontend_static.py`

- [x] 写失败测试，要求 `folder-resources` 视图、返回按钮、箭头独立按钮、分页加载和 ID-only 补齐函数。
- [x] 箭头事件阻止冒泡并调用 `openFolderResources(fid, title, count)`。
- [x] 实现第一页加载、加载更多、错误重试和加载结束后的 `appendInaccessibleResources()`。
- [x] 资源条目仅渲染封面、标题、UP 主、类型和状态，不绑定修改操作。
- [x] 运行前端静态测试与 JavaScript 语法检查。

### Task 6: 全量验证

**Files:**
- Test: `tests/`

- [x] 运行 `./.venv/Scripts/python.exe -m pytest -q`。
- [x] 运行 Python 编译、JavaScript 语法及 `git diff --check`。
- [x] 在本地服务中用请求拦截验证精细度值、箭头不选中、分页和不可访问补齐，不调用真实整理接口。
- [x] 在桌面和 390px 手机宽度检查控件、资源列表、返回按钮和文字不溢出。
