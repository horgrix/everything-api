"""定期报告聚合接口用例（sqlite 夹具，验证契约而不是数值本身）。"""

from __future__ import annotations

import pytest

PREFIX = "/api/v09/data/periodic"


def _report(client, **params):
    response = client.get(f"{PREFIX}/report", params={"period": "weekly", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _kpi(data, key):
    """从 /report 的 data 里取一条核心 KPI。"""
    return next(item for item in data["coreKpis"] if item["key"] == key)


def test_periods_lists_available_and_flags_short_history(client):
    body = client.get(f"{PREFIX}/periods", params={"period": "weekly"}).json()
    data = body["data"]

    assert data["periodType"] == "weekly"
    assert data["latest"] == "2026-46"
    # 下载从 W43 起、广告从 W44 起、在线只有 W45/W46
    assert data["availablePeriods"] == ["2026-43", "2026-44", "2026-45", "2026-46"]
    latest = data["periods"][-1]
    assert latest["start"] == "2026-11-09" and latest["end"] == "2026-11-15"
    assert data["insufficientHistory"] == {"download": False, "ad": False, "online": True}


def test_report_core_kpis_have_delta_and_trend(client):
    data = _report(client)["data"]
    payload = data
    meta = payload["meta"]

    assert meta["periodKey"] == "2026-46"
    assert meta["prevPeriodKey"] == "2026-45"
    assert meta["isLatest"] is True
    assert [kpi["key"] for kpi in payload["coreKpis"]] == [
        "online_game_cnt",
        "online_peak_players",
        "download_total",
        "download_pc",
        "download_app",
        "ad_total",
    ]

    downloads = _kpi(payload, "download_total")
    assert downloads["value"] == 10475431
    assert downloads["prevValue"] == 7459661
    assert downloads["prevPeriodKey"] == "2026-45"
    assert downloads["deltaPct"] == 40.4
    assert downloads["direction"] == "up"
    assert [point["period"] for point in downloads["trend"]] == [
        "2026-43",
        "2026-44",
        "2026-45",
        "2026-46",
    ]
    assert downloads["insufficientHistory"] is False

    # 在线只有两期：值本身可用，但必须标记历史不足
    peak = _kpi(payload, "online_peak_players")
    assert peak["value"] == 146354
    assert peak["availablePeriods"] == ["2026-45", "2026-46"]
    assert peak["insufficientHistory"] is True

    # 广告 W44 起才有：可用期数 3，趋势点不多画
    ads = _kpi(payload, "ad_total")
    assert ads["availablePeriods"] == ["2026-44", "2026-45", "2026-46"]
    assert len(ads["trend"]) == 3


def test_report_breakdown_keeps_unclassified_out_of_share(client):
    payload = _report(client)["data"]
    download = next(line for line in payload["lines"] if line["key"] == "download")
    breakdown = download["breakdown"]

    assert [item["key"] for item in breakdown["items"]] == ["0", "1", "2"]
    assert breakdown["total"] == 7024506 + 1803164 + 1647761
    assert breakdown["items"][0]["prevValue"] == 5113959
    assert sum(item["share"] for item in breakdown["items"]) == 1.0

    ad = next(line for line in payload["lines"] if line["key"] == "ad")
    # 未分类只算 is_active=1 的行（夹具里额外塞了一条 is_active=0、value=500 的残留行）
    assert ad["breakdown"]["unclassifiedTotal"] == 99
    assert ad["breakdown"]["unclassified"][0]["value"] == 99
    assert ad["breakdown"]["unclassified"][0]["games"] == 14
    # 未分类不计入三类占比分母
    assert ad["breakdown"]["total"] == 5402 + 468


def test_report_players_and_quality_flags(client):
    payload = _report(client)["data"]
    players = {item["key"]: item for item in payload["players"]}

    torchlight = players["torchlight"]
    assert torchlight["download"] == 47319
    assert torchlight["downloadPrev"] == 1224138
    assert len(torchlight["downloadTrend"]) == 3

    flag_items = {flag["item"] for flag in payload["flags"]}
    assert "torchlight_download_jump" in flag_items          # W44 → W45 14 倍
    # 在线款数不再跨表比：概览 671（单小时最大）vs 周期表 728（整周去重）定义不同，
    # 比出来的 flag 每周都是噪音，已删除
    assert "online_game_cnt_mismatch" not in flag_items

    assert "ad_unclassified_dist" in flag_items
    assert "retired_columns" in flag_items
    # 高危项排在最前
    assert payload["flags"][0]["level"] == "high"

    # 换一期看：概览与在线周期表的口径差异也要被抓出来
    older = _report(client, key="2026-45")["data"]
    older_flags = {flag["item"] for flag in older["flags"]}
    assert "online_peak_players_mismatch" in older_flags


def test_report_does_not_turn_missing_value_into_zero(client):
    payload = _report(client, key="2026-45")["data"]
    # 测试夹具里 W45 在线分型齐全，但下载/广告都应有值；这里验证「期键解析 + 环比取最近有数据的期」
    downloads = _kpi(payload, "download_total")
    assert downloads["value"] == 7459661
    assert downloads["prevValue"] == 8962027

    # 无数据期返回 404，而不是空图表
    missing = client.get(f"{PREFIX}/report", params={"period": "weekly", "key": "2026-30"})
    assert missing.status_code == 404
    assert missing.json()["code"] == 40000

    bad = client.get(f"{PREFIX}/report", params={"period": "weekly", "key": "2026-99"})
    assert bad.status_code == 400


def test_rank_dedupes_and_limits(client):
    body = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "download",
            "period": "monthly",
            "key": "2026-11",
            "category": "top",
            "limit": 5,
        },
    ).json()
    data = body["data"]

    assert data["periodKey"] == "2026-11"
    assert data["unit"] == "次"
    assert len(data["items"]) == 5
    # 同 (rank_no, app_id) 的重跑残留行被去重，且不影响 limit
    assert data["availableRows"] == 6
    assert [item["rankNo"] for item in data["items"]] == [1, 2, 3, 4, 5]
    assert data["items"][0]["appName"] == "柚松屋"
    assert data["items"][0]["distributionLabel"] == "商业游戏"
    assert data["items"][1]["diff"] == -1381013

    filtered = client.get(
        f"{PREFIX}/rank",
        params={"line": "download", "period": "monthly", "key": "2026-11", "dist": "1"},
    ).json()["data"]
    assert {item["distributionType"] for item in filtered["items"]} == {1}

    unknown = client.get(f"{PREFIX}/rank", params={"line": "nope"})
    assert unknown.status_code == 400


