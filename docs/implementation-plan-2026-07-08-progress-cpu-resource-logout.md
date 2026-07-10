# 整理进度、资源分类、CPU 治理与账号退出实施计划

> **给执行智能体的说明：** 本文使用复选框 `- [ ]` 作为任务跟踪格式。

**目标：** 修复整理中进度条长期停留 0% 的问题，降低/定位异常 CPU 占用，让跳过条目支持折叠，将非视频收藏资源纳入智能分类与移动流程，补齐退出 B 站账号入口，并允许用户在设置中自定义 AI 每批分类条数。

**总体架构：** 本次改动分两层推进：先修复现有视频整理流程的进度、跳过项折叠、账号退出与运行时治理；再把“视频”抽象升级为“收藏资源”，让 `id:type` 成为分类和移动的核心标识。为了降低风险，保留旧 `videos`、`session_video_sources`、`avid` 字段的兼容读取；方案项不新增第二张并行表，而是在现有 `classification_plan_items` 上迁移出 `resource_id/resource_type` 语义，避免新旧方案表同步问题。

**技术栈：** FastAPI、SQLite、httpx、OpenAI 兼容聊天接口、原生 JavaScript、SSE、pytest、respx、Windows bat 启动脚本。

---

## 一、需求拆解与当前判断

1. 图 1 中进度条一直是 0%，到最后突然 100%：当前 `core/session.py` 在采集分页过程中发送 `progress: None`，前端 `static/app.js` 只在 `progress != null` 时更新百分比，因此采集阶段即使 `source_total=266`、`scanned=39` 也不会更新条形进度。AI 分类阶段也只在调用 AI 前后发送 0% 和 100%，中间批次没有进度事件。
2. 图 2 中多个“控制台窗口主机”各占 12% 左右 CPU：单个正常空闲的 BiBiTool 不应长期占用这么高 CPU。截图更像是重复启动多个控制台/服务实例，或者控制台频繁输出导致 `conhost.exe` 消耗 CPU。计划中需要增加单实例启动保护、减少访问日志/控制台刷屏，并提供运行时诊断。
3. 预览分类方案页面的跳过条目要支持折叠：当前 `renderSkippedPanel()` 会展示明细，但没有折叠状态，需要支持整个面板折叠，最好再按跳过原因分组折叠。
4. 用户原文编号跳过了 4，本文保留该编号空缺，不新增解释性需求。
5. 非视频类型也要智能分类：当前 `core/bilibili_api.py` 把 `type != 2` 直接作为 `non_video_type` 跳过，且 `move_videos()` 固定拼接 `avid:2`。这与“智能整理不是只针对视频”冲突，需要把收藏资源统一建模为 `(resource_id, resource_type)`，AI 分类输入也要包含资源类型、标题、UP、分区、简介等可用元数据。
6. 增加退出 B 站账号功能：后端已有 `POST /api/logout`，但前端账号管理页没有退出按钮，也没有处理退出后的界面跳转与当前账号状态刷新。
7. AI 每批分类条数需要可配置：当前计划默认按 50 个条目一批调用 AI，对大量收藏夹会显得偏保守。应在设置页增加“每批分类条数”，保存到配置中，整理时从配置读取。默认值设为 100，允许范围 10-200；旧配置没有该字段时按 100 处理，超出范围时后端校验拒绝或兜底裁剪。

## 二、文件变更地图

- 修改 `core/bilibili_api.py`
  - 新增通用收藏资源分页方法 `get_folder_resource_pages()`，保留 `get_folder_video_pages()` 兼容调用。
  - 新增通用移动方法 `move_resources(src_media_id, tar_media_id, resources)`，保留 `move_videos()` 包装。
  - 非视频资源不再因 `type != 2` 进入 skipped；只有失效、无资源 ID 等不可处理项进入 skipped。

- 修改 `core/storage.py`
  - 新增 `favorite_resources`、`session_resource_sources`。
  - 迁移 `classification_plan_items` 为 `(version_id, resource_id, resource_type)` 主键，保留 `avid` 兼容列；迁移 `failed_items` 增加 `resource_id/resource_type`。
  - 增加资源级 CRUD：`upsert_resource()`、`list_resources_by_keys()`、`add_session_resource_source()`、`list_session_resource_sources()`。
  - 增加方案项资源级 API：`adjust_plan_resource_item()`、`mark_plan_resource_item_executed()`，旧 `adjust_plan_item()` 作为 `resource_type=2` 包装保留。
  - 增加 `deactivate_account(account_id)`，退出账号时清除当前激活状态。
  - 保留旧视频表兼容读取，避免旧会话无法打开。

- 修改 `core/ai_classifier.py`
  - 将 `VideoInfo` 扩展为通用 `ResourceInfo`，或新增 `ResourceInfo` 并让视频走同一路径。
  - AI prompt 明确说明输入可能包含视频、合集、音频、课程、专栏等收藏资源，分类依据优先使用标题、UP、分区、类型、简介和标签。
  - 保留批量分类能力，允许 `ClassifySession` 按配置的批大小推进进度。

- 修改 `core/session.py`
  - 采集阶段按 `scanned / source_total` 发送可计算进度。
  - AI 分类阶段按设置中的 `ai_batch_size` 分批，并按批次发送可计算进度。
  - 分类、预览、微调、执行、失败重试从 `avid` 过渡到 `(resource_id, resource_type)`，旧视频会话继续可读。
  - 执行阶段按 `(category, source_fid)` 分组后调用 `move_resources()`。

