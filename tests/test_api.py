"""接口契约测试。"""

from __future__ import annotations

SQL_PATH = "/api/data/sql"


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
    body = client.get("/api/data/tables").json()
    assert body["code"] == 0
    assert "dws_taptap_download_daily" in body["data"]