def test_report_labels_and_kpi_notes(client):
    """分发类型文案与全站统一，广告口径在 KPI 上带 note，在线差异已定口径。"""
    payload = _report(client)["data"]
    assert payload["meta"]["distLabels"] == {
        "0": "商业游戏",
        "1": "独立小游戏",
        "2": "Maker 小游戏",
    }

    ads = _kpi(payload, "ad_total")
    assert ads["label"] == "广告曝光"
    assert "位次" in ads["note"]  # 采集位次，不是用户级曝光量
    # 没有口径需要解释的指标不带 note
    assert "note" not in _kpi(payload, "download_total")

    # 款数不再报跨表不一致（并发 vs 去重是两个指标，没有可比的共同定义）
    assert not [flag for flag in payload["flags"] if flag["item"] == "online_game_cnt_mismatch"]


def test_online_metric_decisions(client):
    """在线口径三条：去重款数为主、并发款数为副且无环比、W45 半周不带环比。"""
    payload = _report(client)["data"]
    online = next(line for line in payload["lines"] if line["key"] == "online")
    kpis = {kpi["key"]: kpi for kpi in online["kpis"]}

    dedupe = kpis["online_game_cnt"]
    assert dedupe["label"] == "在线游戏数（去重）"
    assert dedupe["value"] == 860
    # 上期 W45 只有 11-07~11-08 两天，860 vs 581 的 +48.0% 是假的 → 环比整体置 null
    assert dedupe["prevValue"] is None
    assert dedupe["deltaPct"] is None and dedupe["deltaAbs"] is None
    assert dedupe["deltaComparable"] is False
    assert dedupe["deltaBlocked"] == "prev_coverage_partial"

    # 在线峰值同一条线同样不可比
    assert kpis["online_peak_players"]["deltaPct"] is None
    assert kpis["online_peak_players"]["deltaComparable"] is False

    # 副指标：单小时同时在线款数最高，没有环比，也不进 6 个主 KPI
    concurrent = kpis["concurrent_game_cnt"]
    assert concurrent["value"] == 671
    # 不能叫「峰值小时」：671 出现的那一小时与在线峰值（人数最多）不是同一小时
    assert concurrent["label"] == "单小时同时在线款数最高"
    assert "峰值小时" not in concurrent["label"]
    assert "不一定同一小时" in concurrent["note"]
    # 取值小时来自概览表 online_game_cnt_at，**不能**用 peak_hour（11-13 22）
    assert concurrent["concurrent_game_cnt_at"] == "2026-11-15 22"
    assert "2026-11-15 22" in concurrent["note"]
    assert online["peakHour"]["crawledAt"] == "2026-11-13 22"
    assert concurrent["concurrent_game_cnt_at"] != online["peakHour"]["crawledAt"]
    assert concurrent["deltaPct"] is None and concurrent["deltaComparable"] is False
    assert concurrent["deltaBlocked"] == "secondary_metric"
    assert "concurrent_game_cnt" not in {kpi["key"] for kpi in payload["coreKpis"]}

    # 分型各自峰值不可加（146,929 ≠ 146,354），同小时快照可加
    peak_hour = online["peakHour"]
    assert peak_hour["crawledAt"] == "2026-11-13 22"
    assert peak_hour["gameCnt"] == 653
    assert peak_hour["peakOnlinePlayers"] == 146354
    assert [item["distributionType"] for item in peak_hour["breakdown"]] == ["0", "1", "2"]
    assert sum(item["gameCnt"] for item in peak_hour["breakdown"]) == 653
    assert sum(item["peakOnlinePlayers"] for item in peak_hour["breakdown"]) == 146354
    assert peak_hour["sumsMatch"] is True

    flag_items = {flag["item"] for flag in payload["flags"]}
    assert "online_partial_coverage" in flag_items
    # 覆盖用实测采样点算：W45 只有 8/84 个采样点（2 小时/次），不是写死的起始日
    # 覆盖拆成三个信号：窗口内完整性（唯一告警项）、源起点（info）、新鲜度（meta.updatedAt）
    coverage = next(f for f in payload["flags"] if f["item"] == "online_partial_coverage")
    assert coverage["reason"] == "collection_gap"
    # W46 的响应里这条讲的是上期 W45：scope=previous、level=info（只解释没有环比的原因）
    assert coverage["scope"] == "previous"
    assert coverage["relatedPeriod"] == "2026-45"
    assert coverage["level"] == "info"
    assert "上期" in coverage["label"]   # label 必须指向它讲的那一期
    # W45 采集窗口只有 11-08 一天 = 12 点，实采 8 → 8/12 = 67%，窗口内确有缺失
    assert "8/12" in coverage["detail"]
    assert "8/84" in coverage["detail"]          # 期口径同时给出，供参考

    # W45 自身也是半周：现场置 null，而不是拿更早的期硬凑一个环比
    older = _report(client, key="2026-45")["data"]
    older_online = next(line for line in older["lines"] if line["key"] == "online")
    older_kpis = {kpi["key"]: kpi for kpi in older_online["kpis"]}
    # 源起点单独成 info flag（本期期初 < 在线数据起点）
    source = next(f for f in older["flags"] if f["item"] == "online_source_start")
    assert source["level"] == "info"
    assert source["scope"] == "current" and source["relatedPeriod"] == "2026-45"
    # W45 自己那期：窗口内只有 8/12，KPI 是拿 1 天样本当整周算的 → 必须告警（medium）
    own = next(f for f in older["flags"] if f["item"] == "online_partial_coverage")
    assert own["level"] == "medium" and own["scope"] == "current"
    assert own["relatedPeriod"] == "2026-45"
    assert "本期" in own["label"]        # W45 讲的是自己，不能写成「上期」
    assert "2026-11-08" in source["detail"]
    assert older_online["coverage"]["sourceStartsLate"] is True
    assert older_kpis["online_game_cnt"]["value"] == 581
    assert older_kpis["online_game_cnt"]["deltaBlocked"] == "coverage_partial"
    assert older_kpis["online_game_cnt"]["deltaPct"] is None


