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

# distribution_type → 业务名称（前端不再自己维护一份）
DIST_LABELS = {"0": "商业游戏", "1": "独立小游戏", "2": "Maker 小游戏"}
UNKNOWN_DIST = "未分类"
MAX_DIST_KEYS = ("0", "1", "2")

# 预约：DWS 2026-11-16 落库（数据从 11-08 才有，更早的期永久补不回来）。
# 键列叫 `crawled_at`（不是 period_key），分型行 category_value 是 '0'/'1'/'2' 字符串。
RESERVE_TABLE = "dws_taptap_reserve_periodic_report"
RESERVE_RANK_TABLE = "dws_taptap_reserve_periodic_rank"
RESERVE_START = "2026-11-08"

# 逐日趋势（分型拆解下面那张图）：日粒度的源表。
# 已核过与周期表同源：「下载」W46 七天之和 = 10,475,431、按分型累计 = 7024506/1803164/1647761，
# 与周期表 download_total 与 breakdown 一致；「广告」七天之和 = 5,969，分型 5822/147 也一致。
DAILY_SOURCES: dict[str, tuple[str, str, str, str]] = {
    # 线: (表, 日增量表达式, metric 名（对齐周期表列）, 单位)
    "download": (
        "dws_taptap_download_v2_daily",
        "download_count",
        "download_total",
        "次",
    ),
    "ad": (
        "dws_taptap_ad_aggr_v6_daily",
        "COALESCE(ad_views, 0) + COALESCE(pcebid_views, 0)",
        "total_views",
        "次",
    ),
}
# 在线没有日表：用平台快照小时表按天取 MAX（与周期表「峰值」同口径）。
# 已核（W46 七天）：v2_hourly 每日 MAX = 121104 / 121865 / 120764 / 122297 / 146354 /
# 139416 / 127466，与「details_hourly 按小时求和后取当日最大」逐个相同，
# 且 max(每日) = 146354 = 周期表 peak_online_players —— 图上不会出现「日线高点高于期值」。
# 用 v2_hourly 而不是 details：每小时一行（整周 84 行），不必把 860 款 × 84 小时的明细搬回来。
ONLINE_HOURLY_TABLE = "dws_taptap_pc_online_peak_players_v2_hourly"
RESERVE_RANK_LIMIT = 50
# 分型款数各自去重：期内换过分型的游戏会在多个分型里各算一次，所以款数相加 > 合计；
# 只有 reserve_add 恒等于合计。这句话要跟前端/老板两边都对齐，直接随 payload 透出去
RESERVE_DIST_NOTE = (
    "分型款数按分型各自去重，期内换过分型会算进多个分型，款数相加大于合计；"
    "新增预约量不受影响，三项相加恒等于合计"
)

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
# 榜单「样本太薄」阈值：该榜 realHours 的中位数**严格小于** 8 小时才算。
#   · 量的是「每个游戏自己的有效在线小时数」，即榜内单条数据够不够厚（W46 dist_1 中位数 4.5 → 薄）；
#   · 用严格小于：W45 dist_0 中位数正好 8.0，它是高区分度的商业榜，只是那一周覆盖只有 8 小时，
#     那属于覆盖问题，由 online_partial_coverage 那条 flag 说，不在这里重复判。
LOW_SAMPLE_HOURS = 8
# 概览表与在线周期表的容差（相对差）
OVERVIEW_TOLERANCE = 0.01

# 在线周期表的数据从这一天开始有（明细覆盖 11-08 08:00 起；11-07 只有 v2 无明细，
# 不进周期表）；起点更早的期（W45、2026-11）覆盖不全，环比不可比
ONLINE_COVERAGE_START = "2026-11-08"
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
    # 分型三项能否相加：在线线的分型是「各自周内峰值」，峰值不在同一小时，结构上不可加
    summable: bool = True


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
        # 在线分型名是各自周内峰值，不是同小时快照；要相加请看 peakHour
        summable=False,
    ),
)

LINE_BY_KEY = {source.key: source for source in LINE_SOURCES}

# 采集覆盖款数（DWS 后加的列，与 game_cnt 同一行）：它回答的是「当期名单覆盖多少款」，
# 不是「有下载/有在线多少款」。两者相减才是「当期为 0 的款数」，不许互相代替。
ROSTER_COLUMN = "game_cnt_roster"
ROSTER_LABEL = "覆盖游戏款数"
ROSTER_NOTE = (
    "当期采集名单覆盖到的游戏款数（含该项为 0 的）；"
    "与同线款数相减即为该项为 0 的款数，不能当作有下载/有在线"
)


def _roster_spec(source: LineSource) -> MetricSpec | None:
    """覆盖款数指标：表里没这一列就不存在（老库、还没重算的期都不强造）。"""
    if ROSTER_COLUMN not in _columns_of(source.table):
        if source.key in ("download", "online"):
            _warn_missing_column(
                source.table, ROSTER_COLUMN, "覆盖款数这条（含前端说明句）会整条缺失"
            )
        return None
    if source.key == "download":
        key = ROSTER_COLUMN
    elif source.key == "online":
        # 不能把 online_game_cnt 的列改成 roster：那会把「有在线的款数」变成覆盖款数
        key = f"online_{ROSTER_COLUMN}"
    else:
        return None
    return MetricSpec(key, ROSTER_COLUMN, ROSTER_LABEL, "款", note=ROSTER_NOTE)

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
    # 榜单图标列：三张榜都有（在线榜的 icon 由 DWS 2026-11-16 从 details_hourly 回填，
    # 262/262 行非空）。个别老 app 仍可能为空，前端保留首字兜底。
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
    ),
}


# ---------------------------------------------------------------- 基础工具