- 修改 `main.py`
  - `ConfigIn` 增加 `ai_batch_size`，`GET /api/config` 返回该值，`POST /api/config` 校验范围。
  - SSE 复用连接时从数据库 stats 恢复可显示进度。
  - 启动时降低高频访问日志噪音，优先过滤 `/stream`、`/login/poll` 等高频路径，而不是完全丢失全部调试信息。
  - 增加运行时诊断接口 `GET /api/runtime`，返回当前进程 PID、运行任务数、待登录会话数、服务启动时间。
  - 保留并强化 `POST /api/logout` 行为，确保退出当前账号后清理对应 Cookie，并清除 `accounts.is_active`。

- 修改 `static/app.js`
  - 设置页读取、保存 `ai_batch_size`。
  - 进度条支持 `progress` 数值、`scanned/source_total` 推导和阶段内文案。
  - 跳过项面板支持折叠与按原因折叠；折叠交互复用已加载数据，不因每次展开/收起重复请求接口。
  - 预览页、结果页文案从“视频”改为“条目/收藏条目”，避免非视频被误导。
  - 账号管理页增加“退出当前账号”按钮，调用 `/api/logout` 后回到登录页或刷新账号列表。

- 修改 `static/index.html`
  - 设置页增加“每批分类条数”数字输入。
  - 账号页增加退出按钮区域。
  - 必要时为跳过项折叠按钮增加稳定 DOM 容器。

- 修改 `start.bat`、`启动.bat`
  - 启动前检测 `127.0.0.1:8765` 是否已经有服务；若已有服务，只打开浏览器，不再启动第二个 Python/控制台实例。
  - 设置 UTF-8 代码页，保留当前虚拟环境选择逻辑。

- 修改测试
  - `tests/test_bilibili_api.py`
  - `tests/test_session.py`
  - `tests/test_storage.py`
  - `tests/test_ai_classifier.py`
  - `tests/test_frontend_static.py`
  - `tests/test_main_stream.py`

## 三、实施任务

### Task 1：进度条按真实采集进度更新

**Files:**
- Modify: `core/session.py`
- Modify: `static/app.js`
- Test: `tests/test_session.py`
- Test: `tests/test_frontend_static.py`

- [ ] **Step 1：写后端失败测试**

在 `tests/test_session.py` 增加测试，模拟 `source_total=266`、第一页 `raw_count=39`、`usable_count=33`、`skipped_count=6`，断言采集阶段会发送约 `39 / 266` 的进度。

```python
@pytest.mark.asyncio
async def test_collect_emits_numeric_progress_from_scanned_total(deps):
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
```

- [ ] **Step 2：运行测试确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_session.py::test_collect_emits_numeric_progress_from_scanned_total -q`

Expected: 当前实现只发 `progress=None`，测试失败。

- [ ] **Step 3：实现后端采集进度**

在 `core/session.py` 采集分页后计算：

```python
progress = None
if source_total:
    progress = min(0.99, scanned / source_total)
await _emit_progress(on_progress, {
    "stage": "collecting",
    "progress": progress,
    "source_fid": source_fid,
    "collected": collected,
    "scanned": scanned,
    "skipped": skipped,
    "source_total": source_total,
})
```

保留最终 `progress: 1.0` 事件，避免最后一页因 B 站总数波动无法到 100%。

- [ ] **Step 4：补前端兜底测试**

在 `tests/test_frontend_static.py` 增加静态断言：

```python
def test_frontend_progress_can_derive_percent_from_scanned_source_total():
    assert "deriveProgressPercent" in APP_JS
    assert "d.scanned" in APP_JS
    assert "d.source_total" in APP_JS
```

- [ ] **Step 5：实现前端兜底**

在 `static/app.js` 增加：

```javascript
function deriveProgressPercent(d) {
  if (typeof d.progress === 'number') return Math.max(0, Math.min(100, Math.round(d.progress * 100)));
  if (d.source_total && d.scanned != null) return Math.max(0, Math.min(99, Math.round((d.scanned / d.source_total) * 100)));
  return null;
}
```

在 `updateProgress(d)` 中统一使用 `deriveProgressPercent(d)`，这样即使后端复用连接时发了 `progress:null`，前端也能根据 `scanned/source_total` 更新。

- [ ] **Step 6：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_session.py::test_collect_emits_numeric_progress_from_scanned_total tests\test_frontend_static.py -q
```

Expected: 相关测试通过。

Optional: 如果本机安装了 Node.js，可额外执行 `node --check static\app.js` 做 JS 语法检查；该检查不能作为必须通过的唯一验证条件。

### Task 2：AI 分类阶段按可配置批次推进进度

**Files:**
- Modify: `core/ai_classifier.py`
- Modify: `core/session.py`
- Modify: `main.py`
- Modify: `static/index.html`
- Modify: `static/app.js`
- Test: `tests/test_session.py`
- Test: `tests/test_ai_classifier.py`
- Test: `tests/test_frontend_static.py`
- Test: `tests/test_main_stream.py`

- [ ] **Step 1：写失败测试**

在 `tests/test_session.py` 增加测试，模拟 120 个条目、批大小 50，断言分类阶段至少出现 0%、约 42%、约 83%、100%。

```python
@pytest.mark.asyncio
async def test_classify_emits_batch_progress(deps):
    storage, bili, ai = deps
    storage.save_config({
        "ai_base_url": "http://x",
        "ai_api_key": "k",
        "ai_model": "m",
        "default_privacy": 1,
        "ai_batch_size": 50,
    })
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    for avid in range(1, 121):
        storage.upsert_video({
            "avid": avid, "bvid": f"BV{avid}", "title": f"条目{avid}", "intro": "",
            "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
        })
        storage.add_session_video_source(sid, avid=avid, source_fid=100)

    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def classify_batch(videos):
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
```

再增加一个测试，确认会话会读取设置中的 `ai_batch_size` 并传给 `AiClassifier.classify()`。这个测试要独立于上面的进度测试，避免只验证了进度、没有验证用户配置真正生效。

