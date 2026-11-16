"""One Search 专用聚合接口（服务端查 MySQL，前端不拼 SQL）。

- `GET /api/v09/data/search/reserve-trend?app_id=..&days=30`
  某游戏最近 N 天的预约趋势（读 `dws_taptap_reserve_v2_daily`）：
  `reserve_count` 为当日新增预约，`total_reserve_count` 为当日快照的累计预约。

主前缀与首页概览一致挂在 `/api/v09/data/` 下，复用线上 nginx 既有的反代规则。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from ..config import ConfigError
from ..db import SqlExecutionError, run_query
from ..errors import CODE_DB_UNAVAILABLE, CODE_SQL_ERROR, error_response, ok

try:  # 与 stats / home 模块保持同一时区口径
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 回退到 UTC+8（上海无夏令时）
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

logger = logging.getLogger(__name__)
routes = APIRouter()

RESERVE_DAILY_TABLE = "dws_taptap_reserve_v2_daily"


def _today() -> datetime:
    return datetime.now(TZ)


@routes.get("/reserve-trend", summary="某游戏最近 N 天的预约趋势")
def reserve_trend(
    app_id: int = Query(..., ge=1, description="TapTap APPID", examples=[1042742]),
    days: int = Query(default=30, ge=1, le=365, description="最近 N 天（含今天），默认 30"),
) -> Any:
    today = _today().date()
    start = today - timedelta(days=days - 1)

    sql = f"""
        SELECT crawled_at, reserve_count, total_reserve_count
          FROM {RESERVE_DAILY_TABLE}
         WHERE app_id = {int(app_id)}
           AND crawled_at >= '{start.isoformat()}'
           AND crawled_at <= '{today.isoformat()}'
         ORDER BY crawled_at ASC
    """
    try:
        rows = run_query(sql, max_rows=800).rows
    except ConfigError as exc:
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    except SqlExecutionError as exc:
        logger.warning("预约趋势查询失败：%s", exc)
        return error_response(CODE_SQL_ERROR, str(exc), 400)

    def to_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    points = [
        {
            "date": str(row.get("crawled_at")),
            "added": to_int(row.get("reserve_count")),
            "total": to_int(row.get("total_reserve_count")),
        }
        for row in rows
    ]

    payload = {
        "app_id": app_id,
        "days": days,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "points": points,
        "latest": points[-1] if points else None,
    }
    return ok(payload, total=len(points))


router = APIRouter(prefix="/api/v09/data/search", tags=["search"])
router.include_router(routes)
