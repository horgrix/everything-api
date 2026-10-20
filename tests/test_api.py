"""接口契约测试。"""

from __future__ import annotations

PREFIX = "/api/v09/data"
SQL_PATH = f"{PREFIX}/sql"
TABLES_PATH = f"{PREFIX}/tables"


def test_health(client):
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["database_configured"] is True


def test_sql_select(client):
    resp = client.post(SQL_PATH, json={"sql": "SELECT * FROM dws_taptap_download_daily"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["total"] == 3
    assert body["data"][0]["game_name"] == "火炬之光"
    assert body["truncated"] is False


def test_sql_named_params(client):
    resp = client.post(
        SQL_PATH,
        json={
            "sql": "SELECT game_name FROM dws_taptap_download_daily WHERE dt = :dt ORDER BY id",
            "params": {"dt": "2026-10-02"},
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["total"] == 1
    assert body["data"][0]["game_name"] == "火炬之光"


def test_sql_max_rows_truncates(client):
    resp = client.post(
        SQL_PATH, json={"sql": "SELECT * FROM dws_taptap_download_daily", "max_rows": 2}
    )
    body = resp.json()
    assert body["total"] == 2
    assert body["truncated"] is True


def test_sql_rejects_multiple_statements(client):
    resp = client.post(SQL_PATH, json={"sql": "SELECT 1; SELECT 2"})
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


def test_sql_rejects_write_in_readonly_mode(client):
    resp = client.post(SQL_PATH, json={"sql": "DELETE FROM dws_taptap_download_daily"})
    assert resp.status_code == 403
    assert "只读" in resp.json()["message"]


def test_sql_reports_database_error(client):
    resp = client.post(SQL_PATH, json={"sql": "SELECT * FROM not_exists_table"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_sql_empty_body_rejected(client):
    resp = client.post(SQL_PATH, json={"sql": "   "})
    assert resp.status_code == 403


def test_list_tables(client):
    body = client.get(TABLES_PATH).json()
    assert body["code"] == 0
    assert "dws_taptap_download_daily" in body["data"]


# ---------------- 跨域 ----------------

ORIGIN = "http://localhost:5173"


def test_cors_preflight(client):
    resp = client.options(
        SQL_PATH,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert resp.headers["access-control-max-age"] == "600"


def test_cors_header_on_success_and_error(client):
    ok_resp = client.post(
        SQL_PATH, json={"sql": "SELECT 1 AS v"}, headers={"Origin": ORIGIN}
    )
    assert ok_resp.status_code == 200
    assert ok_resp.headers["access-control-allow-origin"] == "*"

    guarded = client.post(
        SQL_PATH,
        json={"sql": "DELETE FROM dws_taptap_download_daily"},
        headers={"Origin": ORIGIN},
    )
    assert guarded.status_code == 403
    assert guarded.headers["access-control-allow-origin"] == "*"


def test_cors_options_for_restricted_origins_and_credentials():
    from app.config import Settings
    from app.main import build_cors_options

    restricted = build_cors_options(
        Settings(api_cors_origins="https://a.example.com, https://b.example.com")
    )
    assert restricted["allow_origins"] == ["https://a.example.com", "https://b.example.com"]
    assert restricted["allow_credentials"] is False

    wildcard_with_credentials = build_cors_options(
        Settings(api_cors_origins="*", api_cors_allow_credentials=True)
    )
    assert wildcard_with_credentials["allow_origin_regex"] == ".*"
    assert wildcard_with_credentials["allow_credentials"] is True
    assert "allow_origins" not in wildcard_with_credentials
