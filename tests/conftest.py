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
        # 访问统计表（MySQL 侧见 sql/visit_event.sql，这里放 SQLite 等价结构）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visit_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                visitor TEXT NOT NULL,
                referrer TEXT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def clear_visit_event() -> None:
    """清空访问明细，并让本周统计的进程内缓存失效。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.execute("DELETE FROM visit_event")
        conn.commit()
    finally:
        conn.close()

    from app.stats import clear_week_cache

    clear_week_cache()


@pytest.fixture(scope="session")
def client():
    _seed()
    os.environ["API_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
    os.environ["API_MAX_ROWS"] = "100"
    # 访问统计：测试用固定盐，写库指向同一个 SQLite
    os.environ["API_STATS_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
    os.environ["API_STATS_SALT"] = "test-salt"
    os.environ["API_STATS_CACHE_SECONDS"] = "0"

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stats_db_path():
    """测试库文件路径，方便用例直接造历史数据。"""
    return _DB_PATH


@pytest.fixture
def clean_stats():
    """每条统计用例开始前清空访问明细，保证 PV/UV 断言是确定值。"""
    clear_visit_event()
    yield
    clear_visit_event()
