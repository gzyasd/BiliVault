import httpx
import pytest
import respx

from core.bilibili_api import API_BASE, BilibiliClient, _get_mixin_key, _wbi_sign
from core.storage import Storage


_WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def test_mixin_key_construction():
    img_key = "7cd088941d418c9b7d4932caff0ff715"
    sub_key = "e3a47cd088941d418c9b7d4932caff0f"
    img = img_key[:32]
    sub = sub_key[:32]
    raw = img + sub
    mixin = "".join(raw[i] for i in _WBI_TABLE)[:32]
    assert _get_mixin_key(img_key, sub_key) == mixin


def test_wbi_sign_basic():
    img_key = "7cd088941d418c9b7d4932caff0ff715"
    sub_key = "e3a47cd088941d418c9b7d4932caff0f"
    params = {"foo": "114", "bar": "514", "wts": 1702204800}
    signed = _wbi_sign(params, img_key, sub_key)
    assert "w_rid" in signed
    assert signed["wts"] == 1702204800
    assert signed["foo"] == "114"


def test_wbi_sign_url_encodes_params():
    img_key = "7cd088941d418c9b7d4932caff0ff715"
    sub_key = "e3a47cd088941d418c9b7d4932caff0f"
    signed = _wbi_sign({"keyword": "a b", "wts": 1702204800}, img_key, sub_key)
    assert signed["w_rid"] == "63ed5e31397023ef3266021a4c98ac86"


@pytest.fixture
def client(tmp_path):
    return BilibiliClient(cookie_store_path=tmp_path / "cookie.json")


@pytest.mark.asyncio
async def test_client_uses_browser_headers(client):
    async with client._client() as http_client:
        assert "Mozilla" in http_client.headers["user-agent"]
        assert http_client.headers["referer"] == "https://www.bilibili.com/"


@respx.mock
async def test_qrcode_generate(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    ).mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"url": "https://x/scan", "qrcode_key": "key123", "returnMessage": ""},
    }))
    result = await client.qrcode_generate()
    assert result["qrcode_key"] == "key123"
    assert result["url"] == "https://x/scan"


@respx.mock
async def test_qrcode_poll_success(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(
        200,
        json={"code": 0, "data": {"mid": 12345}},
        headers=[
            ("Set-Cookie", "SESSDATA=abc; Path=/"),
            ("Set-Cookie", "bili_jct=csrf; Path=/"),
            ("Set-Cookie", "DedeUserID=12345; Path=/"),
        ],
    ))
    result = await client.qrcode_poll("key123")
    assert result["status"] == "success"
    assert client.cookies["SESSDATA"] == "abc"
    assert client.cookies["bili_jct"] == "csrf"


@respx.mock
async def test_qrcode_poll_legacy_expired_code(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(200, json={"code": 86038, "data": {}}))
    result = await client.qrcode_poll("key123")
    assert result["status"] == "expired"


@respx.mock
async def test_qrcode_poll_current_waiting_shape(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(200, json={
        "code": 0,
        "message": "0",
        "data": {
            "url": "",
            "refresh_token": "",
            "timestamp": 0,
            "code": 86101,
            "message": "未扫码",
        },
    }))
    result = await client.qrcode_poll("key123")
    assert result["status"] == "waiting"
    assert client.cookies == {}


@respx.mock
async def test_qrcode_poll_expired(client):
    respx.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    ).mock(return_value=httpx.Response(200, json={"code": 86039, "data": {}}))
    result = await client.qrcode_poll("key123")
    assert result["status"] == "expired"


@respx.mock
async def test_fetch_wbi_keys_and_cache(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)

    route_nav = respx.get(f"{API_BASE}/x/web-interface/nav").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd088941d418cwallet.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff715e3a4.png",
                }
            },
        })
    )
    keys = await client._fetch_wbi_keys(storage=None)
    assert keys["img_key"] == "7cd088941d418cwallet"
    assert keys["sub_key"] == "4932caff0ff715e3a4"
    assert route_nav.called


@respx.mock
async def test_get_my_folders(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "123", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)

    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd088941d418c9b7d4932caff0ff715.png",
                              "sub_url": "https://i0.hdslb.com/bfs/wbi/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/folder/created/list-all").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": {
                "count": 2,
                "list": [
                    {"id": 100, "title": "默认收藏夹", "media_count": 10, "cover": "http://c.jpg", "attr": 0},
                    {"id": 200, "title": "学习", "media_count": 5, "cover": "", "attr": 23},
                ],
            },
        })
    )
    folders = await client.get_my_folders(storage=None)
    assert len(folders) == 2
    assert folders[0]["fid"] == 100
    assert folders[0]["title"] == "默认收藏夹"
    assert folders[0]["is_default"] is True
    assert folders[1]["is_default"] is False