```python
@pytest.mark.asyncio
async def test_classify_uses_configured_ai_batch_size(deps):
    storage, bili, ai = deps
    storage.save_config({
        "ai_base_url": "http://x",
        "ai_api_key": "k",
        "ai_model": "m",
        "default_privacy": 1,
        "ai_batch_size": 120,
    })
    sid = storage.create_session(source_fid=100, mode="quick")
    storage.update_session_status(sid, "classifying")
    for avid in range(1, 4):
        storage.upsert_video({
            "avid": avid, "bvid": f"BV{avid}", "title": f"条目{avid}", "intro": "",
            "tags": "[]", "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "fid": 100,
        })
        storage.add_session_video_source(sid, avid=avid, source_fid=100)

    seen = {}

    async def classify(videos, batch_size=50, on_progress=None):
        seen["batch_size"] = batch_size
        if on_progress:
            await on_progress({"stage": "classifying", "progress": 1.0, "classified": len(videos), "total": len(videos)})
        return [Classification(v.avid, "动画", 0.9, "测试") for v in videos]

    ai.classify = classify

    await ClassifySession(storage, bili, ai).classify(sid, on_progress=lambda e: None)

    assert seen["batch_size"] == 120
```

在 `tests/test_frontend_static.py` 增加设置页静态测试：

```python
def test_frontend_config_supports_ai_batch_size():
    assert "config-ai-batch-size" in INDEX_HTML
    assert "ai_batch_size" in APP_JS
```

在 `tests/test_main_stream.py` 增加配置接口测试：

```python
@pytest.mark.asyncio
async def test_config_api_accepts_ai_batch_size(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)

    await main.api_save_config(main.ConfigIn(
        ai_base_url="http://x",
        ai_api_key="k",
        ai_model="m",
        ai_batch_size=120,
    ))

    cfg = await main.api_get_config()
    assert cfg["ai_batch_size"] == 120
```

- [ ] **Step 2：运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_session.py::test_classify_emits_batch_progress tests\test_session.py::test_classify_uses_configured_ai_batch_size tests\test_frontend_static.py::test_frontend_config_supports_ai_batch_size tests\test_main_stream.py::test_config_api_accepts_ai_batch_size -q
```

Expected: 当前只有 0 和 1，且配置页没有 `config-ai-batch-size`，测试失败。

- [ ] **Step 3：实现配置字段与设置页输入**

在 `main.py` 中给 `ConfigIn` 增加校验字段。需要把 pydantic import 改为：

```python
from pydantic import BaseModel, Field
```

并把 `ConfigIn` 改为：

```python
class ConfigIn(BaseModel):
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    default_privacy: int = 1
    ai_batch_size: int = Field(default=100, ge=10, le=200)
```

`api_get_config()` 返回旧配置时要补默认值。不要把函数改名为 `api_config`，当前路由函数名是 `api_get_config()`，测试也应调用这个名字：

```python
return {
    "configured": True,
    "ai_base_url": cfg["ai_base_url"],
    "ai_model": cfg["ai_model"],
    "ai_batch_size": int(cfg.get("ai_batch_size", 100)),
}
```

在 `static/index.html` 的模型名称输入下方增加数字输入：

```html
<div class="flex flex-col">
  <label class="text-sm font-medium mb-2" style="color: var(--foreground);">每批分类条数</label>
  <div class="field" style="width:100%;min-width:0;">
    <i data-lucide="list-ordered" class="w-[18px] h-[18px] shrink-0" style="color: var(--icon-muted);"></i>
    <input data-dom-id="config-ai-batch-size" class="control" type="number" min="10" max="200" step="10" value="100">
  </div>
</div>
```

在 `static/app.js` 的 `loadConfig()` 中读取和保存：

```javascript
const batchInput = $('config-ai-batch-size');
if (batchInput) batchInput.value = cfg.ai_batch_size || 100;
```

保存时加入：

```javascript
ai_batch_size: Number($('config-ai-batch-size').value || 100),
```

- [ ] **Step 4：实现批次分类进度并使用设置值**

不要把 AI 分批策略复制到 `ClassifySession.classify()`，否则容易丢掉 `AiClassifier.classify()` 内已有的 `merge_categories()` 合并逻辑。改为给 `AiClassifier.classify()` 增加可选进度回调参数，并保持合并逻辑在 `core/ai_classifier.py` 内部。

在 `core/ai_classifier.py` 中把签名改为：

```python
import inspect


async def classify(self, videos: list[VideoInfo], batch_size: int = 50, on_progress=None) -> list[Classification]:
    results: list[Classification] = []
    total = len(videos)
    for i in range(0, total, batch_size):
        batch = videos[i:i + batch_size]
        results.extend(await self.classify_batch(batch))
        if on_progress:
            event = {
                "stage": "classifying",
                "progress": len(results) / total if total else 1.0,
                "classified": len(results),
                "total": total,
            }
            maybe = on_progress(event)
            if inspect.isawaitable(maybe):
                await maybe

    if results:
        cats = list({c.category for c in results if c.category != "未分类"})
        if len(cats) > 10:
            mapping = await self.merge_categories(cats)
            for c in results:
                if c.category in mapping:
                    c.category = mapping[c.category]
    return results
```

在 `core/session.py` 中只负责包装事件，补充 `source_total` 和 `skipped` 后透传给上层 SSE：

```python
def _ai_batch_size(self) -> int:
    cfg = self.storage.load_config() or {}
    try:
        value = int(cfg.get("ai_batch_size", 100))
    except (TypeError, ValueError):
        value = 100
    return max(10, min(200, value))

