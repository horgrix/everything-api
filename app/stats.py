"""访问统计（PV/UV）核心逻辑。

与通用 SQL 查询完全隔离：
- 读走主引擎（只读账号即可）；
- 写走独立引擎（`API_STATS_DATABASE_URL` 或 `API_STATS_MYSQL_*`），
  默认复用 `HK_DB_MYSQL_*` 的主机 / 库名，只有需要 INSERT 时才另配账号。

口径：
- 时区固定 Asia/Shanghai，统计窗口按自然日对齐；
- PV = 明细行数，每次上报算 1 次（写入不做去重，防连点交给前端路由级去重）；
- UV = 窗口内去重的独立访客数；
- 访客身份 = 前端第一方匿名 ID 的 sha256 前 32 位；前端没带 ID 时
  回退到 sha256(IP + UA + 盐) 前 32 位。原 IP 不入库。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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

# 前端种的第一方匿名 ID：uuid4 / nanoid / 自定义串，统一收窄字符集
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# 爬虫 / 监控 / 命令行工具的 UA 片段
_BOT_UA = re.compile(
    r"bot|spider|crawl|slurp|scrapy|headless|phantom|"
    r"python-requests|python-urllib|aiohttp|httpx|curl/|wget|"
    r"go-http-client|java/|okhttp|libwww|httpclient|"
    r"monitor|uptime|pingdom|zgrab|nmap|masscan|"
    r"facebookexternalhit|preview|node-fetch|axios/",
    re.IGNORECASE,
)

# 支持的时间窗口；不变参数时走 week（老行为）
SUPPORTED_RANGES: tuple[str, ...] = ("week", "7d", "30d", "today", "all")
DEFAULT_RANGE = "week"

# 超过这个天数用「月」做 series 粒度，避免一次返回几百个点
MAX_DAILY_POINTS = 180
MAX_BUCKETS = 400

# 访问统计的访客身份来源（优先级从高到低）
SRC_SSO = "sso"
SRC_CLIENT_ID = "cid"
SRC_FINGERPRINT = "fp"

_stats_engine: Engine | None = None
_stats_engine_lock = threading.Lock()

_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stats_cache_lock = threading.Lock()

_purge_state: dict[str, float] = {"last": 0.0}
_purge_lock = threading.Lock()


class StatsError(RuntimeError):
    """访问统计相关的配置或执行错误。"""


class RangeError(ValueError):
    """`?range=` 取值不合法。"""


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
    """兜底身份：sha256(IP + UA + 盐) 前 32 位。"""
    raw = f"{ip}|{ua}|{salt}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def visitor_id_hash(client_id: str, salt: str) -> str:
    """前端第一方匿名 ID 的哈希，加前缀避免与 IP/UA 指纹撞在同一命名空间。"""
    raw = f"cid:{client_id}|{salt}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def normalize_client_id(value: str | None) -> str | None:
    """校验前端送来的匿名 ID；不合规一律当作没带。"""
    candidate = (value or "").strip()
    if not candidate or not _CLIENT_ID_RE.match(candidate):
        return None
    return candidate


def visitor_sso_hash(sso_user: str, salt: str) -> str:
    """SSO 用户（Keycloak sub）的哈希；同一账号跨设备、跨 IP 恒等。"""
    raw = f"sso:{sso_user.strip().lower()}|{salt}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def resolve_visitor(
    client_id: str | None,
    ip: str,
    ua: str,
    salt: str,
    sso_user: str | None = None,
) -> tuple[str, str]:
    """返回 (visitor, visitor_src)，按 SSO 用户 > 第一方 ID > IP+UA 指纹取。"""
    if sso_user and sso_user.strip():
        return visitor_sso_hash(sso_user, salt), SRC_SSO
    normalized = normalize_client_id(client_id)
    if normalized:
        return visitor_id_hash(normalized, salt), SRC_CLIENT_ID
    return visitor_hash(ip, ua, salt), SRC_FINGERPRINT


def _as_local(moment: datetime | None = None) -> datetime:
    current = moment or datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    else:
        current = current.astimezone(TZ)
    return current.replace(microsecond=0)


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def week_window(now: datetime | None = None) -> tuple[datetime, str]:
    """返回 (本周一 00:00, 'YYYY-MM-DD HH:MM:SS')，Asia/Shanghai。"""
    current = _as_local(now)
    monday = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, _fmt(monday)


# ---------------------------------------------------------------- 时间窗口


@dataclass(frozen=True)
class Window:
    """一个统计窗口：左闭右闭，按 Asia/Shanghai 的自然日对齐。"""

    name: str
    start: datetime
    end: datetime
    granularity: str  # "day" | "month"

    @property
    def since(self) -> str:
        return _fmt(self.start)

    @property
    def until(self) -> str:
        return _fmt(self.end)

    @property
    def start_date(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def end_date(self) -> str:
        return self.end.strftime("%Y-%m-%d")


def normalize_range(value: str | None) -> str:
    """`?range=` 归一化；非法值抛 RangeError（交给接口返回 40000）。"""
    name = (value or "").strip().lower() or DEFAULT_RANGE
    if name not in SUPPORTED_RANGES:
        raise RangeError(f"range 只支持 {'|'.join(SUPPORTED_RANGES)}，收到 {value!r}")
    return name


def resolve_window(
    name: str,
    now: datetime | None = None,
    earliest: datetime | None = None,
) -> Window:
    """按窗口名算出起止时间与 series 粒度。"""
    current = _as_local(now)
    today = current.replace(hour=0, minute=0, second=0)

    if name == "today":
        start = today
    elif name == "7d":
        start = today - timedelta(days=6)
    elif name == "30d":
        start = today - timedelta(days=29)
    elif name == "all":
        start = today
        if earliest is not None:
            floor = _as_local(earliest).replace(hour=0, minute=0, second=0)
            if floor < start:
                start = floor
    elif name == "week":
        start = today - timedelta(days=current.weekday())
    else:  # pragma: no cover - normalize_range 已挡住
        raise RangeError(f"未知窗口：{name!r}")

    days = (current - start).days + 1
    granularity = "day" if days <= MAX_DAILY_POINTS else "month"
    return Window(name=name, start=start, end=current, granularity=granularity)


def series_buckets(window: Window) -> list[str]:
    """窗口内所有 series 桶（含没有数据的桶，保证曲线连续）。"""
    buckets: list[str] = []
    if window.granularity == "day":
        cursor = window.start.date()
        last = window.end.date()
        while cursor <= last and len(buckets) < MAX_BUCKETS:
            buckets.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return buckets

    year, month = window.start.year, window.start.month
    last_pair = (window.end.year, window.end.month)
    while (year, month) <= last_pair and len(buckets) < MAX_BUCKETS:
        buckets.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year += 1
            month = 1
    return buckets


def _bucket_expr(granularity: str) -> str:
    """series 分组表达式。

    用 `SUBSTR(created_at, 1, n)` 而不是 `DATE()` / `DATE_FORMAT()`：
    MySQL 与 SQLite 行为一致，也避开 `%` 在 text() 里要转义的问题。
    """
    width = 7 if granularity == "month" else 10
    return f"SUBSTR(created_at, 1, {width})"


def _bucket_of(value: Any, granularity: str) -> str | None:
    """把驱动返回的 DATE 值归一成桶 key。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        text_value = value.strftime("%Y-%m-%d")
    elif isinstance(value, date):
        text_value = value.isoformat()
    else:
        text_value = str(value)[:10]
    return text_value[:7] if granularity == "month" else text_value


