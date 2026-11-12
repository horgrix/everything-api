"""首页概览接口契约测试（活跃 APP / 类型占比）。"""

from __future__ import annotations

ACTIVE_PATH = "/api/v09/home/active-apps"
SHARE_PATH = "/api/v09/home/type-share"


def test_active_apps_returns_monthly_and_daily(client):
    body = client.get(ACTIVE_PATH, params={"month": "2026-11"}).json()
    assert body["code"] == 0
    data = body["data"]
    assert data["month"] == "2026-11"
    # 月度快照只取当月那一条
    assert data["monthly"] == {
        "ad_app_cnt": 113,
        "download_app_cnt": 885,
        "online_players_app_cnt": 763,
        "reserve_app_cnt": 206,
    }
    # 每日明细只取当月（上月 monthly 行不应混进来），按日期升序
    assert [row["date"] for row in data["daily"]] == ["2026-11-01", "2026-11-02", "2026-11-11"]
    assert data["daily"][0]["online_players_app_cnt"] is None
    assert data["daily"][-1]["online_players_app_cnt"] == 645
    assert data["latest_day"] == "2026-11-11"


def test_active_apps_defaults_to_current_month(client):
    from app.routers.home import _current_month

    body = client.get(ACTIVE_PATH).json()
    assert body["code"] == 0
    # 不传 month 时按服务器当前月（Asia/Shanghai）取数
    assert body["data"]["month"] == _current_month()


def test_active_apps_rejects_bad_month(client):
    resp = client.get(ACTIVE_PATH, params={"month": "2026-13"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


def test_type_share_aggregates_three_dims(client):
    body = client.get(SHARE_PATH, params={"month": "2026-11"}).json()
    assert body["code"] == 0
    data = body["data"]
    by_dist = {item["dist"]: item for item in data["types"]}

    assert set(by_dist) == {"0", "1", "2"}
    assert by_dist["0"]["download_total"] == 5366097
    assert by_dist["0"]["peak_online_players"] == 115069
    assert by_dist["0"]["ad_views"] == 8555
    assert by_dist["0"]["game_cnt"] == 443
    # 广告表没有 dist=2 的行：补 0 而不是缺项
    assert by_dist["2"]["ad_views"] == 0

    # 合计只算三类，未分类不进分母
    assert data["totals"] == {
        "download_total": 5366097 + 1227639 + 1397506,
        "peak_online_players": 115069 + 12683 + 16711,
        "ad_views": 8555 + 73,
    }
    assert data["unclassified"]["ad_views"] == 89


def test_type_share_has_api_home_alias(client):
    direct = client.get(SHARE_PATH, params={"month": "2026-11"}).json()
    alias = client.get("/api/home/type-share", params={"month": "2026-11"}).json()
    assert alias["code"] == 0
    assert alias["data"] == direct["data"]
