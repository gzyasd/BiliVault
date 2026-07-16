import asyncio

import httpx
import pytest
from openai import BadRequestError
from unittest.mock import AsyncMock, MagicMock

from core.ai_classifier import AiClassifier, VideoInfo, Classification, AiApiError


def make_video(avid, title, up="UP", tname="科技"):
    return VideoInfo(avid=avid, title=title, up_name=up, tname=tname, tags=[])


def test_ai_response_rejects_category_names_beyond_bilibili_limit():
    with pytest.raises(AiApiError, match="分类名称"):
        AiClassifier._validated_response_items({
            "items": [{
                "avid": 1,
                "resource_type": 2,
                "category": "超" * 21,
                "confidence": 0.9,
                "reason": "",
            }],
        })


@pytest.mark.asyncio
async def test_classify_single_batch(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='{"items":[{"avid":1,"category":"编程","confidence":0.9,"reason":"Python"}]}'))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    result = await classifier.classify([make_video(1, "Python入门")])
    assert len(result) == 1
    assert result[0].category == "编程"
    assert result[0].avid == 1


@pytest.mark.asyncio
async def test_classify_handles_bad_json(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content="not json"))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    result = await classifier.classify([make_video(1, "x")])
    assert len(result) == 1
    assert result[0].category == "未分类"
    assert result[0].confidence == 0.0


@pytest.mark.asyncio
async def test_classify_includes_intro_in_prompt(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    captured = {}

    async def fake_chat_json(system, user):
        captured["user"] = user
        return {"items": [{"avid": 1, "category": "编程", "confidence": 0.9, "reason": "Python"}]}

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)

    result = await classifier.classify([
        VideoInfo(
            avid=1,
            title="Python入门",
            up_name="UP",
            tname="科技",
            intro="这是简介",
            tags=["Python"],
        )
    ])

    assert result[0].category == "编程"
    assert "这是简介" in captured["user"]


@pytest.mark.asyncio
async def test_classify_includes_resource_type_name_in_prompt(monkeypatch):
    """非视频资源应把 resource_type_name 中文名一并传给 AI。"""
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    captured = {}

    async def fake_chat_json(system, user):
        captured["user"] = user
        return {"items": [{"avid": 201, "category": "官方合集", "confidence": 0.9, "reason": "合集"}]}

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)

    await classifier.classify([
        VideoInfo(avid=201, title="某合集", up_name="UP", tname="科技", resource_type=11),
    ])

    import json
    payload = json.loads(captured["user"])
    assert payload[0]["resource_type"] == 11
    assert payload[0]["resource_type_name"] == "合集"


@pytest.mark.asyncio
async def test_merge_categories(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='{"mapping":{"编程":"编程教程","代码教学":"编程教程","音乐":"音乐"}}'))]
    client_mock = AsyncMock()
    client_mock.chat.completions.create = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    mapping = await classifier.merge_categories(["编程", "代码教学", "音乐"])
    assert mapping["编程"] == "编程教程"
    assert mapping["代码教学"] == "编程教程"
    assert mapping["音乐"] == "音乐"


@pytest.mark.asyncio
async def test_classify_enforces_global_category_limit(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    videos = [make_video(i, f"资源{i}") for i in range(1, 13)]
    classifier.classify_batch = AsyncMock(return_value=[
        Classification(i, f"分类{i}", 0.9, "") for i in range(1, 13)
    ])
    mapping = {f"分类{i}": f"归并{(i - 1) % 3}" for i in range(1, 13)}
    classifier.merge_categories = AsyncMock(return_value=mapping)

    result = await classifier.classify(videos, batch_size=20, max_categories=3)

    assert len({item.category for item in result}) == 3
    classifier.merge_categories.assert_awaited_once()
    assert classifier.merge_categories.await_args.kwargs["max_categories"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", [
    {"分类1": "归并1", "分类2": "归并2"},
    {"分类1": "归并1", "分类2": "归并2", "分类3": "归并3"},
])
async def test_merge_categories_rejects_incomplete_or_over_limit_mapping(monkeypatch, mapping):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier._chat_json = AsyncMock(return_value={"mapping": mapping})

    with pytest.raises(AiApiError) as exc_info:
        await classifier.merge_categories(["分类1", "分类2", "分类3"], max_categories=2)

    assert exc_info.value.code == "AI_CATEGORY_LIMIT_FAILED"


