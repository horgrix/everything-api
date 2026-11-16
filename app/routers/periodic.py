"""定期报告（周报 / 月报）聚合接口。

前端「定期报告」页此前是**前端拼 SQL**：一次渲染要发 4~6 条 `/api/v09/data/sql`，
且把「本期 / 上期 / 可用期数」的取舍逻辑散在 27KB 的 data.js 里。这里把它收敛成
服务端聚合接口，一次请求返回整篇报告需要的数值：

- `GET .../periodic/periods`  可选期列表（每期各业务线是否有数据）
- `GET .../periodic/report`   一期完整载荷：核心 KPI + 趋势点 + 分型拆解 + 数据质量标记
- `GET .../periodic/rank`     一期某业务线的榜单（top / gain / loss）

取数口径（与爬虫侧 report-metric-spec.json 一致）：

| 业务线 | 表 | 口径 |
|---|---|---|
| 下载 | `dws_taptap_download_periodic_report` | `download_*` 是增量列（`total_*` 是累计值，不用） |
| 广告 | `dws_taptap_ad_aggr_v6_periodic_report` | `total_views = ad_views + pcebid_views`（v6） |
| 在线 | `dws_taptap_pc_online_peak_players_periodic_report` | 按 `distribution_type` 拆分 |
| 概览 | `dws_periodic_report_overview_weekly` | 只用于「按游戏」与跨表校验 |

三条业务线的汇总行取 `category_type='all'`，分型取 `category_type='dist'`。

**缺值就是缺值**：某指标某期没有数据时 `value` 为 `null`，环比不回退到更早的期、
也不当 0 处理；`availablePeriods` 说明这个指标实际能画几期，`insufficientHistory`
告诉前端「别画趋势线」。已下线不再产出的列（`online_peak_pc` 等）不取、不占位。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from ..config import ConfigError
from ..db import SqlExecutionError, run_query
from ..errors import (
    CODE_BAD_REQUEST,
    CODE_DB_UNAVAILABLE,
    CODE_SQL_ERROR,
    error_response,
    ok,
)

try:  # 与 home / stats 保持同一时区口径
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 回退到 UTC+8（上海无夏令时）
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

logger = logging.getLogger(__name__)
routes = APIRouter()

# ---- 表名常量（不可由请求控制）----
OVERVIEW_TABLE = "dws_periodic_report_overview_weekly"
DOWNLOAD_TABLE = "dws_taptap_download_periodic_report"
AD_TABLE = "dws_taptap_ad_aggr_v6_periodic_report"
ONLINE_TABLE = "dws_taptap_pc_online_peak_players_periodic_report"
DOWNLOAD_RANK_TABLE = "dws_taptap_download_periodic_rank"
AD_RANK_TABLE = "dws_taptap_ad_aggr_v6_periodic_rank"
ONLINE_RANK_TABLE = "dws_taptap_pc_online_peak_players_periodic_rank"
# 在线明细（小时级）与 v2 汇总：用来取「全局峰值那一小时」的同小时分型快照
ONLINE_DETAILS_TABLE = "dws_taptap_pc_online_peak_players_details_hourly"
ONLINE_V2_TABLE = "dws_taptap_pc_online_peak_players_v2_hourly"

# distribution_type → 业务名称（前端不再自己维护一份）
DIST_LABELS = {"0": "商业游戏", "1": "独立小游戏", "2": "Maker 小游戏"}
UNKNOWN_DIST = "未分类"
MAX_DIST_KEYS = ("0", "1", "2")

# 已下线、不再产出的列：接口不取，也不给前端预留位置
RETIRED_COLUMNS = (
    "online_peak_pc",
    "online_peak_pc_emulator",
    "online_peak_creative",
)
# PC 在线榜 W45 起换维度，旧 list_type 只存在于历史周
RETIRED_LIST_TYPES = ("pc_played", "pc_emulator_played", "app_hot_creative")
CURRENT_LIST_TYPES = ("dist_0", "dist_1", "dist_2")

PERIOD_PATTERNS = {
    "weekly": re.compile(r"^\d{4}-(0[1-9]|[1-4]\d|5[0-3])$"),
    "monthly": re.compile(r"^\d{4}-(0[1-9]|1[0-2])$"),
}
PERIOD_TYPES = tuple(PERIOD_PATTERNS)

# 少于 3 期无法构成趋势线
INSUFFICIENT_POINTS = 3
MAX_WINDOW = 24
RANK_CATEGORIES = ("top", "gain", "loss")
# 火炬之光单期环比超过这个倍数就按「疑似口径切换」告警
SUSPECT_JUMP_RATIO = 3.0
# 概览表与在线周期表的容差（相对差）
OVERVIEW_TOLERANCE = 0.01

# 在线数据从这一天开始采集；更早的期（如 W45、2026-11）覆盖不全，环比不可比
ONLINE_COVERAGE_START = "2026-11-07"
# 进程内结果缓存秒数：报告一天只更新几次，60 秒内的重复请求不必再打库
CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class MetricSpec:
    """一条业务线里的一个指标（列名 + 对外键名 + 展示单位）。"""

    key: str
    column: str
    label: str
    unit: str
    is_int: bool = True
    # 口径备注（前端可做 tooltip）。比如广告是「位次」而不是用户级曝光
    note: str = ""


@dataclass(frozen=True)
class LineSource:
    key: str
    label: str
    table: str
    breakdown_column: str
    games_column: str
    metrics: tuple[MetricSpec, ...]
    # 该业务线的数据从哪天开始有：期起点早于它就算覆盖不全
    coverage_start: str | None = None


LINE_SOURCES: tuple[LineSource, ...] = (
    LineSource(
        key="download",
        label="TapTap 下载",
        table=DOWNLOAD_TABLE,
        breakdown_column="download_total",
        games_column="game_cnt",
        metrics=(
            MetricSpec("download_total", "download_total", "下载总量", "次"),
            MetricSpec("download_app", "download_app", "APP 下载量", "次"),
            MetricSpec("download_pc", "download_pc", "PC 下载量", "次"),
            MetricSpec("game_cnt", "game_cnt", "下载游戏数", "款"),
        ),
    ),
    LineSource(
        key="ad",
        label="TapTap 广告",
        table=AD_TABLE,
        breakdown_column="total_views",
        games_column="app_cnt",
        metrics=(
            MetricSpec(
                "ad_total",
                "total_views",
                "广告曝光",
                "次",
                note="采集位次：每游戏每小时在每个广告坑位出现计 1 次，不等于用户曝光量",
            ),
            MetricSpec(
                "ad_app",
                "ad_views",
                "APP 曝光",
                "次",
                note="采集位次，每游戏每小时每个坑位计 1 次",
            ),
            MetricSpec(
                "ad_pcebid",
                "pcebid_views",
                "PC 曝光",
                "次",
                note="资源互换位次，每游戏每小时每个坑位计 1 次",
            ),
            MetricSpec("app_cnt", "app_cnt", "投放游戏数", "款"),
        ),
    ),
    LineSource(
        key="online",
        label="TapTap PC 在线",
        table=ONLINE_TABLE,
        breakdown_column="peak_online_players",
        games_column="game_cnt",
        metrics=(
            MetricSpec("online_peak_players", "peak_online_players", "在线峰值", "人"),
            MetricSpec("online_avg", "avg_online_players", "周均在线", "人", is_int=False),
            MetricSpec(
                "online_game_cnt",
                "game_cnt",
                "在线游戏数（去重）",
                "款",
                note="本周有在线记录的游戏，按 app_id 去重；不等于某一小时的并发款数",
            ),
        ),
        coverage_start=ONLINE_COVERAGE_START,
    ),
)

LINE_BY_KEY = {source.key: source for source in LINE_SOURCES}

# {(period_type, key, window): (写入时刻, payload)}
_CACHE: dict[tuple, tuple[float, Any]] = {}

# 概览表里按游戏的两个重点游戏
PLAYER_COLUMNS = (
    ("torchlight", "火炬之光：无限", "torchlight_download_total", "torchlight_avg_online_players"),
    ("heartopia", "心动小镇", "heartopia_download_total", "heartopia_avg_online_players"),
)


@dataclass(frozen=True)
class RankSource:
    key: str
    label: str
    table: str
    value: str
    prev: str
    diff: str
    unit: str
    # 在线榜没有 icon 列，用 NULL 占位，保持三张榜返回结构一致
    icon: str = "icon"


RANK_SOURCES: dict[str, RankSource] = {
    "download": RankSource(
        "download", "TapTap 下载", DOWNLOAD_RANK_TABLE,
        "total_download", "prev_total_download", "diff_download", "次",
    ),
    "ad": RankSource(
        "ad", "TapTap 广告", AD_RANK_TABLE,
        "total_views", "prev_total_views", "diff_views", "次",
    ),
    "online": RankSource(
        "online", "TapTap PC 在线", ONLINE_RANK_TABLE,
        "avg_online_players", "prev_avg_online_players", "diff_online_players", "人",
        icon="NULL",
    ),
}


# ---------------------------------------------------------------- 基础工具


def _as_number(value: Any) -> float | int | None:
    """把驱动返回值收敛成数字；None / 空串 / 非数字一律视为缺值。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_number(value)
    if number is None:
        return None
    return int(round(number))