@respx.mock
async def test_wbi_get_refreshes_stale_cached_keys(client, monkeypatch, tmp_path):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "123", "bili_jct": "x"})
    storage = Storage(tmp_path)
    storage.save_wbi_keys(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )

    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd088941d418c9b7d4932caff0ff715.png",
                              "sub_url": "https://i0.hdslb.com/bfs/wbi/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    route = respx.get(f"{API_BASE}/x/v3/fav/folder/created/list-all").mock(
        side_effect=[
            httpx.Response(200, json={"code": -403, "message": "签名错误"}),
            httpx.Response(200, json={
                "code": 0,
                "data": {"list": [
                    {"id": 100, "title": "默认收藏夹", "media_count": 10, "cover": ""},
                ]},
            }),
        ]
    )

    folders = await client.get_my_folders(storage=storage)
    assert folders[0]["fid"] == 100
    assert route.call_count == 2


@respx.mock
async def test_get_folder_video_pages_continues_when_has_more_with_short_page(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}}
    }))
    page1 = {"code": 0, "data": {"info": {"media_count": 3}, "medias": [
        {"id": 1, "bvid": "BV1", "title": "A", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
    ], "has_more": True}}
    page2 = {"code": 0, "data": {"info": {"media_count": 3}, "medias": [
        {"id": 2, "bvid": "BV2", "title": "B", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
        {"id": 3, "bvid": "BV3", "title": "C", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
    ], "has_more": False}}
    route = respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    pages = []
    async for p in client.get_folder_video_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert len(pages) == 2
    assert route.call_count == 2
    assert pages[0]["has_more"] is True
    assert pages[1]["has_more"] is False


@respx.mock
async def test_get_folder_video_pages_counts_skipped_attr_invalid(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}}
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 3}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "A", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
            {"id": 2, "bvid": "BV2", "title": "失效", "upper": {"name": "U"}, "cover": "", "attr": 1, "type": 2, "tname": "科技"},
            {"id": 3, "bvid": "BV3", "title": "也失效", "upper": {"name": "U"}, "cover": "", "attr": 9, "type": 2, "tname": "科技"},
        ], "has_more": False}
    }))
    pages = []
    async for p in client.get_folder_video_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert pages[0]["raw_count"] == 3
    assert pages[0]["usable_count"] == 1
    assert pages[0]["skipped_count"] == 2
    assert pages[0]["skipped_reasons"]["attr_invalid"] == 2


@respx.mock
async def test_get_folder_video_pages_returns_skipped_items(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 3}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "有效", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
            {"id": 2, "bvid": "", "title": "已失效", "upper": {"name": "U"}, "cover": "", "attr": 1, "type": 2, "tname": "科技"},
            {"id": 3, "bvid": "BV3", "title": "合集", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 11, "tname": "合集"},
        ], "has_more": False},
    }))

    pages = []
    async for page in client.get_folder_video_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(page)

    assert pages[0]["skipped_items"][0]["avid"] == 2
    assert pages[0]["skipped_items"][0]["reason_code"] == "attr_invalid"
    assert pages[0]["skipped_items"][0]["removable"] is True
    assert pages[0]["skipped_items"][1]["avid"] == 3
    assert pages[0]["skipped_items"][1]["reason_code"] == "non_video_type"
    assert pages[0]["skipped_items"][1]["removable"] is False


@respx.mock
async def test_get_folder_resource_pages_detects_invalid_by_empty_title(client, monkeypatch):
    """attr=0 但 title 为空 → 视为失效并跳过。"""
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 2}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "有效视频", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
            {"id": 2, "bvid": "", "title": "", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
        ], "has_more": False},
    }))
    pages = []
    async for p in client.get_folder_resource_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert pages[0]["usable_count"] == 1
    assert pages[0]["skipped_count"] == 1
    assert pages[0]["skipped_items"][0]["avid"] == 2
    assert pages[0]["skipped_items"][0]["reason_code"] == "attr_invalid"
    assert pages[0]["skipped_items"][0]["removable"] is True


@respx.mock
async def test_get_folder_resource_pages_detects_invalid_by_placeholder_title(client, monkeypatch):
    """attr=0 但 title 为 '已失效视频' 占位文本 → 视为失效并跳过。"""
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 2}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "正常视频", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
            {"id": 2, "bvid": "", "title": "已失效视频", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
        ], "has_more": False},
    }))
    pages = []
    async for p in client.get_folder_resource_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert pages[0]["usable_count"] == 1
    assert pages[0]["skipped_items"][0]["avid"] == 2
    assert pages[0]["skipped_items"][0]["reason_code"] == "attr_invalid"
    assert pages[0]["skipped_items"][0]["title"] == "已失效视频"


