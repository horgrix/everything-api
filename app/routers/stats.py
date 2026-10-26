"""访问统计（PV/UV）接口：上报 + 读本周。

路径前缀由 `API_STATS_PREFIX` 决定，默认挂在 `/api/v09/stats`。
依赖缺失（没有盐 / 连不上库 / 表不存在）时统一降级为空数据，
不向前端抛 5xx —— 前端组件的约定是「拿不到就隐藏」。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from ..config import ConfigError, get_settings
from ..errors import ok
from ..schemas import HitRequest
from ..stats import (
    StatsError,
    clear_week_cache,
    client_ip,
    empty_week,
    is_bot_ua,
    normalize_path,
    record_hit,
    visitor_hash,
    week_stats,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stats"])

_NO_TRACK_VALUES = {"1", "true", "yes", "on"}


def _no_track(request: Request) -> bool:
    header = request.headers.get("x-no-track")
    if header is not None and header.strip().lower() not in {"", "0", "false", "no"}:
        return True
    value = request.query_params.get("no_track")
    if value is not None and value.strip().lower() in _NO_TRACK_VALUES:
        return True
    # 健康检查 / 探活工具常见的显式关闭
    return False


@router.post("/hit", summary="上报一次页面浏览")
def hit(payload: HitRequest, request: Request) -> Any:
    """记录一次浏览。

    - 未配置盐或写库不可用：返回 `{"counted": false}`，不报错；
    - 命中 `?no_track=1` / `X-No-Track` / Bot UA / 空 UA：不计数；
    - 同一访客同一路径 30 分钟内重复上报只算 1 次。
    """
    settings = get_settings()
    if not settings.stats_enabled:
        return ok({"counted": False})

    if _no_track(request):
        return ok({"counted": False})

    ua = (request.headers.get("user-agent") or "").strip()
    if is_bot_ua(ua):
        return ok({"counted": False})

    path = normalize_path(payload.path)
    ip = client_ip(dict(request.headers), request.client.host if request.client else None)
    visitor = visitor_hash(ip, ua, settings.api_stats_salt)
    referrer = (payload.referrer or "").strip()[:255] or None

    try:
        counted = record_hit(path, visitor, referrer)
    except (StatsError, ConfigError) as exc:
        logger.warning("浏览上报降级（记 0 次）：%s", exc)
        return ok({"counted": False})

    if counted:
        clear_week_cache()
    return ok({"counted": counted})


@router.get("/week", summary="本周访问统计（PV/UV + Top3 路径）")
def week() -> Any:
    """本周（周一 00:00 Asia/Shanghai 起）PV/UV 与访问量 Top3 页面。"""
    settings = get_settings()
    if not settings.stats_enabled:
        return ok(empty_week())

    try:
        data = week_stats()
    except (StatsError, ConfigError) as exc:
        logger.warning("本周访问统计降级（返回空数据）：%s", exc)
        return ok(empty_week())

    return ok(data)