def _as_float(value: Any) -> float | None:
    number = _as_number(value)
    if number is None:
        return None
    return round(float(number), 2)


def _ratio(cur: float | int | None, prev: float | int | None) -> float | None:
    """环比百分比（1 位小数）。缺值或上期为 0 时返回 None —— 不编造 -100%。"""
    if cur is None or prev is None:
        return None
    if prev == 0:
        return None
    return round((float(cur) - float(prev)) / float(prev) * 100, 1)


def _direction(delta: float | None) -> str:
    if delta is None:
        return "flat"
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def _query(sql: str, max_rows: int = 5000) -> list[dict[str, Any]]:
    return run_query(sql, max_rows=max_rows).rows


# 表→列名集合（表结构不会在进程存活期间变），用来判断该不该加 is_active 过滤
_COLUMNS: dict[str, set[str]] = {}


def _columns_of(table: str) -> set[str]:
    """取表的列名（每表只查一次）。顺便兼容 MySQL / 测试用 SQLite。"""
    if table in _COLUMNS:
        return _COLUMNS[table]
    try:
        columns = set(run_query(f"SELECT * FROM {table} LIMIT 0", max_rows=1).columns)
    except SqlExecutionError:  # 表不存在/无权限：交给真正的查询去报错
        return set()
    _COLUMNS[table] = columns
    return columns


def _active_filter(table: str) -> str:
    """表里 is_active 列时只取有效行：DWS 下线残留是置 0 不删数据。"""
    return " AND is_active = 1" if "is_active" in _columns_of(table) else ""


def _validate_period(period_type: str, key: str | None) -> str | None:
    """校验期键格式；不合法抛 ValueError。"""
    if not key or key == "latest":
        return None
    pattern = PERIOD_PATTERNS[period_type]
    if not pattern.match(key):
        raise ValueError(f"期键格式不合法：{key!r}（{period_type}）")
    return key


