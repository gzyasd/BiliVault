import asyncio
import inspect
import json
from dataclasses import dataclass, field

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from core.errors import AiApiError


RESOURCE_TYPE_NAMES: dict[int, str] = {
    2: "视频",
    11: "合集",
    12: "音频",
    21: "图文专栏",
    17: "剧集",
    19: "番剧",
    7: "直播",
}


def resource_type_name(resource_type: int) -> str:
    return RESOURCE_TYPE_NAMES.get(resource_type, "其他")


@dataclass
class VideoInfo:
    avid: int
    title: str
    up_name: str
    tname: str
    intro: str = ""
    tags: list[str] = field(default_factory=list)
    resource_type: int = 2

    @property
    def resource_type_name(self) -> str:
        return resource_type_name(self.resource_type)


@dataclass
class Classification:
    avid: int
    category: str
    confidence: float
    reason: str
    resource_type: int = 2

    @property
    def resource_id(self) -> int:
        return self.avid


SYSTEM_PROMPT = """你是B站收藏夹内容分类助手。给你一批收藏资源的元数据，为每个资源选一个分类。
输入：JSON数组，每项 {avid, title, up_name, tname, tags, resource_type, resource_type_name}
resource_type_name 是资源类型中文名（视频/合集/音频/图文专栏/剧集/番剧/直播/其他）。非视频资源按内容主题分类。
输出：严格JSON，形如 {"items":[{"avid":int,"resource_type":int,"category":"中文2-6字","confidence":0-1,"reason":"≤20字原因"}]}
约束：
- category 用中文短语，如"编程教程""美食""游戏解说""音乐MV""知识科普""生活vlog"
- 同一批分类数控制在3-10个，相近主题合并
- confidence<0.6 也给最佳猜测，不要用"其他"
"""


REFINE_PROMPT = """你是B站收藏夹分类方案微调助手。
输入包含视频元数据、当前分类方案、用户微调指令。
你必须返回完整的新方案，不要只返回变化项。
输出严格JSON: {"items":[{"avid":int,"resource_type":int,"category":"中文2-10字","confidence":0-1,"reason":"<=40字原因"}]}
"""


class AiClassifier:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self._api = AsyncOpenAI(base_url=base_url, api_key=api_key)

    def _client(self):
        return self._api

    async def _chat_json(self, system: str, user: str) -> dict:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client().chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.2,
                )
                content = resp.choices[0].message.content or ""
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    raise AiApiError("AI返回非JSON:" + content[:200], code="AI_BAD_JSON")
            except AuthenticationError:
                raise AiApiError("API Key 无效或已失效，请检查配置", code="AI_AUTH_FAILED")
            except RateLimitError:
                last_exc = AiApiError("AI 接口限流，请稍后再试", code="AI_RATE_LIMIT")
            except APITimeoutError:
                last_exc = AiApiError("AI 请求超时", code="AI_TIMEOUT")
            except APIConnectionError:
                last_exc = AiApiError("无法连接 AI 服务，请检查网络或 base_url", code="AI_CONNECTION")
            except AiApiError:
                raise
            if attempt < 2:
                await asyncio.sleep(1.0 * (2 ** attempt))
        raise last_exc if last_exc else AiApiError("AI 调用失败", code="AI_API_ERROR")

    async def classify_batch(self, videos: list[VideoInfo]) -> list[Classification]:
        user = json.dumps([{
            "avid": v.avid, "title": v.title, "up_name": v.up_name,
            "tname": v.tname, "intro": v.intro, "tags": v.tags,
            "resource_type": v.resource_type,
            "resource_type_name": v.resource_type_name,
        } for v in videos], ensure_ascii=False)
        try:
            data = await self._chat_json(SYSTEM_PROMPT, user)
        except AiApiError as e:
            # 仅单批解析问题降级为未分类；认证/连接/限流等系统级错误必须向上抛出
            if e.code == "AI_BAD_JSON":
                return [Classification(v.avid, "未分类", 0.0, "AI解析失败", resource_type=v.resource_type) for v in videos]
            raise
        items = data.get("items", [])
        # 用 (resource_id, resource_type) 组合键匹配，避免同 ID 不同类型互相覆盖
        by_key = {
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2)): it
            for it in items
        }
        result = []
        for v in videos:
            it = by_key.get((v.avid, v.resource_type))
            if not it:
                # 回退：旧 AI 响应可能不返回 resource_type，按 avid 单键匹配
                legacy = {it.get("avid"): it for it in items}
                it = legacy.get(v.avid)
            if not it:
                result.append(Classification(v.avid, "未分类", 0.0, "AI未返回", resource_type=v.resource_type))
            else:
                result.append(Classification(
                    avid=it.get("resource_id", it.get("avid")),
                    category=it["category"],
                    confidence=float(it.get("confidence", 0.0)),
                    reason=it.get("reason", ""),
                    resource_type=it.get("resource_type", v.resource_type),
                ))
        return result

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

    async def merge_categories(self, categories: list[str]) -> dict[str, str]:
        system = "给你一组中文分类名，把语义相同的合并成统一名称。输出严格JSON: {\"mapping\":{\"原名\":\"统一名\"}}。每个原名都必须出现在mapping里且值为最终统一名。"
        user = json.dumps(categories, ensure_ascii=False)
        data = await self._chat_json(system, user)
        mapping = data.get("mapping", {})
        for c in categories:
            if c not in mapping:
                mapping[c] = c
        return mapping

    async def refine_plan(self, videos: list[VideoInfo], current: list[Classification], instruction: str) -> list[Classification]:
        user = json.dumps({
            "instruction": instruction,
            "videos": [v.__dict__ for v in videos],
            "current_plan": [c.__dict__ for c in current],
        }, ensure_ascii=False)
        data = await self._chat_json(REFINE_PROMPT, user)
        # 用 (resource_id, resource_type) 组合键匹配
        by_key = {
            (it.get("resource_id", it.get("avid")), it.get("resource_type", 2)): it
            for it in data.get("items", [])
        }
        expected_keys = {(c.avid, c.resource_type) for c in current}
        returned_keys = set(by_key)
        if returned_keys != expected_keys:
            raise AiApiError(
                f"AI 微调结果数量不一致，缺少 {sorted(expected_keys - returned_keys)}，多出 {sorted(returned_keys - expected_keys)}",
                code="AI_BAD_JSON",
            )
        result = []
        for old in current:
            it = by_key.get((old.avid, old.resource_type))
            result.append(Classification(
                avid=old.avid,
                category=it.get("category", old.category),
                confidence=float(it.get("confidence", old.confidence)),
                reason=it.get("reason", old.reason),
                resource_type=old.resource_type,
            ))
        return result