async def ai_progress(event: dict):
    await _emit_progress(on_progress, {
        **event,
        "source_total": prior_stats.get("source_total"),
        "skipped": prior_stats.get("skipped_total", 0),
    })

results = await self.ai.classify(videos, batch_size=self._ai_batch_size(), on_progress=ai_progress)
```

这样分类批次进度可以推进，用户设置可以生效，同时 `merge_categories()` 仍由 `AiClassifier` 统一维护。旧配置没有 `ai_batch_size` 时默认按 100；如果配置文件被手动写成非法值，`ClassifySession` 兜底裁剪到 10-200。

- [ ] **Step 5：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_session.py::test_classify_emits_batch_progress tests\test_session.py::test_classify_uses_configured_ai_batch_size tests\test_ai_classifier.py tests\test_frontend_static.py::test_frontend_config_supports_ai_batch_size tests\test_main_stream.py::test_config_api_accepts_ai_batch_size -q
```

Expected: 分类进度测试、批大小配置测试、AI 分类测试和前端静态测试通过。

### Task 3：CPU 异常治理与单实例启动保护

**Files:**
- Modify: `main.py`
- Modify: `start.bat`
- Modify: `启动.bat`
- Test: `tests/test_main_stream.py`

- [ ] **Step 1：明确判断标准**

把以下结论写入实现备注或 README：单个空闲服务长期 CPU 高于 10% 不正常；截图中多个“控制台窗口主机”同时占用 CPU，优先怀疑重复启动多个实例或控制台输出过多。

- [ ] **Step 2：降低访问日志噪音但保留调试能力**

不要直接把全部 access log 永久关闭。优先过滤高频轮询/SSE 路径，保留普通 API 的请求日志，便于后续定位问题。修改 `main.py`，增加日志过滤器：

```python
import logging
import re


class UvicornAccessFilter(logging.Filter):
    HIGH_FREQ_PATHS = (
        re.compile(r"GET /api/session/.*/stream"),
        re.compile(r"GET /api/accounts/login/poll"),
        re.compile(r"GET /api/qrcode/poll"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(pattern.search(message) for pattern in self.HIGH_FREQ_PATHS)


logging.getLogger("uvicorn.access").addFilter(UvicornAccessFilter())
```

底部启动保留 access log：

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, access_log=True)
```

如果过滤器在当前 uvicorn 版本下不能稳定过滤，再退而求其次使用 `log_level="warning"`，并把这个选择写入实现说明。

- [ ] **Step 3：增加运行时诊断接口**

在 `main.py` 增加服务启动时间：

```python
from datetime import datetime
import os

STARTED_AT = datetime.now().isoformat(timespec="seconds")
```

增加接口：

```python
@app.get("/api/runtime")
async def api_runtime():
    return {
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "running_pipelines": len(_running_pipelines),
        "pending_logins": len(_pending_logins),
    }
```

- [ ] **Step 4：启动脚本检测已有服务**

在 `start.bat` 和 `启动.bat` 的“正在启动服务”之前加入端口探测：

```bat
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/runtime; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [√] BiBiTool 已在运行，正在打开浏览器
    start "" "http://127.0.0.1:8765"
    exit /b 0
)
```

再继续启动 Python 服务。这样用户重复双击启动脚本时不会产生多个控制台服务。

- [ ] **Step 5：写接口测试**

在 `tests/test_main_stream.py` 增加：

```python
@pytest.mark.asyncio
async def test_runtime_endpoint_reports_process_state():
    data = await main.api_runtime()
    assert data["pid"] > 0
    assert "started_at" in data
    assert "running_pipelines" in data
    assert "pending_logins" in data
```

- [ ] **Step 6：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_main_stream.py -q
.\.venv\Scripts\python.exe -m py_compile main.py
```

Expected: 测试通过；重复运行 `start.bat` 时第二次只打开浏览器，不再启动第二个服务。

### Task 4：跳过条目支持折叠

**Files:**
- Modify: `static/app.js`
- Modify: `static/index.html` if extra container is needed
- Test: `tests/test_frontend_static.py`

- [ ] **Step 1：写静态失败测试**

在 `tests/test_frontend_static.py` 增加：

```python
def test_frontend_skipped_panel_supports_collapse():
    assert "collapsedSkippedReasons" in APP_JS
    assert "toggleSkippedReason" in APP_JS
    assert "toggle-skipped-panel" in APP_JS
    assert "toggle-skipped-reason-" in APP_JS
```

- [ ] **Step 2：实现折叠状态**

在 `static/app.js` 顶部增加：

```javascript
let skippedPanelCollapsed = false;
const collapsedSkippedReasons = new Set();
let cachedSkippedItems = null;
```

新增方法：

```javascript
function toggleSkippedPanel() {
  skippedPanelCollapsed = !skippedPanelCollapsed;
  renderSkippedPanelFromItems(currentSid, cachedSkippedItems || []);
}

function toggleSkippedReason(reasonCode) {
  if (collapsedSkippedReasons.has(reasonCode)) collapsedSkippedReasons.delete(reasonCode);
  else collapsedSkippedReasons.add(reasonCode);
  renderSkippedPanelFromItems(currentSid, cachedSkippedItems || []);
}
```

- [ ] **Step 3：按原因分组渲染**

把 `renderSkippedPanel()` 拆成“请求数据”和“纯渲染”两层，折叠时只走纯渲染，不重新请求接口：

```javascript
async function renderSkippedPanel(sid) {
  const data = await api(`/api/session/${sid}/skipped-items`);
  cachedSkippedItems = data.items || [];
  renderSkippedPanelFromItems(sid, cachedSkippedItems);
}

function renderSkippedPanelFromItems(sid, items) {
  // 后续分组渲染逻辑放这里
}
```

在 `renderSkippedPanelFromItems()` 中把 `items` 分组：