def _period_range(period_type: str, key: str) -> tuple[str, str]:
    """期键 → (起, 止) 日期字符串。周报按 ISO 周（周一至周日）。"""
    year_text, num_text = key.split("-")
    year, num = int(year_text), int(num_text)
    if period_type == "weekly":
        try:
            start = date.fromisocalendar(year, num, 1)
        except ValueError as exc:  # 例如某年没有第 53 周
            raise ValueError(f"{year} 年没有第 {num} 周") from exc
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    start = date(year, num, 1)
    next_month = date(year + (num == 12), num % 12 + 1, 1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def _today() -> date:
    return datetime.now(TZ).date()


def _is_partial(period_type: str, key: str) -> bool:
    """当期是否还没走完（月中取数时月底未到）。未结束的期不能拿去和完整期比环比。"""
    start, end = _period_range(period_type, key)
    return date.fromisoformat(end) >= _today()


def _coverage(
    period_type: str, key: str, coverage_start: str | None
) -> tuple[bool, str | None]:
    """某一期的数据覆盖是否完整。

    业务线有「起始采集日」时，期起点早于它就是半期数据（如在线线 W45 只有 11-07~11-08），
    这种期的环比不可比。返回 (是否覆盖不全, 人话说明)。
    """
    if not coverage_start:
        return False, None
    start, end = _period_range(period_type, key)
    if start >= coverage_start:
        return False, None
    covered_from = max(start, coverage_start)
    covered_days = (date.fromisoformat(end) - date.fromisoformat(covered_from)).days + 1
    total_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if covered_days >= total_days:
        return False, None
    return True, (
        f"在线数据自 {coverage_start} 起才开始采集，{key} 仅覆盖 "
        f"{covered_from}~{end}（{covered_days}/{total_days} 天）"
    )


def _cache_get(cache_key: tuple) -> Any | None:
    entry = _CACHE.get(cache_key)
    if not entry:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _CACHE.pop(cache_key, None)
        return None
    return value


def _cache_set(cache_key: tuple, value: Any) -> Any:
    _CACHE[cache_key] = (time.monotonic(), value)
    return value


def _period_label(period_type: str, key: str) -> str:
    year, num = key.split("-")
    if period_type == "weekly":
        return f"{year} 年第 {int(num)} 周"
    return f"{year} 年 {int(num)} 月"


def _iso_timestamp(value: Any) -> str | None:
    """驱动返回的 updated_at → ISO+08:00 字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TZ)
        return moment.astimezone(TZ).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    return moment.astimezone(TZ).isoformat()


def _pick_value(row: dict[str, Any] | None, column: str, is_int: bool = True) -> float | int | None:
    if not row:
        return None
    return _as_int(row.get(column)) if is_int else _as_float(row.get(column))


# ---------------------------------------------------------------- 取数


def _load_line_rows(period_type: str) -> dict[str, dict[str, dict[str, Any]]]:
    """三条业务线的汇总行（category_type='all'），返回 {line: {period: row}}。"""
    cache_key = ("lines", period_type)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for source in LINE_SOURCES:
        columns = ", ".join(metric.column for metric in source.metrics)
        active = _active_filter(source.table)
        sql = f"""
            SELECT crawled_at, {columns}, updated_at
              FROM {source.table}
             WHERE period_type = '{period_type}' AND category_type = 'all'{active}
             ORDER BY crawled_at
        """
        rows = _query(sql)
        series: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("crawled_at") or "").strip()
            if key:
                # 同键多条（重跑残留）时保留最后一条（ORDER BY crawled_at 稳定，后写覆盖）
                series[key] = row
        result[source.key] = series
    return _cache_set(cache_key, result)


def _load_overview() -> dict[str, dict[str, Any]]:
    """概览周表（仅周报）：按游戏与跨表校验用。"""
    cache_key = ("overview",)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sql = f"""
        SELECT week_key, week_start, week_end,
               online_game_cnt, online_peak_players,
               torchlight_download_total, torchlight_avg_online_players,
               heartopia_download_total, heartopia_avg_online_players,
               updated_at
          FROM {OVERVIEW_TABLE}
         {('WHERE is_active = 1' if 'is_active' in _columns_of(OVERVIEW_TABLE) else '')}
         ORDER BY week_key
    """
    series: dict[str, dict[str, Any]] = {}
    for row in _query(sql):
        key = str(row.get("week_key") or "").strip()
        if key:
            series[key] = row
    return _cache_set(cache_key, series)


def _load_breakdown(
    source: LineSource, period_type: str, keys: list[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """分型拆解（category_type='dist'），返回 {period: {dist: row}}。"""
    if not keys:
        return {}
    quoted = ", ".join(f"'{key}'" for key in keys)
    sql = f"""
        SELECT crawled_at, category_value AS dist,
               {source.breakdown_column} AS value, {source.games_column} AS games
          FROM {source.table}
         WHERE period_type = '{period_type}' AND category_type = 'dist'
           AND crawled_at IN ({quoted}){_active_filter(source.table)}
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in keys}
    for row in _query(sql):
        period = str(row.get("crawled_at") or "").strip()
        dist = str(row.get("dist") if row.get("dist") is not None else "").strip()
        if period in grouped:
            grouped[period][dist] = row
    return grouped


# ---------------------------------------------------------------- 组装