# ---------------------------------------------------------------- 上报


def record_hit(
    path: str,
    visitor: str,
    referrer: str | None = None,
    visitor_src: str = SRC_FINGERPRINT,
    now: datetime | None = None,
) -> bool:
    """写入一条浏览明细。返回是否写入成功。"""
    table = table_name()
    current = _as_local(now)
    engine = get_stats_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {table} "
                    "(day, path, visitor, visitor_src, referrer, created_at) "
                    "VALUES (:day, :path, :visitor, :visitor_src, :referrer, :created_at)"
                ),
                {
                    "day": current.strftime("%Y-%m-%d"),
                    "path": path,
                    "visitor": visitor,
                    "visitor_src": visitor_src,
                    "referrer": (referrer or None),
                    "created_at": _fmt(current),
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
    current = _as_local(now)
    stamp = current.timestamp()
    with _purge_lock:
        if stamp - _purge_state["last"] < interval_seconds:
            return 0
        _purge_state["last"] = stamp

    table = table_name()
    threshold = _fmt(current - timedelta(days=settings.api_stats_retention_days))
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


# ---------------------------------------------------------------- 聚合


def clear_stats_cache() -> None:
    with _stats_cache_lock:
        _stats_cache.clear()


# 兼容旧名字（测试与老调用方用得到）
clear_week_cache = clear_stats_cache


def _cached(name: str, ttl: int) -> dict[str, Any] | None:
    if not ttl:
        return None
    with _stats_cache_lock:
        hit = _stats_cache.get(name)
        if hit and time.time() - hit[0] < ttl:
            return dict(hit[1])
    return None


def _store_cache(name: str, payload: dict[str, Any], ttl: int) -> None:
    if not ttl:
        return
    with _stats_cache_lock:
        _stats_cache[name] = (time.time(), payload)


def _earliest(conn: Any, table: str) -> datetime | None:
    """`range=all` 的起点：明细里最早的一条。"""
    row = conn.execute(text(f"SELECT MIN(created_at) AS m FROM {table}")).first()
    raw = row[0] if row else None
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw)[:19], fmt)
        except ValueError:
            continue
    logger.warning("无法解析最早访问时间：%r", raw)
    return None


def _rows_to_series(rows: Iterable[Any], window: Window) -> list[dict[str, Any]]:
    """把按桶聚合的行铺平成连续 series（缺失补 0）。"""
    collected: dict[str, dict[str, int]] = {}
    for row in rows:
        key = _bucket_of(row[0], window.granularity)
        if key is None:
            continue
        bucket = collected.setdefault(key, {"pv": 0, "uv": 0})
        bucket["pv"] += int(row[1] or 0)
        bucket["uv"] += int(row[2] or 0)

    return [
        {
            "date": key,
            "pv": collected.get(key, {}).get("pv", 0),
            "uv": collected.get(key, {}).get("uv", 0),
        }
        for key in series_buckets(window)
    ]


