"""测试用固定装置：用临时 SQLite 库替代 MySQL，验证接口契约。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.mkdtemp(prefix="everything-api-test-"))
_DB_PATH = _TMP_DIR / "test.db"


def _seed() -> None:
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE dws_taptap_download_daily (
                id INTEGER PRIMARY KEY,
                game_name TEXT NOT NULL,
                download_count INTEGER NOT NULL,
                dt TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_download_daily "
            "(game_name, download_count, dt) VALUES (?, ?, ?)",
            [
                ("火炬之光", 1200, "2026-10-01"),
                ("心动小镇", 980, "2026-10-01"),
                ("火炬之光", 1500, "2026-10-02"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def client():
    _seed()
    os.environ["API_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
    os.environ["API_MAX_ROWS"] = "100"

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
