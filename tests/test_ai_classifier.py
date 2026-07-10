import pytest
from unittest.mock import AsyncMock, MagicMock

from core.ai_classifier import AiClassifier, VideoInfo, Classification, AiApiError


def make_video(avid, title, up="UP", tname="科技"):
    return VideoInfo(avid=avid, title=title, up_name=up, tname=tname, tags=[])


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