```javascript
const byReason = {};
items.forEach(it => {
  const key = it.reason_code || 'unknown';
  byReason[key] = byReason[key] || [];
  byReason[key].push(it);
});
```

面板标题增加折叠按钮 `data-dom-id="toggle-skipped-panel"`；每个原因组标题增加 `data-dom-id="toggle-skipped-reason-${escapeDomId(reasonCode)}"`。折叠时隐藏明细，但保留总数、可移除数和删除按钮状态。

删除跳过项成功后必须清空缓存再重新加载：

```javascript
cachedSkippedItems = null;
renderSkippedPanel(sid);
```

- [ ] **Step 4：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_frontend_static.py::test_frontend_skipped_panel_supports_collapse -q
```

Expected: 测试通过。

Optional: 如果本机安装了 Node.js，可额外执行 `node --check static\app.js` 做 JS 语法检查。

### Task 5：把非视频收藏资源纳入智能分类

**Files:**
- Modify: `core/storage.py`
- Modify: `core/bilibili_api.py`
- Modify: `core/ai_classifier.py`
- Modify: `core/session.py`
- Modify: `static/app.js`
- Test: `tests/test_storage.py`
- Test: `tests/test_bilibili_api.py`
- Test: `tests/test_ai_classifier.py`
- Test: `tests/test_session.py`

- [ ] **Step 1：新增通用资源表测试**

在 `tests/test_storage.py` 增加：

```python
def test_favorite_resources_are_keyed_by_account_id_resource_id_type(tmp_path):
    storage = Storage(tmp_path)
    storage.upsert_resource({
        "account_id": "acc1", "resource_id": 10, "resource_type": 2,
        "bvid": "BV10", "title": "视频", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "动画", "source_fid": 100,
    })
    storage.upsert_resource({
        "account_id": "acc1", "resource_id": 10, "resource_type": 11,
        "bvid": "", "title": "合集", "intro": "", "tags": "[]",
        "up_name": "UP", "up_mid": 1, "cover_url": "", "tname": "合集", "source_fid": 100,
    })

    rows = storage.list_resources_by_keys([
        {"resource_id": 10, "resource_type": 2},
        {"resource_id": 10, "resource_type": 11},
    ], account_id="acc1")
    assert {(r["resource_id"], r["resource_type"]) for r in rows} == {(10, 2), (10, 11)}
```

- [ ] **Step 2：实现通用资源表、方案项迁移和失败项迁移**

在 `_SCHEMA` 中新增通用资源表和会话资源来源表：

```sql
CREATE TABLE IF NOT EXISTS favorite_resources (
  account_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER,
  bvid TEXT,
  title TEXT,
  intro TEXT,
  tags TEXT,
  up_name TEXT,
  up_mid INTEGER,
  cover_url TEXT,
  tname TEXT,
  source_fid INTEGER,
  cached_at TEXT,
  PRIMARY KEY (account_id, resource_id, resource_type)
);
CREATE TABLE IF NOT EXISTS session_resource_sources (
  session_id TEXT,
  resource_id INTEGER,
  resource_type INTEGER,
  source_fid INTEGER,
  moved INTEGER DEFAULT 0,
  move_error TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (session_id, resource_id, resource_type, source_fid)
);
```

不要新增 `classification_plan_resource_items` 双表。直接迁移现有 `classification_plan_items`，让旧方案项和新资源方案项都走同一张表。

注意：SQLite 不能用 `ALTER TABLE` 修改主键，`CREATE TABLE IF NOT EXISTS` 也不会改变已存在表的主键。因此不能只改 `_SCHEMA`，必须像当前 `videos`、`fav_folders` 的迁移一样重建旧表。

先把 `_SCHEMA` 中的 `classification_plan_items` 定义改为：

```sql
CREATE TABLE IF NOT EXISTS classification_plan_items (
  version_id TEXT,
  avid INTEGER,
  resource_id INTEGER,
  resource_type INTEGER DEFAULT 2,
  category TEXT,
  confidence REAL,
  reason TEXT,
  adjusted INTEGER DEFAULT 0,
  executed INTEGER DEFAULT 0,
  PRIMARY KEY (version_id, resource_id, resource_type)
);
```

在 `_migrate_schema()` 中调用：

```python
self._migrate_plan_items_to_resource_key(conn)
self._migrate_failed_items_to_resource_key(conn)
```

新增迁移方法：

```python
def _migrate_plan_items_to_resource_key(self, conn: sqlite3.Connection) -> None:
    pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)") if row["pk"]]
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(classification_plan_items)")}
    if pk_cols == ["version_id", "resource_id", "resource_type"] and "resource_id" in cols:
        return
    need_backup = self._db_path.exists() and self._db_path.stat().st_size > 0
    if need_backup:
        self._backup_database()
    conn.execute("ALTER TABLE classification_plan_items RENAME TO classification_plan_items_legacy")
    conn.execute(
        "CREATE TABLE classification_plan_items ("
        "version_id TEXT, avid INTEGER, resource_id INTEGER, resource_type INTEGER DEFAULT 2, "
        "category TEXT, confidence REAL, reason TEXT, adjusted INTEGER DEFAULT 0, executed INTEGER DEFAULT 0, "
        "PRIMARY KEY (version_id, resource_id, resource_type))"
    )
    if "resource_id" in cols and "resource_type" in cols:
        conn.execute(
            "INSERT OR REPLACE INTO classification_plan_items "
            "(version_id, avid, resource_id, resource_type, category, confidence, reason, adjusted, executed) "
            "SELECT version_id, COALESCE(avid, resource_id), resource_id, COALESCE(resource_type, 2), "
            "category, confidence, reason, adjusted, executed FROM classification_plan_items_legacy"
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO classification_plan_items "
            "(version_id, avid, resource_id, resource_type, category, confidence, reason, adjusted, executed) "
            "SELECT version_id, avid, avid, 2, category, confidence, reason, adjusted, executed "
            "FROM classification_plan_items_legacy"
        )
    conn.execute("DROP TABLE classification_plan_items_legacy")
