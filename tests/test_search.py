"""One Search 聚合接口契约测试（预约趋势）。"""

from __future__ import annotations

from datetime import datetime, timedelta

PATH = "/api/v09/data/search/reserve-trend"


def _recent(days_ago: int) -> str:
    """按服务器时区生成日期，保证落在接口默认窗口内。"""
    from app.routers.search import _today

    return (_today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _seed_recent(db_file, app_id: int) -> None:
    import sqlite3

    conn = sqlite3.connect(db_file)
    try:
        conn.executemany(
            "INSERT INTO dws_taptap_reserve_v2_daily "
            "(crawled_at, app_id, app_name, total_reserve_count, reserve_count) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (_recent(2), app_id, "近期游戏", 100, 30),
                (_recent(1), app_id, "近期游戏", 180, 80),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_reserve_trend_returns_points_in_window(client, db_path):
    _seed_recent(db_path, 555001)
    body = client.get(PATH, params={"app_id": 555001, "days": 30}).json()
    assert body["code"] == 0
    data = body["data"]
    assert data["app_id"] == 555001
    assert data["days"] == 30
    assert len(data["points"]) == 2
    assert data["points"][0]["date"] == _recent(2)
    assert data["points"][0]["added"] == 30
    assert data["points"][0]["total"] == 100
    assert data["latest"] == {"date": _recent(1), "added": 80, "total": 180}
    # 接口只回该 app 的数据
    assert all(p["date"] for p in data["points"])


def test_reserve_trend_empty_for_unknown_app(client):
    body = client.get(PATH, params={"app_id": 123456789}).json()
    assert body["code"] == 0
    assert body["data"]["points"] == []
    assert body["data"]["latest"] is None


def test_reserve_trend_window_excludes_old_rows(client):
    # 两天窗口 = 今天 + 昨天，看不到两天前那条
    body = client.get(PATH, params={"app_id": 555001, "days": 2}).json()
    assert [p["date"] for p in body["data"]["points"]] == [_recent(1)]
    # 单天窗口只有今天，接口返回空点集
    body1 = client.get(PATH, params={"app_id": 555001, "days": 1}).json()
    assert body1["data"]["points"] == []


def test_reserve_trend_rejects_bad_params(client):
    assert client.get(PATH, params={"app_id": 0}).status_code == 400
    assert client.get(PATH, params={"app_id": 1, "days": 0}).status_code == 400
    assert client.get(PATH, params={"days": 30}).status_code == 400


def test_reserve_trend_is_dated_by_server_today(client):
    from app.routers.search import _today

    body = client.get(PATH, params={"app_id": 555001}).json()
    assert body["data"]["end"] == _today().strftime("%Y-%m-%d")
    start = datetime.strptime(body["data"]["start"], "%Y-%m-%d")
    end = datetime.strptime(body["data"]["end"], "%Y-%m-%d")
    assert (end - start).days == 30 - 1
