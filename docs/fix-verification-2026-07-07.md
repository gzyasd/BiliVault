# BiBiTool 修复与接口核验记录

- 日期：2026-07-07
- 目标：修复审查报告中的阻断问题，并核实当前使用的 B 站接口是否真实可访问。

## 已修复内容

1. 扫码登录轮询解析
   - 修复点：`core/bilibili_api.py`
   - 当前 B 站 poll 响应的顶层 `code` 为 0，扫码状态在 `data.code`。
   - 已支持：
     - `data.code=86101`：未扫码
     - `data.code=86090`：已扫码未确认
     - `data.code=86038`/`86039`：二维码过期或失效
     - `data.code=0`：登录成功并保存 cookie

2. SSE 进度推送
   - 修复点：`core/session.py`
   - 增加 `_emit_progress()`，同步回调和 async 回调都能正确执行。
   - 修复了 FastAPI `/api/session/{sid}/stream` 中 async queue 回调未被 await 的问题。

3. 完整模式
   - 修复点：`core/bilibili_api.py`、`core/session.py`、`core/ai_classifier.py`
   - 新增 `BilibiliClient.get_video_info(bvid)`，读取视频简介和标签。
   - full 模式会在采集阶段补齐 `intro` 和 `tags`，并写入 SQLite 缓存。
   - AI prompt 已包含 `intro` 字段。

4. 失败重试
   - 修复点：`core/storage.py`、`core/session.py`
   - 重试成功的失败项会被删除。
   - 重试仍失败的失败项会保留，并标记 `retried=1`，不再丢失失败详情。

5. 测试配置
   - 修复点：`pytest.ini`
   - `pytest` 默认只收集 `tests/`，不会再误收集 `_libs` 或虚拟环境中的第三方测试。

6. B 站请求头
   - 修复点：`core/bilibili_api.py`
   - `BilibiliClient` 统一使用浏览器 User-Agent 和 `Referer: https://www.bilibili.com/`。
   - 真实接口验证发现默认 `python-httpx` User-Agent 可能拿到非 JSON 页面，导致 `r.json()` 失败。

7. 二次自检新增修复
   - WBI 签名改为按 URL 编码后的 query 计算，避免带空格等特殊字符时签名错误。
   - WBI 缓存命中但接口返回 `-400/-403` 时，会清理缓存并重新拉取 WBI key 后重试一次。
   - 创建目标收藏夹失败时，不再让会话卡在 `executing`；失败视频会进入 `failed_items`。
   - 对因创建目标收藏夹失败而没有 `target_fid` 的失败项，重试时会先重新创建目标收藏夹，再移动视频。
   - 前端对 B 站标题、UP 主、AI 分类名、失败原因等外部文本做 HTML 转义。
   - 前端首页拉收藏夹时如果后端返回 `NOT_LOGGED_IN`，会回到扫码登录页。
   - 前端预览空方案时不会因 `byCat[undefined]` 崩溃。

## 自动化验证

命令：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
47 passed in 2.61s
```

新增覆盖：

- 当前 B 站二维码 poll 结构：顶层 `code=0`、`data.code=86101` 时应为 waiting。
- `get_video_info()` 能组合 `/x/web-interface/view` 与 `/x/tag/archive/tags`。
- async progress callback 能收到采集、分类、预览阶段事件。
- full 模式会在分类前补齐简介和标签。
- 失败项重试后仍失败时不会被清空。
- AI prompt 包含视频简介。
- B 站客户端请求带浏览器 User-Agent/Referer。
- WBI 特殊字符签名、WBI 缓存刷新、创建收藏夹失败、缺目标收藏夹失败重试、前端文本转义和 stale-cookie 回登录兜底。

## 真实接口核验

已通过真实 HTTP 请求核验以下接口。

| 用途 | 接口 | 核验结果 |
|---|---|---|
| 生成扫码二维码 | `GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate` | 返回 `code=0`，包含 `url` 和 `qrcode_key` |
| 轮询扫码状态 | `GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll` | 未扫码时返回顶层 `code=0`、`data.code=86101`、`data.message=未扫码` |
| 视频详情 | `GET https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7` | 返回 `code=0`，包含 `desc`、`title` 等字段 |
| 视频标签 | `GET https://api.bilibili.com/x/tag/archive/tags?bvid=BV1GJ411x7h7` | 返回 `code=0`，样例视频返回 6 个标签 |
| 收藏夹列表 | `GET https://api.bilibili.com/x/v3/fav/folder/created/list-all` | 无 cookie 请求可达，返回 `code=0` |
| 收藏夹内容 | `GET https://api.bilibili.com/x/v3/fav/resource/list` | 无 cookie 请求可达，返回 `code=0` |
| 创建收藏夹 | `POST https://api.bilibili.com/x/v3/fav/folder/add` | 无 cookie 请求可达，返回 `code=-101`、`账号未登录` |
| 移动视频 | `POST https://api.bilibili.com/x/v3/fav/resource/move` | 无 cookie 请求可达，返回 `code=-101`、`账号未登录` |

说明：

- 创建收藏夹和移动视频是账号写操作，需要有效 B 站登录 cookie 和 CSRF；本次只验证了接口路径真实可达，未在你的账号上执行真实写操作。
- `https://api.bilibili.com/x/web-interface/nav` 在无 cookie 时返回 `code=-101`，这是当前接口行为。应用内只有在本地 cookie 看起来已登录后才会拉 WBI key；cookie 失效时会清理登录态并要求重新扫码。
- 使用当前 `BilibiliClient` 真实调用验证：
  - `qrcode_generate_has_key=True`
  - `qrcode_poll_status=waiting`
  - `video_info_has_intro=True`
  - `video_info_tag_count=6`

## 参考资料

- QR 登录状态码参考：`bilibili-API-collect` 的二维码登录说明记录了 `data.code=0/86038/86090/86101` 的含义。
- 视频详情接口参考：`bilibili-API-collect` 的视频基本信息文档记录了 `/x/web-interface/view` 和 `bvid` 参数。