```

同时迁移 `failed_items`，避免非视频失败重试时丢失类型：

```python
def _migrate_failed_items_to_resource_key(self, conn: sqlite3.Connection) -> None:
    if not self._has_column(conn, "failed_items", "resource_id"):
        conn.execute("ALTER TABLE failed_items ADD COLUMN resource_id INTEGER")
    if not self._has_column(conn, "failed_items", "resource_type"):
        conn.execute("ALTER TABLE failed_items ADD COLUMN resource_type INTEGER DEFAULT 2")
    conn.execute("UPDATE failed_items SET resource_id = avid WHERE resource_id IS NULL")
```

新增或调整方法：

```python
def upsert_resource(self, resource: dict) -> None: ...
def list_resources_by_keys(self, keys: list[dict], account_id: str | None = None) -> list[dict]: ...
def add_session_resource_source(self, session_id: str, resource_id: int, resource_type: int, source_fid: int) -> None: ...
def list_session_resource_sources(self, session_id: str) -> list[dict]: ...
def mark_session_resource_source_moved(self, session_id: str, resource_id: int, resource_type: int, source_fid: int, moved: bool, error: str = "") -> None: ...
def adjust_plan_resource_item(self, version_id: str, resource_id: int, resource_type: int, new_category: str) -> None: ...
def mark_plan_resource_item_executed(self, version_id: str, resource_id: int, resource_type: int, executed: bool) -> None: ...
```

兼容包装：

```python
def adjust_plan_item(self, version_id: str, avid: int, new_category: str) -> None:
    self.adjust_plan_resource_item(version_id, avid, 2, new_category)

def mark_plan_item_executed(self, version_id: str, avid: int, executed: bool) -> None:
    self.mark_plan_resource_item_executed(version_id, avid, 2, executed)
```

`create_plan_version(items=...)` 统一写 `classification_plan_items`。如果 item 只有 `avid`，则补 `resource_id=avid, resource_type=2`；如果 item 已有 `resource_id/resource_type`，则按资源键写入，同时视频资源也保留 `avid=resource_id`。`load_plan_items(version_id)` 统一返回 `resource_id`、`resource_type`、`avid`，其中旧视频项 `avid == resource_id`。

旧 `videos` 和 `session_video_sources` 不删除；打开旧会话时继续回退读取旧表。

- [ ] **Step 3：B 站 API 改为拉取资源**

在 `tests/test_bilibili_api.py` 增加：

```python
@respx.mock
async def test_get_folder_resource_pages_includes_non_video_resources(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 2}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "视频", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "动画"},
            {"id": 2, "bvid": "", "title": "合集", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 11, "tname": "合集"},
        ], "has_more": False},
    }))

    pages = []
    async for page in client.get_folder_resource_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(page)

    assert pages[0]["usable_count"] == 2
    assert {(r["resource_id"], r["resource_type"]) for r in pages[0]["resources"]} == {(1, 2), (2, 11)}
    assert pages[0]["skipped_count"] == 0
```

实现 `get_folder_resource_pages()`：`attr & 1` 和 `not id` 仍进入 skipped；`type != 2` 不再跳过，生成资源：

```python
{
    "resource_id": m["id"],
    "resource_type": m.get("type", 2),
    "avid": m["id"] if m.get("type", 2) == 2 else 0,
    "bvid": m.get("bvid", ""),
    "title": m.get("title", ""),
    "intro": "",
    "tags": "[]",
    "up_name": m.get("upper", {}).get("name", ""),
    "up_mid": m.get("upper", {}).get("mid", 0),
    "cover_url": m.get("cover", ""),
    "tname": m.get("tname", ""),
    "source_fid": fid,
}
```

`get_folder_resource_pages()` 的统计语义：

- `raw_count` = B 站本页返回的全部 `medias` 数量。
- `usable_count` = 可进入智能分类的全部收藏资源数量，包含 `resource_type != 2` 的合集、音频、课程、专栏等。
- `skipped_count` = 只统计 `attr_invalid`、`no_id` 等确实不可处理资源，不再因为 `type != 2` 跳过。

`get_folder_video_pages()` 保留为兼容包装，内部调用 `get_folder_resource_pages()` 后只返回 `resource_type == 2` 的旧格式数据。为了不破坏旧测试和旧调用者，它的统计语义保持旧视角：

- `raw_count` = B 站本页返回的全部 `medias` 数量。
- `usable_count` = 可用视频数量。
- `skipped_count` = `raw_count - usable_count`，从旧视频视角看非视频仍属于“被视频流程跳过”。
- `skipped_reasons` 中可以继续包含 `non_video_type`，但只用于旧兼容接口；新整理流程不得再用它判断非视频不可处理。

- [ ] **Step 4：移动接口支持任意资源类型**

在 `tests/test_bilibili_api.py` 增加：

```python
@respx.mock
async def test_move_resources_uses_id_type_pairs(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    respx.post(f"{API_BASE}/x/v3/fav/resource/move").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"success": True}})
    )

    ok = await client.move_resources(
        src_media_id=100,
        tar_media_id=999,
        resources=[{"id": 1001, "type": 2}, {"id": 2002, "type": 11}],
    )

    assert ok is True
    body = respx.calls.last.request.content.decode()
    assert ("1001%3A2" in body or "1001:2" in body)
    assert ("2002%3A11" in body or "2002:11" in body)