def test_active_filter_applies_only_where_column_exists(client):
    """is_active 列存在才加过滤，且 NULL 视为有效（只排显式置 0 的残骸）。"""
    from app.routers import periodic

    assert periodic._active_filter(periodic.AD_TABLE) == " AND COALESCE(is_active, 1) = 1"
    # 下载/在线周期表在夹具里没有 is_active 列 → 不得凭空加条件（线上加列后会自动生效）
    assert periodic._active_filter(periodic.DOWNLOAD_TABLE) == ""


def test_peak_hour_reads_dist_peak_and_breakdown_marks_summable(client):
    """peakHour 只读 dist_peak（W45 也有值），分型拆解标明可否相加。"""
    payload = _report(client)["data"]
    online = next(line for line in payload["lines"] if line["key"] == "online")

    peak = online["peakHour"]
    assert peak["crawledAt"] == "2026-11-13 22"
    assert peak["sumsMatch"] is True
    # 有 ALL 行时以它为分母：该小时并发 653 款 ≠ 整周去重 860 款，两个数都要在
    assert peak["gameCnt"] == 653
    assert online["kpis"][2]["value"] == 860
    assert peak["sumGameCnt"] == 653

    # 在线分型是「各自峰值」，结构上不可加：gapPct 不是数据质量问题
    assert online["breakdown"]["consistency"]["summable"] is False
    assert "peakHour" in online["breakdown"]["consistency"]["note"]

    download = next(line for line in payload["lines"] if line["key"] == "download")
    assert download["breakdown"]["consistency"]["summable"] is True
    assert download["breakdown"]["consistency"]["gapPct"] == 0

    # W45 没有 ALL 行：走「三项求和 + all 行峰值校验」的兼容分支，仍然有值
    older = _report(client, key="2026-45")["data"]
    older_peak = next(
        line for line in older["lines"] if line["key"] == "online"
    )["peakHour"]
    assert older_peak["crawledAt"] == "2026-11-08 20"
    assert older_peak["sumPeakOnlinePlayers"] == 125697
    assert older_peak["sumsMatch"] is True
    # W45 没有 ALL 行、也没有 placeholder_player_ratio 列值（模拟老库）：
    # 人数口径回退到推导 —— 183+50+60 = 293 条兜底 × 100 / 125,697 = 0.2331
    assert older_peak["placeholderPlayerRatio"] == pytest.approx(0.2331, abs=5e-4)