def _build_kpi(
    spec: MetricSpec,
    line_key: str,
    series: dict[str, dict[str, Any]],
    period_key: str,
    periods: list[str],
    window: int,
    is_partial: bool,
    delta_blocked: str | None = None,
) -> dict[str, Any]:
    """一个指标的当期值 / 上期值 / 环比 / 趋势点 / 可用期数。

    delta_blocked 非空时环比一律置 null（不让前端自己除一个看似合理的涨幅）：
      - coverage_partial      本期数据覆盖不全（如在线线 W45 只有 2 天）
      - prev_coverage_partial 上期覆盖不全，拿它当基数会算出一个假的涨幅
      - secondary_metric      副指标，本来就不出环比
    """
    available = [
        period
        for period in periods
        if _pick_value(series.get(period), spec.column, spec.is_int) is not None
    ]
    value = _pick_value(series.get(period_key), spec.column, spec.is_int)
    # 上期只在「该指标自己有数据」的期里取，避免拿 0 或空行算出假环比
    prior_available = [period for period in available if period < period_key]
    prior_key = prior_available[-1] if prior_available else None
    prev_value = _pick_value(series.get(prior_key), spec.column, spec.is_int) if prior_key else None

    delta_abs = None
    if value is not None and prev_value is not None:
        delta_abs = round(float(value) - float(prev_value), 2)
        if spec.is_int:
            delta_abs = int(round(delta_abs))

    trend = [
        {
            "period": period,
            "label": _period_label_short(period),
            "value": _pick_value(series[period], spec.column, spec.is_int),
        }
        for period in available[-window:]
    ]

    delta_pct = _ratio(value, prev_value)
    if delta_blocked:
        # 不可比：连上期值和环比一起置 null，避免前端又用 prevValue 自己算出假涨幅
        prev_value = None
        prior_key = None
        delta_abs = None
        delta_pct = None

    result = {
        "key": spec.key,
        "line": line_key,
        "label": spec.label,
        "unit": spec.unit,
        "isInt": spec.is_int,
        "value": value,
        "prevValue": prev_value,
        "prevPeriodKey": prior_key,
        "deltaAbs": delta_abs,
        "deltaPct": delta_pct,
        "direction": _direction(delta_pct),
        # 本期未结束（月中/周中）时环比不可比：数值照给，但别当成真实下跌
        "deltaComparable": not is_partial and not delta_blocked and delta_pct is not None,
        "trend": trend,
        "availablePeriods": available,
        "historyPoints": len(available),
        "insufficientHistory": len(available) < INSUFFICIENT_POINTS,
        # 前端可直接判断：本期有没有这个值（缺值不要显示 0）
        "hasValue": value is not None,
        # 口径备注：只有需要解释的指标才有（如广告是位次而不是用户曝光）
        **(({"note": spec.note}) if spec.note else {}),
    }
    if delta_blocked:
        result["deltaBlocked"] = delta_blocked
    return result


def _build_concurrent_kpi(
    overview: dict[str, dict[str, Any]],
    period_key: str,
    periods: list[str],
    window: int,
) -> dict[str, Any]:
    """副指标：峰值小时同时在线款数（概览表的 online_game_cnt）。

    这个概念与主线「本周去重款数」不同：前者是某一小时的并发款数（同一时刻），
    后者是整周 app_id 去重。两者相差几十到上百款都是正常的，因此**不出环比**。
    """
    spec = MetricSpec(
        "concurrent_game_cnt",
        "online_game_cnt",
        "峰值小时同时在线款数",
        "款",
        note="该周并发最高那一小时同时在线的款数（概览表口径），与「本周去重款数」不同口径，两者不可相减",
    )
    available = [
        period
        for period in periods
        if _pick_value(overview.get(period), spec.column) is not None
    ]
    value = _pick_value(overview.get(period_key), spec.column)
    return {
        "key": spec.key,
        "line": "online",
        "label": spec.label,
        "unit": spec.unit,
        "isInt": True,
        "value": value,
        "prevValue": None,
        "prevPeriodKey": None,
        "deltaAbs": None,
        "deltaPct": None,
        "direction": "flat",
        "deltaComparable": False,
        "deltaBlocked": "secondary_metric",
        "trend": [
            {
                "period": period,
                "label": _period_label_short(period),
                "value": _pick_value(overview[period], spec.column),
            }
            for period in available[-window:]
        ],
        "availablePeriods": available,
        "historyPoints": len(available),
        "insufficientHistory": len(available) < INSUFFICIENT_POINTS,
        "hasValue": value is not None,
        "note": spec.note,
    }


def _load_peak_hour(period_type: str, period_key: str) -> dict[str, Any] | None:
    """全局峰值那一小时的在线分型快照。

    分型拆解默认是「每个分型各自周内峰值」，三者峰值出现在不同小时，加不到一起。
    这里额外汇总同一小时的快照：三项 gameCnt / peakOnlinePlayers 相加后严格等于
    该小时的总值，可以放心画堆叠图。取不到（表缺失 / 当天明细被清）就返回 None，
    不影响整篇报告。
    """
    start, end = _period_range(period_type, period_key)
    sql = f"""
        SELECT d.distribution_type AS dist,
               COUNT(*) AS game_cnt,
               SUM(d.online_players) AS peak_online_players,
               MAX(d.crawled_at) AS crawled_at,
               MAX(k.total_players) AS total_players,
               MAX(k.total_games) AS total_games
          FROM {ONLINE_DETAILS_TABLE} d
          JOIN (
                SELECT p.crawled_at AS crawled_at,
                       p.online_players AS total_players,
                       p.game_cnt AS total_games
                  FROM {ONLINE_V2_TABLE} p
                 WHERE p.crawled_at >= '{start}' AND p.crawled_at <= '{end} 23:59'
                 ORDER BY p.online_players DESC
                 LIMIT 1
               ) k ON k.crawled_at = d.crawled_at
         WHERE d.crawled_at = k.crawled_at
         GROUP BY d.distribution_type
    """
    try:
        rows = _query(sql)
    except SqlExecutionError as exc:  # 表缺失/当天明细被清：降级，不让整篇报告挂掉
        logger.info("峰值小时快照不可用：%s", exc)
        return None
    if not rows:
        return None

    breakdown = []
    for row in rows:
        dist = str(row.get("dist"))
        breakdown.append(
            {
                "distributionType": dist,
                "label": DIST_LABELS.get(dist, UNKNOWN_DIST),
                "gameCnt": _as_int(row.get("game_cnt")),
                "peakOnlinePlayers": _as_int(row.get("peak_online_players")),
            }
        )
    breakdown.sort(key=lambda item: (MAX_DIST_KEYS.index(item["distributionType"])
                                     if item["distributionType"] in MAX_DIST_KEYS else 99))
    total_games = _as_int(rows[0].get("total_games"))
    total_players = _as_int(rows[0].get("total_players"))
    sum_games = sum(item["gameCnt"] or 0 for item in breakdown)
    sum_players = sum(item["peakOnlinePlayers"] or 0 for item in breakdown)
    return {
        "crawledAt": str(rows[0].get("crawled_at") or ""),
        "gameCnt": total_games if total_games is not None else sum_games,
        "peakOnlinePlayers": total_players if total_players is not None else sum_players,
        "sumGameCnt": sum_games,
        "sumPeakOnlinePlayers": sum_players,
        # 同小时快照必须可加：不平就是口径被动过了，前端标红
        "sumsMatch": sum_games == total_games and sum_players == total_players,
        "breakdown": breakdown,
    }