```

实现：

```python
async def move_resources(self, src_media_id: int, tar_media_id: int, resources: list[dict]) -> bool:
    resource_text = ",".join(f"{r['id']}:{r.get('type', 2)}" for r in resources)
    await self._post_form(
        "/x/v3/fav/resource/move",
        {"src_media_id": src_media_id, "tar_media_id": tar_media_id, "resources": resource_text, "platform": "web"},
    )
    return True
```

`move_videos()` 改为调用 `move_resources()`。

- [ ] **Step 5：AI 输入支持资源类型**

在 `core/ai_classifier.py` 新增或替换为：

```python
@dataclass
class ResourceInfo:
    resource_id: int
    resource_type: int
    resource_type_name: str
    title: str
    up_name: str
    tname: str
    intro: str = ""
    tags: list[str] = field(default_factory=list)
```

prompt 中加入：

```text
输入条目可能是视频、合集、音频、课程、专栏或其他 B 站收藏资源。
resource_type 和 resource_type_name 表示 B 站收藏资源类型；不要因为不是视频就归为“其他”，应根据标题、UP、分区、类型名称、简介、标签推断主题。
常见 resource_type 映射：2=视频，11=合集，12=音频，21=课程，31=专栏；其他数字视为未知类型，但仍要根据标题等元数据分类。
输出必须包含 resource_id 和 resource_type。
```

新增测试：非视频 `ResourceInfo(resource_type=11, resource_type_name="合集", title="Python 学习合集")` 应被传给 AI，并能返回分类。测试还要断言发给 AI 的 JSON 中包含 `resource_type_name`，避免模型只看到数字。

- [ ] **Step 6：会话流程改用资源**

`ClassifySession.collect()` 优先调用 `get_folder_resource_pages()`，保存到 `favorite_resources`，写入 `session_resource_sources`。`ClassifySession.classify()` 优先读取 `session_resource_sources` 和 `favorite_resources`，构造 `ResourceInfo`；旧会话没有资源表记录时回退旧 `session_video_sources`。

`mode == "full"` 的 enrich 规则必须明确：

- `resource_type == 2` 且有 `bvid`：继续调用现有 `_enrich_video()` / `get_video_info()` 获取简介和标签。
- `resource_type != 2`：本轮暂不调用额外详情 API，使用收藏夹分页接口返回的基础字段 `title/up_name/cover_url/tname/resource_type_name` 参与分类。
- 后续如要支持合集、课程、专栏详情，再单独新增 `get_resource_info(resource_id, resource_type)`，不要在本轮猜接口。

`ClassifySession.execute()` 中分组值从 `list[int]` 改为资源列表：

```python
move_groups: dict[tuple[str, int], list[dict]] = {}
key = (cat, src["source_fid"])
move_groups.setdefault(key, []).append({"id": item["resource_id"], "type": item["resource_type"]})
```

执行时调用：

```python
await self.bili.move_resources(src_media_id=sf, tar_media_id=target_fid, resources=chunk)
```

失败记录可以继续写 `failed_items`，但要新增 `resource_id`、`resource_type` 字段；如果暂时保留 `avid`，非视频写 `avid=resource_id`，同时必须保存 `resource_type`，避免同 ID 不同类型混淆。

`retry_failed()` 和 `_retry_failed_sources()` 必须按 `(resource_id, resource_type)` 匹配 plan item 和 source item，不能只用 `avid`。兼容旧失败记录时，如果 `resource_id` 为空，则使用 `avid` 且 `resource_type=2`。

预览页手动调整分类的 API 也要升级。`AdjustIn` 请求体统一为：

```python
class AdjustIn(BaseModel):
    resource_id: int | None = None
    resource_type: int = 2
    avid: int | None = None
    new_category: str

    def normalized_resource_id(self) -> int:
        return self.resource_id if self.resource_id is not None else int(self.avid)
```

后端 `adjust_item()` 签名改为：

```python
def adjust_item(self, sid: str, resource_id: int, resource_type: int, new_category: str) -> None:
    ...
```

同时必须修改 `main.py` 的调整分类路由，不能继续调用旧的 `mgr.adjust_item(sid, payload.avid, payload.new_category)`：

```python
@app.post("/api/session/{sid}/adjust")
async def api_adjust(sid: str, payload: AdjustIn):
    mgr = get_session_mgr()
    mgr.adjust_item(sid, payload.normalized_resource_id(), payload.resource_type, payload.new_category)
    return {"ok": True}
```

旧前端如果仍传 `avid`，后端按 `resource_type=2` 兼容；新前端必须传 `{resource_id, resource_type, new_category}`。`renderReview()` 的下拉框 DOM id 应改为 `adj-${resource_id}-${resource_type}`，避免同 ID 不同类型冲突。

- [ ] **Step 7：前端文案改成“条目”**

把进度、预览、结果页中面向用户的“视频”改成“条目”或“收藏条目”。保留 B 站接口内部变量名时不影响用户，但新代码应优先使用 resource 命名。

- [ ] **Step 8：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_storage.py tests\test_bilibili_api.py tests\test_ai_classifier.py tests\test_session.py -q
.\.venv\Scripts\python.exe -m py_compile core\storage.py core\bilibili_api.py core\ai_classifier.py core\session.py
```

Expected: 所有资源相关测试通过；旧视频测试仍通过。

### Task 6：退出 B 站账号功能

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `main.py`
- Test: `tests/test_frontend_static.py`
- Test: `tests/test_main_stream.py`

- [ ] **Step 1：写前端静态测试**

在 `tests/test_frontend_static.py` 增加：

```python
def test_frontend_account_logout_ui():
    assert "account-logout" in INDEX_HTML
    assert "logoutAccount" in APP_JS
    assert "/api/logout" in APP_JS
```