@pytest.mark.asyncio
async def test_classify_batch_prompt_includes_category_limit(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    captured = {}

    async def fake_chat_json(system, user):
        captured["system"] = system
        return {"items": [{"avid": 1, "category": "编程", "confidence": 0.9, "reason": ""}]}

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)
    await classifier.classify_batch([make_video(1, "Python")], max_categories=8)

    assert "最多 8 个" in captured["system"]


@pytest.mark.asyncio
async def test_refine_plan_uses_instruction(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def fake_chat_json(system, user):
        assert "微调" in system
        assert "把官方的作品单独放在一个收藏夹内" in user
        return {"items": [
            {"avid": 1, "category": "官方作品", "confidence": 0.93, "reason": "UP主名称包含官方"},
            {"avid": 2, "category": "动漫", "confidence": 0.9, "reason": "保持原分类"},
        ]}

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)
    videos = [
        VideoInfo(avid=1, title="官方PV", up_name="某某官方", tname="动画"),
        VideoInfo(avid=2, title="剪辑", up_name="普通UP", tname="动画"),
    ]
    current = [
        Classification(1, "动漫", 0.9, ""),
        Classification(2, "动漫", 0.9, ""),
    ]

    result = await classifier.refine_plan(videos, current, "把官方的作品单独放在一个收藏夹内")

    assert result[0].category == "官方作品"
    assert result[1].category == "动漫"