def test_concurrent_kpi_explains_overview_lag():
    """概览周表周一才出上一周：当期副指标缺值要说原因，而不是空着让人猜。"""
    from app.routers.periodic import _build_concurrent_kpi

    kpi = _build_concurrent_kpi({}, "2026-47", ["2026-46", "2026-47"], 12)
    assert kpi["key"] == "concurrent_game_cnt"
    assert kpi["value"] is None and kpi["hasValue"] is False
    assert kpi["deltaComparable"] is False
    assert "周一" in kpi["unavailableReason"]

    # 有值时不带 unavailableReason
    with_value = _build_concurrent_kpi(
        {"2026-46": {"online_game_cnt": 671}}, "2026-46", ["2026-46"], 12
    )
    assert with_value["value"] == 671
    assert "unavailableReason" not in with_value
    # 概览表还没有 argmax 列/值时：不写时间，也**不得**回退到 peak_hour 编一个
    assert with_value["concurrent_game_cnt_at"] is None
    assert "取值于" not in with_value["note"]

    with_at = _build_concurrent_kpi(
        {"2026-46": {"online_game_cnt": 671, "online_game_cnt_at": "2026-11-15 22"}},
        "2026-46",
        ["2026-46"],
        12,
    )
    assert with_at["concurrent_game_cnt_at"] == "2026-11-15 22"
    assert "取值于 2026-11-15 22" in with_at["note"]


def test_placeholder_ratio_exposed_for_online_breakdown(client):
    """采集兜底值占比原样透出（dist 行与 dist_peak 行都有），其他线不带这个字段。"""
    payload = _report(client)["data"]
    online = next(line for line in payload["lines"] if line["key"] == "online")

    # 整期（dist 行）：W46 真库条数口径 0.5943 / 人数口径 0.74
    dist1 = next(item for item in online["breakdown"]["items"] if item["key"] == "1")
    assert dist1["placeholderRatio"] == pytest.approx(0.5943)
    assert dist1["placeholderPlayerRatio"] == pytest.approx(0.74)
    # 整期口径的整数兜底人数（DWS 对 dist 行也给值）
    assert dist1["placeholderPlayers"] == 411100
    assert online["breakdown"]["items"][0]["placeholderRatio"] == pytest.approx(0.3404)

    peak = online["peakHour"]
    assert peak["placeholderRatio"] == pytest.approx(0.5329)
    maker = next(item for item in peak["breakdown"] if item["distributionType"] == "2")
    assert maker["placeholderRatio"] == pytest.approx(0.7143)

    # 人数口径直接读 DWS 的 placeholder_player_ratio 列（不是峰值小时那套推导）

    # 行数口径 ≠ 人数口径：峰值小时 dist_1 条数 78.72%，人数 86.79%
    # 人数口径优先读列；W46 夹具这两列都来自真库，所以断言列值
    peak_dist1 = next(
        item for item in peak["breakdown"] if item["distributionType"] == "1"
    )
    assert peak_dist1["placeholderPlayerRatio"] == pytest.approx(0.8679, abs=1e-4)
    # dist_0：人数口径 14.87%（行数口径 42.95%）
    assert peak["breakdown"][0]["placeholderPlayerRatio"] == pytest.approx(
        0.1487, abs=1e-3
    )
    # 整体取 ALL 行：人数口径 23.78%（行数口径 53.29%）
    assert peak["placeholderPlayerRatio"] == pytest.approx(0.2378, abs=1e-3)
    # 整数口径：兜底贡献 34,800 人，扣除后余 111,554（不用 round，不会 ±1）
    assert peak["placeholderPlayers"] == 34800
    dist1_peak = peak["breakdown"][1]
    assert dist1_peak["placeholderPlayers"] == 7400
    # 该分型余量 = 8,526 − 7,400 = 1,126（整数，不用 round）
    assert dist1_peak["peakOnlinePlayers"] - dist1_peak["placeholderPlayers"] == 1126

    download = next(line for line in payload["lines"] if line["key"] == "download")
    assert download["breakdown"]["items"][0]["placeholderRatio"] is None


def test_rank_rejects_camel_case_list_type(client):
    """listType 会被 FastAPI 静默忽略并返回混合榜，这里直接给 400。"""
    resp = client.get(
        f"{PREFIX}/rank",
        params={"line": "online", "period": "weekly", "key": "2026-46", "listType": "dist_1"},
    )
    assert resp.status_code == 400
    assert "list_type" in resp.json()["message"]


def test_rank_reads_retired_dimension_history(client):
    """旧维度（pc_played）的行是 is_active=0，但历史周只有它们，不能被 is_active 过滤掉。"""
    old = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-42",
            "category": "top",
            "list_type": "pc_played",
        },
    )
    body = old.json()
    assert old.status_code == 200
    assert body["data"]["retiredDimension"] is True
    assert [item["appName"] for item in body["data"]["items"]] == ["旧维度游戏"]

    current = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-46",
            "category": "top",
            "list_type": "dist_0",
        },
    ).json()["data"]
    assert current["retiredDimension"] is False
    assert [item["appName"] for item in current["items"]] == ["新维度游戏"]

    # 不传维度时只认有效行，不会把下线维度的残留混进来
    default = client.get(
        f"{PREFIX}/rank",
        params={"line": "online", "period": "weekly", "key": "2026-42", "category": "top"},
    ).json()
    assert default["code"] == 0
    assert default["data"]["items"] == []