def _period_label_short(period_key: str) -> str:
    """趋势轴用短标签：2026-45 → W45；2026-11 → 11 月。"""
    _year, num = period_key.split("-")
    num_int = int(num)
    if len(num) == 2 and num_int <= 53:
        return f"W{num_int:02d}"
    return f"{num_int} 月"


def _build_breakdown(
    source: LineSource,
    period_key: str,
    prev_key: str | None,
    grouped: dict[str, dict[str, dict[str, Any]]],
    all_value: float | int | None,
) -> dict[str, Any]:
    """分型拆解：三类固定输出（缺则 0 且 hasData=false），未分类单独归集。"""
    current = grouped.get(period_key, {})
    previous = grouped.get(prev_key, {}) if prev_key else {}

    items = []
    total = 0
    for dist in MAX_DIST_KEYS:
        raw = _as_number((current.get(dist) or {}).get("value")) or 0
        total += raw
        items.append(
            {
                "key": dist,
                "label": DIST_LABELS[dist],
                "value": int(raw),
                "prevValue": (
                    int(_as_number((previous.get(dist) or {}).get("value")) or 0)
                    if prev_key
                    else None
                ),
                "games": _as_int((current.get(dist) or {}).get("games")),
                "hasData": dist in current,
            }
        )

    for item in items:
        item["share"] = round(item["value"] / total, 4) if total else None
        item["deltaPct"] = _ratio(item["value"], item["prevValue"])

    unclassified = [
        {
            "key": dist,
            "label": UNKNOWN_DIST,
            "value": _as_int(row.get("value")) or 0,
            "games": _as_int(row.get("games")),
        }
        for dist, row in current.items()
        if dist not in MAX_DIST_KEYS and (_as_number(row.get("value")) or 0) > 0
    ]
    unknown_total = sum(item["value"] for item in unclassified)

    # 分型明细合计（三类 + 未归类）应与 category_type='all' 的汇总行对得上；
    # 周期性任务重算期间可能读到半新半旧的行，这里把差异透出来让前端提示
    detail_total = int(total) + unknown_total
    gap = None
    if all_value is not None and all_value != 0:
        gap = round((detail_total - float(all_value)) / float(all_value) * 100, 2)
    return {
        "line": source.key,
        "metric": source.breakdown_column,
        "items": items,
        "unclassified": unclassified,
        "unclassifiedTotal": unknown_total,
        "total": int(total),
        "consistency": {
            "allValue": _as_int(all_value),
            "detailTotal": detail_total,
            "gapPct": gap,
            "consistent": gap is None or abs(gap) <= OVERVIEW_TOLERANCE * 100,
        },
    }


def _build_players(
    overview: dict[str, dict[str, Any]], period_key: str, window: int
) -> list[dict[str, Any]]:
    """按游戏的重点指标（来源：概览周表）。"""
    periods = sorted(overview)
    if period_key not in overview:
        return []
    current = overview[period_key]
    prior_periods = [period for period in periods if period < period_key]
    prev_key = prior_periods[-1] if prior_periods else None
    previous = overview.get(prev_key) if prev_key else None

    players = []
    for key, name, download_col, online_col in PLAYER_COLUMNS:
        download = _as_int(current.get(download_col))
        download_prev = _as_int(previous.get(download_col)) if previous else None
        online = _as_float(current.get(online_col))
        online_prev = _as_float(previous.get(online_col)) if previous else None
        players.append(
            {
                "key": key,
                "name": name,
                "download": download,
                "downloadPrev": download_prev,
                "downloadDeltaPct": _ratio(download, download_prev),
                "avgOnline": online,
                "avgOnlinePrev": online_prev,
                "avgOnlineDeltaPct": _ratio(online, online_prev),
                "downloadTrend": [
                    {"period": period, "value": _as_int(overview[period].get(download_col))}
                    for period in periods[-window:]
                    if _as_int(overview[period].get(download_col)) is not None
                ],
                "avgOnlineTrend": [
                    {"period": period, "value": _as_float(overview[period].get(online_col))}
                    for period in periods[-window:]
                    if _as_float(overview[period].get(online_col)) is not None
                ],
            }
        )
    return players


