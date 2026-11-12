"""首页概览接口（服务端聚合，前端不再直连 MySQL 拼 SQL）。

两个只读接口（挂在 `/api/v09/data/home` 下 —— 线上 nginx 只反代了 `/api/v09/data/`，
这样不用改 nginx 就能生效；另外保留 `/api/v09/home` 与 `/api/home` 两个别名）：
- `GET .../home/active-apps`  本月「采集活跃 APP 数」（4 个指标）
- `GET .../home/type-share`   本月「游戏类型占比」（商业 / 独立 / Maker）

数据源（均为 MySQL 的预聚合物化表，按 `period_type='monthly'` 取当月快照）：
- `dws_taptap_active_game_stat`                       活跃 APP 计数（monthly + daily）
- `dws_taptap_download_periodic_report`               下载量 / 游戏数（category_type='dist'）
- `dws_taptap_pc_online_peak_players_periodic_report` 在线峰值（category_type='dist'）
- `dws_taptap_ad_aggr_v6_periodic_report`             广告曝光（category_type='dist'）

表名是模块内常量、`month` 走正则校验后再拼进 SQL —— 没有可注入的入口，
所以这里用字面量而不是绑定参数（`run_query` 走驱动层，占位符风格随方言变化，
字面量在 MySQL 与测试用的 SQLite 下行为一致）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from ..config import ConfigError
from ..db import SqlExecutionError, run_query
from ..errors import CODE_DB_UNAVAILABLE, CODE_SQL_ERROR, error_response, ok

try:  # 与 stats 模块保持同一时区口径
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 回退到 UTC+8（上海无夏令时）
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

logger = logging.getLogger(__name__)
# 路由先挂在无前缀的子路由上，文件末尾再分别挂到主前缀与 /api/home 别名
routes = APIRouter()

# 表名常量（不可由请求控制）
ACTIVE_STAT_TABLE = "dws_taptap_active_game_stat"
DOWNLOAD_REPORT_TABLE = "dws_taptap_download_periodic_report"
ONLINE_REPORT_TABLE = "dws_taptap_pc_online_peak_players_periodic_report"
AD_REPORT_TABLE = "dws_taptap_ad_aggr_v6_periodic_report"
DOWNLOAD_RANK_TABLE = "dws_taptap_download_periodic_rank"
AD_RANK_TABLE = "dws_taptap_ad_aggr_v6_periodic_rank"

# 活跃 APP 的 4 个指标列
ACTIVE_COLUMNS = (
    "ad_app_cnt",
    "download_app_cnt",
    "online_players_app_cnt",
    "reserve_app_cnt",
)

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
_MONTH_RE = re.compile(MONTH_PATTERN)

# distribution_type：0 商业 / 1 独立 / 2 Maker
DIST_TYPES = ("0", "1", "2")

# 榜单的三种排名：总量榜 / 增量榜 / 下滑榜（顺序即前端展示顺序）
RANK_CATEGORIES = ("top", "gain", "loss")


@dataclass(frozen=True)
class RankSource:
    """一个榜单的数据源：表名 + 三个数值列名（各表列名不同，原样透出给前端）。"""

    table: str
    total: str
    prev: str
    diff: str
    label: str


# 下载榜（下载量）/ 广告榜（广告曝光次数）
DOWNLOAD_RANK = RankSource(
    DOWNLOAD_RANK_TABLE, "total_download", "prev_total_download", "diff_download", "下载榜"
)
AD_RANK = RankSource(AD_RANK_TABLE, "total_views", "prev_total_views", "diff_views", "广告榜")


def _current_month() -> str:
    return datetime.now(TZ).strftime("%Y-%m")


def _month_bounds(month: str | None) -> tuple[str, str, str]:
    """返回 (YYYY-MM, 月初日期, 次月月初日期)；month 不合法抛 ValueError。"""
    key = month or _current_month()
    if not _MONTH_RE.match(key):
        raise ValueError(f"month 需为 YYYY-MM，收到：{month!r}")
    year, mon = (int(part) for part in key.split("-"))
    start = f"{key}-01"
    nxt_year, nxt_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return key, start, f"{nxt_year:04d}-{nxt_mon:02d}-01"


def _as_int(value: Any) -> int | None:
    """把驱动返回的 Decimal / str 统一成 int；NULL 保持 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query(sql: str, max_rows: int = 2000) -> list[dict[str, Any]]:
    return run_query(sql, max_rows=max_rows).rows


