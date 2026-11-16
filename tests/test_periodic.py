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
    assert "online_game_cnt_mismatch" in flag_items          # 概览 671 vs 周期表 860

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

    # 款数差异已核实为两个不同指标（并发 vs 去重），只做 info 说明，不再报成数据错误
    mismatch = next(
        flag for flag in payload["flags"] if flag["item"] == "online_game_cnt_mismatch"
    )
    assert mismatch["level"] == "info"
    assert "并发" in mismatch["detail"] and "去重" in mismatch["detail"]
    assert "concurrent_game_cnt" in mismatch["action"]


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

    # 副指标：峰值小时并发款数，没有环比，也不进 6 个主 KPI
    concurrent = kpis["concurrent_game_cnt"]
    assert concurrent["value"] == 671
    assert concurrent["label"] == "峰值小时同时在线款数"
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

    # W45 自身也是半周：现场置 null，而不是拿更早的期硬凑一个环比
    older = _report(client, key="2026-45")["data"]
    older_online = next(line for line in older["lines"] if line["key"] == "online")
    older_kpis = {kpi["key"]: kpi for kpi in older_online["kpis"]}
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


def test_placeholder_ratio_exposed_for_online_breakdown(client):
    """采集兜底值占比原样透出（dist 行与 dist_peak 行都有），其他线不带这个字段。"""
    payload = _report(client)["data"]
    online = next(line for line in payload["lines"] if line["key"] == "online")

    # 整期（dist 行）：W46 真库条数口径 0.5943 / 人数口径 0.74
    dist1 = next(item for item in online["breakdown"]["items"] if item["key"] == "1")
    assert dist1["placeholderRatio"] == pytest.approx(0.5943)
    assert dist1["placeholderPlayerRatio"] == pytest.approx(0.74)
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