def range_stats(
    range_name: str = DEFAULT_RANGE,
    now: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """按窗口返回 PV / UV / Top3 路径 / 按天（月）曲线。"""
    settings = get_settings()
    name = normalize_range(range_name)
    ttl = max(0, settings.api_stats_cache_seconds)

    if use_cache:
        cached = _cached(name, ttl)
        if cached is not None:
            return cached

    table = table_name()
    engine = get_stats_engine()
    try:
        with engine.connect() as conn:
            earliest = _earliest(conn, table) if name == "all" else None
            window = resolve_window(name, now=now, earliest=earliest)
            bucket_expr = _bucket_expr(window.granularity)
            params = {"since": window.since, "until": window.until}

            totals = (
                conn.execute(
                    text(
                        f"SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor) AS uv "
                        f"FROM {table} "
                        "WHERE created_at >= :since AND created_at <= :until"
                    ),
                    params,
                )
                .mappings()
                .first()
                or {}
            )
            top_rows = (
                conn.execute(
                    text(
                        f"SELECT path, COUNT(*) AS pv, COUNT(DISTINCT visitor) AS uv "
                        f"FROM {table} "
                        "WHERE created_at >= :since AND created_at <= :until "
                        "GROUP BY path ORDER BY COUNT(*) DESC, path ASC LIMIT 3"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            series_rows = conn.execute(
                text(
                    f"SELECT {bucket_expr} AS bucket, "
                    "COUNT(*) AS pv, COUNT(DISTINCT visitor) AS uv "
                    f"FROM {table} "
                    "WHERE created_at >= :since AND created_at <= :until "
                    f"GROUP BY {bucket_expr} ORDER BY bucket ASC"
                ),
                params,
            ).all()
    except SQLAlchemyError as exc:
        raise StatsError(f"读取访问统计失败：{exc}") from exc

    monday = window.start if name == "week" else _week_monday(now)
    payload: dict[str, Any] = {
        "range": name,
        "start": window.start_date,
        "end": window.end_date,
        "granularity": window.granularity,
        "week_start": monday.strftime("%Y-%m-%d"),
        "pv": int(totals.get("pv") or 0),
        "uv": int(totals.get("uv") or 0),
        "series": _rows_to_series(series_rows, window),
        "top_paths": [
            {"path": row["path"], "pv": int(row["pv"] or 0), "uv": int(row["uv"] or 0)}
            for row in top_rows
        ],
    }
    _store_cache(name, payload, ttl)
    return dict(payload)


def _week_monday(now: datetime | None = None) -> datetime:
    monday, _ = week_window(now)
    return monday


def week_stats(now: datetime | None = None, use_cache: bool = True) -> dict[str, Any]:
    """老契约：只要 week_start / pv / uv / top_paths，不带 range 相关字段。"""
    data = range_stats("week", now=now, use_cache=use_cache)
    return {
        "week_start": data["week_start"],
        "pv": data["pv"],
        "uv": data["uv"],
        "top_paths": data["top_paths"],
    }


def empty_range(range_name: str = DEFAULT_RANGE, now: datetime | None = None) -> dict[str, Any]:
    """接口降级时的空结构，字段与正常响应一致（series 也补齐窗口）。"""
    try:
        name = normalize_range(range_name)
    except RangeError:
        name = DEFAULT_RANGE
    window = resolve_window(name, now=now)
    return {
        "range": name,
        "start": window.start_date,
        "end": window.end_date,
        "granularity": window.granularity,
        "week_start": _week_monday(now).strftime("%Y-%m-%d"),
        "pv": 0,
        "uv": 0,
        "series": [{"date": key, "pv": 0, "uv": 0} for key in series_buckets(window)],
        "top_paths": [],
    }


def empty_week(now: datetime | None = None) -> dict[str, Any]:
    """老契约的降级空结构。"""
    return {
        "week_start": _week_monday(now).strftime("%Y-%m-%d"),
        "pv": 0,
        "uv": 0,
        "top_paths": [],
    }


__all__ = [
    "ConfigError",
    "DEFAULT_RANGE",
    "RangeError",
    "SRC_CLIENT_ID",
    "SRC_FINGERPRINT",
    "SRC_SSO",
    "SUPPORTED_RANGES",
    "StatsError",
    "Window",
    "clear_stats_cache",
    "clear_week_cache",
    "client_ip",
    "empty_range",
    "empty_week",
    "get_stats_engine",
    "is_bot_ua",
    "maybe_purge",
    "normalize_client_id",
    "normalize_path",
    "normalize_range",
    "range_stats",
    "record_hit",
    "reset_stats_engine",
    "resolve_visitor",
    "resolve_window",
    "series_buckets",
    "table_name",
    "visitor_hash",
    "visitor_id_hash",
    "visitor_sso_hash",
    "week_stats",
    "week_window",
]