@routes.get("/active-apps", summary="本月采集活跃 APP 数（月度快照 + 每日明细）")
def active_apps(
    month: str | None = Query(
        default=None,
        pattern=MONTH_PATTERN,
        description="统计月份 YYYY-MM，缺省取服务器当前月（Asia/Shanghai）",
        examples=["2026-11"],
    ),
) -> Any:
    try:
        month_key, start, end = _month_bounds(month)
    except ValueError as exc:
        return error_response(40000, str(exc), 400)

    sql = (
        "SELECT period_type, crawled_at, "
        + ", ".join(ACTIVE_COLUMNS)
        + f"""
          FROM {ACTIVE_STAT_TABLE}
         WHERE (period_type = 'monthly' AND crawled_at = '{month_key}')
            OR (period_type = 'daily' AND crawled_at >= '{start}' AND crawled_at < '{end}')
         ORDER BY crawled_at ASC"""
    )
    try:
        rows = _query(sql, max_rows=100)
    except ConfigError as exc:
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    except SqlExecutionError as exc:
        logger.warning("活跃 APP 查询失败：%s", exc)
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    monthly_row = next((r for r in rows if str(r.get("period_type")) == "monthly"), None)
    daily_rows = [r for r in rows if str(r.get("period_type")) == "daily"]
    daily_rows.sort(key=lambda r: str(r.get("crawled_at") or ""))

    monthly = None
    if monthly_row is not None:
        monthly = {col: _as_int(monthly_row.get(col)) for col in ACTIVE_COLUMNS}

    daily = [
        {
            "date": str(row.get("crawled_at")),
            **{col: _as_int(row.get(col)) for col in ACTIVE_COLUMNS},
        }
        for row in daily_rows
    ]

    payload = {
        "month": month_key,
        "monthly": monthly,
        "daily": daily,
        "latest_day": daily[-1]["date"] if daily else None,
    }
    return ok(payload)


@routes.get("/type-share", summary="本月游戏类型占比（下载量 / 在线峰值 / 广告曝光）")
def type_share(
    month: str | None = Query(
        default=None,
        pattern=MONTH_PATTERN,
        description="统计月份 YYYY-MM，缺省取服务器当前月（Asia/Shanghai）",
        examples=["2026-11"],
    ),
) -> Any:
    try:
        month_key, _start, _end = _month_bounds(month)
    except ValueError as exc:
        return error_response(40000, str(exc), 400)

    download_sql = f"""
        SELECT category_value AS dist, download_total AS val, game_cnt AS games
          FROM {DOWNLOAD_REPORT_TABLE}
         WHERE period_type = 'monthly' AND crawled_at = '{month_key}' AND category_type = 'dist'
    """
    online_sql = f"""
        SELECT category_value AS dist, peak_online_players AS val, game_cnt AS games
          FROM {ONLINE_REPORT_TABLE}
         WHERE period_type = 'monthly' AND crawled_at = '{month_key}' AND category_type = 'dist'
    """
    ad_sql = f"""
        SELECT category_value AS dist, total_views AS val, app_cnt AS games
          FROM {AD_REPORT_TABLE}
         WHERE period_type = 'monthly' AND crawled_at = '{month_key}' AND category_type = 'dist'
    """

    try:
        download_rows = _query(download_sql)
        online_rows = _query(online_sql)
        ad_rows = _query(ad_sql)
    except ConfigError as exc:
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    except SqlExecutionError as exc:
        logger.warning("类型占比查询失败：%s", exc)
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    types: dict[str, dict[str, Any]] = {
        dist: {
            "dist": dist,
            "download_total": 0,
            "peak_online_players": 0,
            "ad_views": 0,
            "game_cnt": None,
        }
        for dist in DIST_TYPES
    }
    # dist 不在 0/1/2 的行（如「未分类」）单独归集，不混进三类的占比分母
    unclassified = {"download_total": 0, "peak_online_players": 0, "ad_views": 0}

    def fill(rows: list[dict[str, Any]], field: str) -> None:
        for row in rows:
            key = str(row.get("dist") if row.get("dist") is not None else "").strip()
            value = _as_int(row.get("val")) or 0
            target = types.get(key)
            if target is not None:
                target[field] = value
                if field == "download_total":
                    target["game_cnt"] = _as_int(row.get("games"))
            else:
                unclassified[field] += value

    fill(download_rows, "download_total")
    fill(online_rows, "peak_online_players")
    fill(ad_rows, "ad_views")

    totals = {
        field: sum(item[field] for item in types.values())
        for field in ("download_total", "peak_online_players", "ad_views")
    }

    payload = {
        "month": month_key,
        "types": [types[dist] for dist in DIST_TYPES],
        "totals": totals,
        "unclassified": unclassified,
    }
    return ok(payload, total=len(payload["types"]))

