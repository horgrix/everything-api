"""接口契约测试。"""

from __future__ import annotations

SQL_PATH = "/api/data/sql"
TABLE_PATH = "/api/data/dws_taptap_download_daily/query"


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


def test_table_query_with_where_and_order(client):
    resp = client.get(
        TABLE_PATH,
        params={
            "fields": "game_name,download_count",
            "where": '[{"col":"download_count","op":">=","value":1000}]',
            "order_by": "download_count DESC",
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["total"] == 2
    assert body["data"][0]["download_count"] == 1500
    assert set(body["data"][0]) == {"game_name", "download_count"}


def test_table_query_with_count(client):
    resp = client.get(TABLE_PATH, params={"with_count": True, "limit": 1})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["data"]) == 1


def test_table_query_group_by_aggregate(client):
    resp = client.get(
        TABLE_PATH,
        params={
            "fields": "game_name",
            "aggregate": "SUM(download_count) AS total_download",
            "group_by": "game_name",
            "order_by": "total_download DESC",
        },
    )
    body = resp.json()
    assert body["data"][0]["game_name"] == "火炬之光"
    assert body["data"][0]["total_download"] == 2700


def test_table_query_rejects_bad_identifier(client):
    resp = client.get("/api/data/dws;drop/query")
    assert resp.status_code == 400
    assert resp.json()["code"] == 40004


def test_list_tables(client):
    body = client.get("/api/data/tables").json()
    assert body["code"] == 0
    assert "dws_taptap_download_daily" in body["data"]
