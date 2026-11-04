"""访问统计（PV/UV）接口契约测试。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

PREFIX = "/api/v09/stats"
HIT = f"{PREFIX}/hit"
WEEK = f"{PREFIX}/week"
SUMMARY = f"{PREFIX}/summary"

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
CID_A = "3f2b9c1e-8d4a-4b77-9c02-7b1e5a6d9f10"
CID_B = "7a1d2c3b-4e5f-4061-8273-8a9b0c1d2e3f"


def _hit(client, path="/taptap", ip="1.1.1.1", ua=CHROME, **kwargs):
    headers = {"User-Agent": ua, "X-Forwarded-For": ip}
    headers.update(kwargs.pop("headers", {}))
    body = {"path": path}
    body.update(kwargs)
    return client.post(HIT, json=body, headers=headers)


def _summary(client, range_name=None):
    params = {} if range_name is None else {"range": range_name}
    return client.get(SUMMARY, params=params)


# ---------------------------------------------------------------- 上报


def test_hit_counts_every_report(client, clean_stats):
    """PV 就是明细行数：同一访客同一路径重复上报也各算一次。"""
    first = _hit(client, visitor_id=CID_A).json()
    assert first["code"] == 0
    assert first["data"]["counted"] is True
    assert first["data"]["identity"] == "cid"

    second = _hit(client, visitor_id=CID_A).json()
    assert second["data"]["counted"] is True

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 2
    assert data["uv"] == 1


def test_hit_falls_back_to_fingerprint(client, clean_stats):
    """没带 visitor_id 就走 IP+UA 指纹，identity 回 fp。"""
    body = _hit(client).json()["data"]
    assert body == {"counted": True, "identity": "fp"}

    # 同一 IP+UA = 同一访客；换 IP = 新访客
    _hit(client)
    _hit(client, ip="2.2.2.2")
    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 3
    assert data["uv"] == 2


def test_hit_rejects_bad_visitor_id(client, clean_stats):
    """visitor_id 不合规时不该报错，静默回退到指纹。"""
    for bad in ("short", "with space here", "x" * 65, "中文中文中文中文"):
        body = _hit(client, visitor_id=bad).json()["data"]
        assert body["identity"] == "fp", bad
    assert client.get(WEEK).json()["data"]["uv"] == 1


def test_sso_identity_wins_over_visitor_id(client, clean_stats):
    """整站 SSO 时，同一个登录账号换设备、换 IP 都算 1 个 UV。"""
    sso = {"X-Auth-Request-User": "kc-sub-9f8e7d6c-1111"}
    first = _hit(client, ip="10.5.0.1", visitor_id=CID_A, headers=sso).json()["data"]
    assert first == {"counted": True, "identity": "sso"}
    # 换 IP + 换浏览器指纹 + 换 visitor_id，只要还是同一个 SSO 账号就是同一人
    _hit(client, ip="10.5.0.2", ua=CHROME + " v140", visitor_id=CID_B, headers=sso)
    _hit(client, path="/business", ip="10.5.0.3", headers=sso)
    # 另一个 SSO 账号
    _hit(client, ip="10.5.0.1", headers={"X-Auth-Request-User": "kc-sub-2222"})

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 4
    assert data["uv"] == 2


def test_sso_email_unifies_same_person_across_clients(client, clean_stats):
    """同一个人在不同 client（Keycloak sub 不同）下登录，email 一样就算 1 个 UV。"""
    dev = {"X-Auth-Request-Email": "Someone@horgrix.com", "X-Auth-Request-User": "sub-dev-1"}
    prod = {"X-Auth-Request-Email": "someone@horgrix.com", "X-Auth-Request-User": "sub-prod-9"}
    first = _hit(client, path="/taptap", ip="10.6.0.1", headers=dev).json()["data"]
    assert first == {"counted": True, "identity": "sso"}
    _hit(client, path="/business", ip="10.6.0.2", headers=prod)
    # 没 email 时回落到 sub
    _hit(client, path="/taptap", ip="10.6.0.3", headers={"X-Auth-Request-User": "sub-only-1"})

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 3
    assert data["uv"] == 2


def test_sso_header_respects_trust_switch(monkeypatch):
    """`API_STATS_TRUST_SSO_HEADER=false` 时完全不采信该头（降级到 cid / fp）。"""
    from app.routers import stats as stats_router

    class _Settings:
        api_stats_trust_sso_header = False

    class _Request:
        client = None
        headers = {
            "x-auth-request-email": "attacker@example.com",
            "x-auth-request-user": "attacker",
        }

    monkeypatch.setattr(stats_router, "get_settings", lambda: _Settings())
    assert stats_router._sso_user(_Request()) is None

    class _Trusted:
        api_stats_trust_sso_header = True

    monkeypatch.setattr(stats_router, "get_settings", lambda: _Trusted())
    # 有 email 就用 email（跨 client 稳定），不再用 sub
    assert stats_router._sso_user(_Request()) == "attacker@example.com"

    # 只带 sub 时回落用 sub
    class _SubOnly(_Request):
        headers = {"x-auth-request-user": "kc-sub-1"}

    assert stats_router._sso_user(_SubOnly()) == "kc-sub-1"

    # 没有任何 SSO 头时返回 None，交给下一级身份
    class _NoHeader(_Request):
        headers = {}

    assert stats_router._sso_user(_NoHeader()) is None


def test_client_id_beats_ip_drift(client, clean_stats):
    """同一个第一方 ID 换 IP / 换 UA 仍算一个 UV —— 这是改口径要解决的问题。"""
    _hit(client, ip="3.3.3.3", ua=CHROME, visitor_id=CID_A)
    _hit(client, ip="4.4.4.4", ua=CHROME + " v131", visitor_id=CID_A)
    _hit(client, ip="5.5.5.5", ua=CHROME + " v132", visitor_id=CID_B)

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 3
    assert data["uv"] == 2


def test_hit_normalizes_path(client, clean_stats):
    _hit(client, path="/taptap?tab=weekly#top")
    _hit(client, path="/taptap")
    body = client.get(WEEK).json()["data"]
    assert body["pv"] == 2
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


# ---------------------------------------------------------------- /week 老契约


def test_week_empty_returns_zeros(client, clean_stats):
    body = client.get(WEEK).json()
    assert body["code"] == 0
    data = body["data"]
    # 老契约字段一个不多一个不少
    assert set(data) == {"week_start", "pv", "uv", "top_paths"}
    assert data["pv"] == 0
    assert data["uv"] == 0
    assert data["top_paths"] == []
    assert isinstance(data["week_start"], str) and len(data["week_start"]) == 10


def test_week_pv_uv_and_top_paths(client, clean_stats):
    # /taptap: 3 个访客各 1 次 = pv 3 / uv 3
    for cid, ip in ((CID_A, "10.0.0.1"), (CID_B, "10.0.0.2"), ("cid-10-0-0-3", "10.0.0.3")):
        assert _hit(client, path="/taptap", ip=ip, visitor_id=cid).json()["data"]["counted"] is True
    # /business: 同一访客 2 次，pv 2 / uv 1
    _hit(client, path="/business", ip="10.0.0.1", visitor_id=CID_A)
    _hit(client, path="/business", ip="10.0.0.1", ua=CHROME + " v2", visitor_id=CID_A)
    # /taprep-monthly: 1 次
    _hit(client, path="/taprep-monthly", ip="10.0.0.9", visitor_id="cid-10-0-0-9")

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 6
    # CID_A 在 /taptap 已经算过，/business 不再增加 UV
    assert data["uv"] == 4
    assert [item["path"] for item in data["top_paths"]] == [
        "/taptap",
        "/business",
        "/taprep-monthly",
    ]
    assert data["top_paths"][0] == {"path": "/taptap", "pv": 3, "uv": 3}
    assert data["top_paths"][1] == {"path": "/business", "pv": 2, "uv": 1}


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
    last_week = datetime.now() - timedelta(days=8)
    _insert(stats_db_path, last_week, "/old")

    data = client.get(WEEK).json()["data"]
    assert data["pv"] == 0
    assert data["top_paths"] == []


# ---------------------------------------------------------------- /summary 新口径


def _insert(db_path, moment, path, visitor="f" * 32, src="fp"):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO visit_event (day, path, visitor, visitor_src, referrer, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                moment.strftime("%Y-%m-%d"),
                path,
                visitor,
                src,
                None,
                moment.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    from app.stats import clear_stats_cache

    clear_stats_cache()


def test_summary_defaults_to_week_and_matches_week(client, clean_stats):
    _hit(client, path="/taptap", visitor_id=CID_A)
    _hit(client, path="/business", visitor_id=CID_B)

    summary = _summary(client).json()["data"]
    week = client.get(WEEK).json()["data"]

    assert summary["range"] == "week"
    assert summary["granularity"] == "day"
    assert summary["pv"] == week["pv"] == 2
    assert summary["uv"] == week["uv"] == 2
    assert summary["top_paths"] == week["top_paths"]
    assert summary["week_start"] == week["week_start"]
    assert summary["start"] == week["week_start"]


def test_summary_series_is_contiguous(client, clean_stats):
    _hit(client, path="/taptap", visitor_id=CID_A)

    data = _summary(client, "7d").json()["data"]
    assert data["range"] == "7d"
    assert len(data["series"]) == 7
    dates = [point["date"] for point in data["series"]]
    assert dates == sorted(dates)
    assert data["series"][-1]["pv"] == 1  # 今天
    assert data["series"][-1]["date"] == data["end"]
    assert data["series"][0]["date"] == data["start"]
    assert sum(point["pv"] for point in data["series"]) == data["pv"]


def test_summary_today(client, clean_stats):
    _hit(client, path="/taptap", visitor_id=CID_A)

    data = _summary(client, "today").json()["data"]
    assert data["range"] == "today"
    assert data["start"] == data["end"]
    assert len(data["series"]) == 1
    assert data["pv"] == 1


def test_summary_all_uses_first_record(client, clean_stats, stats_db_path):
    now = datetime.now()
    _insert(stats_db_path, now - timedelta(days=40), "/a")
    _insert(stats_db_path, now - timedelta(days=10), "/b")
    _insert(stats_db_path, now - timedelta(days=2), "/c")

    data = _summary(client, "all").json()["data"]
    assert data["range"] == "all"
    assert data["pv"] == 3
    assert data["start"] <= (now - timedelta(days=40)).strftime("%Y-%m-%d")
    assert sum(point["pv"] for point in data["series"]) == 3


def test_summary_7d_excludes_old_records(client, clean_stats, stats_db_path):
    now = datetime.now()
    _insert(stats_db_path, now - timedelta(days=1), "/recent")
    _insert(stats_db_path, now - timedelta(days=20), "/old")

    data = _summary(client, "7d").json()["data"]
    assert data["pv"] == 1
    assert data["top_paths"][0]["path"] == "/recent"


def test_summary_rejects_bad_range(client, clean_stats):
    resp = _summary(client, "yesterday")
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40000
    assert "range" in body["message"]


def test_summary_empty_still_has_series(client, clean_stats):
    data = _summary(client, "30d").json()["data"]
    assert data["pv"] == 0
    assert data["uv"] == 0
    assert len(data["series"]) == 30
    assert all(point["pv"] == 0 for point in data["series"])


# ---------------------------------------------------------------- 路由契约


def test_stats_route_prefix_matches_contract(client):
    """路由前缀与前端契约一致，且与 /api/v09/data 并列。"""
    body = client.get("/openapi.json").json()
    assert HIT in body["paths"]
    assert WEEK in body["paths"]
    assert SUMMARY in body["paths"]
    assert "/api/v09/data/sql" in body["paths"]


def test_stats_alias_paths_work(client, clean_stats):
    """前端默认的 /api/stats 别名同样可用（不进 openapi）。"""
    headers = {"User-Agent": CHROME, "X-Forwarded-For": "10.2.2.2"}
    resp = client.post("/api/stats/hit", json={"path": "/alias"}, headers=headers)
    assert resp.json()["data"]["counted"] is True

    data = client.get("/api/stats/week").json()["data"]
    assert data["pv"] == 1
    assert data["top_paths"][0]["path"] == "/alias"

    summary = client.get("/api/stats/summary", params={"range": "7d"}).json()["data"]
    assert summary["range"] == "7d"
    assert summary["pv"] == 1

    # 别名与主前缀共用同一份数据与同一个路由，openapi 里不重复列出
    assert "/api/stats/hit" not in client.get("/openapi.json").json()["paths"]