@pytest.mark.asyncio
async def test_refine_plan_rejects_result_over_category_limit(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier._chat_json = AsyncMock(return_value={"items": [
        {"avid": 1, "category": "分类1", "confidence": 0.9, "reason": ""},
        {"avid": 2, "category": "分类2", "confidence": 0.9, "reason": ""},
        {"avid": 3, "category": "分类3", "confidence": 0.9, "reason": ""},
        {"avid": 4, "category": "分类4", "confidence": 0.9, "reason": ""},
    ]})
    videos = [make_video(i, f"资源{i}") for i in range(1, 5)]
    current = [Classification(i, "原分类", 0.9, "") for i in range(1, 5)]

    with pytest.raises(AiApiError) as exc_info:
        await classifier.refine_plan(videos, current, "细分", max_categories=3)

    assert exc_info.value.code == "AI_CATEGORY_LIMIT_FAILED"


@pytest.mark.asyncio
async def test_classify_batch_propagates_auth_error(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def fake_chat_json(system, user):
        raise AiApiError("API Key 无效", code="AI_AUTH_FAILED")

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)
    videos = [VideoInfo(avid=1, title="A", up_name="UP", tname="科技")]

    with pytest.raises(AiApiError) as ei:
        await classifier.classify_batch(videos)
    assert ei.value.code == "AI_AUTH_FAILED"


@pytest.mark.asyncio
async def test_classify_batch_propagates_connection_error(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def fake_chat_json(system, user):
        raise AiApiError("无法连接", code="AI_CONNECTION")

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)

    with pytest.raises(AiApiError) as ei:
        await classifier.classify_batch([VideoInfo(avid=1, title="A", up_name="UP", tname="科技")])
    assert ei.value.code == "AI_CONNECTION"


@pytest.mark.asyncio
async def test_classify_batch_falls_back_for_bad_json(monkeypatch):
    """仅 AI_BAD_JSON 这类单批解析问题允许降级为未分类。"""
    from core.ai_classifier import AiApiError
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")

    async def fake_chat_json(system, user):
        raise AiApiError("AI返回非JSON", code="AI_BAD_JSON")

    monkeypatch.setattr(classifier, "_chat_json", fake_chat_json)
    result = await classifier.classify_batch([VideoInfo(avid=1, title="A", up_name="UP", tname="科技")])
    assert result[0].category == "未分类"


@pytest.mark.asyncio
async def test_chat_json_requests_json_output_and_parses_markdown_fence(monkeypatch):
    classifier = AiClassifier(base_url="https://api.deepseek.com", api_key="k", model="m")
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(
        finish_reason="stop",
        message=MagicMock(content='```json\n{"items":[]}\n```'),
    )]
    create = AsyncMock(return_value=fake_resp)
    client_mock = MagicMock()
    client_mock.chat.completions.create = create
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    data = await classifier._chat_json("输出 JSON", "[]")

    assert data == {"items": []}
    kwargs = create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_classify_recovers_bad_batch_by_splitting(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    calls = []

    async def fake_once(videos, max_categories):
        calls.append([video.avid for video in videos])
        if len(videos) == 20:
            raise AiApiError("AI返回非JSON", code="AI_BAD_JSON")
        return [Classification(video.avid, "编程", 0.9, "已恢复") for video in videos]

    monkeypatch.setattr(classifier, "_classify_batch_once", fake_once)
    videos = [make_video(i, f"资源{i}") for i in range(1, 21)]

    result = await classifier.classify(videos, batch_size=20)

    assert calls == [list(range(1, 21)), list(range(1, 11)), list(range(11, 21))]
    assert len(result) == 20
    assert all(item.category == "编程" for item in result)


@pytest.mark.asyncio
async def test_classify_retries_only_missing_items(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    calls = []

    async def fake_once(videos, max_categories):
        calls.append([video.avid for video in videos])
        if len(calls) == 1:
            return [Classification(video.avid, "知识", 0.8, "首轮") for video in videos[:-1]]
        return [Classification(videos[0].avid, "知识", 0.9, "补偿")]

    monkeypatch.setattr(classifier, "_classify_batch_once", fake_once)
    videos = [make_video(i, f"资源{i}") for i in range(1, 21)]

    result = await classifier.classify(videos, batch_size=20)

    assert calls == [list(range(1, 21)), [20]]
    assert len(result) == 20
    assert result[-1].reason == "补偿"


@pytest.mark.asyncio
async def test_non_video_response_without_resource_type_is_treated_as_missing(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier._chat_json = AsyncMock(side_effect=[
        {"items": [{"avid": 9, "category": "图文", "confidence": 0.8, "reason": "缺类型"}]},
        {"items": [{"avid": 9, "resource_type": 21, "category": "图文", "confidence": 0.9, "reason": "已补齐"}]},
    ])
    resource = make_video(9, "图文资源")
    resource.resource_type = 21

    result = await classifier.classify([resource], batch_size=10)

    assert classifier._chat_json.await_count == 2
    assert result[0].resource_type == 21
    assert result[0].reason == "已补齐"


@pytest.mark.asyncio
async def test_classify_retries_structurally_invalid_json_item(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier._chat_json = AsyncMock(side_effect=[
        {"items": [{"avid": 1, "confidence": 0.8, "reason": "缺分类"}]},
        {"items": [{"avid": 1, "category": "知识", "confidence": 0.9, "reason": "已修复"}]},
    ])

    result = await classifier.classify([make_video(1, "资源1")], batch_size=10)

    assert classifier._chat_json.await_count == 2
    assert result[0].category == "知识"


@pytest.mark.asyncio
async def test_refine_retries_invalid_confidence_type(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier.build_refine_policy = AsyncMock(return_value={"summary": "保持"})
    classifier._chat_json = AsyncMock(side_effect=[
        {"items": [{"avid": 1, "category": "知识", "confidence": "not-a-number", "reason": "坏字段"}]},
        {"items": [{"avid": 1, "category": "知识", "confidence": 0.9, "reason": "已修复"}]},
    ])
    videos = [make_video(1, "资源1")]
    current = [Classification(1, "原分类", 0.8, "")]

    result = await classifier.refine_plan(videos, current, "保持", batch_size=10)

    assert classifier._chat_json.await_count == 2
    assert result[0].confidence == 0.9


@pytest.mark.asyncio
async def test_chat_json_falls_back_when_provider_rejects_json_mode(monkeypatch):
    classifier = AiClassifier(base_url="http://compatible-provider.test", api_key="k", model="m")
    response = httpx.Response(400, request=httpx.Request("POST", "http://compatible-provider.test/chat/completions"))
    unsupported = BadRequestError("unsupported response_format", response=response, body={})
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(finish_reason="stop", message=MagicMock(content='{"items":[]}'))]
    create = AsyncMock(side_effect=[unsupported, fake_resp])
    client_mock = MagicMock()
    client_mock.chat.completions.create = create
    monkeypatch.setattr(classifier, "_client", lambda: client_mock)

    data = await classifier._chat_json("输出 JSON", "[]")

    assert data == {"items": []}
    first_kwargs, second_kwargs = [call.kwargs for call in create.await_args_list]
    assert first_kwargs["response_format"] == {"type": "json_object"}
    assert "response_format" not in second_kwargs


@pytest.mark.asyncio
async def test_classify_empty_response_stops_after_finite_retries(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    calls = 0

    async def fake_once(videos, max_categories):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(classifier, "_classify_batch_once", fake_once)

    result = await asyncio.wait_for(
        classifier.classify([make_video(1, "资源1")], batch_size=10),
        timeout=0.2,
    )

    assert calls == 2
    assert result[0].category == "未分类"
    assert "AI漏项" in result[0].reason
    assert "重试1次" in result[0].reason


@pytest.mark.asyncio
async def test_classify_truncated_response_records_reason_and_retry_count(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    calls = 0

    async def fake_once(videos, max_categories):
        nonlocal calls
        calls += 1
        raise AiApiError("输出截断", code="AI_OUTPUT_TRUNCATED")

    monkeypatch.setattr(classifier, "_classify_batch_once", fake_once)

    result = await classifier.classify([make_video(1, "资源1")], batch_size=10)

    assert calls == 2
    assert result[0].category == "未分类"
    assert "响应截断" in result[0].reason
    assert "重试1次" in result[0].reason


@pytest.mark.asyncio
async def test_refine_plan_builds_policy_then_processes_batches_with_progress(monkeypatch):
    classifier = AiClassifier(base_url="http://x", api_key="k", model="m")
    classifier.build_refine_policy = AsyncMock(return_value={
        "summary": "官方作品单独分类",
        "categories": ["动漫", "官方作品"],
    })
    calls = []

    async def fake_refine_once(videos, current, policy, max_categories):
        calls.append([video.avid for video in videos])
        return [
            Classification(
                video.avid,
                "官方作品" if "官方" in video.up_name else old.category,
                0.9,
                "按统一规则",
                resource_type=video.resource_type,
            )
            for video, old in zip(videos, current)
        ]

    monkeypatch.setattr(classifier, "_refine_batch_once", fake_refine_once)
    videos = [make_video(i, f"资源{i}", up="官方UP" if i == 1 else "普通UP") for i in range(1, 6)]
    current = [Classification(i, "动漫", 0.8, "") for i in range(1, 6)]
    events = []

    result = await classifier.refine_plan(
        videos,
        current,
        "把官方作品单独分类",
        batch_size=2,
        on_progress=events.append,
    )

    assert calls == [[1, 2], [3, 4], [5]]
    assert result[0].category == "官方作品"
    assert [event["processed"] for event in events if event["stage"] == "refining"] == [2, 4, 5]
    assert events[0]["stage"] == "analyzing"