def test_rank_exposes_real_hours_and_sample_flag(client):
    """real_hours 透出；全员有效小时 <= 2 时 lowSample=true（前端别再自己猜数值分布）。"""
    normal = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-46",
            "category": "top",
            "list_type": "dist_0",
        },
    ).json()["data"]
    assert normal["items"][0]["realHours"] == 20
    assert normal["sample"] == {
        "minRealHours": 20,
        "maxRealHours": 20,
        "medianRealHours": 20,
        "lowSample": False,
    }

    low = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-46",
            "category": "loss",
            "list_type": "dist_1",
        },
    ).json()["data"]
    assert [item["realHours"] for item in low["items"]] == [2, 1]
    assert low["sample"] == {
        "minRealHours": 1,
        "maxRealHours": 2,
        "medianRealHours": 1.5,
        "lowSample": True,
    }

    # 中位数正好 8 但 max 20：两条阈值都不触发 → 正常榜（严格小于，边界不误伤）
    edge = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-46",
            "category": "gain",
            "list_type": "dist_0",
        },
    ).json()["data"]
    assert edge["sample"]["medianRealHours"] == 8
    assert edge["sample"]["lowSample"] is False

    # W45 dist_0 的真实形状：median 8 / max 8 —— 覆盖太薄但单条数据不薄，
    # 由 online_partial_coverage 说话，lowSample 不该重复判
    thin = client.get(
        f"{PREFIX}/rank",
        params={
            "line": "online",
            "period": "weekly",
            "key": "2026-46",
            "category": "loss",
            "list_type": "dist_2",
        },
    ).json()["data"]
    assert thin["sample"]["medianRealHours"] == 8
    assert thin["sample"]["maxRealHours"] == 8
    assert thin["sample"]["lowSample"] is False

    # 下载/广告榜没有这一列：字段为 null，sample 为 null
    download = client.get(
        f"{PREFIX}/rank",
        params={"line": "download", "period": "monthly", "category": "top"},
    ).json()["data"]
    assert download["items"][0]["realHours"] is None
    assert download["sample"] is None


def test_expected_samples_uses_elapsed_days_for_running_month(client):
    """月报分母用「已过天数」：整月 30 天会把还没到的日子算成覆盖缺失。"""
    from datetime import date

    from app.routers.periodic import SAMPLES_PER_DAY, _expected_samples, _today

    # 已结束的周：整周 7 天 × 12
    assert _expected_samples("weekly", "2026-45") == 84
    assert _expected_samples("weekly", "2026-46") == 84

    # 进行中的月：按已过天数，最多整月天数
    elapsed = min(date(2026, 11, 30), _today())
    if elapsed >= date(2026, 11, 1):
        days = (elapsed - date(2026, 11, 1)).days + 1
        expected = _expected_samples("monthly", "2026-11")
        assert expected == days * SAMPLES_PER_DAY
        assert expected <= 30 * SAMPLES_PER_DAY


def test_coverage_info_three_signals(client):
    """覆盖三信号：窗口内完整性（告警）/ 源起点（info）/ 期口径（参考）。"""
    from datetime import date

    from app.routers.periodic import _coverage_info, _today

    today = _today()
    if today == date(2026, 11, 16):
        monthly = _coverage_info("monthly", "2026-11", 92, "2026-11-08")
        # 窗口 = 11-08 ~ 昨天(11-15) 共 8 天 = 96；期口径 = 已过 16 天 = 192
        assert monthly["windowStart"] == "2026-11-08"
        assert monthly["windowEnd"] == "2026-11-15"
        assert monthly["expectedSamples"] == 96
        assert monthly["periodExpectedSamples"] == 192
        assert round(monthly["ratio"], 3) == 0.958
        assert monthly["gap"] is False            # 窗口内没缺采 → 不告警
        assert monthly["windowEndFinal"] is False  # 进行中：窗口末端还没定稿
        assert monthly["sourceStartsLate"] is True   # 但期初无源，环比仍不可比
        assert monthly["partial"] is True

        # W45：窗口 11-08 一天 = 12 点，实采 8 → 8/12，窗口内确有洞
        weekly = _coverage_info("weekly", "2026-45", 8, "2026-11-08")
        assert weekly["windowStart"] == "2026-11-08"
        assert weekly["expectedSamples"] == 12
        assert round(weekly["ratio"], 3) == 0.667
        assert weekly["gap"] is True

    # W46：满覆盖且期初就在源起点之后
    full = _coverage_info("weekly", "2026-46", 84, "2026-11-08")
    assert full["expectedSamples"] == 84
    assert full["windowEndFinal"] is True  # 已结束的期，窗口末端是定稿值
    assert full["ratio"] == 1.0
    assert full["gap"] is False and full["sourceStartsLate"] is False
    assert full["partial"] is False

    # sample_hours 缺失（老库）时不编造比例
    unknown = _coverage_info("weekly", "2026-46", None, "2026-11-08")
    assert unknown["ratio"] is None and unknown["gap"] is False


