"""数据查询接口。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from ..config import ConfigError, get_settings
from ..db import (
    SqlExecutionError,
    SqlGuardError,
    get_engine,
    run_query,
    validate_sql,
)
from ..errors import (
    CODE_DB_UNAVAILABLE,
    CODE_SQL_ERROR,
    CODE_SQL_GUARD,
    error_response,
    ok,
)
from ..schemas import SqlQueryRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v09/data", tags=["data"])


@router.post("/sql", summary="直接执行 SQL")
def execute_sql(payload: SqlQueryRequest) -> Any:
    settings = get_settings()
    try:
        sql = validate_sql(payload.sql, settings.api_allow_write)
    except SqlGuardError as exc:
        return error_response(CODE_SQL_GUARD, str(exc), 403)

    try:
        result = run_query(sql, payload.params, payload.max_rows)
    except ConfigError as exc:
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    except SqlExecutionError as exc:
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    return ok(result.rows, total=result.total, truncated=result.truncated)


@router.get("/tables", summary="列出库中的表")
def list_tables() -> Any:
    try:
        engine = get_engine()
    except ConfigError as exc:
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)

    dialect = engine.dialect.name
    if dialect == "mysql":
        sql = "SHOW FULL TABLES"
    elif dialect == "sqlite":
        sql = "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    else:
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        )

    try:
        result = run_query(sql)
    except SqlExecutionError as exc:
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    names = [list(row.values())[0] for row in result.rows if row]
    return ok(names, total=len(names))
