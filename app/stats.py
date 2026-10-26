"""访问统计（PV/UV）核心逻辑。

与通用 SQL 查询完全隔离：
- 读走主引擎（只读账号即可）；
- 写走独立引擎（`API_STATS_DATABASE_URL` 或 `API_STATS_MYSQL_*`），
  默认复用 `HK_DB_MYSQL_*` 的主机 / 库名，只有需要 INSERT 时才另配账号。

口径（与前端契约一致）：
- 「本周」= 本周一 00:00:00 ~ 现在，时区 Asia/Shanghai；
- PV = 页面浏览次数；UV = 本周去重的独立访客数；
- 访客匿名标识 = sha256(IP + UA + 盐) 前 32 位，不落原 IP；
- 同一 (visitor, path) 在 `API_STATS_DEDUP_MINUTES` 分钟内重复上报只算 1 次。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import ConfigError, get_settings

logger = logging.getLogger(__name__)

try:  # Windows 默认没有系统时区库，装了 tzdata 才会命中
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 回退到 UTC+8（上海无夏令时，与 ZoneInfo 等价）
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 表名会被拼进 SQL（方言不支持绑定表名），只允许标识符字符
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# 爬虫 / 监控 / 命令行工具的 UA 片段
_BOT_UA = re.compile(
    r"bot|spider|crawl|slurp|scrapy|headless|phantom|"
    r"python-requests|python-urllib|aiohttp|httpx|curl/|wget|"
    r"go-http-client|java/|okhttp|libwww|httpclient|"
    r"monitor|uptime|pingdom|zgrab|nmap|masscan|"
    r"facebookexternalhit|preview|node-fetch|axios/",
    re.IGNORECASE,
)

_stats_engine: Engine | None = None
_stats_engine_lock = threading.Lock()

_week_cache: dict[str, Any] = {}
_week_cache_lock = threading.Lock()

_purge_state: dict[str, float] = {"last": 0.0}
_purge_lock = threading.Lock()


class StatsError(RuntimeError):
    """访问统计相关的配置或执行错误。"""


# ---------------------------------------------------------------- 引擎


def get_stats_engine() -> Engine:
    """写库引擎单例；未单独配置时复用主库连接信息。"""
    global _stats_engine
    with _stats_engine_lock:
        if _stats_engine is None:
            settings = get_settings()
            url = settings.stats_url()
            if isinstance(url, str) and url.startswith("sqlite"):
                _stats_engine = create_engine(
                    url, future=True, connect_args={"check_same_thread": False}
                )
            else:
                _stats_engine = create_engine(
                    url,
                    future=True,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    pool_size=2,
                    max_overflow=4,
                    connect_args={"connect_timeout": 10},
                )
            logger.info(
                "访问统计写库引擎已创建：%s",
                _stats_engine.url.render_as_string(hide_password=True),
            )
    return _stats_engine


def reset_stats_engine() -> None:
    """释放单例（测试或配置变更后调用）。"""
    global _stats_engine
    with _stats_engine_lock:
        if _stats_engine is not None:
            _stats_engine.dispose()
        _stats_engine = None


# ---------------------------------------------------------------- 工具函数


def table_name() -> str:
    name = get_settings().api_stats_table
    if not _TABLE_RE.match(name):
        raise StatsError(f"API_STATS_TABLE 不合法：{name!r}")
    return name


def is_bot_ua(ua: str | None) -> bool:
    """空 UA 与爬虫 UA 一律不计数。"""
    if not ua or not ua.strip():
        return True
    return bool(_BOT_UA.search(ua))


def normalize_path(raw: str | None, max_length: int = 128) -> str:
    """只保留 pathname：去掉 query / hash / 空白，保证以 / 开头并限长。"""
    value = (raw or "").strip()
    for sep in ("#", "?"):
        index = value.find(sep)
        if index != -1:
            value = value[:index]
    value = "".join(ch for ch in value if ch.isprintable() and not ch.isspace())
    if not value.startswith("/"):
        value = "/" + value
    return value[:max_length]


def client_ip(headers: dict[str, str], fallback: str | None) -> str:
    """优先取反代写入的 X-Forwarded-For 首个地址。"""
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return (fallback or "").strip() or "unknown"


def visitor_hash(ip: str, ua: str, salt: str) -> str:
    raw = f"{ip}|{ua}|{salt}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def week_window(now: datetime | None = None) -> tuple[datetime, str]:
    """返回 (本周一 00:00, 'YYYY-MM-DD HH:MM:SS')，Asia/Shanghai。"""
    current = now or datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    else:
        current = current.astimezone(TZ)
    monday = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 上报


def record_hit(
    path: str,
    visitor: str,
    referrer: str | None = None,
    now: datetime | None = None,
) -> bool:
    """写入一条浏览记录；命中 30 分钟防连点则返回 False。"""
    settings = get_settings()
    table = table_name()
    current = now or datetime.now(TZ)
    created_at = current.strftime("%Y-%m-%d %H:%M:%S")
    day = current.strftime("%Y-%m-%d")
    since = (current - timedelta(minutes=settings.api_stats_dedup_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    engine = get_stats_engine()
    try:
        with engine.begin() as conn:
            duplicated = conn.execute(
                text(
                    f"SELECT 1 FROM {table} "
                    "WHERE visitor = :visitor AND path = :path AND created_at >= :since "
                    "LIMIT 1"
                ),
                {"visitor": visitor, "path": path, "since": since},
            ).first()
            if duplicated:
                return False
            conn.execute(
                text(
                    f"INSERT INTO {table} (day, path, visitor, referrer, created_at) "
                    "VALUES (:day, :path, :visitor, :referrer, :created_at)"
                ),
                {
                    "day": day,
                    "path": path,
                    "visitor": visitor,
                    "referrer": (referrer or None),
                    "created_at": created_at,
                },
            )
    except SQLAlchemyError as exc:
        raise StatsError(f"写入浏览记录失败：{exc}") from exc

    maybe_purge(now=current)
    return True


def maybe_purge(now: datetime | None = None, interval_seconds: int = 3600) -> int:
    """每小时最多清理一次过期数据（默认保留 180 天）。返回删除行数。"""
    settings = get_settings()
    if settings.api_stats_retention_days <= 0:
        return 0
    current = now or datetime.now(TZ)
    stamp = current.timestamp()
    with _purge_lock:
        if stamp - _purge_state["last"] < interval_seconds:
            return 0
        _purge_state["last"] = stamp

    table = table_name()
    threshold = (current - timedelta(days=settings.api_stats_retention_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        with get_stats_engine().begin() as conn:
            result = conn.execute(
                text(f"DELETE FROM {table} WHERE created_at < :threshold"),
                {"threshold": threshold},
            )
            deleted = result.rowcount or 0
    except SQLAlchemyError as exc:
        logger.warning("清理过期浏览记录失败：%s", exc)
        return 0
    if deleted:
        logger.info("清理过期浏览记录 %s 行（早于 %s）", deleted, threshold)
    return deleted


# ---------------------------------------------------------------- 本周聚合


def clear_week_cache() -> None:
    with _week_cache_lock:
        _week_cache.clear()


def week_stats(now: datetime | None = None, use_cache: bool = True) -> dict[str, Any]:
    """本周 PV / UV 与 Top3 路径。无数据时返回 0 / [] 而不是 null。"""
    settings = get_settings()
    monday, since = week_window(now)
    ttl = max(0, settings.api_stats_cache_seconds)

    if use_cache and ttl:
        with _week_cache_lock:
            cached = _week_cache.get("week")
            if cached and time.time() - cached[0] < ttl:
                return dict(cached[1])

    table = table_name()
    engine = get_stats_engine()
    try:
        with engine.connect() as conn:
            totals = (
                conn.execute(
                    text(
                        f"SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor) AS uv "
                        f"FROM {table} WHERE created_at >= :since"
                    ),
                    {"since": since},
                )
                .mappings()
                .first()
                or {}
            )
            rows = (
                conn.execute(
                    text(
                        f"SELECT path, COUNT(*) AS pv, COUNT(DISTINCT visitor) AS uv "
                        f"FROM {table} WHERE created_at >= :since "
                        "GROUP BY path ORDER BY COUNT(*) DESC, path ASC LIMIT 3"
                    ),
                    {"since": since},
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as exc:
        raise StatsError(f"读取访问统计失败：{exc}") from exc

    payload: dict[str, Any] = {
        "week_start": monday.strftime("%Y-%m-%d"),
        "pv": int(totals.get("pv") or 0),
        "uv": int(totals.get("uv") or 0),
        "top_paths": [
            {"path": row["path"], "pv": int(row["pv"] or 0), "uv": int(row["uv"] or 0)}
            for row in rows
        ],
    }

    if use_cache and ttl:
        with _week_cache_lock:
            _week_cache["week"] = (time.time(), payload)
    return dict(payload)


def empty_week(now: datetime | None = None) -> dict[str, Any]:
    """接口降级时返回的空结构，字段与正常响应一致。"""
    monday, _ = week_window(now)
    return {
        "week_start": monday.strftime("%Y-%m-%d"),
        "pv": 0,
        "uv": 0,
        "top_paths": [],
    }


__all__ = [
    "ConfigError",
    "StatsError",
    "clear_week_cache",
    "client_ip",
    "empty_week",
    "get_stats_engine",
    "is_bot_ua",
    "maybe_purge",
    "normalize_path",
    "record_hit",
    "reset_stats_engine",
    "table_name",
    "visitor_hash",
    "week_stats",
    "week_window",
]