def test_coverage_level_by_impact_not_fixability(client, monkeypatch):
    """告警级别按「本期数字准不准」定：窗口内缺采就 medium，与巡检 FAIL 一致。

    把「今天」钉在 2026-11-16：期望采样点 = 已过天数 × 12，跟着真实日期漂，
    过一天这个用例就会自己变红（曾经在 11-17 凌晨失效过）。
    """
    from datetime import date

    from app.routers import periodic
    from app.routers.periodic import _coverage_info, _coverage_level

    monkeypatch.setattr(periodic, "_today", lambda: date(2026, 11, 16))

    # 假设 W46 只采到 60/84：真故障
    broken = _coverage_info("weekly", "2026-46", 60, "2026-11-08")
    assert broken["gap"] is True and broken["sourceStartsLate"] is False
    assert _coverage_level(broken, is_current=True) == "medium"

    # W45 8/12：同样是「KPI 建立在残缺样本上」，虽然不可修也必须告警
    startup = _coverage_info("weekly", "2026-45", 8, "2026-11-08")
    assert startup["gap"] is True and startup["sourceStartsLate"] is True
    assert _coverage_level(startup, is_current=True) == "medium"

    # 2026-11 假设：窗口内够、只是期初无源 → info
    monthly = _coverage_info("monthly", "2026-11", 92, "2026-11-08")
    assert monthly["gap"] is False and monthly["sourceStartsLate"] is True
    assert _coverage_level(monthly, is_current=True) == "info"

    # 上期的问题永不占告警位
    assert _coverage_level(broken, is_current=False) == "info"


def test_roster_kpi_is_a_separate_metric(client):
    """覆盖款数（game_cnt_roster）是**另一格**，不能顶掉有下载 / 有在线的款数。"""
    payload = _report(client)["data"]
    lines = {line["key"]: line for line in payload["lines"]}
    download = {kpi["key"]: kpi for kpi in lines["download"]["kpis"]}
    online = {kpi["key"]: kpi for kpi in lines["online"]["kpis"]}

    assert download["game_cnt_roster"]["value"] == 860
    assert download["game_cnt_roster"]["label"] == "覆盖游戏款数"
    assert download["game_cnt_roster"]["unit"] == "款"
    assert "名单" in download["game_cnt_roster"]["note"]
    assert "game_cnt" in download  # 有下载款数照旧

    assert online["online_game_cnt_roster"]["value"] == 860
    assert online["online_game_cnt_roster"]["label"] == "覆盖游戏款数"
    # 主口径没被换掉：有在线的款数仍然是 online_game_cnt 那一格
    assert online["online_game_cnt"]["label"] == "在线游戏数（去重）"
    assert {kpi["key"] for kpi in payload["coreKpis"]} >= {"online_game_cnt"}

    # 覆盖款数不进 6 个主 KPI（它解释的是「多少款为 0」，不是每期都看的数）
    core_keys = {kpi["key"] for kpi in payload["coreKpis"]}
    assert "game_cnt_roster" not in core_keys
    assert "online_game_cnt_roster" not in core_keys

    # 列在、但这一期还没重算出值（W45 为 NULL）：宁可不吐，也别让前端去拼「覆盖 null 款」
    older = _report(client, key="2026-45")["data"]
    older_lines = {line["key"]: line for line in older["lines"]}
    assert "game_cnt_roster" not in {kpi["key"] for kpi in older_lines["download"]["kpis"]}
    assert "game_cnt_roster" not in {kpi["key"] for kpi in older_lines["online"]["kpis"]}


def test_roster_spec_requires_column(monkeypatch):
    """老库没有 game_cnt_roster 列时，这条指标不存在——不猜、不拿 game_cnt 顶。"""
    from app.routers import periodic

    download = periodic.LINE_BY_KEY["download"]
    monkeypatch.setitem(periodic._COLUMNS, download.table, {"game_cnt", "download_total"})
    assert periodic._roster_spec(download) is None

    monkeypatch.setitem(
        periodic._COLUMNS, download.table, {"game_cnt", "game_cnt_roster"}
    )
    spec = periodic._roster_spec(download)
    assert spec is not None
    assert (spec.key, spec.column) == ("game_cnt_roster", "game_cnt_roster")

    online = periodic.LINE_BY_KEY["online"]
    monkeypatch.setitem(periodic._COLUMNS, online.table, {"game_cnt", "game_cnt_roster"})
    online_spec = periodic._roster_spec(online)
    assert (online_spec.key, online_spec.column) == (
        "online_game_cnt_roster",
        "game_cnt_roster",
    )
    # 广告线没有名单覆盖的概念
    monkeypatch.setitem(periodic._COLUMNS, periodic.LINE_BY_KEY["ad"].table, {"app_cnt"})
    assert periodic._roster_spec(periodic.LINE_BY_KEY["ad"]) is None


def test_overview_argmax_column_accepts_both_names():
    """「MAX(game_cnt) 取值小时」线上叫 online_game_cnt_hour，DWS 口述叫 _at：两个都认。"""
    from app.routers.periodic import _overview_argmax_select

    assert _overview_argmax_select({"week_key", "online_game_cnt_hour"}) == (
        ", online_game_cnt_hour AS online_game_cnt_at"
    )
    assert _overview_argmax_select({"week_key", "online_game_cnt_at"}) == (
        ", online_game_cnt_at"
    )
    # 老概览表两列都没有：不拼、不报错，at 字段为 null
    assert _overview_argmax_select({"week_key", "online_game_cnt"}) == ""