- [ ] **Step 2：账号页增加退出按钮**

在 `static/index.html` 的账号管理页增加：

```html
<button data-dom-id="account-logout" type="button" class="btn btn-text" style="color: var(--state-error);">
  <i data-lucide="log-out" class="w-4 h-4"></i><span>退出当前账号</span>
</button>
```

- [ ] **Step 3：实现前端退出逻辑**

在 `static/app.js` 增加：

```javascript
async function logoutAccount() {
  if (!confirm('确认退出当前 B 站账号？本地 Cookie 会被清除，需要重新扫码登录。')) return;
  await api('/api/logout', { method: 'POST' });
  addAccountQrToken++;
  const navName = $('nav-account-name');
  if (navName) navName.textContent = '';
  showView('login');
  renderLogin();
}
```

在 `renderAccounts()` 末尾绑定：

```javascript
const logoutBtn = $('account-logout');
if (logoutBtn) logoutBtn.onclick = logoutAccount;
```

- [ ] **Step 4：后端退出行为补测试**

在 `tests/test_main_stream.py` 增加：已激活账号退出后，对应 cookie 文件被删除，当前账号激活状态被清空，`/api/state` 返回未登录。

```python
@pytest.mark.asyncio
async def test_logout_clears_active_account_cookie(monkeypatch, tmp_path):
    real_storage = Storage(tmp_path)
    monkeypatch.setattr(main, "storage", real_storage)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    cookie = tmp_path / "accounts/a1/bilibili_cookie.json"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text('{"SESSDATA":"s","DedeUserID":"1"}', encoding="utf-8")
    real_storage.upsert_account({
        "account_id": "a1", "mid": 1, "uname": "A", "avatar_url": "",
        "cookie_path": "accounts/a1/bilibili_cookie.json",
    })
    real_storage.activate_account("a1")

    await main.api_logout()

    assert not cookie.exists()
    assert real_storage.get_active_account() is None
    assert main.get_bili().is_logged_in is False
```

- [ ] **Step 5：实现后端账号激活状态清理**

在 `core/storage.py` 增加：

```python
def deactivate_account(self, account_id: str) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE accounts SET is_active = 0, updated_at = datetime('now') WHERE account_id = ?",
            (account_id,),
        )
```

在 `main.py` 的 `api_logout()` 中，删除当前账号 Cookie 后同步清理激活状态：

```python
active = storage.get_active_account()
if active:
    # existing cookie removal logic
    storage.deactivate_account(active["account_id"])
```

注意顺序：先取 active account，再删除 cookie 和清理状态；退出完成后 `get_active_account()` 必须返回 `None`，避免账号管理页继续把该账号显示为“当前账号”。

- [ ] **Step 6：验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_frontend_static.py::test_frontend_account_logout_ui tests\test_main_stream.py::test_logout_clears_active_account_cookie -q
```

Expected: 测试通过，退出后进入扫码登录页。

Optional: 如果本机安装了 Node.js，可额外执行 `node --check static\app.js` 做 JS 语法检查。

## 四、总体验收清单

- [ ] 启动程序后，在单个收藏夹 266 条、首批扫描 39 条时，进度条显示约 15%，不再停留 0%。
- [ ] AI 分类 100 条以上时，分类阶段进度按批次推进，不再到最后才跳 100%。
- [ ] 设置页可以配置“每批分类条数”，默认 100，允许 10-200；整理时实际调用 AI 使用该设置值，而不是固定 50。
- [ ] 重复双击 `start.bat` 或 `启动.bat` 不会产生多个 Python 服务；第二次只打开浏览器。
- [ ] 空闲状态下 CPU 不应长期高于 10%；若仍异常，可通过 `/api/runtime` 确认是否有多个服务实例或未结束任务。
- [ ] 预览页“跳过条目”可以整体折叠，也可以按原因折叠。
- [ ] B 站返回 `type != 2` 且 `attr` 正常、有 `id` 的收藏资源会进入 AI 分类和预览方案，不再作为 `non_video_type` 跳过。
- [ ] 执行整理时，非视频资源用 `id:type` 调用 `/x/v3/fav/resource/move`，不是硬编码 `id:2`。
- [ ] 失效资源、无 ID 资源仍进入跳过项；可安全移除的失效资源仍可一键移除。
- [ ] 账号管理页有“退出当前账号”，退出后本地 Cookie 清除，并进入扫码登录流程。
- [ ] 旧会话、旧视频数据仍能打开预览和执行，不因新增资源表报错。

## 五、建议执行顺序

1. 先执行 Task 3，降低 CPU 和重复启动风险，确保后续测试和人工验证环境稳定。
2. 再执行 Task 1 和 Task 2，解决用户最直观看到的进度问题；Task 2 必须通过 `AiClassifier.classify(batch_size=..., on_progress=...)` 保留原有 `merge_categories()` 合并逻辑，并让设置中的批大小真正生效。
3. 执行 Task 4 和 Task 6，这两项主要是交互与账号状态补齐，Task 6 必须清除 `accounts.is_active`。
4. 最后执行 Task 5。非视频资源分类涉及数据模型、AI 输入、B 站移动接口和旧数据兼容，是本轮最大改动，应单独完成并完整回归。

## 六、自检

- 进度条 0% 问题：Task 1、Task 2 覆盖。
- AI 每批分类条数可配置：Task 2 覆盖，包含配置接口、设置页输入和会话读取。
- CPU 异常与重复控制台进程：Task 3 覆盖。
- 跳过条目折叠：Task 4 覆盖。
- 非视频类型智能分类：Task 5 覆盖。
- 退出 B 站账号：Task 6 覆盖。
- 文档没有使用待定类占位写法；每个任务都有文件、测试、命令和预期结果。