@respx.mock
async def test_get_folder_resource_pages_keeps_normal_video_without_upper_mid(client, monkeypatch):
    """attr=0、title 正常、upper 无 mid → 不应误判为失效（验证不误伤）。"""
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 1}, "medias": [
            {"id": 1, "bvid": "BV1", "title": "正常标题", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 2, "tname": "科技"},
        ], "has_more": False},
    }))
    pages = []
    async for p in client.get_folder_resource_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert pages[0]["usable_count"] == 1
    assert pages[0]["skipped_count"] == 0
    assert pages[0]["resources"][0]["avid"] == 1
    assert pages[0]["resources"][0]["title"] == "正常标题"


@respx.mock
async def test_get_folder_resource_pages_keeps_non_video_with_normal_title(client, monkeypatch):
    """attr=0、title 正常、type=11 合集 → 不应误判为失效（验证不误伤非视频正常资源）。"""
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"info": {"media_count": 1}, "medias": [
            {"id": 1, "bvid": "", "title": "合集标题", "upper": {"name": "U"}, "cover": "", "attr": 0, "type": 11, "tname": "合集"},
        ], "has_more": False},
    }))
    pages = []
    async for p in client.get_folder_resource_pages(fid=100, storage=None, page_size=20, sleep_seconds=0):
        pages.append(p)
    assert pages[0]["usable_count"] == 1
    assert pages[0]["skipped_count"] == 0
    assert pages[0]["resources"][0]["resource_type"] == 11
    assert pages[0]["resources"][0]["title"] == "合集标题"


@pytest.mark.asyncio
async def test_get_folder_resource_page_reads_requested_page_and_keeps_display_states(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    captured = {}

    async def fake_wbi_get(path, params, storage):
        captured["path"] = path
        captured["params"] = params
        return {
            "info": {"media_count": 60},
            "medias": [
                {"id": 1, "bvid": "BV1", "title": "正常视频", "upper": {"name": "UP1"}, "cover": "c1", "attr": 0, "type": 2, "tname": "科技"},
                {"id": 2, "bvid": "BV2", "title": "已失效视频", "upper": {"name": ""}, "cover": "", "attr": 1, "type": 2, "tname": ""},
                {"id": 3, "bvid": "", "title": "正常合集", "upper": {"name": "UP3"}, "cover": "c3", "attr": 0, "type": 11, "tname": "合集"},
            ],
            "has_more": True,
        }

    monkeypatch.setattr(client, "_wbi_get", fake_wbi_get)

    result = await client.get_folder_resource_page(100, page=2, page_size=20, storage=None)

    assert captured["path"] == "/x/v3/fav/resource/list"
    assert captured["params"]["pn"] == 2
    assert captured["params"]["ps"] == 20
    assert result["total"] == 60
    assert result["has_more"] is True
    assert [(item["resource_id"], item["resource_type"], item["status"]) for item in result["items"]] == [
        (1, 2, "available"),
        (2, 2, "invalid"),
        (3, 11, "available"),
    ]


@respx.mock
async def test_get_folder_videos_paginated(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0, "data": {"wbi_img": {"img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png", "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png"}}
    }))

    page1 = {
        "code": 0,
        "data": {
            "info": {"media_count": 3},
            "medias": [
                {"id": 1001, "bvid": "BV1aaa", "title": "视频A", "upper": {"name": "UP1", "mid": 11},
                 "cover": "http://a.jpg", "attr": 0, "type": 2, "tid": 122, "tname": "野生技术协会"},
                {"id": 1002, "bvid": "BV1bbb", "title": "视频B", "upper": {"name": "UP2", "mid": 12},
                 "cover": "http://b.jpg", "attr": 0, "type": 2, "tid": 95, "tname": "数码"},
            ],
        },
    }
    page2 = {
        "code": 0,
        "data": {"info": {"media_count": 3}, "medias": [
            {"id": 1003, "bvid": "BV1ccc", "title": "视频C", "upper": {"name": "UP3", "mid": 13},
             "cover": "http://c.jpg", "attr": 0, "type": 2, "tid": 122, "tname": "野生技术协会"},
        ]},
    }

    route = respx.get(f"{API_BASE}/x/v3/fav/resource/list").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    videos = []
    async for batch in client.get_folder_videos(fid=100, storage=None, page_size=2, sleep_seconds=0):
        videos.extend(batch)
    assert len(videos) == 3
    assert videos[0]["avid"] == 1001
    assert videos[2]["title"] == "视频C"
    assert route.call_count == 2


