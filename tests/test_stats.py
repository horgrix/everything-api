"""访问统计（PV/UV）接口契约测试。"""

from __future__ import annotations

PREFIX = "/api/v09/stats"
HIT = f"{PREFIX}/hit"
WEEK = f"{PREFIX}/week"

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _hit(client, path="/taptap", ip="1.1.1.1", ua=CHROME, **kwargs):
    headers = {"User-Agent": ua, "X-Forwarded-For": ip}
    headers.update(kwargs.pop("headers", {}))
    body = {"path": path}
    body.update(kwargs)
    return client.post(HIT, json=body, headers=headers)


def test_hit_counts_first_view(client, clean_stats):
    resp = _hit(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"counted": True}


def test_hit_dedup_within_30_minutes(client, clean_stats):
    assert _hit(client).json()["data"]["counted"] is True
    # 同一 visitor + path 重复上报
    assert _hit(client).json()["data"]["counted"] is False
    # 换个路径就是新的一次浏览
    assert _hit(client, path="/business").json()["data"]["counted"] is True
    # 换访客也算一次
    assert _hit(client, ip="2.2.2.2").json()["data"]["counted"] is True


def test_hit_normalizes_path(client, clean_stats):
    _hit(client, path="/taptap?tab=weekly#top")
    _hit(client, path="/taptap")
    body = client.get(WEEK).json()["data"]
    # 归一化后是同一个 path，且只算一次（防连点）
    assert body["pv"] == 1
    assert body["top_paths"][0]["path"] == "/taptap"


def test_hit_skips_no_track(client, clean_stats):
    assert (
        _hit(client, path="/taptap", headers={"X-No-Track": "1"}).json()["data"]["counted"] is False
    )
    resp = client.post(
        f"{HIT}?no_track=1",
        json={"path": "/taptap"},
        headers={"User-Agent": CHROME, "X-Forwarded-For": "1.1.1.1"},
    )
    assert resp.json()["data"]["counted"] is False
    assert client.get(WEEK).json()["data"]["pv"] == 0


def test_hit_skips_bots_and_empty_ua(client, clean_stats):
    bots = [
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "python-requests/2.32.3",
        "curl/8.7.1",
        "Go-http-client/1.1",
        "",
    ]
    for ua in bots:
        assert _hit(client, ua=ua).json()["data"]["counted"] is False, ua
    assert client.get(WEEK).json()["data"]["pv"] == 0


def test_week_empty_returns_zeros(client, clean_stats):
    body = client.get(WEEK).json()
    assert body["code"] == 0
    data = body["data"]
    assert data["pv"] == 0
    assert data["uv"] == 0
    assert data["top_paths"] == []
    # 无数据也必须是字符串日期，不能是 null
    assert isinstance(data["week_start"], str) and len(data["week_start"]) == 10


def test_week_pv_uv_and_top_paths(client, clean_stats):
    # /taptap: 3 个访客各 1 次 = pv 3 / uv 3
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert _hit(client, path="/taptap", ip=ip).json()["data"]["counted"] is True
    # /business: 同一访客 2 个路径，pv 2 / uv 1
    _hit(client, path="/business", ip="10.0.0.1")
    _hit(client, path="/business", ip="10.0.0.1", ua=CHROME + " v2")
    # /taprep-monthly: 1 次
    _hit(client, path="/taprep-monthly", ip="10.0.0.9")

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 6
    assert data["uv"] == 5
    assert [item["path"] for item in data["top_paths"]] == [
        "/taptap",
        "/business",
        "/taprep-monthly",
    ]
    assert data["top_paths"][0] == {"path": "/taptap", "pv": 3, "uv": 3}
    assert data["top_paths"][1] == {"path": "/business", "pv": 2, "uv": 2}


def test_week_top_paths_limited_to_three(client, clean_stats):
    for index in range(5):
        for ip in ("10.1.0.1", "10.1.0.2"):
            _hit(client, path=f"/page-{index}", ip=ip, ua=f"{CHROME} r{index}-{ip}")

    data = client.get(WEEK).json()["data"]
    assert len(data["top_paths"]) == 3
    assert data["pv"] == 10
    pvs = [item["pv"] for item in data["top_paths"]]
    assert pvs == sorted(pvs, reverse=True)


def test_week_ignores_last_week(client, clean_stats, stats_db_path):
    """上周的记录不进本周统计。"""
    import sqlite3

    conn = sqlite3.connect(stats_db_path)
    try:
        conn.execute(
            "INSERT INTO visit_event (day, path, visitor, referrer, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2020-01-06", "/old", "f" * 32, None, "2020-01-06 10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 0
    assert data["top_paths"] == []


def test_stats_route_prefix_matches_contract(client):
    """路由前缀与前端契约一致，且与 /api/v09/data 并列。"""
    body = client.get("/openapi.json").json()
    assert HIT in body["paths"]
    assert WEEK in body["paths"]
    assert "/api/v09/data/sql" in body["paths"]


def test_stats_alias_paths_work(client, clean_stats):
    """前端默认的 /api/stats 别名同样可用（不进 openapi）。"""
    headers = {"User-Agent": CHROME, "X-Forwarded-For": "10.2.2.2"}
    resp = client.post("/api/stats/hit", json={"path": "/alias"}, headers=headers)
    assert resp.json()["data"] == {"counted": True}

    data = client.get("/api/stats/week").json()["data"]
    assert data["pv"] == 1
    assert data["top_paths"][0]["path"] == "/alias"

    # 别名与主前缀共用同一份数据与同一个路由，openapi 里不重复列出
    assert "/api/stats/hit" not in client.get("/openapi.json").json()["paths"]