def _rank_payload(source: RankSource, month: str | None, limit: int) -> Any:
    """按榜单类型取当月前 N 名（top / gain / loss），失败时返回 error_response。"""
    try:
        month_key, _start, _end = _month_bounds(month)
    except ValueError as exc:
        return error_response(40000, str(exc), 400)

    category_list = ", ".join(f"'{key}'" for key in RANK_CATEGORIES)
    sql = f"""
        SELECT rank_category, rank_no, app_id, app_name, icon, distribution_type,
               tag_1, tag_2, tag_3,
               {source.total} AS total_value,
               {source.prev} AS prev_value,
               {source.diff} AS diff_value
          FROM {source.table}
         WHERE period_type = 'monthly'
           AND crawled_at = '{month_key}'
           AND rank_category IN ({category_list})
           AND rank_no <= {int(limit)}
         ORDER BY rank_category ASC, rank_no ASC
    """
    try:
        rows = _query(sql, max_rows=200)
    except ConfigError as exc:
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    except SqlExecutionError as exc:
        logger.warning("%s 查询失败：%s", source.label, exc)
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in RANK_CATEGORIES}
    seen: dict[str, set[tuple[Any, Any]]] = {key: set() for key in RANK_CATEGORIES}
    for row in rows:
        category = str(row.get("rank_category") or "").strip()
        if category not in buckets:
            continue
        # 同月同榜可能有多条同 rank_no / 同 app 的残留行，按 (rank_no, app_id) 去重
        key = (_as_int(row.get("rank_no")), _as_int(row.get("app_id")))
        if key in seen[category]:
            continue
        seen[category].add(key)
        buckets[category].append(
            {
                "rank_no": key[0],
                "app_id": key[1],
                "app_name": row.get("app_name"),
                "icon": row.get("icon"),
                "distribution_type": _as_int(row.get("distribution_type")),
                "tags": [tag for tag in (row.get("tag_1"), row.get("tag_2")) if tag],
                # 各表列名不同（下载量 / 广告曝光），这里统一成通用键名
                "total": _as_int(row.get("total_value")),
                "prev": _as_int(row.get("prev_value")),
                "diff": _as_int(row.get("diff_value")),
            }
        )

    categories = []
    for key in RANK_CATEGORIES:
        items = sorted(
            buckets[key],
            key=lambda item: (
                item["rank_no"] if item["rank_no"] is not None else 1_000_000,
                item["app_id"] if item["app_id"] is not None else 0,
            ),
        )[:limit]
        categories.append({"key": key, "items": items})

    return ok(
        {
            "month": month_key,
            "limit": limit,
            "total_field": source.total,
            "prev_field": source.prev,
            "diff_field": source.diff,
            "categories": categories,
        }
    )


@routes.get("/download-rank", summary="本月下载榜 TopN（总量 / 增量 / 下滑）")
def download_rank(
    month: str | None = Query(
        default=None,
        pattern=MONTH_PATTERN,
        description="统计月份 YYYY-MM，缺省取服务器当前月（Asia/Shanghai）",
        examples=["2026-11"],
    ),
    limit: int = Query(default=5, ge=1, le=20, description="每个排名类型返回前 N 名，默认 5"),
) -> Any:
    return _rank_payload(DOWNLOAD_RANK, month, limit)


@routes.get("/ad-rank", summary="本月广告曝光榜 TopN（总量 / 增量 / 下滑）")
def ad_rank(
    month: str | None = Query(
        default=None,
        pattern=MONTH_PATTERN,
        description="统计月份 YYYY-MM，缺省取服务器当前月（Asia/Shanghai）",
        examples=["2026-11"],
    ),
    limit: int = Query(default=5, ge=1, le=20, description="每个排名类型返回前 N 名，默认 5"),
) -> Any:
    return _rank_payload(AD_RANK, month, limit)


# 主前缀：/api/v09/data/home（能被现有 nginx 的 /api/v09/data/ 反代规则转发）
router = APIRouter(prefix="/api/v09/data/home", tags=["home"])
router.include_router(routes)
