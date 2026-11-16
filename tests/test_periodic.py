"""定期报告聚合接口用例（sqlite 夹具，验证契约而不是数值本身）。"""

from __future__ import annotations

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
    assert ad["breakdown"]["unclassifiedTotal"] == 99
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

    # 换一期看：概览的在线峰值是旧快照，也要被抓出来
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