def _build_flags(
    period_key: str,
    lines: dict[str, dict[str, dict[str, Any]]],
    overview: dict[str, dict[str, Any]],
    breakdowns: dict[str, dict[str, Any]],
    line_kpis: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """数据质量标记：前端拿来加角标 / 提示，不用自己算。"""
    flags: list[dict[str, Any]] = []

    # 1) 概览表与在线周期表：款数本来就是两个指标（并发 vs 去重），峰值才叫口径差异
    online_row = lines.get("online", {}).get(period_key) or {}
    overview_row = overview.get(period_key) or {}
    for column, label in (("online_peak_players", "在线峰值"), ("online_game_cnt", "在线游戏数")):
        from_overview = _as_number(overview_row.get(column))
        line_column = "peak_online_players" if column == "online_peak_players" else "game_cnt"
        from_line = _as_number(online_row.get(line_column))
        if from_overview is None or from_line is None:
            continue
        base = max(abs(from_overview), abs(from_line), 1)
        if abs(from_overview - from_line) / base <= OVERVIEW_TOLERANCE:
            continue
        if column == "online_game_cnt":
            # 已核过库：概览表是「峰值小时并发款数」，周期表是「周内去重款数」
            flags.append(
                {
                    "level": "info",
                    "item": "online_game_cnt_mismatch",
                    "label": "在线款数两种口径并存",
                    "detail": (
                        f"概览表 {int(from_overview):,} 是峰值小时同时在线款数（并发）；"
                        f"在线周期表 {int(from_line):,} 是本周有在线记录的游戏数（去重）。"
                        f"相差 {abs(int(from_overview) - int(from_line)):,} 款属正常，不是数据错误"
                    ),
                    "action": (
                        "主口径用去重款数；并发款数已作为 concurrent_game_cnt 副指标输出，"
                        "两者不要相减、不要算环比"
                    ),
                }
            )
            continue
        flags.append(
            {
                "level": "medium",
                "item": f"{column}_mismatch",
                "label": f"{label}跨表口径差异",
                "detail": (
                    f"概览表 {int(from_overview):,} vs 在线周期表 {int(from_line):,}"
                    f"（{period_key}）；两表统计池不同，不是同一份数据的旧快照"
                ),
                "action": (
                    "已定口径：在线类指标一律用在线周期表（本接口返回值），"
                    "概览表仅用于「按游戏」指标"
                ),
            }
        )

    # 2) 趋势期数不足的业务线
    for line_key, kpis in line_kpis.items():
        shortest = min(kpis, key=lambda item: item["historyPoints"]) if kpis else None
        if shortest and shortest["insufficientHistory"]:
            flags.append(
                {
                    "level": "medium",
                    "item": f"{line_key}_insufficient_history",
                    "label": f"{shortest['label']}历史不足",
                    "detail": (
                        f"仅 {shortest['historyPoints']} 期可用"
                        f"（{'、'.join(shortest['availablePeriods']) or '无'}）"
                    ),
                    "action": "不画趋势线，只显示当期值；环比照实标「—」",
                }
            )

    # 3) 按游戏：单期异常放量（如火炬之光 14 倍）
    periods = sorted(overview)
    for key, name, download_col, _online in PLAYER_COLUMNS:
        for prev_period, period in zip(periods, periods[1:], strict=False):
            prev_value = _as_number(overview[prev_period].get(download_col))
            value = _as_number(overview[period].get(download_col))
            if not prev_value or value is None:
                continue
            jump = value / prev_value
            if jump >= SUSPECT_JUMP_RATIO:
                flags.append(
                    {
                        "level": "high",
                        "item": f"{key}_download_jump",
                        "label": f"{name}下载量异常放量",
                        "detail": (
                            f"{prev_period} {int(prev_value):,} → {period} {int(value):,}"
                            f"（{jump:.1f}×），疑似口径切换或活动冲量"
                        ),
                        "action": "趋势图上单独加注，不与正常周期并排解读",
                    }
                )

    # 4) 未归类的 distribution_type（不当作业务分类展示）
    for line_key, breakdown in breakdowns.items():
        if breakdown["unclassifiedTotal"] > 0:
            names = "、".join(item["key"] for item in breakdown["unclassified"])
            flags.append(
                {
                    "level": "medium",
                    "item": f"{line_key}_unclassified_dist",
                    "label": f"{LINE_BY_KEY[line_key].label}存在未归类游戏",
                    "detail": (
                        f"{period_key} 有 {len(breakdown['unclassified'])} 组未归类"
                        f"（{names}），合计 {breakdown['unclassifiedTotal']:,}"
                    ),
                    "action": "单独标注，不计入三类占比的分母",
                }
            )

    # 5) 分型明细合计与汇总行不一致（周期性任务重算中途 / 口径差异）
    for line_key, breakdown in breakdowns.items():
        consistency = breakdown["consistency"]
        if not consistency["consistent"]:
            flags.append(
                {
                    "level": "medium",
                    "item": f"{line_key}_breakdown_gap",
                    "label": f"{LINE_BY_KEY[line_key].label}分型合计与汇总不一致",
                    "detail": (
                        f"{period_key} 汇总 {consistency['allValue']:,} vs 分型合计 "
                        f"{consistency['detailTotal']:,}（{consistency['gapPct']:+.2f}%）"
                    ),
                    "action": "多为重算窗口读到半新半旧的行，稍后自动一致；占比按分型合计算",
                }
            )

    # 6) 已下线列（不展示，但要让使用者知道为什么没有这些数）
    flags.append(
        {
            "level": "info",
            "item": "retired_columns",
            "label": "已下线字段",
            "detail": "、".join(RETIRED_COLUMNS) + " 自 W45 起不再产出",
            "action": "新版报表不展示、不预留位置",
        }
    )

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    flags.sort(key=lambda item: order.get(item["level"], 9))
    return flags


# ---------------------------------------------------------------- 接口实现


def _fail(exc: Exception) -> Any:
    if isinstance(exc, ConfigError):
        logger.error("数据库未配置：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, str(exc), 503)
    if isinstance(exc, SqlExecutionError):
        logger.warning("定期报告查询失败：%s", exc)
        return error_response(CODE_SQL_ERROR, str(exc), 400)
    raise exc


@routes.get("/periods", summary="定期报告可选期列表")
def periods(
    period: str = Query(
        default="weekly", description="weekly / monthly", pattern="^(weekly|monthly)$"
    ),
) -> Any:
    cache_key = ("periods", period)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        lines = _load_line_rows(period)
    except (ConfigError, SqlExecutionError) as exc:
        return _fail(exc)

    all_periods = sorted({key for series in lines.values() for key in series})
    items = []
    for key in all_periods:
        start, end = _period_range(period, key)
        items.append(
            {
                "key": key,
                "label": _period_label(period, key),
                "start": start,
                "end": end,
                "lines": {line_key: key in series for line_key, series in lines.items()},
            }
        )

    insufficient = {
        line_key: len(series) < INSUFFICIENT_POINTS for line_key, series in lines.items()
    }

    return _cache_set(
        cache_key,
        ok(
            {
                "periodType": period,
                "latest": all_periods[-1] if all_periods else None,
                "periods": items,
                "availablePeriods": all_periods,
                "insufficientHistory": insufficient,
            },
            total=len(items),
        ),
    )


@routes.get("/report", summary="一期定期报告聚合数据")
def report(
    period: str = Query(
        default="weekly", description="weekly / monthly", pattern="^(weekly|monthly)$"
    ),
    key: str | None = Query(
        default=None, description="期键，如 2026-46 / 2026-11；缺省取最新一期"
    ),
    window: int = Query(default=12, ge=2, le=MAX_WINDOW, description="趋势点最多回看的期数"),
) -> Any:
    try:
        period_key = _validate_period(period, key)
    except ValueError as exc:
        return error_response(CODE_BAD_REQUEST, str(exc), 400)

    try:
        lines = _load_line_rows(period)
    except (ConfigError, SqlExecutionError) as exc:
        return _fail(exc)

    all_periods = sorted({row_key for series in lines.values() for row_key in series})
    if not all_periods:
        return error_response(CODE_BAD_REQUEST, "尚无任何周期数据", 404)

    period_key = period_key or all_periods[-1]
    if period_key not in all_periods:
        return error_response(CODE_BAD_REQUEST, f"{period_key} 没有数据", 404)

    cache_key = ("report", period, period_key, window)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    is_partial = _is_partial(period, period_key)

    earlier = [row_key for row_key in all_periods if row_key < period_key]
    prev_period = earlier[-1] if earlier else None

    try:
        breakdowns = {
            source.key: _build_breakdown(
                source,
                period_key,
                prev_period,
                _load_breakdown(source, period, [k for k in (period_key, prev_period) if k]),
                _pick_value(
                    lines[source.key].get(period_key),
                    source.breakdown_column,
                    is_int=source.breakdown_column != "avg_online_players",
                ),
            )
            for source in LINE_SOURCES
        }
        overview = _load_overview() if period == "weekly" else {}
        # 全局峰值那一小时的同小时快照（只有在线线需要；取不到就降级为 None）
        peak_hour = _load_peak_hour(period, period_key)
    except (ConfigError, SqlExecutionError) as exc:
        return _fail(exc)

    # 概览表可能缺这一期（月报没有概览表），此时按游戏区块返回空
    line_payloads = []
    line_kpis: dict[str, list[dict[str, Any]]] = {}
    coverage_flags: list[dict[str, Any]] = []
    for source in LINE_SOURCES:
        series = lines[source.key]
        current_partial, current_note = _coverage(period, period_key, source.coverage_start)
        prev_partial, prev_note = (
            _coverage(period, prev_period, source.coverage_start) if prev_period else (False, None)
        )
        # 本期或上期覆盖不全，环比一律置 null：半周去重 / 首轮孤点都会造出假涨幅
        delta_blocked = (
            "coverage_partial"
            if current_partial
            else ("prev_coverage_partial" if prev_partial else None)
        )
        kpis = [
            _build_kpi(
                metric,
                source.key,
                series,
                period_key,
                all_periods,
                window,
                is_partial,
                delta_blocked=delta_blocked,
            )
            for metric in source.metrics
        ]
        # 副指标：峰值小时并发款数（只在周报有概览表；不进主 KPI、不出环比）
        if source.key == "online" and overview:
            kpis.append(_build_concurrent_kpi(overview, period_key, all_periods, window))
        line_kpis[source.key] = kpis
        if current_partial or prev_partial:
            coverage_flags.append(
                {
                    "level": "medium",
                    "item": f"{source.key}_partial_coverage",
                    "label": f"{source.label}数据覆盖不全",
                    "detail": current_note or prev_note,
                    "action": (
                        "本期与上期的环比一律置 null（deltaBlocked），"
                        "趋势点保留原始值，但不代表完整周"
                    ),
                }
            )
        updated = _iso_timestamp((series.get(period_key) or {}).get("updated_at"))
        line_payloads.append(
            {
                "key": source.key,
                "label": source.label,
                "updatedAt": updated,
                "periods": sorted(series),
                "kpis": kpis,
                "breakdown": breakdowns[source.key],
                **(({"peakHour": peak_hour}) if (source.key == "online" and peak_hour) else {}),
            }
        )

    # 核心 KPI：按「每期都要看」的顺序拉平，附带 sparkline 点与可用期数
    core_keys = (
        "online_game_cnt",
        "online_peak_players",
        "download_total",
        "download_pc",
        "download_app",
        "ad_total",
    )
    flat = [kpi for line in line_payloads for kpi in line["kpis"]]
    core = [next((kpi for kpi in flat if kpi["key"] == key), None) for key in core_keys]
    core = [kpi for kpi in core if kpi is not None]

    updated_candidates = [line["updatedAt"] for line in line_payloads if line["updatedAt"]]
    start, end = _period_range(period, period_key)

    payload = {
        "meta": {
            "periodType": period,
            "periodKey": period_key,
            "periodLabel": _period_label(period, period_key),
            "periodStart": start,
            "periodEnd": end,
            "prevPeriodKey": prev_period,
            "isLatest": period_key == all_periods[-1],
            "updatedAt": max(updated_candidates) if updated_candidates else None,
            "availablePeriods": all_periods,
            "insufficientHistory": len(all_periods) < INSUFFICIENT_POINTS,
            # 本期还没走完（月中/周中取数）：环比只作参考，前端要标「未完」
            "isPartial": is_partial,
            "window": window,
            "distLabels": DIST_LABELS,
            "retiredColumns": list(RETIRED_COLUMNS),
            "sources": [
                *([OVERVIEW_TABLE] if period == "weekly" else []),
                *(source.table for source in LINE_SOURCES),
            ],
        },
        "coreKpis": core,
        "lines": line_payloads,
        "players": _build_players(overview, period_key, window),
        # 数据质量标记（缺值、跨表不一致、未归类、异常放量）
        "flags": _build_flags(period_key, lines, overview, breakdowns, line_kpis),
    }
    payload["meta"]["sources"] = [item for item in payload["meta"]["sources"] if item]
    payload["flags"].extend(coverage_flags)
    if is_partial:
        payload["flags"].insert(
            0,
            {
                "level": "info",
                "item": "partial_period",
                "label": "本期未结束",
                "detail": (
                    f"{payload['meta']['periodLabel']} 截至 {_today().isoformat()} 尚未走完，"
                    "数值为累计到当前的进度，与上一完整期不可比"
                ),
                "action": "环比标「进行中」，不参与涨跌判断",
            },
        )
    return _cache_set(cache_key, ok(payload))


@routes.get("/rank", summary="一期某业务线榜单（总量 / 增量 / 下滑）")
def rank(
    line: str = Query(default="download", description="download / ad / online"),
    period: str = Query(default="weekly", pattern="^(weekly|monthly)$"),
    key: str | None = Query(default=None, description="期键，缺省取该表最新一期"),
    category: str = Query(default="top", pattern="^(top|gain|loss)$"),
    limit: int = Query(default=10, ge=1, le=100),
    dist: str | None = Query(
        default=None, pattern="^[0-2]$", description="按 distribution_type 过滤"
    ),
    list_type: str | None = Query(
        default=None,
        description="仅 PC 在线榜使用：dist_0/1/2（W45 起）；pc_played 等旧维度仅存于历史周",
    ),
) -> Any:
    source = RANK_SOURCES.get(line)
    if source is None:
        return error_response(CODE_BAD_REQUEST, f"未知业务线：{line}", 400)

    try:
        period_key = _validate_period(period, key)
    except ValueError as exc:
        return error_response(CODE_BAD_REQUEST, str(exc), 400)

    allowed_list_types = (*CURRENT_LIST_TYPES, *RETIRED_LIST_TYPES)
    if line == "online" and list_type and list_type not in allowed_list_types:
        return error_response(CODE_BAD_REQUEST, f"未知 list_type：{list_type}", 400)

    try:
        period_key = period_key or _latest_period(source.table, period)
        if period_key is None:
            return error_response(CODE_BAD_REQUEST, "尚无榜单数据", 404)

        cache_key = ("rank", line, period, period_key, category, limit, dist, list_type)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        filters = [
            f"period_type = '{period}'",
            f"crawled_at = '{period_key}'",
            f"rank_category = '{category}'",
            "is_active = 1",
        ]
        if line == "online" and list_type:
            filters.append(f"list_type = '{list_type}'")
        if dist:
            filters.append(f"distribution_type = {int(dist)}")

        sql = f"""
            SELECT rank_no, app_id, app_name, {source.icon} AS icon, distribution_type,
                   tag_1, tag_2, {source.value} AS value,
                   {source.prev} AS prev_value, {source.diff} AS diff_value,
                   updated_at
              FROM {source.table}
             WHERE {' AND '.join(filters)}
             ORDER BY rank_no
        """
        rows = _query(sql, max_rows=400)
    except (ConfigError, SqlExecutionError) as exc:
        return _fail(exc)

    items: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for row in rows:
        mark = (_as_int(row.get("rank_no")), _as_int(row.get("app_id")))
        if mark in seen:  # 同榜重跑残留行去重
            continue
        seen.add(mark)
        value = _as_number(row.get("value"))
        prev_value = _as_number(row.get("prev_value"))
        diff_value = _as_number(row.get("diff_value"))
        items.append(
            {
                "rankNo": mark[0],
                "appId": mark[1],
                "appName": row.get("app_name"),
                "icon": row.get("icon"),
                "distributionType": _as_int(row.get("distribution_type")),
                "distributionLabel": DIST_LABELS.get(
                    str(_as_int(row.get("distribution_type"))), UNKNOWN_DIST
                ),
                "tags": [tag for tag in (row.get("tag_1"), row.get("tag_2")) if tag],
                "value": _as_int(value) if source.unit != "人" else _as_float(value),
                "prevValue": _as_int(prev_value) if source.unit != "人" else _as_float(prev_value),
                "diff": _as_int(diff_value) if source.unit != "人" else _as_float(diff_value),
                "diffPct": _ratio(value, prev_value),
            }
        )

    return _cache_set(
        cache_key,
        ok(
            {
                "line": line,
                "label": source.label,
                "periodType": period,
                "periodKey": period_key,
                "category": category,
                "listType": list_type,
                "unit": source.unit,
                "availableRows": len(items),
                "items": items[:limit],
            },
            total=len(items),
        ),
    )


def _latest_period(table: str, period_type: str) -> str | None:
    sql = f"SELECT MAX(crawled_at) AS latest FROM {table} WHERE period_type = '{period_type}'"
    rows = _query(sql)
    latest = rows[0].get("latest") if rows else None
    return str(latest).strip() if latest else None


# 主前缀：/api/v09/data/periodic（沿用现有 nginx 的 /api/v09/data/ 反代）
router = APIRouter(prefix="/api/v09/data/periodic", tags=["periodic"])
router.include_router(routes)