@respx.mock
async def test_get_video_info_fetches_intro_and_tags(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "x"})
    respx.get(f"{API_BASE}/x/web-interface/view").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": {
                "aid": 1001,
                "bvid": "BV1aaa",
                "desc": "这是视频简介",
                "title": "Python入门",
                "owner": {"name": "UP1", "mid": 11},
                "pic": "http://a.jpg",
                "tname": "知识",
            },
        })
    )
    respx.get(f"{API_BASE}/x/tag/archive/tags").mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "data": [
                {"tag_name": "Python"},
                {"tag_name": "教程"},
            ],
        })
    )
    info = await client.get_video_info("BV1aaa")
    assert info["intro"] == "这是视频简介"
    assert info["tags"] == ["Python", "教程"]
    assert info["title"] == "Python入门"


@respx.mock
async def test_create_folder(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    respx.post(f"{API_BASE}/x/v3/fav/folder/add").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"id": 999, "title": "编程教程"}})
    )
    fid = await client.create_folder(title="编程教程", privacy=1)
    assert fid == 999
    last_request = respx.calls.last.request
    body = last_request.content.decode()
    assert "title=%E7%BC%96%E7%A8%8B%E6%95%99%E7%A8%8B" in body or "编程教程" in body
    assert "csrf=csrf_tok" in body


@respx.mock
async def test_move_videos(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    respx.post(f"{API_BASE}/x/v3/fav/resource/move").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"success": True}})
    )
    ok = await client.move_videos(src_media_id=100, tar_media_id=999, avids=[1001, 1002])
    assert ok is True
    body = respx.calls.last.request.content.decode()
    assert "src_media_id=100" in body
    assert "tar_media_id=999" in body
    assert ("1001:2" in body or "1001%3A2" in body) and ("1002:2" in body or "1002%3A2" in body)
    assert "csrf=csrf_tok" in body


@respx.mock
async def test_batch_delete_resources(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    route = respx.post(f"{API_BASE}/x/v3/fav/resource/batch-del").mock(
        return_value=httpx.Response(200, json={"code": 0, "message": "0", "ttl": 1, "data": 0})
    )

    ok = await client.batch_delete_resources(media_id=100, resources=[{"id": 1, "type": 2}, {"id": 2, "type": 2}])

    assert ok is True
    form = route.calls[0].request.content.decode()
    assert "media_id=100" in form
    assert "resources=1%3A2%2C2%3A2" in form
    assert "csrf=csrf_tok" in form


@respx.mock
async def test_get_my_profile(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "123", "bili_jct": "x"})
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"mid": 12345, "name": "测试用户", "face": "http://x/avatar.jpg"},
    }))
    profile = await client.get_my_profile()
    assert profile["mid"] == 12345
    assert profile["uname"] == "测试用户"
    assert profile["avatar_url"] == "http://x/avatar.jpg"


@respx.mock
async def test_delete_folders(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    route = respx.post(f"{API_BASE}/x/v3/fav/folder/del").mock(
        return_value=httpx.Response(200, json={"code": 0, "message": "0", "ttl": 1, "data": 0})
    )

    ok = await client.delete_folders(media_ids=[100, 200])

    assert ok is True
    form = route.calls[0].request.content.decode()
    assert "media_ids=100%2C200" in form
    assert "csrf=csrf_tok" in form


@respx.mock
async def test_sort_folders_posts_complete_order(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1", "bili_jct": "csrf_tok"})
    route = respx.post(f"{API_BASE}/x/v3/fav/folder/sort").mock(
        return_value=httpx.Response(200, json={"code": 0, "message": "0", "ttl": 1, "data": 0})
    )

    ok = await client.sort_folders(media_ids=[300, 100, 200])

    assert ok is True
    form = route.calls[0].request.content.decode()
    assert "sort=300%2C100%2C200" in form
    assert "csrf=csrf_tok" in form


@pytest.mark.asyncio
@respx.mock
async def test_get_folder_resource_ids_normalizes_resource_keys(client, monkeypatch):
    monkeypatch.setattr(client, "cookies", {"SESSDATA": "abc", "DedeUserID": "1"})
    monkeypatch.setattr("core.bilibili_api._time_now", lambda: 1700000000)
    respx.get(f"{API_BASE}/x/web-interface/nav").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {
            "img_url": "https://x/7cd088941d418c9b7d4932caff0ff715.png",
            "sub_url": "https://x/e3a47cd088941d418c9b7d4932caff0f.png",
        }},
    }))
    respx.get(f"{API_BASE}/x/v3/fav/resource/ids").mock(return_value=httpx.Response(200, json={
        "code": 0,
        "data": [
            {"id": 101, "type": 2, "bvid": "BV1visible"},
            {"id": 202, "type": 11, "bv_id": ""},
            {"id": 0, "type": 2, "bvid": "BV1ignore"},
        ],
    }))

    assert await client.get_folder_resource_ids(100, storage=None) == [
        {"resource_id": 101, "resource_type": 2, "bvid": "BV1visible"},
        {"resource_id": 202, "resource_type": 11, "bvid": ""},
    ]
