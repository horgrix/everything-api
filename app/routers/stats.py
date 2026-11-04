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
from ..errors import CODE_BAD_REQUEST, error_response, ok
from ..schemas import HitRequest
from ..stats import (
    DEFAULT_RANGE,
    RangeError,
    StatsError,
    clear_stats_cache,
    client_ip,
    empty_range,
    empty_week,
    is_bot_ua,
    normalize_path,
    range_stats,
    record_hit,
    resolve_visitor,
    week_stats,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stats"])

_NO_TRACK_VALUES = {"1", "true", "yes", "on"}


def _sso_user(request: Request) -> str | None:
    """从反向代理（oauth2-proxy）透传的登录身份。

    站点整站受 SSO 保护时，Keycloak 的 `sub` 是天然的稳定访客标识：
    同一账号换设备、换 IP 都算一个人。

    信任边界在边缘 nginx：受保护站点的 `location /api/stats/` 用
    `proxy_set_header X-Auth-Request-User $sso_user;` 覆盖该头，
    其它入口显式置空，所以这里不需要再校验对端 IP
    （uvicorn 开了 proxy-headers 后 `request.client.host` 已不是代理地址）。
    """
    if not get_settings().api_stats_trust_sso_header:
        return None
    for name in (
        "x-auth-request-user",
        "x-auth-request-email",
        "x-auth-request-preferred-username",
    ):
        value = (request.headers.get(name) or "").strip()
        if value:
            return value
    return None


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
    - 访客身份优先级：SSO 登录用户（反代透传的 `X-Auth-Request-User`）
      > 请求体里的 `visitor_id`（前端第一方匿名 ID）
      > 回退到 IP + UA 指纹；`identity` 回 sso / cid / fp 告诉前端实际用了哪条。
    - 每次上报都记 1 次 PV，服务端不做时间去重（防连点在前端路由层）。
    """
    settings = get_settings()
    if not settings.stats_enabled:
        return ok({"counted": False, "identity": None})

    if _no_track(request):
        return ok({"counted": False, "identity": None})

    ua = (request.headers.get("user-agent") or "").strip()
    if is_bot_ua(ua):
        return ok({"counted": False, "identity": None})

    path = normalize_path(payload.path)
    ip = client_ip(dict(request.headers), request.client.host if request.client else None)
    visitor, identity = resolve_visitor(
        payload.visitor_id, ip, ua, settings.api_stats_salt, sso_user=_sso_user(request)
    )
    referrer = (payload.referrer or "").strip()[:255] or None

    try:
        counted = record_hit(path, visitor, referrer, visitor_src=identity)
    except (StatsError, ConfigError) as exc:
        logger.warning("浏览上报降级（记 0 次）：%s", exc)
        return ok({"counted": False, "identity": identity})

    if counted:
        clear_stats_cache()
    return ok({"counted": counted, "identity": identity})


@router.get("/week", summary="本周访问统计（PV/UV + Top3 路径）")
def week() -> Any:
    """本周（周一 00:00 Asia/Shanghai 起）PV/UV 与访问量 Top3 页面。

    老契约，字段固定为 `week_start / pv / uv / top_paths`，不做任何改动；
    要曲线或其他时间窗口请用 `/summary?range=`。
    """
    settings = get_settings()
    if not settings.stats_enabled:
        return ok(empty_week())

    try:
        data = week_stats()
    except (StatsError, ConfigError) as exc:
        logger.warning("本周访问统计降级（返回空数据）：%s", exc)
        return ok(empty_week())

    return ok(data)


@router.get("/summary", summary="按时间窗口返回访问统计（含 series 曲线）")
def summary(range: str = DEFAULT_RANGE) -> Any:  # noqa: A002 - 与 query 名对齐
    """`?range=week|7d|30d|today|all`，默认 `week`。

    - `range=week` 的 `pv / uv / top_paths` 与 `/week` 完全一致；
    - `series` 按窗口粒度铺满（缺数据的桶补 0），`date` 为 `YYYY-MM-DD`，
      跨度过大时 `granularity=month`，`date` 为 `YYYY-MM`；
    - 非法 `range` 返回 40000，不静默回落（避免前端参数写错看不到）。
    """
    settings = get_settings()
    try:
        name = range.strip().lower() if range else DEFAULT_RANGE
    except AttributeError:
        name = DEFAULT_RANGE

    if not settings.stats_enabled:
        return ok(empty_range(name))

    try:
        data = range_stats(name)
    except RangeError as exc:
        return error_response(CODE_BAD_REQUEST, str(exc), 400)
    except (StatsError, ConfigError) as exc:
        logger.warning("访问统计降级（返回空数据）：%s", exc)
        return ok(empty_range(name))

    return ok(data)