def _as_text(value: Any) -> str | None:
    """文本列收敛：None / 空串 / 字符串 "NULL" 一律视为缺值（老视图会用 NULL 字面量占位）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return None
    return text


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


def _as_ratio(value: Any) -> float | None:
    """0~1 的占比保留 4 位小数（_as_float 是按「人数」设计的 2 位，会把 0.4295 压成 0.43）。"""
    number = _as_number(value)
    if number is None:
        return None
    return round(float(number), 4)


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


# 依赖列缺失时只喊一次：缺列是部署/口径变更事故，不应该每请求刷屏
_WARNED_MISSING: set[str] = set()


def _warn_missing_column(table: str, column: str, impact: str) -> None:
    """依赖列不在时打一条 WARNING。

    为什么特意加：`game_cnt_roster` / `online_game_cnt_hour` 都是后加的列，取不到时
    逻辑是**静默降级**的（少一条 KPI / 时间为 null），不报错、数字也仍然对，只有文案
    少一句——靠数字巡检抓不到，只能靠人眼看 note；库列名差一个词就白做一版。
    """
    key = f"{table}.{column}"
    if key in _WARNED_MISSING:
        return
    _WARNED_MISSING.add(key)
    logger.warning("依赖列缺失 %s：%s（只报一次）", key, impact)


def _columns_of(table: str) -> set[str]:
    """取表的列名（每表只查一次）。MySQL / 测试用 SQLite 都适用。"""
    if table in _COLUMNS:
        return _COLUMNS[table]
    try:
        columns = set(run_query(f"SELECT * FROM {table} LIMIT 0", max_rows=1).columns)
    except SqlExecutionError as exc:
        # 探列失败就「不过滤」并告警，绝不把维度的过滤条件变成整篇 500；
        # 真正的查询会再报错，错误信息比这里丢出来更准确
        logger.warning("探测 %s 的列名失败，本轮按“无 is_active 过滤”处理：%s", table, exc)
        return set()
    _COLUMNS[table] = columns
    return columns


def _active_filter(table: str) -> str:
    """表里 is_active 列时只取有效行：DWS 下线残留是置 0 不删数据。

    本库这几张表的 is_active 是 `tinyint NOT NULL DEFAULT 1`（实操 NULL 计数为 0），
    所以 COALESCE 是防御性写法：将来若出现 NULL 行，按“有效”处理而不静默丢数。
    列名探测结果进程内缓存：给报表表新增 is_active 后需重启服务才会自动生效。
    """
    return " AND COALESCE(is_active, 1) = 1" if "is_active" in _columns_of(table) else ""


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


# 采集节奏：每 2 小时一次 → 每天 12 个采样点；满周 84、满月 天数×12
SAMPLES_PER_DAY = 12
COVERAGE_MIN_RATIO = 0.9


def _expected_samples(period_type: str, key: str) -> int:
    """该期按采集节奏应有多少个采样点（分母用**已过天数**）。

    月报 2026-11 在 11-16 时只该要求 16 天 × 12 = 192，用整月 30 天会把
    「还没到」误算成「没采到」。这个数只作「期口径」参考，不参与告警判定。
    """
    start, end = _period_range(period_type, key)
    start_day = date.fromisoformat(start)
    last_day = min(date.fromisoformat(end), _today())
    if last_day < start_day:
        return 0
    return ((last_day - start_day).days + 1) * SAMPLES_PER_DAY


def _coverage_info(
    period_type: str,
    key: str,
    sample_hours: int | None,
    coverage_start: str | None = None,
) -> dict[str, Any]:
    """把「覆盖」拆成三个互不冒充的信号。

    1. 窗口内完整性 ratio —— 采集窗口（源起点~期末，未结束的期只到昨天）里
       实采 / 应采。**只有它参与告警**：窗口内缺采才是可修的采集故障。
    2. sourceStart / sourceStartsLate —— 在线数据从哪天开始有。期初更早的那几天
       「本来就没有源数据」，是事实说明，不是故障。
    3. 新鲜度（是否还在采）由 `meta.updatedAt` + 巡检判断，不在这里算。

    用「末采样日」当窗口末端会藏掉「采集中途挂掉」：W45 只要 11-08 那天采满
    就是 12/12 = 100% 绿。所以已结束的期窗口取到期末，进行中的期只算到昨天。
    """
    start, end = _period_range(period_type, key)
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    today = _today()
    in_progress = end_day >= today
    window_end = today - timedelta(days=1) if in_progress else end_day
    source_start = date.fromisoformat(coverage_start) if coverage_start else None
    window_start = max(start_day, source_start) if source_start else start_day
    source_starts_late = bool(source_start and start_day < source_start)

    expected = (
        ((window_end - window_start).days + 1) * SAMPLES_PER_DAY
        if window_end >= window_start
        else 0
    )
    period_expected = _expected_samples(period_type, key)
    ratio = sample_hours / expected if (sample_hours is not None and expected) else None
    period_ratio = (
        sample_hours / period_expected
        if (sample_hours is not None and period_expected)
        else None
    )
    # gap = 窗口内真的采少了（可修，要告警）；
    # sourceStartsLate 只阻断环比，不告警
    gap = ratio is not None and ratio < COVERAGE_MIN_RATIO

    return {
        "sampleHours": sample_hours,
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        # 窗口末端是不是「已定稿」：进行中的期取到昨天，昨天的数据要等每天 00:05
        # 的同步任务落库，同步延迟时这一格会短暂偏小 —— 巡检对 false 的期只打印不判失败
        "windowEndFinal": not in_progress,
        "expectedSamples": expected,
        # 窗口内完整性：唯一参与告警的指标
        "ratio": round(ratio, 4) if ratio is not None else None,
        "gap": gap,
        # 期口径：含「期初无源」的那几天，只作参考，不告警（否则永远红）
        "periodExpectedSamples": period_expected,
        "periodRatio": round(period_ratio, 4) if period_ratio is not None else None,
        # 源起点：期初到源起点之间本来就没有数据
        "sourceStart": coverage_start,
        "sourceStartsLate": source_starts_late,
        # 环比是否可比：窗口缺采 或 期初无源，都不该拿来做环比基数
        "partial": gap or source_starts_late,
    }


def _coverage_level(info: dict[str, Any], is_current: bool) -> str:
    """覆盖 flag 的严重级别：按「本期数字还准不准」定，不按「能不能修」定。

    - 上期的问题：只解释「本期为什么没有环比」，info；
    - 本期窗口内完整度 < 0.9：这一期的 KPI/分型都建立在残缺样本上（W45 用 8/12
      算出了整周的 581 款），必须让人看见 —— medium，与巡检的 FAIL 结论一致；
    - 期初无源但窗口内采得够（2026-11 的 92/96）：info。

    早先按「首日启动不可修」把 W45 降成 info，和巡检「67% FAIL」直接矛盾，
    已按「影响优先」改回。
    """
    if not is_current:
        return "info"
    return "medium" if info["gap"] else "info"


def _coverage_note(info: dict[str, Any], key: str) -> str | None:
    """覆盖情况的人话说明（给 flag 的 detail 用）。"""
    sample = info.get("sampleHours")
    if sample is None:
        if info["sourceStartsLate"]:
            return (
                f"在线数据自 {info['sourceStart']} 起才有，{key} 期初几天本就无源数据"
                "（属事实，不是采集故障）"
            )
        return None
    ratio = info.get("ratio")
    window = f"{info['windowStart']}~{info['windowEnd']}"
    if ratio is None:
        return f"{key} 采集窗口 {window} 尚未产生采样点"
    pct = ratio * 100
    note = (
        f"{key} 采集窗口 {window} 内 {sample}/{info['expectedSamples']} 个采样点"
        f"（约 {pct:.0f}%，采集节奏 2 小时/次）"
    )
    if info["periodRatio"] is not None:
        note += (
            f"；期口径 {sample}/{info['periodExpectedSamples']}"
            f"（约 {info['periodRatio'] * 100:.0f}%，含期初无源的天）"
        )
    if info["sourceStartsLate"]:
        note += (
            f"；在线数据自 {info['sourceStart']} 起才有，"
            f"期初（{info['windowStart']} 之前）无源数据，属事实不是故障"
        )
    if info["gap"]:
        note += "，窗口内采集有缺失"
    return note


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
        # 覆盖款数是后加的列，不在 metrics 里按线固定，列在才取（和 sample_hours 一样按列存断）
        if ROSTER_COLUMN in _columns_of(source.table):
            columns += f", {ROSTER_COLUMN}"
        if "sample_hours" in _columns_of(source.table):
            columns += ", sample_hours"
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


def _overview_argmax_select(columns: set[str]) -> str:
    """概览表「MAX(game_cnt) 取值小时」那一列的 SELECT 片段（没有就是空串）。

    列名有两个版本：DWS 口述/提交信息里叫 `online_game_cnt_at`，线上表里实际叫
    `online_game_cnt_hour`（两者数据一样，W46 = 2026-11-15 22）。两个都认，统一
    别名成 `online_game_cnt_at` 往上走，免得下游记两个名字。
    """
    if "online_game_cnt_hour" in columns:
        return ", online_game_cnt_hour AS online_game_cnt_at"
    if "online_game_cnt_at" in columns:
        return ", online_game_cnt_at"
    # 两列都没有是能力缺失，不是「本期无值」：静默跳过会让「发布了但 note 仍无时间」
    # 只能靠人眼比文案才能发现，所以留一条 WARNING（同一列只报一次）
    _warn_missing_column(
        OVERVIEW_TABLE,
        "online_game_cnt_hour",
        "概览表没有 argmax 小时列，concurrent_game_cnt_at 将恒为 null",
    )
    return ""


def _load_overview() -> dict[str, dict[str, Any]]:
    """概览周表（仅周报）：按游戏与跨表校验用。"""
    cache_key = ("overview",)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # online_game_cnt_at 是 DWS 后加的列（MAX(game_cnt) 那条快照的时间），老概览表没有
    argmax_column = _overview_argmax_select(_columns_of(OVERVIEW_TABLE))
    # 重点游戏的 app_id / icon 同样是后加的列（2026-11-16）；列在才拼，老库不炸
    overview_columns = _columns_of(OVERVIEW_TABLE)
    player_columns = "".join(
        f", {key}_{suffix}"
        for key, *_ in PLAYER_COLUMNS
        for suffix in ("app_id", "icon")
        if f"{key}_{suffix}" in overview_columns
    )
    sql = f"""
        SELECT week_key, week_start, week_end,
               online_game_cnt, online_peak_players{argmax_column}{player_columns},
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
    # placeholder_ratio 只有在线报表有（采集兜底值占比），其他线自动跳过
    columns = _columns_of(source.table)
    ratio = "placeholder_ratio" if "placeholder_ratio" in columns else "NULL"
    player_ratio = (
        "placeholder_player_ratio" if "placeholder_player_ratio" in columns else "NULL"
    )
    placeholder_players = (
        "placeholder_players" if "placeholder_players" in columns else "NULL"
    )
    sql = f"""
        SELECT crawled_at, category_value AS dist,
               {source.breakdown_column} AS value, {source.games_column} AS games,
               {ratio} AS placeholder_ratio, {player_ratio} AS placeholder_player_ratio,
               {placeholder_players} AS placeholder_players
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
    partial_periods: frozenset[str] = frozenset(),
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
            # 该期还没走完（如进行中的 W47）：末点会是一截假断崖
            "isPartial": period in partial_periods,
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
    """副指标：单小时同时在线款数最高（概览表的 online_game_cnt）。

    名字里不能出现「峰值小时」：这个值取的是 MAX(game_cnt)，出现在采集名单覆盖最全的那一小时
    （W46 = 11-15 22 / 671）；而「在线峰值」是人数最多的那一小时（W46 = 11-13 22 / 146,354 人 /
    653 款），两者不是同一小时。写成「峰值小时同时在线款数」会被拿去和 653 对账，
    怎么看都像错了一个数。

    它也与主线「本周去重款数」不同：前者是某一小时的并发款数（同一时刻），后者是整周 app_id
    去重。两者相差几十到上百款都是正常的，因此**不出环比**。
    """
    row = overview.get(period_key) or {}
    # 取值所在小时由概览表 online_game_cnt_at 给（DWS 重算的 argmax 时间），不是 peak_hour：
    # peak_hour 是「人数最多」那一小时（W46 = 11-13 22），671 在 11-15 22，拼在一起就是错句。
    # 没有这一列/这一值时宁可不写时间，也不拿 peak_hour 顶。
    at = str(row.get("online_game_cnt_at") or "").strip() or None
    note = "该周任意一小时里同时在线的最大款数（概览表取 MAX）"
    if at:
        note += f"，取值于 {at}"
    note += "；与在线峰值（人数最多的那一小时，见 peakHour）不一定同一小时，也不可与「本周去重款数」相减"
    spec = MetricSpec(
        "concurrent_game_cnt",
        "online_game_cnt",
        "单小时同时在线款数最高",
        "款",
        note=note,
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
        # 取值所在小时（可与 peakHour.crawledAt 对照：两者不一定同一小时）
        "concurrent_game_cnt_at": at,
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
        # 概览周表只在周一产出上一周：当期（如进行中的 W47）必然还没这一行。
        # 告诉前端为什么是空的，别渲染成 0、也别当成数据断了
        **(
            {}
            if value is not None
            else {
                "unavailableReason": (
                    "概览周表每周一才生成上一周，本期还没有这一行；"
                    "等下一个周一生成后本字段会自动出现"
                )
            }
        ),
    }


def _placeholder_player_ratio(row: dict[str, Any]) -> float | None:
    """兜底值在「人」上的占比（0~1）。优先读 DWS 的 placeholder_player_ratio 列。

    DWS 的 placeholder_ratio 分母是**行数**（app-小时），回答「多少款游戏这一小时没真值」；
    UI 上那句「8,526 人里 86.8% 是兜底」用的是**人数**。两者可以差 3 倍
    （W46 商业游戏：行数 43% vs 人数 15%），共用一句话会标错对象。

    由同一行推导：兜底行数 = round(placeholder_ratio × game_cnt)，
    每行贡献的在线人数恰好是哨兵值 100（上游对低体量游戏返回 100，不是我们填的），
    所以 players_from_placeholder = 兜底行数 × 100，再除以该分型的 peak_online_players。
    仅对 dist_peak 行成立（那是同一小时、人数可加）；周期汇总的 dist 行分母是峰值，不能这么算。
    """
    provided = _as_ratio(row.get("placeholder_player_ratio"))
    if provided is not None:
        return provided
    ratio = _as_ratio(row.get("placeholder_ratio"))
    games = _as_int(row.get("game_cnt"))
    players = _as_int(row.get("peak_online_players"))
    if ratio is None or not games or not players:
        return None
    placeholder_rows = round(ratio * games)
    return round(placeholder_rows * 100 / players, 4)


def _load_peak_hour(period_type: str, period_key: str) -> dict[str, Any] | None:
    """全局峰值那一小时的同小时分型快照，直接读周期表的 category_type='dist_peak'。

    为什么不再自己拼明细表：
      1. details_hourly 是原始层、有清理策略（11-07 那段就被清过），报表层读它迟早踩坑；
      2. 历史期只有表里才有正确峰值小时——W45 的 11-07 22 点明细已清，
         自算会整块缺省，而 dist_peak 给的是 11-08 20（576 款 / 125,697 人）；
      3. 口径只在一处维护：DWS 每天与 dist/all 同批重算，接口不参与计算。

    表里有 `category_value='ALL'` 行时以它为准（分母就是 653，不是整周去重的 860）；
    没有该行时退回「三项求和 + all 行峰值校验」。取不到整个字段缺省，不影响整篇报告。
    """
    table = ONLINE_TABLE
    # placeholder_ratio：该行里「采集兜底值」的占比（上游没采到真值时写 100），新库才有
    columns = _columns_of(table)
    ratio_column = "MAX(placeholder_ratio)" if "placeholder_ratio" in columns else "NULL"
    player_ratio_column = (
        "MAX(placeholder_player_ratio)" if "placeholder_player_ratio" in columns else "NULL"
    )
    players_column = (
        "MAX(placeholder_players)" if "placeholder_players" in columns else "NULL"
    )
    sql = f"""
        SELECT category_value AS dist, game_cnt, peak_online_players,
               MAX(peak_hour) AS peak_hour, {ratio_column} AS placeholder_ratio,
               {player_ratio_column} AS placeholder_player_ratio,
               {players_column} AS placeholder_players
          FROM {table}
         WHERE period_type = '{period_type}' AND crawled_at = '{period_key}'
           AND category_type = 'dist_peak'{_active_filter(table)}
         GROUP BY category_value, game_cnt, peak_online_players
    """
    try:
        rows = _query(sql)
    except SqlExecutionError as exc:
        # 老库没有 peak_hour 列 / 还没回填：字段缺省比整篇 400 好
        logger.info("dist_peak 快照不可用：%s", exc)
        return None
    if not rows:
        return None

    total_row = next(
        (row for row in rows if str(row.get("dist") or "").strip().upper() == "ALL"), None
    )
    breakdown = []
    for row in rows:
        dist = str(row.get("dist"))
        if dist.strip().upper() == "ALL":
            continue
        breakdown.append(
            {
                "distributionType": dist,
                "label": DIST_LABELS.get(dist, UNKNOWN_DIST),
                "gameCnt": _as_int(row.get("game_cnt")),
                "peakOnlinePlayers": _as_int(row.get("peak_online_players")),
                # 行数口径：该分型里多少 app-小时没拿到真值（数据质量）
                "placeholderRatio": _as_ratio(row.get("placeholder_ratio")),
                # 人数口径：该分型在线人数里哨兵值贡献的占比（UI 说「这 8,526 人含水」用它）
                "placeholderPlayerRatio": _placeholder_player_ratio(row),
                # 整数口径：兜底贡献的在线人数（8,526 − 7,400 = 1,126，不用 round）
                "placeholderPlayers": _as_int(row.get("placeholder_players")),
            }
        )
    breakdown.sort(
        key=lambda item: (
            MAX_DIST_KEYS.index(item["distributionType"])
            if item["distributionType"] in MAX_DIST_KEYS
            else 99
        )
    )
    sum_games = sum(item["gameCnt"] or 0 for item in breakdown)
    sum_players = sum(item["peakOnlinePlayers"] or 0 for item in breakdown)

    if total_row is not None:
        game_cnt = _as_int(total_row.get("game_cnt"))
        peak_players = _as_int(total_row.get("peak_online_players"))
        sums_match = sum_games == game_cnt and sum_players == peak_players
    else:
        # 没有 ALL 行：以 period_type/crawled_at 的 all 行峰值作为应等于的目标
        game_cnt, peak_players = sum_games, sum_players
        all_peak = None
        try:
            all_rows = _query(
                f"""
                SELECT peak_online_players
                  FROM {table}
                 WHERE period_type = '{period_type}' AND crawled_at = '{period_key}'
                   AND category_type = 'all'{_active_filter(table)}
                 LIMIT 1
                """
            )
            if all_rows:
                all_peak = _as_int(all_rows[0].get("peak_online_players"))
        except SqlExecutionError:
            all_peak = None
        sums_match = all_peak is None or sum_players == all_peak

    if total_row is not None:
        # 有 ALL 行：直接用它的两个聚合
        overall_player_ratio = _placeholder_player_ratio(total_row)
    else:
        # 没有 ALL 行（新周期首日还没重算）：按分型的人数和再除该小时总人数
        known_pairs = [
            (item["placeholderPlayerRatio"], item["peakOnlinePlayers"])
            for item in breakdown
            if item["placeholderPlayerRatio"] is not None
        ]
        if known_pairs and peak_players:
            overall_player_ratio = round(
                sum(ratio * players for ratio, players in known_pairs) / peak_players, 4
            )
        else:
            overall_player_ratio = None
    if total_row is not None:
        overall_ratio = _as_ratio(total_row.get("placeholder_ratio"))
    else:
        known = [
            item["placeholderRatio"]
            for item in breakdown
            if item["placeholderRatio"] is not None
        ]
        overall_ratio = max(known) if known else None

    if total_row is not None:
        overall_placeholder_players = _as_int(total_row.get("placeholder_players"))
    else:
        known_players = [
            item["placeholderPlayers"]
            for item in breakdown
            if item["placeholderPlayers"] is not None
        ]
        overall_placeholder_players = sum(known_players) if known_players else None

    return {
        "crawledAt": str(rows[0].get("peak_hour") or ""),
        "gameCnt": game_cnt,
        "peakOnlinePlayers": peak_players,
        "sumGameCnt": sum_games,
        "sumPeakOnlinePlayers": sum_players,
        # 表内自洽校验：Σ三项 必须等于 ALL 行（或 all 行峰值），不平就是 DWS 写坏了
        "sumsMatch": sums_match,
        # 行数口径：该小时整体有多少 app-小时没拿到真值（ALL 行优先，否则取各分型最大）
        "placeholderRatio": overall_ratio,
        # 人数口径：该小时总在线里哨兵值贡献的占比（W46 = 23.78%）
        "placeholderPlayerRatio": overall_player_ratio,
        # 整数口径：该小时兜底贡献的在线人数（W46 ALL = 34,800；扣减用它，不要 round 反推）
        "placeholderPlayers": overall_placeholder_players,
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
    trend_periods: tuple[str, ...] = (),
    partial_periods: frozenset[str] = frozenset(),
    is_partial: bool = False,
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
                # 采集兜底值（上游没采到真值时写的 100）占比，0~1；>0.5 建议 UI 标「仅作参考」
                "placeholderRatio": _as_ratio(
                    (current.get(dist) or {}).get("placeholder_ratio")
                ),
                # 人数口径（整期）；DWS 新列没产出时为 null —— 整期的分母是「期在线合计」，
                # 不能用峰值小时的推导公式硬算
                "placeholderPlayerRatio": _as_ratio(
                    (current.get(dist) or {}).get("placeholder_player_ratio")
                ),
                # 兜底贡献的在线人数（整数，DWS 按 SUM(v where v=哨兵) 算）。
                # 前端算「扣除兜底后余量」时用这个，别再用 ratio×total（会 ±1 抖动）
                "placeholderPlayers": _as_int(
                    (current.get(dist) or {}).get("placeholder_players")
                ),
            }
        )

    for item in items:
        item["share"] = round(item["value"] / total, 4) if total else None
        # 本期还没走完时分型环比同样不可比：与 KPI 的 deltaBlocked 同词、同做法
        item["deltaPct"] = None if is_partial else _ratio(item["value"], item["prevValue"])
        # 逐期值（与 KPI 的 trend 同形）：前端画「小游戏下载量趋势」这类分型曲线时
        # 不必再逐期打 /report（八期就是八次请求）。缺行的期不补 0，直接不出现。
        item["trend"] = [
            {
                "period": period,
                "label": _period_label_short(period),
                "value": int(_as_number((grouped.get(period, {}).get(item["key"]) or {}).get("value")) or 0),
                # 该期还没走完：末点会是一截假断崖，要画趋势就自己跳过
                "isPartial": period in partial_periods,
            }
            for period in trend_periods
            if item["key"] in grouped.get(period, {})
        ]

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
        **({"deltaBlocked": "current_in_progress"} if is_partial else {}),
        "items": items,
        "unclassified": unclassified,
        "unclassifiedTotal": unknown_total,
        "total": int(total),
        "consistency": {
            "allValue": _as_int(all_value),
            "detailTotal": detail_total,
            "gapPct": gap,
            "consistent": gap is None or abs(gap) <= OVERVIEW_TOLERANCE * 100,
            # false = 结构上就不可能相等，gapPct 不是数据质量问题，别标红
            "summable": source.summable,
            "note": (
                "三类是各分型各自的周内峰值，峰值不在同小时，相加必然大于总量；"
                "要占比/堆叠请用 peakHour 的同小时快照"
                if not source.summable
                else "三类互斥且齐全，可直接相加"
            ),
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
                # 角标用真图（老概览表没这两列时为空，前端自行首字兜底）
                "appId": _as_int(current.get(f"{key}_app_id")),
                "icon": _as_text(current.get(f"{key}_icon")),
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


def _reserve_expected_days(period_type: str, key: str) -> int:
    """该期「到今天为止应该有多少天预约数据」——按**采集起点裁剪**。

    不裁剪的话月报永远是 9/17 的「未采满」，而 11 月其实一天都没缺：采集 2026-11-08
    才上线，11-01~11-07 那些天根本不存在可采的数据。裁剪后三种情况才分得开：
      期初早于采集起点（只能采 N 天）· 已采满 · 真缺口（day_cnt < 裁剪后应采）。
    """
    start_text, end_text = _period_range(period_type, key)
    start = max(date.fromisoformat(start_text), date.fromisoformat(RESERVE_START))
    # 上界取「昨天」：今天的采集还没跑完，把今天算进去会把每期都误判成缺一天
    end = min(date.fromisoformat(end_text), _today() - timedelta(days=1))
    return max((end - start).days + 1, 0)


def _period_days(period_type: str, key: str) -> int:
    """该期的**日历长度**（周 = 7，月 = 28~31）。上期用这个当分母，
    因为上期已经结束，「本来应该有几天」就是整期长度。"""
    start_text, end_text = _period_range(period_type, key)
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    return (end - start).days + 1


def _load_reserve_rows(
    period_type: str, keys: list[str]
) -> dict[str, dict[str, dict[str, Any]]] | None:
    """预约报表行 → {期: {"all"|分型: row}}。表不可用时返回 None（不是空字典）。"""
    if not keys:
        return {}
    quoted = ", ".join(f"'{key}'" for key in keys)
    sql = f"""
        SELECT crawled_at, category_type, category_value, reserve_add,
               reserve_game_cnt, reserve_game_cnt_roster, reserve_total_end,
               day_cnt, first_day, last_day, updated_at
          FROM {RESERVE_TABLE}
         WHERE period_type = '{period_type}' AND crawled_at IN ({quoted})
    """
    try:
        rows = _query(sql)
    except SqlExecutionError as exc:
        logger.info("预约报表不可用：%s", exc)
        return None
    grouped: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in keys}
    for row in rows:
        period = str(row.get("crawled_at") or "").strip()
        if period not in grouped:
            continue
        category = str(row.get("category_type") or "").strip()
        slot = "all" if category == "all" else str(row.get("category_value") or "").strip()
        grouped[period][slot] = row
    return grouped


def _load_reserve_ranks(period_type: str, period_key: str) -> dict[str, list[dict[str, Any]]]:
    """预约双榜（gain=本期新增，total=期末池子累计）。取不到就两份都空。"""
    sql = f"""
        SELECT rank_type, rank_no, app_id, app_name, icon, distribution_type,
               reserve_add, total_reserve_count
          FROM {RESERVE_RANK_TABLE}
         WHERE period_type = '{period_type}' AND crawled_at = '{period_key}'
         ORDER BY rank_type, rank_no
         LIMIT {RESERVE_RANK_LIMIT * 2}
    """
    try:
        rows = _query(sql, max_rows=RESERVE_RANK_LIMIT * 2)
    except SqlExecutionError as exc:
        logger.info("预约榜单不可用：%s", exc)
        return {"gain": [], "total": []}
    ranks: dict[str, list[dict[str, Any]]] = {"gain": [], "total": []}
    for row in rows:
        rank_type = str(row.get("rank_type") or "").strip()
        if rank_type not in ranks:
            continue
        ranks[rank_type].append(
            {
                "rankNo": _as_int(row.get("rank_no")),
                "appId": _as_int(row.get("app_id")),
                "appName": _as_text(row.get("app_name")),
                "icon": _as_text(row.get("icon")),
                "distributionType": _as_int(row.get("distribution_type")),
                "reserveAdd": _as_int(row.get("reserve_add")),
                "totalReserveCount": _as_int(row.get("total_reserve_count")),
            }
        )
    return {key: items[:RESERVE_RANK_LIMIT] for key, items in ranks.items()}


def _build_reserve(period: str, period_key: str, prev_period: str | None) -> dict[str, Any]:
    """07 预约：本期新增 / 日均 / 分型 / 双榜。

    三条硬约束（都写在返回值里，不靠文档默契）：
      1. 只有 11-08 起的期才有数据，不给就不装（早期回 available=false 带原因）；
      2. 跨期**不可比总量**（上线即退出池子，W46 期末 6,961 万 < W45 的 9,640 万），
         所以只提供日均及其环比，总量环比不参与计算；
      3. 分型款数各自去重，款数相加 > 合计，只有新增量相加等于合计。
    """
    grouped = _load_reserve_rows(period, [k for k in (period_key, prev_period) if k])
    if grouped is None:
        return {
            "available": False,
            "coverageStart": RESERVE_START,
            "reason": "预约报表不可用（表未就绪或查询失败），本期不展示预约数据",
        }

    current = grouped.get(period_key) or {}
    all_row = current.get("all")
    day_cnt = _as_int(all_row.get("day_cnt")) if all_row else None
    if all_row is None or not day_cnt:
        return {
            "available": False,
            "coverageStart": RESERVE_START,
            "reason": (
                f"预约采集 {RESERVE_START} 才上线（上游小时表只保留约 2 天，早期期永久补不回来），"
                f"本期没有可用预约数据"
            ),
        }

    reserve_add = _as_int(all_row.get("reserve_add"))
    expected_days = _reserve_expected_days(period, period_key)
    period_start_text, _period_end_text = _period_range(period, period_key)
    # 期初早于采集起点：这几天永远不会有数据，不该标成「缺口」，也不该拿它当分母
    start_limited = date.fromisoformat(period_start_text) < date.fromisoformat(RESERVE_START)
    # 期还没走完（周中/月中）：日均不可比，但要和「采集缺口」分开说
    in_progress = _is_partial(period, period_key)
    daily_avg = round(reserve_add / day_cnt) if reserve_add is not None and day_cnt else None

    prev_all = (grouped.get(prev_period) or {}).get("all") if prev_period else None
    prev_add = _as_int(prev_all.get("reserve_add")) if prev_all else None
    prev_day_cnt = _as_int(prev_all.get("day_cnt")) if prev_all else None
    prev_daily_avg = (
        round(prev_add / prev_day_cnt)
        if prev_add is not None and prev_day_cnt
        else None
    )

    # 上期的分母是**整期日历天数**（已结束的期，本来就应该有多少天），不裁剪；
    # 拿裁剪后的 1 去比 W45 的 1 天，会把「周日单日」当成整周而误判为可比
    prev_days = _period_days(period, prev_period) if prev_period else None
    # 日均环比只在「本期已结束且采满、上期整期采满」时给。只要有一边不成立就一直 null，
    # 并用 dailyAvgBlocked 说明是哪一边：一个说「不可比」却同时给出数字的字段，
    # 下一个消费方（导出、脚本）一定会直接读那个数字。
    #   prev_missing         月报 11 对 10 月：上期早于采集起点，根本没分母
    #   current_in_progress  W47 只 1 天（1 天对 7 天均值，周末效应冒充腰斩）
    #   current_partial      本期采集真缺天（这才该标黄）
    #   prev_partial         W46 对 W45：上期只 1/7 天，周日单日对七天均值
    if not prev_day_cnt:
        daily_avg_blocked = "prev_missing"
    elif in_progress:
        daily_avg_blocked = "current_in_progress"
    elif day_cnt < expected_days:
        daily_avg_blocked = "current_partial"
    elif prev_days is not None and prev_day_cnt < prev_days:
        daily_avg_blocked = "prev_partial"
    else:
        daily_avg_blocked = None
    daily_avg_pct = _ratio(daily_avg, prev_daily_avg) if daily_avg_blocked is None else None

    dist = []
    for dist_key in MAX_DIST_KEYS:
        row = current.get(dist_key) or {}
        add = _as_int(row.get("reserve_add"))
        dist_days = _as_int(row.get("day_cnt"))
        dist.append(
            {
                "type": int(dist_key),
                "label": DIST_LABELS[dist_key],
                "reserveAdd": add,
                "gameCnt": _as_int(row.get("reserve_game_cnt")),
                "gameCntRoster": _as_int(row.get("reserve_game_cnt_roster")),
                "share": (
                    round((add or 0) / reserve_add * 100, 2) if reserve_add and row else None
                ),
                # 分型自己的覆盖天数：Maker 等新品类上个周期才上线，会出现 4/7 天。
                # 该分型这一期根本没有行时，这几个字段全部 null（不是 false）——
                # `partial:false` 的意思是「有数据且采满了」，拿去当「没数据」用会把缺口藏起来。
                "dayCnt": dist_days,
                "expectedDayCnt": expected_days if row else None,
                "firstDay": _as_text(row.get("first_day")),
                "lastDay": _as_text(row.get("last_day")),
                "partial": (dist_days < expected_days) if dist_days is not None else None,
                "hasData": bool(row),
            }
        )

    return {
        "available": True,
        "coverageStart": RESERVE_START,
        "coverage": {
            # dayCnt 是该期**真的采到**的天数；expectedDayCnt 已按采集起点裁剪，
            # 所以月报是 9/9（不是 9/17），W45 是 1/1（不是 1/7）
            "dayCnt": day_cnt,
            "expectedDayCnt": expected_days,
            # 真缺口：day_cnt < 裁剪后应采（这才该标黄）
            "partial": day_cnt < expected_days,
            # 期初早于采集起点：那几天永远不会有数据，文案应写「上线首周/采集起点前无数据」
            "startLimited": start_limited,
            # 期还没走完：页面标「进行中」，与「采集缺口」是两件事
            "periodInProgress": in_progress,
            "firstDay": _as_text(all_row.get("first_day")),
            "lastDay": _as_text(all_row.get("last_day")),
        },
        "headline": {
            "reserveAdd": reserve_add,
            "gameCnt": _as_int(all_row.get("reserve_game_cnt")),
            "gameCntRoster": _as_int(all_row.get("reserve_game_cnt_roster")),
            # 期末池子累计：只反映「这一期末还剩多少待上线」，与上期不可相减
            "reserveTotalEnd": _as_int(all_row.get("reserve_total_end")),
            "dailyAvg": daily_avg,
            "prevPeriodKey": prev_period,
            "prevReserveAdd": prev_add,
            "prevDayCnt": prev_day_cnt,
            "prevExpectedDayCnt": prev_days,
            "prevDailyAvg": prev_daily_avg,
            # 两期都采满才出数；否则 null，用 dailyAvgBlocked 区分是哪一边
            "dailyAvgPct": daily_avg_pct,
            **(({"dailyAvgBlocked": daily_avg_blocked}) if daily_avg_blocked else {}),
            "dailyAvgComparable": daily_avg_pct is not None,
        },
        "dist": dist,
        "distNote": RESERVE_DIST_NOTE,
        "ranks": _load_reserve_ranks(period, period_key),
        "updatedAt": _iso_timestamp(all_row.get("updated_at")),
    }


def _daily_window(period_type: str, key: str) -> tuple[date, date]:
    """逐日图的取数区间：期初 ~ min(期末, 昨天)。

    今天不含：当天还没采完，给了会在曲线尾端多一根假断崖。缺的天不补 0，直接不出点。
    """
    start_text, end_text = _period_range(period_type, key)
    start = date.fromisoformat(start_text)
    end = min(date.fromisoformat(end_text), _today() - timedelta(days=1))
    return start, end


def _sum_daily_points(
    table: str, expression: str, start: date, end: date, period_start: date
) -> list[dict[str, Any]]:
    """日粒度求和（下载 / 广告）：按 (日期, 分型) 一次查全，再在内存里合并成每日点。"""
    if end < start:
        return []
    sql = f"""
        SELECT crawled_at AS d, distribution_type AS dist, SUM({expression}) AS v
          FROM {table}
         WHERE crawled_at BETWEEN '{start}' AND '{end}'
         GROUP BY crawled_at, distribution_type
    """
    rows = _query(sql)
    days: dict[str, dict[str, Any]] = {}
    for row in rows:
        day_text = _as_text(row.get("d"))
        if not day_text:
            continue
        value = _as_int(row.get("v")) or 0
        slot = days.setdefault(day_text, {"total": 0, "dist": {}})
        slot["total"] += value
        dist_key = row.get("dist")
        dist_text = _as_text(dist_key)
        if dist_text is not None:
            # 未分类（dist 为 NULL）不建分型条目，但它的量已计入 total
            slot["dist"][dist_text] = slot["dist"].get(dist_text, 0) + value
    return _daily_list(days, period_start)


def _online_daily_points(start: date, end: date, period_start: date) -> list[dict[str, Any]]:
    """在线逐日：先按小时求全平台总人数，再取当天最大的那一小时（峰值口径）。"""
    if end < start:
        return []
    sql = f"""
        SELECT SUBSTR(crawled_at, 1, 10) AS d, MAX(online_players) AS total
          FROM {ONLINE_HOURLY_TABLE}
         WHERE crawled_at >= '{start}' AND crawled_at <= '{end} 23:59:59'
         GROUP BY SUBSTR(crawled_at, 1, 10)
    """
    # SUBSTR 而不是 DATE()：crawled_at 是 varchar，DATE()/HOUR() 在 MySQL 与 SQLite 行为不同，
    # 同一段 SQL 要能在生产（MySQL）和夹具（SQLite）都跑
    rows = _query(sql)
    peaks: dict[str, int] = {}
    for row in rows:
        day_text = _as_text(row.get("d"))
        total = _as_int(row.get("total"))
        if not day_text or total is None:
            continue
        peaks[day_text] = max(total, peaks.get(day_text, 0))
    return _daily_list({day: {"total": value} for day, value in peaks.items()}, period_start)


def _daily_list(days: dict[str, dict[str, Any]], period_start: date) -> list[dict[str, Any]]:
    """{日期: {"total", "dist"}} → 按日排序的点列；day = 期内第几天（从 1 开始）。"""
    points = []
    for day_text in sorted(days):
        moment = date.fromisoformat(day_text)
        point: dict[str, Any] = {
            "day": (moment - period_start).days + 1,
            "date": day_text,
            "total": days[day_text].get("total"),
        }
        if "dist" in days[day_text]:
            point["dist"] = days[day_text]["dist"]
        points.append(point)
    return points


def _build_daily(
    period: str, period_key: str, prev_period: str | None
) -> dict[str, dict[str, Any]]:
    """每条业务线的逐日趋势（本期 + 上期），前端分型拆解下面那张图用。

    三个口径决定：
      1. 缺天的期不出点（不补 0）——采集起点之前、还没采完的今天都不算；
      2. day 是「期内第几天」而不是星期几：两期起始日不同，按星期对齐会错位；
      3. 下载/广告给 total + 分型；在线只给 total（日粒度下分型需先各自取峰值再相加，
         那不是同小时的并发，给了反而与前文的峰值口径打架）。
    """
    result: dict[str, dict[str, Any]] = {}
    windows = {}
    for key in (period_key, prev_period):
        if key:
            windows[key] = _daily_window(period, key)

    for line, (table, expression, metric, unit) in DAILY_SOURCES.items():
        try:
            current_start, current_end = windows[period_key]
            current_points = _sum_daily_points(
                table, expression, current_start, current_end, current_start
            )
            previous_block: dict[str, Any] = {"periodKey": None, "points": []}
            if prev_period and prev_period in windows:
                prev_start, prev_end = windows[prev_period]
                previous_block = {
                    "periodKey": prev_period,
                    "start": prev_start.isoformat(),
                    "end": prev_end.isoformat(),
                    "points": _sum_daily_points(
                        table, expression, prev_start, prev_end, prev_start
                    ),
                }
        except SqlExecutionError as exc:
            logger.info("逐日趋势不可用（%s）：%s", line, exc)
            continue
        result[line] = {
            "unit": unit,
            "metric": metric,
            # 逐日点可以相加得到期总量（下载 / 广告是日增量）
            "summable": True,
            "distLabels": DIST_LABELS,
            "current": {
                "periodKey": period_key,
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
                "points": current_points,
            },
            "previous": previous_block,
        }

    try:
        current_start, current_end = windows[period_key]
        current_points = _online_daily_points(current_start, current_end, current_start)
        previous_block = {"periodKey": None, "points": []}
        if prev_period and prev_period in windows:
            prev_start, prev_end = windows[prev_period]
            previous_block = {
                "periodKey": prev_period,
                "start": prev_start.isoformat(),
                "end": prev_end.isoformat(),
                "points": _online_daily_points(prev_start, prev_end, prev_start),
            }
        result["online"] = {
            "unit": "人",
            "metric": "peak_online_players",
            # 每个点是「当天峰值」，峰值的峰值的 ≠ 各日峰值之和：不可相加
            "summable": False,
            "distLabels": DIST_LABELS,
            "current": {
                "periodKey": period_key,
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
                "points": current_points,
            },
            "previous": previous_block,
        }
    except SqlExecutionError as exc:
        logger.info("在线逐日趋势不可用：%s", exc)

    return result


def _build_flags(
    period_key: str,
    lines: dict[str, dict[str, dict[str, Any]]],
    overview: dict[str, dict[str, Any]],
    breakdowns: dict[str, dict[str, Any]],
    line_kpis: dict[str, list[dict[str, Any]]],
    coverage_notes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """数据质量标记：前端拿来加角标 / 提示，不用自己算。"""
    flags: list[dict[str, Any]] = []

    # 1) 概览表与在线周期表：只在**同定义**的量之间比（在线峰值 = 那一小时的人数，两边同义）。
    #
    #    款数不再比：概览表的 online_game_cnt 是「单小时最大并发款数」（W46 = 671，出现在 11-15 22），
    #    周期表的 game_cnt 是「整周去重款数」（W46 = 728）。两个定义不同的量每周必然不等，
    #    比出来的 flag 是纯噪音（W46 差 57、剔兜底前差 189，都不说明任何数据问题），
    #    所以这条检查直接去掉；也不改成「概览值 vs concurrent_game_cnt」——后者就是读概览表
    #    同一列（见 _build_concurrent_kpi），自比必然相等，留着只会假装做了校验。
    online_row = lines.get("online", {}).get(period_key) or {}
    overview_row = overview.get(period_key) or {}
    from_overview = _as_number(overview_row.get("online_peak_players"))
    from_line = _as_number(online_row.get("peak_online_players"))
    if from_overview is not None and from_line is not None:
        base = max(abs(from_overview), abs(from_line), 1)
        if abs(from_overview - from_line) / base > OVERVIEW_TOLERANCE:
            label = "在线峰值"
            coverage = (coverage_notes or {}).get("online")
            detail = (
                f"概览表 {int(from_overview):,} vs 在线周期表 {int(from_line):,}（{period_key}）"
            )
            if coverage:
                # 半期（如 W45）里概览会取到「没有明细覆盖」的那几个小时，属覆盖差不是数据错误
                detail += f"；{coverage}，概览取到了无明细覆盖的小时，属覆盖差"
            else:
                detail += "；两表统计池不同，不是同一份数据的旧快照"
            flags.append(
                {
                    "level": "medium",
                    "item": "online_peak_players_mismatch",
                    "label": f"{label}跨表口径差异",
                    "detail": detail,
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

    # 窗口内哪些期还没走完（含本期在内）：趋势点自己带 isPartial，前端画图时
    # 统一跳过末点，不用在页面里各自算日期（W47 那种进行中期会把曲线尾已画成断崖）
    trend_periods = tuple(all_periods[-window:]) if window else ()
    partial_periods = frozenset(p for p in trend_periods if _is_partial(period, p))

    try:
        # 分型趋势要逐期取数：一次 IN 查询拿全，别让前端逐期打接口
        breakdown_keys = sorted(
            {*trend_periods, period_key, *((prev_period,) if prev_period else ())}
        )
        breakdowns = {
            source.key: _build_breakdown(
                source,
                period_key,
                prev_period,
                _load_breakdown(source, period, breakdown_keys),
                _pick_value(
                    lines[source.key].get(period_key),
                    source.breakdown_column,
                    is_int=source.breakdown_column != "avg_online_players",
                ),
                trend_periods=trend_periods,
                partial_periods=partial_periods,
                is_partial=is_partial,
            )
            for source in LINE_SOURCES
        }
        overview = _load_overview() if period == "weekly" else {}
        # 逐日趋势：每条线一条或两条日粒度查询，失败就整块不出（前端静默不渲染）
        daily_by_line = _build_daily(period, period_key, prev_period)
        # 全局峰值那一小时的同小时快照（只有在线线需要；取不到就降级为 None）
        peak_hour = _load_peak_hour(period, period_key)
    except (ConfigError, SqlExecutionError) as exc:
        return _fail(exc)

    # 概览表可能缺这一期（月报没有概览表），此时按游戏区块返回空
    line_payloads = []
    line_kpis: dict[str, list[dict[str, Any]]] = {}
    coverage_flags: list[dict[str, Any]] = []
    coverage_notes: dict[str, str] = {}
    for source in LINE_SOURCES:
        series = lines[source.key]
        current = _coverage_info(
            period,
            period_key,
            _as_int((series.get(period_key) or {}).get("sample_hours")),
            source.coverage_start,
        )
        previous = (
            _coverage_info(
                period,
                prev_period,
                _as_int((series.get(prev_period) or {}).get("sample_hours")),
                source.coverage_start,
            )
            if prev_period
            else None
        )
        current_partial = current["partial"]
        prev_partial = bool(previous and previous["partial"])
        current_note = _coverage_note(current, period_key) if current_partial else None
        prev_note = (
            _coverage_note(previous, prev_period)
            if (prev_partial and previous and prev_period)
            else None
        )
        # 本期或上期覆盖不全，环比一律置 null：半周去重 / 首轮孤点都会造出假涨幅。
        # 本期还没走完（W47 只 1 天）同样置 null——拿 1 天比整周会得出 −86% 那种假跌幅，
        # 这个数不能进 payload：页面能拦，导出脚本/别人写的报表不会拦。
        delta_blocked = (
            "coverage_partial"
            if current_partial
            else (
                "prev_coverage_partial"
                if prev_partial
                else ("current_in_progress" if is_partial else None)
            )
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
                partial_periods=partial_periods,
            )
            for metric in source.metrics
        ]
        # 副指标：单小时同时在线款数最高（只在周报有概览表；不进主 KPI、不出环比）
        if source.key == "online" and overview:
            kpis.append(_build_concurrent_kpi(overview, period_key, all_periods, window))
        # 覆盖款数：列在**且该期有值**才吐。重算前是 NULL，吐个 null 出去只会让前端
        # 在页面上写「覆盖 null 款中 0 款为 0」
        roster_spec = _roster_spec(source)
        if roster_spec is not None and _pick_value(series.get(period_key), ROSTER_COLUMN) is None:
            roster_spec = None
        if roster_spec is not None:
            kpis.append(
                _build_kpi(
                    roster_spec,
                    source.key,
                    series,
                    period_key,
                    all_periods,
                    window,
                    is_partial,
                    delta_blocked=delta_blocked,
                    partial_periods=partial_periods,
                )
            )
        line_kpis[source.key] = kpis
        note = current_note or prev_note
        if note:
            coverage_notes[source.key] = note
        # 只有「窗口内真的采少了」才告警；期初无源另有 info 说明，不混进同一句
        flagged = None
        if current["gap"]:
            flagged = current
        elif previous is not None and previous["gap"]:
            flagged = previous
        if flagged is not None:
            is_current = flagged is current
            coverage_flags.append(
                {
                    # 级别见 _coverage_level：首日启动造成的缺失不占告警位
                    "level": _coverage_level(flagged, is_current),
                    "item": f"{source.key}_partial_coverage",
                    # scope + relatedPeriod 钉死这条 flag 讲的是哪一期：
                    # W46 的响应里会带一条讲 W45 的 flag，没有这两个字段时
                    # 消费方会把 W45 的属性当成 W46 的
                    "scope": "current" if is_current else "previous",
                    "relatedPeriod": period_key if is_current else prev_period,
                    "label": (
                        f"{source.label}本期采集有缺失"
                        if is_current
                        else f"{source.label}上期采集有缺失"
                    ),
                    "detail": current_note if is_current else prev_note,
                    "reason": "collection_gap",
                    "action": (
                        "本期与上期的环比一律置 null（deltaBlocked），"
                        "趋势点保留原始值，但不代表完整期"
                    ),
                }
            )
        if current["sourceStartsLate"]:
            coverage_flags.append(
                {
                    "level": "info",
                    "item": f"{source.key}_source_start",
                    "scope": "current",
                    "relatedPeriod": period_key,
                    "label": f"{source.label}数据起点晚于期初",
                    "detail": (
                        f"在线数据自 {current['sourceStart']} 起才开始采集，"
                        f"{period_key} 更早的日期没有源数据（不是采集故障，补不回来）"
                    ),
                    "action": "只在质量区作事实说明；覆盖/环比按采集窗口判断",
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
                **(({"daily": daily_by_line[source.key]}) if source.key in daily_by_line else {}),
                # 覆盖三信号：窗口内完整性 / 源起点 / （新鲜度看 meta.updatedAt）
                "coverage": current,
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
        # 07 预约（2026-11-08 才上线；无覆盖的期回 available=false，不拿 0 充数）
        "reserve": _build_reserve(period, period_key, prev_period),
        # 数据质量标记（缺值、跨表不一致、未归类、异常放量）
        "flags": _build_flags(
            period_key, lines, overview, breakdowns, line_kpis, coverage_notes
        ),
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
    listType: str | None = Query(
        default=None,
        include_in_schema=False,
        description="占位：正确参数名是 list_type（下划线）",
    ),
) -> Any:
    # FastAPI 会静默忽略拼错的 query 参数，listType 会导致返回「混合分组」榜单而看不出来
    if listType is not None:
        return error_response(
            CODE_BAD_REQUEST,
            "参数名应为 list_type（下划线）；listType 会被服务端忽略并返回混合分组榜单，"
            "因此这里直接报错而不是给你一份错数据",
            400,
        )
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
        # 进行中的期（W47 只 1 天）不能出环比：榜内 prev 是整周，diffPct 会是一串 −88%
        period_partial = _is_partial(period, period_key)

        cache_key = ("rank", line, period, period_key, category, limit, dist, list_type)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # 旧维度（pc_played 等）只存在于历史周，且 DWS 按规矩把它们标成 is_active=0；
        # 查这些维度时不能再过滤 is_active，否则历史榜永远是空的
        retired_dimension = line == "online" and list_type in RETIRED_LIST_TYPES
        filters = [
            f"period_type = '{period}'",
            f"crawled_at = '{period_key}'",
            f"rank_category = '{category}'",
        ]
        if not retired_dimension:
            filters.append("is_active = 1")
        if line == "online" and list_type:
            filters.append(f"list_type = '{list_type}'")
        if dist:
            filters.append(f"distribution_type = {int(dist)}")

        # real_hours：该游戏本周期「有有效在线小时」的数量（只有 PC 在线榜有）；
        # 用来判断这个榜有没有可比性（样本太少时名次本身就是噪声），前端不必猜数值分布
        hours_column = (
            "real_hours" if "real_hours" in _columns_of(source.table) else "NULL"
        )
        # icon 列同理按存在性处理：老库的在线榜没有 icon（以前用 NULL 字面量占位），
        # 直接写列名会让整个榜单 400；缺列时降级成 NULL 并留一条 WARNING
        if source.icon in _columns_of(source.table):
            icon_column = source.icon
        else:
            _warn_missing_column(source.table, source.icon, "榜单图标会整列为 null（前端走首字兜底）")
            icon_column = "NULL"
        sql = f"""
            SELECT rank_no, app_id, app_name, {icon_column} AS icon, distribution_type,
                   tag_1, tag_2, {source.value} AS value,
                   {source.prev} AS prev_value, {source.diff} AS diff_value,
                   {hours_column} AS real_hours, updated_at
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
                "diffPct": None if period_partial else _ratio(value, prev_value),
                # 与 KPI 的 deltaBlocked 同词：进行中的期不能拿 1 天比整周（−88% 那种假跌幅）
                **({"diffBlocked": "current_in_progress"} if period_partial else {}),
                # 有有效在线小时数（下载/广告榜没有这个列 → null）
                "realHours": _as_int(row.get("real_hours")),
            }
        )

    # 样本是否足够：全员 real_hours 都 <= 2 视为没样本（阈值写死在这里，前端不要各自判）
    hours = sorted(item["realHours"] for item in items if item["realHours"] is not None)
    sample = None
    if hours:
        mid = len(hours) // 2
        median = hours[mid] if len(hours) % 2 else (hours[mid - 1] + hours[mid]) / 2
        sample = {
            "minRealHours": hours[0],
            "maxRealHours": hours[-1],
            "medianRealHours": median,
            "lowSample": median < LOW_SAMPLE_HOURS,
        }

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
                # true = 用的是 W45 之前的旧维度（is_active=0 的历史行）
                "retiredDimension": line == "online" and list_type in RETIRED_LIST_TYPES,
                "unit": source.unit,
                "availableRows": len(items),
                "sample": sample,
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
