"""MySQL 连接管理与 SQL 执行。

查询一律走 SQLAlchemy Core 的 driver-level 执行接口：语句原样下发，
不做任何改写，参数通过驱动占位符绑定。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .config import ConfigError, get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_engine_lock = threading.Lock()

# 只读模式下允许的语句类型
_READ_PREFIXES = ("select", "with", "show", "desc", "describe", "explain")
_READABLE = "/".join(prefix.upper() for prefix in _READ_PREFIXES)
# 只读模式下额外禁止的片段（绕过前缀检查的写操作）
_FORBIDDEN_PATTERNS = re.compile(
    r"\b(into\s+outfile|into\s+dumpfile|for\s+update|lock\s+in\s+share\s+mode|"
    r"insert|update|delete|drop|alter|truncate|create|grant|revoke|rename)\b",
    re.IGNORECASE,
)


class SqlGuardError(ValueError):
    """SQL 被安全检查拒绝。"""


class SqlExecutionError(RuntimeError):
    """数据库执行失败。"""


@dataclass(slots=True)
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    truncated: bool = False


def get_engine() -> Engine:
    """进程内单例引擎；首次创建时读取环境变量。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            settings = get_settings()
            url = settings.mysql_url()
            if isinstance(url, str) and url.startswith("sqlite"):
                _engine = create_engine(
                    url, future=True, connect_args={"check_same_thread": False}
                )
            else:
                _engine = create_engine(
                    url,
                    future=True,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    pool_size=5,
                    max_overflow=10,
                    connect_args={"connect_timeout": 10},
                )
            logger.info("数据库引擎已创建：%s", _engine.url.render_as_string(hide_password=True))
    return _engine


def reset_engine() -> None:
    """释放单例（测试或配置热更新后调用）。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def validate_sql(sql: str, allow_write: bool) -> str:
    """校验语句，返回去掉尾部分号的 SQL。"""
    if not sql or not sql.strip():
        raise SqlGuardError("SQL 不能为空")

    body = sql.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        raise SqlGuardError("SQL 不能为空")

    if ";" in body:
        raise SqlGuardError("不支持一次提交多条语句")

    if allow_write:
        return body

    head = body.lstrip("( \r\n\t").split(None, 1)[0].lower()
    if head not in _READ_PREFIXES:
        raise SqlGuardError(
            f"只读模式下只允许 {_READABLE} 语句，收到：{head.upper()}"
        )
    match = _FORBIDDEN_PATTERNS.search(body)
    if match:
        raise SqlGuardError(f"只读模式下不允许包含 {match.group(0).upper()}")
    return body


def _apply_timeout(conn, timeout_ms: int) -> None:
    """MySQL 5.7.8+ 支持按会话设置执行超时；其它方言忽略。"""
    if conn.dialect.name != "mysql":
        return
    try:
        conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}")
    except SQLAlchemyError:  # pragma: no cover - 老版本 MySQL 不支持
        logger.debug("设置 MAX_EXECUTION_TIME 失败，忽略", exc_info=True)


def run_query(
    sql: str,
    params: Mapping[str, Any] | Sequence[Any] | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """执行查询并返回结果集（最多 max_rows + 1 行用于判断截断）。"""
    settings = get_settings()
    limit = max_rows if max_rows is not None else settings.api_max_rows
    limit = max(1, min(int(limit), settings.api_max_rows))

    engine = get_engine()
    try:
        with engine.connect() as conn:
            _apply_timeout(conn, settings.api_query_timeout_ms)
            result = conn.exec_driver_sql(sql, params)
            if result.returns_rows is False:  # 非查询语句
                return QueryResult(columns=[], rows=[], total=0)
            columns = list(result.keys())
            fetched = result.fetchmany(limit + 1)
    except DBAPIError as exc:
        raise SqlExecutionError(_format_db_error(exc)) from exc
    except SQLAlchemyError as exc:
        raise SqlExecutionError(str(exc)) from exc

    truncated = len(fetched) > limit
    rows = fetched[:limit]
    return QueryResult(
        columns=columns,
        rows=[dict(zip(columns, row, strict=False)) for row in rows],
        total=len(rows),
        truncated=truncated,
    )


def _format_db_error(exc: DBAPIError) -> str:
    orig = getattr(exc, "orig", None)
    if orig is not None and getattr(orig, "args", None):
        return str(orig.args[0])
    return str(exc.orig if orig is not None else exc)


__all__ = [
    "ConfigError",
    "QueryResult",
    "SqlExecutionError",
    "SqlGuardError",
    "get_engine",
    "reset_engine",
    "run_query",
    "validate_sql",
]
