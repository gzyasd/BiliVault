import asyncio
import inspect
import json
from dataclasses import dataclass, field

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
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
- 相近主题合并，优先复用稳定、宽泛且容易理解的分类名称
- confidence<0.6 也给最佳猜测，不要用"其他"
"""


REFINE_PROMPT = """你是B站收藏夹分类方案微调助手。
输入包含视频元数据、当前分类方案、用户微调指令。
你必须返回完整的新方案，不要只返回变化项。
输出严格JSON: {"items":[{"avid":int,"resource_type":int,"category":"中文2-10字","confidence":0-1,"reason":"<=40字原因"}]}
"""

REFINE_POLICY_PROMPT = """你是B站收藏夹分类方案微调规划助手。
根据用户指令、当前分类统计和代表样本，生成供后续分批执行的统一规则。
输出严格JSON: {"policy":{"summary":"规则摘要","categories":["允许使用的分类"],"rules":["判断规则"]}}
分类总数不能超过指定上限，规则必须能独立应用到每条资源。
"""


class AiClassifier:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self._api = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._json_mode_supported = True

    def _client(self):
        return self._api

    async def aclose(self) -> None:
        result = self._api.close()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _decode_json_content(content: str) -> dict:
        text = (content or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().lower() in ("```", "```json"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise AiApiError("AI返回非JSON:" + text[:200], code="AI_BAD_JSON")
        if not isinstance(value, dict):
            raise AiApiError("AI返回的JSON不是对象", code="AI_BAD_JSON")
        return value

    @staticmethod
    def _validated_response_items(data: dict) -> dict[tuple[int, int], dict]:
        items = data.get("items")
        if not isinstance(items, list):
            raise AiApiError("AI返回缺少items数组", code="AI_BAD_JSON")
        by_key: dict[tuple[int, int], dict] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise AiApiError("AI返回的条目不是对象", code="AI_BAD_JSON")
            resource_id = raw.get("resource_id", raw.get("avid"))
            category = raw.get("category")
            try:
                resource_id = int(resource_id)
                resource_type = int(raw.get("resource_type", 2))
                confidence = float(raw.get("confidence"))
            except (TypeError, ValueError):
                raise AiApiError("AI返回的条目标识或置信度无效", code="AI_BAD_JSON")
            if not resource_id or not isinstance(category, str) or not category.strip():
                raise AiApiError("AI返回的条目缺少标识或分类", code="AI_BAD_JSON")
            category = category.strip()
            if len(category) > 20:
                raise AiApiError("AI返回的分类名称超过20个字符", code="AI_BAD_JSON")
            if not 0.0 <= confidence <= 1.0:
                raise AiApiError("AI返回的置信度超出范围", code="AI_BAD_JSON")
            key = (resource_id, resource_type)
            if key in by_key:
                raise AiApiError("AI返回重复资源条目", code="AI_BAD_JSON")
            by_key[key] = {
                **raw,
                "resource_id": resource_id,
                "resource_type": resource_type,
                "category": category,
                "confidence": confidence,
            }
        return by_key

    async def _chat_json(self, system: str, user: str) -> dict:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                request = {
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": 0.2,
                    "max_tokens": 8192,
                }
                if self._json_mode_supported:
                    request["response_format"] = {"type": "json_object"}
                resp = await self._client().chat.completions.create(
                    **request,
                )
                if resp.choices[0].finish_reason == "length":
                    raise AiApiError("AI输出达到长度上限", code="AI_OUTPUT_TRUNCATED")
                content = resp.choices[0].message.content or ""
                return self._decode_json_content(content)
            except AuthenticationError:
                raise AiApiError("API Key 无效或已失效，请检查配置", code="AI_AUTH_FAILED")
            except BadRequestError as exc:
                message = str(exc).lower()
                if self._json_mode_supported and ("response_format" in message or "json" in message):
                    self._json_mode_supported = False
                    continue
                raise
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

    async def _classify_batch_once(self, videos: list[VideoInfo], max_categories: int = 14) -> list[Classification]:
        user = json.dumps([{
            "avid": v.avid, "title": v.title, "up_name": v.up_name,
            "tname": v.tname, "intro": v.intro, "tags": v.tags,
            "resource_type": v.resource_type,
            "resource_type_name": v.resource_type_name,
        } for v in videos], ensure_ascii=False)
        system = (
            SYSTEM_PROMPT
            + f"\n本次整理最终最多 {max_categories} 个分类；本批也不得产生超过该数量的分类。"
        )
        data = await self._chat_json(system, user)
        # 用 (resource_id, resource_type) 组合键匹配，避免同 ID 不同类型互相覆盖
        by_key = self._validated_response_items(data)
        result = []
        for v in videos:
            it = by_key.get((v.avid, v.resource_type))
            if it:
                result.append(Classification(
                    avid=it["resource_id"],
                    category=it["category"],
                    confidence=it["confidence"],
                    reason=it.get("reason", ""),
                    resource_type=it["resource_type"],
                ))
        return result

    async def _classify_batch_recover(
        self,
        videos: list[VideoInfo],
        max_categories: int,
        missing_retry: int = 0,
        retry_count: int = 0,
    ) -> list[Classification]:
        if not videos:
            return []
        recoverable_codes = {"AI_BAD_JSON", "AI_OUTPUT_TRUNCATED"}
        attempts = 2 if len(videos) <= 10 else 1
        last_code = ""
        returned: list[Classification] = []
        for _ in range(attempts):
            try:
                returned = await self._classify_batch_once(videos, max_categories)
                break
            except AiApiError as exc:
                if exc.code not in recoverable_codes:
                    raise
                last_code = exc.code
        else:
            if len(videos) > 10:
                midpoint = len(videos) // 2
                left = await self._classify_batch_recover(
                    videos[:midpoint], max_categories, retry_count=retry_count + 1
                )
                right = await self._classify_batch_recover(
                    videos[midpoint:], max_categories, retry_count=retry_count + 1
                )
                return left + right
            final_retry_count = retry_count + max(0, attempts - 1)
            failure_type = "AI响应截断" if last_code == "AI_OUTPUT_TRUNCATED" else "AI解析失败"
            reason = f"{failure_type}，重试{final_retry_count}次后仍失败"
            return [
                Classification(v.avid, "未分类", 0.0, reason, resource_type=v.resource_type)
                for v in videos
            ]

        by_key = {(item.avid, item.resource_type): item for item in returned}
        missing = [video for video in videos if (video.avid, video.resource_type) not in by_key]
        if missing:
            if len(missing) == len(videos):
                if len(videos) > 10:
                    midpoint = len(videos) // 2
                    left = await self._classify_batch_recover(
                        videos[:midpoint], max_categories, retry_count=retry_count + 1
                    )
                    right = await self._classify_batch_recover(
                        videos[midpoint:], max_categories, retry_count=retry_count + 1
                    )
                    return left + right
                if missing_retry >= 1:
                    return [
                        Classification(
                            v.avid,
                            "未分类",
                            0.0,
                            f"AI漏项，重试{retry_count}次后仍未返回",
                            resource_type=v.resource_type,
                        )
                        for v in videos
                    ]
                recovered = await self._classify_batch_recover(
                    missing, max_categories, missing_retry + 1, retry_count + 1
                )
            else:
                recovered = await self._classify_batch_recover(
                    missing, max_categories, retry_count=retry_count + 1
                )
            by_key.update({(item.avid, item.resource_type): item for item in recovered})
        return [
            by_key.get(
                (video.avid, video.resource_type),
                Classification(
                    video.avid,
                    "未分类",
                    0.0,
                    f"AI漏项，重试{retry_count}次后仍未返回",
                    resource_type=video.resource_type,
                ),
            )
            for video in videos
        ]

    async def classify_batch(self, videos: list[VideoInfo], max_categories: int = 14) -> list[Classification]:
        return await self._classify_batch_recover(videos, max_categories)

    async def classify(
        self,
        videos: list[VideoInfo],
        batch_size: int = 50,
        on_progress=None,
        max_categories: int = 14,
    ) -> list[Classification]:
        max_categories = max(3, min(50, int(max_categories)))
        results: list[Classification] = []
        total = len(videos)
        for i in range(0, total, batch_size):
            batch = videos[i:i + batch_size]
            results.extend(await self.classify_batch(batch, max_categories=max_categories))
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
            cats = list(dict.fromkeys(c.category for c in results if c.category != "未分类"))
            if len(cats) > max_categories:
                mapping = await self.merge_categories(cats, max_categories=max_categories)
                for c in results:
                    if c.category in mapping:
                        c.category = mapping[c.category]
        return results

    async def merge_categories(self, categories: list[str], max_categories: int = 14) -> dict[str, str]:
        system = (
            "给你一组中文分类名，把语义相近的分类合并成更宽泛、清晰的统一名称。"
            f"最终分类名称最多 {max_categories} 个。"
            "输出严格JSON: {\"mapping\":{\"原名\":\"统一名\"}}。"
            "每个原名都必须出现在mapping里，不能新增未分类或其他分类。"
        )
        user = json.dumps(categories, ensure_ascii=False)
        data = await self._chat_json(system, user)
        mapping = data.get("mapping", {})
        missing = [category for category in categories if category not in mapping]
        normalized = {
            category: str(mapping.get(category, "")).strip()
            for category in categories
        }
        final_categories = {value for value in normalized.values() if value}
        invalid_values = any(
            not value or len(value) > 20 or value in ("未分类", "其他")
            for value in normalized.values()
        )
        if missing or invalid_values or len(final_categories) > max_categories or len(final_categories) == 0:
            raise AiApiError(
                "AI 未能把分类归并到指定数量，请调整分类精细度后重试",
                code="AI_CATEGORY_LIMIT_FAILED",
            )
        return normalized

    async def build_refine_policy(
        self,
        videos: list[VideoInfo],
        current: list[Classification],
        instruction: str,
        max_categories: int = 14,
    ) -> dict:
        video_by_key = {(video.avid, video.resource_type): video for video in videos}
        grouped: dict[str, dict] = {}
        for item in current:
            group = grouped.setdefault(item.category, {"count": 0, "samples": []})
            group["count"] += 1
            video = video_by_key.get((item.avid, item.resource_type))
            if video and len(group["samples"]) < 5:
                group["samples"].append({
                    "title": video.title,
                    "up_name": video.up_name,
                    "resource_type_name": video.resource_type_name,
                })
        user = json.dumps({
            "instruction": instruction,
            "max_categories": max_categories,
            "current_categories": grouped,
        }, ensure_ascii=False)
        data = await self._chat_json(REFINE_POLICY_PROMPT, user)
        policy = data.get("policy")
        if isinstance(policy, dict):
            return policy
        return {
            "summary": instruction,
            "categories": list(grouped),
            "rules": [instruction],
        }

    async def _refine_batch_once(
        self,
        videos: list[VideoInfo],
        current: list[Classification],
        policy: dict,
        max_categories: int,
    ) -> list[Classification]:
        user = json.dumps({
            "policy": policy,
            "videos": [v.__dict__ for v in videos],
            "current_plan": [c.__dict__ for c in current],
        }, ensure_ascii=False)
        system = REFINE_PROMPT + f"\n严格应用统一微调规则。新方案最终最多 {max_categories} 个分类，不得突破该上限。"
        data = await self._chat_json(system, user)
        by_key = self._validated_response_items(data)
        result = []
        for old in current:
            it = by_key.get((old.avid, old.resource_type))
            if it:
                result.append(Classification(
                    avid=old.avid,
                    category=it["category"],
                    confidence=it["confidence"],
                    reason=it.get("reason", old.reason),
                    resource_type=old.resource_type,
                ))
        return result

    async def _refine_batch_recover(
        self,
        videos: list[VideoInfo],
        current: list[Classification],
        policy: dict,
        max_categories: int,
        missing_retry: int = 0,
    ) -> tuple[list[Classification], int]:
        recoverable_codes = {"AI_BAD_JSON", "AI_OUTPUT_TRUNCATED"}
        attempts = 2 if len(videos) <= 10 else 1
        returned: list[Classification] = []
        retry_count = 0
        for attempt in range(attempts):
            try:
                returned = await self._refine_batch_once(videos, current, policy, max_categories)
                break
            except AiApiError as exc:
                if exc.code not in recoverable_codes:
                    raise
                if attempt or attempts == 1:
                    retry_count += 1
        else:
            if len(videos) > 10:
                midpoint = len(videos) // 2
                left, left_retries = await self._refine_batch_recover(
                    videos[:midpoint], current[:midpoint], policy, max_categories
                )
                right, right_retries = await self._refine_batch_recover(
                    videos[midpoint:], current[midpoint:], policy, max_categories
                )
                return left + right, retry_count + left_retries + right_retries
            return list(current), retry_count

        by_key = {(item.avid, item.resource_type): item for item in returned}
        missing_indexes = [
            index for index, old in enumerate(current)
            if (old.avid, old.resource_type) not in by_key
        ]
        if missing_indexes:
            if len(missing_indexes) == len(current) and len(current) > 10:
                midpoint = len(current) // 2
                left, left_retries = await self._refine_batch_recover(
                    videos[:midpoint], current[:midpoint], policy, max_categories
                )
                right, right_retries = await self._refine_batch_recover(
                    videos[midpoint:], current[midpoint:], policy, max_categories
                )
                return left + right, retry_count + 1 + left_retries + right_retries
            if len(missing_indexes) == len(current) and missing_retry >= 1:
                return list(current), retry_count + 1
            missing_videos = [videos[index] for index in missing_indexes]
            missing_current = [current[index] for index in missing_indexes]
            recovered, child_retries = await self._refine_batch_recover(
                missing_videos,
                missing_current,
                policy,
                max_categories,
                missing_retry + 1 if len(missing_indexes) == len(current) else 0,
            )
            by_key.update({(item.avid, item.resource_type): item for item in recovered})
            retry_count += 1 + child_retries
        return [by_key.get((old.avid, old.resource_type), old) for old in current], retry_count

    async def refine_plan(
        self,
        videos: list[VideoInfo],
        current: list[Classification],
        instruction: str,
        max_categories: int = 14,
        batch_size: int = 100,
        on_progress=None,
    ) -> list[Classification]:
        async def emit(event: dict) -> None:
            if not on_progress:
                return
            maybe = on_progress(event)
            if inspect.isawaitable(maybe):
                await maybe

        total = len(current)
        await emit({"stage": "analyzing", "processed": 0, "total": total, "progress": 0.0, "retry_count": 0})
        policy = await self.build_refine_policy(videos, current, instruction, max_categories)
        video_by_key = {(video.avid, video.resource_type): video for video in videos}
        result_by_key = {(item.avid, item.resource_type): item for item in current}
        processable = [item for item in current if (item.avid, item.resource_type) in video_by_key]
        preserved = total - len(processable)
        processed = preserved
        retry_count = 0
        for start in range(0, len(processable), batch_size):
            current_batch = processable[start:start + batch_size]
            video_batch = [video_by_key[(item.avid, item.resource_type)] for item in current_batch]
            refined_batch, batch_retries = await self._refine_batch_recover(
                video_batch, current_batch, policy, max_categories
            )
            result_by_key.update({(item.avid, item.resource_type): item for item in refined_batch})
            processed += len(current_batch)
            retry_count += batch_retries
            await emit({
                "stage": "refining",
                "processed": processed,
                "total": total,
                "progress": processed / total if total else 1.0,
                "retry_count": retry_count,
            })
        result = [result_by_key[(item.avid, item.resource_type)] for item in current]
        await emit({"stage": "merging", "processed": total, "total": total, "progress": 1.0, "retry_count": retry_count})
        categories = {item.category for item in result if item.category != "未分类"}
        if len(categories) > max_categories:
            mapping = await self.merge_categories(sorted(categories), max_categories=max_categories)
            for item in result:
                if item.category in mapping:
                    item.category = mapping[item.category]
        return result