def test_missing_column_logs_one_warning(caplog):
    """列名差一个词 = 静默少一句文案，数字巡检抓不到：缺列时必须留 WARNING 且只留一条。"""
    import logging

    from app.routers import periodic

    periodic._WARNED_MISSING.clear()
    with caplog.at_level(logging.WARNING, logger=periodic.logger.name):
        assert periodic._overview_argmax_select({"week_key", "online_game_cnt"}) == ""
        assert periodic._overview_argmax_select({"week_key", "online_game_cnt"}) == ""
    warned = [r for r in caplog.records if "online_game_cnt_hour" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING


def test_rank_icons_come_from_tables(client):
    """三张榜都从自己的表取 icon；在线榜不再用 NULL 占位（DWS 2026-11-16 已回填）。"""
    body = client.get(f"{PREFIX}/rank", params={"line": "online", "period": "weekly",
                                               "key": "2026-46", "rank_category": "top"}).json()
    rows = body["data"]["items"]
    top = next(row for row in rows if row["rankNo"] == 1)
    assert top["icon"] == "https://img-tc.tapimg.com/online-222.jpg"

    # 榜单里的空 icon 归一成 None（不是字符串 "NULL"），前端才好判空走首字兜底
    from app.routers.periodic import _as_text

    assert _as_text("NULL") is None
    assert _as_text("  ") is None
    assert _as_text(None) is None


def test_players_expose_icon_and_app_id(client):
    payload = _report(client)["data"]
    players = {item["key"]: item for item in payload["players"]}

    torchlight = players["torchlight"]
    assert torchlight["icon"] == "https://img-tc.tapimg.com/torchlight.jpg"
    assert torchlight["appId"] == 172664
    assert players["heartopia"]["icon"] == "https://img-tc.tapimg.com/heartopia.jpg"
    assert players["heartopia"]["appId"] == 45213

    # 更早的期这两列是 NULL：字段在但值为 null，前端走首字兜底
    older = {item["key"]: item for item in _report(client, key="2026-45")["data"]["players"]}
    assert older["torchlight"]["icon"] is None
    assert older["torchlight"]["appId"] is None


def test_breakdown_items_carry_trend(client):
    """分型逐期值：前端画「小游戏下载量趋势」不用再逐期打 /report。"""
    payload = _report(client)["data"]
    download = next(line for line in payload["lines"] if line["key"] == "download")
    items = {item["key"]: item for item in download["breakdown"]["items"]}

    commercial = items["0"]
    # 夹具里下载分型行只有 W45/W46（W43/W44 只有 all 行）：缺行的期不出现，不补 0
    assert [point["period"] for point in commercial["trend"]] == ["2026-45", "2026-46"]
    assert commercial["trend"][-1]["value"] == 7024506      # 与当期 value 一致
    assert commercial["trend"][-1]["label"] == "W46"
    assert commercial["trend"][-1]["isPartial"] is False

    # 小游戏 = 独立 + Maker 逐期相加（前端就是拿这两条曲线加起来的）
    mini = [items["1"]["trend"][-1]["value"], items["2"]["trend"][-1]["value"]]
    assert mini == [1803164, 1647761]

    # KPI 的 trend 点也带 isPartial：进行中的期（W47）画图时要能自己跳过
    kpi = next(k for k in download["kpis"] if k["key"] == "download_total")
    assert all("isPartial" in point for point in kpi["trend"])


def test_breakdown_trend_flags_partial_period():
    """进行中的期在 trend 里被标 isPartial，缺行的期不补 0。"""
    from app.routers.periodic import LINE_BY_KEY, _build_breakdown

    grouped = {
        "2026-45": {"0": {"value": 100}, "1": {"value": 10}, "2": {"value": 5}},
        "2026-46": {"0": {"value": 200}, "1": {"value": 20}},   # 46 缺 "2" 这一行
    }
    built = _build_breakdown(
        LINE_BY_KEY["download"],
        "2026-46",
        "2026-45",
        grouped,
        all_value=220,
        trend_periods=("2026-45", "2026-46"),
        partial_periods=frozenset({"2026-46"}),
    )
    items = {item["key"]: item for item in built["items"]}

    assert items["0"]["trend"] == [
        {"period": "2026-45", "label": "W45", "value": 100, "isPartial": False},
        {"period": "2026-46", "label": "W46", "value": 200, "isPartial": True},
    ]
    # 46 期没有 "2" 的分型行：该点在 trend 里直接不出现，不补一个 0 造出断崖
    assert [point["period"] for point in items["2"]["trend"]] == ["2026-45"]


def test_reserve_block_headline_and_dist(client):
    """07 预约：新增 / 日均 / 分型 / 双榜，结构与前端契约一致。"""
    payload = _report(client)["data"]
    reserve = payload["reserve"]

    assert reserve["available"] is True
    assert reserve["coverage"] == {
        "dayCnt": 7,
        "expectedDayCnt": 7,
        "partial": False,
        "firstDay": "2026-11-09",
        "lastDay": "2026-11-15",
    }
    head = reserve["headline"]
    assert head["reserveAdd"] == 1131061
    assert head["gameCnt"] == 162 and head["gameCntRoster"] == 163
    assert head["reserveTotalEnd"] == 69613606
    assert head["dailyAvg"] == round(1131061 / 7)
    assert head["prevPeriodKey"] == "2026-45"
    assert head["prevDayCnt"] == 1
    # 只比日均：163,381/1 → 161,580/日，-1.1%
    assert head["dailyAvgPct"] == pytest.approx(-1.1, abs=0.05)

    # 分型三项固定输出，新增量相加恒等于合计（款数不可加，另有 distNote 说明）
    assert [item["type"] for item in reserve["dist"]] == [0, 1, 2]
    assert sum(item["reserveAdd"] for item in reserve["dist"]) == head["reserveAdd"]
    assert reserve["dist"][0]["share"] == pytest.approx(84.87, abs=0.01)
    assert reserve["dist"][2]["gameCnt"] == 1
    assert "款数相加大于合计" in reserve["distNote"]

    ranks = reserve["ranks"]
    assert ranks["gain"][0] == {
        "rankNo": 1,
        "appId": 43639,
        "appName": "白银之城",
        "icon": "https://img-tc.tapimg.com/gain-43639.jpg",
        "distributionType": 0,
        "reserveAdd": 114809,
        "totalReserveCount": 2452558,
    }
    assert ranks["total"][0]["appId"] == 777320


def test_reserve_missing_coverage_is_not_zero(client, monkeypatch):
    """11-08 之前没有预约采集：回 available=false + 原因，不拿 0 充数。"""
    payload = _report(client, key="2026-44")["data"]
    reserve = payload["reserve"]
    assert reserve["available"] is False
    assert "2026-11-08" in reserve["reason"]
    assert "headline" not in reserve and "dist" not in reserve

    # 半覆盖的期：W45 只 1/7 天，日均环比仍给（两期都有采集），但 partial 要标出来
    w45 = _report(client, key="2026-45")["data"]["reserve"]
    assert w45["coverage"]["dayCnt"] == 1
    assert w45["coverage"]["expectedDayCnt"] == 7
    assert w45["coverage"]["partial"] is True

    # 月报 9/17 天：分母是「月内已过天数」，不是期长 30（夹具里没有月报 all 行，故直接调构建函数）
    from datetime import date

    from app.routers import periodic

    monkeypatch.setattr(periodic, "_today", lambda: date(2026, 11, 17))
    monthly = periodic._build_reserve("monthly", "2026-11", None)
    assert monthly["coverage"]["dayCnt"] == 9
    assert monthly["coverage"]["expectedDayCnt"] == 17
    assert monthly["coverage"]["partial"] is True
    assert monthly["headline"]["dailyAvg"] == round(1422592 / 9)
    assert monthly["headline"]["dailyAvgPct"] is None       # 月报上期没有预约数据


def test_reserve_not_in_periods_lines(client):
    """预约不进 /periods 的 lines：否则选择器会给 W40~W44 标「1 条线缺数据」。"""
    body = client.get(f"{PREFIX}/periods", params={"period": "weekly"}).json()
    data = body["data"]
    assert set(data["insufficientHistory"]) == {"download", "ad", "online"}
    assert all("reserve" not in period["lines"] for period in data["periods"])


def test_reserve_dist_carries_own_coverage(client):
    """分型各自的覆盖天数（Maker 这类新品类会出现 4/7 天），款数不能相加。"""
    reserve = _report(client)["data"]["reserve"]
    dist = {item["type"]: item for item in reserve["dist"]}

    assert dist[0]["dayCnt"] == 7 and dist[0]["partial"] is False
    assert dist[0]["firstDay"] == "2026-11-09" and dist[0]["lastDay"] == "2026-11-15"
    # Maker：11-12~11-15 才 4 天 → 前端显示「4/7 天」
    assert dist[2]["dayCnt"] == 4
    assert dist[2]["firstDay"] == "2026-11-12"
    assert dist[2]["partial"] is True

    # 款数相加大于合计（110+52+1=163 vs 162），只有新增量恒等
    assert sum(item["gameCnt"] for item in reserve["dist"]) == 163
    assert reserve["headline"]["gameCnt"] == 162
    assert sum(item["reserveAdd"] for item in reserve["dist"]) == reserve["headline"]["reserveAdd"]


def test_reserve_daily_avg_pct_blocked_when_current_partial(client, monkeypatch):
    """本期没采满时不给日均环比：W47 只 1 天，拿它比 W46 的 7 天日均会「腰斩」。"""
    from datetime import date

    from app.routers import periodic

    monkeypatch.setattr(periodic, "_today", lambda: date(2026, 11, 17))
    w47 = periodic._build_reserve("weekly", "2026-47", "2026-46")

    assert w47["coverage"]["dayCnt"] == 1
    assert w47["coverage"]["expectedDayCnt"] == 2
    assert w47["coverage"]["partial"] is True
    assert w47["headline"]["dailyAvg"] == 128150          # 日均本身照给
    assert w47["headline"]["dailyAvgPct"] is None          # 但不给环比
    assert w47["headline"]["dailyAvgBlocked"] == "current_partial"
    assert w47["headline"]["dailyAvgComparable"] is False

    # W46 本期满 7 天：环比给数，但上期 W45 只 1/7 天 → 可比性为 false（数仅供参考）
    w46 = periodic._build_reserve("weekly", "2026-46", "2026-45")
    assert w46["headline"]["dailyAvgPct"] == pytest.approx(-1.1, abs=0.05)
    assert w46["headline"]["prevExpectedDayCnt"] == 7
    assert w46["headline"]["dailyAvgComparable"] is False
