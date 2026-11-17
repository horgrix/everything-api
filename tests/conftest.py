"""测试用固定装置：用临时 SQLite 库替代 MySQL，验证接口契约。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.mkdtemp(prefix="everything-api-test-"))
_DB_PATH = _TMP_DIR / "test.db"


def _rank(category, rank_no, app_id, app_name, dist, tag1, tag2,
          total, prev, diff, month="2026-11"):
    """造一条下载榜行（monthly）。"""
    return (
        "monthly", month, category, rank_no, app_id, app_name, f"icon-{app_id}.png",
        dist, tag1, tag2, total, prev, diff,
    )


def _ad_rank(category, rank_no, app_id, app_name, dist, tag1, total, prev, diff,
             month="2026-11"):
    """造一条广告曝光榜行（monthly）。"""
    return (
        "monthly", month, category, rank_no, app_id, app_name, f"icon-{app_id}.png",
        dist, tag1, total, prev, diff,
    )


def _seed() -> None:
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE dws_taptap_download_daily (
                id INTEGER PRIMARY KEY,
                game_name TEXT NOT NULL,
                download_count INTEGER NOT NULL,
                dt TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_download_daily "
            "(game_name, download_count, dt) VALUES (?, ?, ?)",
            [
                ("火炬之光", 1200, "2026-10-01"),
                ("心动小镇", 980, "2026-10-01"),
                ("火炬之光", 1500, "2026-10-02"),
            ],
        )
        # 首页概览：活跃 APP 计数表（monthly + daily 两种 period_type）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_active_game_stat (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                ad_app_cnt INTEGER NULL,
                download_app_cnt INTEGER NULL,
                online_players_app_cnt INTEGER NULL,
                reserve_app_cnt INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_active_game_stat "
            "(period_type, crawled_at, ad_app_cnt, download_app_cnt, "
            " online_players_app_cnt, reserve_app_cnt) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("monthly", "2026-11", 113, 885, 763, 206),
                ("daily", "2026-11-01", 58, 375, None, None),
                ("daily", "2026-11-02", 59, 391, None, None),
                ("daily", "2026-11-11", 64, 638, 645, 95),
                # 上月行不应被当月窗口取到
                ("monthly", "2026-10", 151, 1169, None, None),
            ],
        )

        # 首页概览：三张 periodic_report（只造 dist 维度，够验证聚合与未分类归集）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_download_periodic_report (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                category_type TEXT NOT NULL,
                category_value TEXT NOT NULL,
                game_cnt INTEGER NULL,
                game_cnt_roster INTEGER NULL,
                download_total INTEGER NULL,
                download_app INTEGER NULL,
                download_pc INTEGER NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_download_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, download_total) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("monthly", "2026-11", "dist", "0", 443, 5366097),
                ("monthly", "2026-11", "dist", "1", 151, 1227639),
                ("monthly", "2026-11", "dist", "2", 162, 1397506),
            ],
        )
        # 定期报告：下载周报汇总行（category_type='all'）。W45 刻意留空下载量，
        # 用来验证「缺值不显示 0、环比回退到最近有数据的期」。
        conn.executemany(
            "INSERT INTO dws_taptap_download_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, "
            " download_total, download_app, download_pc, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-43", "all", "ALL", 644, 8132065, 7982020, 150045,
                 "2026-10-31 23:07:18"),
                ("weekly", "2026-44", "all", "ALL", 633, 8962027, 8832897, 129130,
                 "2026-11-02 11:35:10"),
                ("weekly", "2026-45", "all", "ALL", 977, 7459661, 7355143, 104518,
                 "2026-11-12 19:07:02"),
                ("weekly", "2026-46", "all", "ALL", 860, 10475431, 10340459, 134972,
                 "2026-11-16 00:05:10"),
            ],
        )
        # 定期报告：下载周报分型行（W45 / W46）
        conn.executemany(
            "INSERT INTO dws_taptap_download_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, "
            " download_total, download_app, download_pc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-45", "dist", "0", 730, 5113959, 5011404, 102555),
                ("weekly", "2026-45", "dist", "1", 245, 1112860, 1110897, 1963),
                ("weekly", "2026-45", "dist", "2", 163, 1232842, 1232842, 0),
                ("weekly", "2026-46", "dist", "0", 539, 7024506, 6889534, 134972),
                ("weekly", "2026-46", "dist", "1", 151, 1803164, 1803164, 0),
                ("weekly", "2026-46", "dist", "2", 175, 1647761, 1647761, 0),
            ],
        )
        # 覆盖款数（DWS 后加的列）：只有重算过的期才有值，W45 故意留 NULL，
        # 验证「列在但该期没数」时接口不吐这条 KPI
        conn.execute(
            "UPDATE dws_taptap_download_periodic_report SET game_cnt_roster = 860 "
            "WHERE period_type = 'weekly' AND crawled_at = '2026-46' AND category_type = 'all'"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_pc_online_peak_players_periodic_report (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                category_type TEXT NOT NULL,
                category_value TEXT NOT NULL,
                game_cnt INTEGER NULL,
                game_cnt_roster INTEGER NULL,
                peak_online_players INTEGER NULL,
                avg_online_players REAL NULL,
                updated_at TEXT NULL,
                sample_hours INTEGER NULL,
                peak_hour TEXT NULL,
                placeholder_ratio REAL NULL,
                placeholder_player_ratio REAL NULL,
                placeholder_players INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_pc_online_peak_players_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, "
            " peak_online_players) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("monthly", "2026-11", "dist", "0", 338, 115069),
                ("monthly", "2026-11", "dist", "1", 230, 12683),
                ("monthly", "2026-11", "dist", "2", 262, 16711),
            ],
        )
        # 定期报告：PC 在线周报（实测只有 W45 起两期，用于验证 insufficientHistory）
        conn.executemany(
            "INSERT INTO dws_taptap_pc_online_peak_players_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, "
            " peak_online_players, avg_online_players, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-45", "all", "ALL", 581, 125697, 101768.88,
                 "2026-11-12 19:08:28"),
                ("weekly", "2026-46", "all", "ALL", 860, 146354, 83223.93,
                 "2026-11-16 00:05:12"),
                ("weekly", "2026-45", "dist", "0", 444, 113272, 89907.0, None),
                ("weekly", "2026-45", "dist", "1", 68, 5973, 5465.38, None),
                ("weekly", "2026-45", "dist", "2", 75, 6894, 6396.5, None),
                ("weekly", "2026-46", "dist", "0", 539, 127110, 68139.58, None),
                ("weekly", "2026-46", "dist", "1", 151, 8633, 6613.48, None),
                ("weekly", "2026-46", "dist", "2", 177, 11186, 8470.87, None),
            ],
        )
        # 覆盖采样点：W45 只有 11-08 的 8 个点（2 小时节奏），W46 满周 84
        conn.execute(
            "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
            "SET sample_hours = 8 WHERE crawled_at = '2026-45' AND category_type = 'all'"
        )
        conn.execute(
            "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
            "SET sample_hours = 84 WHERE crawled_at = '2026-46' AND category_type = 'all'"
        )
        conn.execute(
            "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
            "SET game_cnt_roster = 860 "
            "WHERE period_type = 'weekly' AND crawled_at = '2026-46' AND category_type = 'all'"
        )
        # dist_peak：同一小时的快照，三项在线之和恒等于 all 行（DWS 每天重算）
        conn.executemany(
            "INSERT INTO dws_taptap_pc_online_peak_players_periodic_report "
            "(period_type, crawled_at, category_type, category_value, game_cnt, "
            " peak_online_players, peak_hour) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-45", "dist_peak", "0", 443, 113272, "2026-11-08 20"),
                ("weekly", "2026-45", "dist_peak", "1", 63, 5842, "2026-11-08 20"),
                ("weekly", "2026-45", "dist_peak", "2", 70, 6583, "2026-11-08 20"),
                ("weekly", "2026-46", "dist_peak", "0", 440, 127110, "2026-11-13 22"),
                ("weekly", "2026-46", "dist_peak", "1", 94, 8526, "2026-11-13 22"),
                ("weekly", "2026-46", "dist_peak", "2", 119, 10718, "2026-11-13 22"),
                # 有 ALL 行时接口以它为分母（653 款不是整周去重的 860）；
                # W45 故意不放 ALL 行，覆盖「三项求和 + all 行校验」的兼容分支
                ("weekly", "2026-46", "dist_peak", "ALL", 653, 146354, "2026-11-13 22"),
            ],
        )
        conn.execute(
            "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
            "SET placeholder_ratio = 0.0 WHERE category_type IN ('all', 'dist')"
        )
        # dist 行：W46 条数口径 0.3404/0.5943/0.5761、人数口径 0.2122/0.74/0.7179（真库值）
        for value, ratio, player_ratio, players in (
            ("0", 0.3404, 0.2122, 1214700),
            ("1", 0.5943, 0.74, 411100),
            ("2", 0.5761, 0.7179, 510800),
        ):
            conn.execute(
                "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
                "SET placeholder_ratio = ?, placeholder_player_ratio = ?, "
                "    placeholder_players = ? "
                "WHERE category_type = 'dist' AND crawled_at = '2026-46' "
                "AND category_value = ?",
                (ratio, player_ratio, players, value),
            )
        # W45 没有 ALL 行（模拟新周期首日还没重算），只有分型行带占位占比
        for value, ratio in (("0", 0.4131), ("1", 0.7937), ("2", 0.8571)):
            conn.execute(
                "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
                "SET placeholder_ratio = ? "
                "WHERE category_type = 'dist_peak' AND crawled_at = '2026-45' "
                "AND category_value = ?",
                (ratio, value),
            )
        # dist_peak 用 W46 真库的三个值：行数口径 0.4295 / 0.7872 / 0.7143，ALL 0.5329
        for value, ratio, player_ratio, players in (
            ("0", 0.4295, 0.1487, 18900),
            ("1", 0.7872, 0.8679, 7400),
            ("2", 0.7143, 0.7931, 8500),
            ("ALL", 0.5329, 0.2378, 34800),
        ):
            conn.execute(
                "UPDATE dws_taptap_pc_online_peak_players_periodic_report "
                "SET placeholder_ratio = ?, placeholder_player_ratio = ?, "
                "    placeholder_players = ? "
                "WHERE category_type = 'dist_peak' AND crawled_at = '2026-46' "
                "AND category_value = ?",
                (ratio, player_ratio, players, value),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_ad_aggr_v6_periodic_report (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                category_type TEXT NOT NULL,
                category_value TEXT NOT NULL,
                app_cnt INTEGER NULL,
                total_views INTEGER NULL,
                ad_views INTEGER NULL,
                pcebid_views INTEGER NULL,
                updated_at TEXT NULL,
                is_active INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_ad_aggr_v6_periodic_report "
            "(period_type, crawled_at, category_type, category_value, app_cnt, total_views) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("monthly", "2026-11", "dist", "0", 102, 8555),
                ("monthly", "2026-11", "dist", "1", 1, 73),
                ("monthly", "2026-11", "dist", "(未分类)", 21, 89),
                ("monthly", "2026-11", "tag", "三国", 1, 1),
            ],
        )

        # 首页概览：下载榜（top / gain / loss，每月每榜 rank_no 1..50）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_download_periodic_rank (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                rank_category TEXT NOT NULL,
                rank_no INTEGER NOT NULL,
                app_id INTEGER NOT NULL,
                app_name TEXT NULL,
                icon TEXT NULL,
                distribution_type INTEGER NULL,
                tag_1 TEXT NULL,
                tag_2 TEXT NULL,
                tag_3 TEXT NULL,
                total_download INTEGER NULL,
                prev_total_download INTEGER NULL,
                diff_download INTEGER NULL,
                is_active INTEGER NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_download_periodic_rank "
            "(period_type, crawled_at, rank_category, rank_no, app_id, app_name, icon, "
            " distribution_type, tag_1, tag_2, total_download, prev_total_download, "
            " diff_download) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # 当月：每个榜 6 条，验证 limit=5 截断
                _rank("top", 1, 1007673, "柚松屋", 0, "模拟", None, 568784, 262734, 306050),
                _rank("top", 2, 43639, "我的世界", 0, "沙盒", "开放世界",
                      457309, 1838322, -1381013),
                _rank("top", 3, 165287, "Phigros", 0, "音乐", None, 286374, 1288645, -1002271),
                _rank("top", 4, 995770, "网约车", 1, "模拟", None, 283315, 259971, 23344),
                _rank("top", 5, 168332, "原神", 0, "角色扮演", None,
                      214460, 659879, -445419),
                _rank("top", 6, 999999, "第六名", 2, "模拟", None, 100000, 0, 100000),
                # 同 rank_no 的残留重复行，应被去重
                _rank("top", 1, 1007673, "柚松屋", 0, "模拟", None, 568784, 262734, 306050),
                _rank("gain", 1, 1007673, "柚松屋", 0, "模拟", None, 568784, 262734, 306050),
                _rank("gain", 2, 41333, "韵律源点", 0, "音乐", None, 135346, 0, 135346),
                _rank("loss", 1, 43639, "我的世界", 0, "沙盒", "开放世界",
                      457309, 1838322, -1381013),
                _rank("loss", 2, 165287, "Phigros", 0, "音乐", None, 286374, 1288645, -1002271),
                # 上月行不应混进当月结果
                _rank("top", 1, 111, "上月第一名", 0, "模拟", None, 999, 0, 999, month="2026-10"),
            ],
        )

        # 首页概览：广告曝光榜（top / gain / loss，列名与下载榜不同）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_ad_aggr_v6_periodic_rank (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                rank_category TEXT NOT NULL,
                rank_no INTEGER NOT NULL,
                app_id INTEGER NOT NULL,
                app_name TEXT NULL,
                icon TEXT NULL,
                distribution_type INTEGER NULL,
                tag_1 TEXT NULL,
                tag_2 TEXT NULL,
                tag_3 TEXT NULL,
                total_views INTEGER NULL,
                prev_total_views INTEGER NULL,
                diff_views INTEGER NULL,
                is_active INTEGER NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_ad_aggr_v6_periodic_rank "
            "(period_type, crawled_at, rank_category, rank_no, app_id, app_name, icon, "
            " distribution_type, tag_1, total_views, prev_total_views, diff_views) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _ad_rank("top", 1, 239372, "燕云十六声", 0, "动作", 585, 684, -99),
                _ad_rank("top", 2, 893924, "星途天城", 0, "角色扮演", 514, 522, -8),
                _ad_rank("top", 3, 860527, "地城狩猎", 0, "策略", 512, 305, 207),
                _ad_rank("gain", 1, 824115, "三国志：王道天下", 0, "策略", 331, 34, 297),
                _ad_rank("gain", 2, 826044, "斗罗大陆：传承", 0, "角色扮演", 262, 44, 218),
                _ad_rank("loss", 1, 903352, "我叫MT：崛起", 0, "卡牌", 445, 1026, -581),
                _ad_rank("loss", 2, 748031, "伊莫", 0, "动作", 316, 704, -388),
            ],
        )
        # 定期报告榜单默认只看 is_active = 1 的行（线上有大量下线残留行）
        conn.execute("UPDATE dws_taptap_download_periodic_rank SET is_active = 1")
        conn.execute("UPDATE dws_taptap_ad_aggr_v6_periodic_rank SET is_active = 1")
        # PC 在线榜：当前维度（dist_*，is_active=1）+ 旧维度（pc_played，is_active=0，仅历史周）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_pc_online_peak_players_periodic_rank (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                rank_category TEXT NOT NULL,
                rank_no INTEGER NOT NULL,
                app_id INTEGER NOT NULL,
                app_name TEXT NULL,
                icon TEXT NULL,
                distribution_type INTEGER NULL,
                list_type TEXT NULL,
                tag_1 TEXT NULL,
                tag_2 TEXT NULL,
                avg_online_players REAL NULL,
                prev_avg_online_players REAL NULL,
                diff_online_players REAL NULL,
                updated_at TEXT NULL,
                is_active INTEGER NULL,
                real_hours INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_pc_online_peak_players_periodic_rank "
            "(period_type, crawled_at, rank_category, rank_no, app_id, app_name, "
            " distribution_type, list_type, avg_online_players, is_active, real_hours) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # 旧维度只存在于历史周，且已被标记下线（必须仍能查到）
                ("weekly", "2026-42", "top", 1, 111, "旧维度游戏", 0, "pc_played",
                 500.0, 0, 12),
                ("weekly", "2026-46", "top", 1, 222, "新维度游戏", 0, "dist_0",
                 900.0, 1, 20),
                # 样本不足的一榜：dist_1 全员 real_hours 都 <= 2
                ("weekly", "2026-46", "loss", 1, 333, "低样本甲", 1, "dist_1", 100.0, 1, 2),
                ("weekly", "2026-46", "loss", 2, 444, "低样本乙", 1, "dist_1", 100.0, 1, 1),
                # 中位数正好 8、但 max 20：两条阈值都不触发 → 正常榜
                ("weekly", "2026-46", "gain", 1, 555, "边界丙", 0, "dist_0", 500.0, 1, 8),
                ("weekly", "2026-46", "gain", 2, 666, "边界丁", 0, "dist_0", 480.0, 1, 8),
                ("weekly", "2026-46", "gain", 3, 777, "边界戊", 0, "dist_0", 460.0, 1, 20),
                # W45 dist_0 的真实形状：中位数 8 卡边界，但 max 也只有 8 → 覆盖太薄，判低样本
                ("weekly", "2026-46", "loss", 1, 888, "薄样本甲", 2, "dist_2", 300.0, 1, 8),
                ("weekly", "2026-46", "loss", 2, 999, "薄样本乙", 2, "dist_2", 280.0, 1, 8),
            ],
        )
        # 在线榜 icon：DWS 2026-11-16 从 details_hourly 回填，接口不再用 NULL 占位
        conn.execute(
            "UPDATE dws_taptap_pc_online_peak_players_periodic_rank "
            "SET icon = 'https://img-tc.tapimg.com/online-222.jpg' "
            "WHERE crawled_at = '2026-46' AND rank_no = 1 AND is_active = 1"
        )

        # 定期报告：广告周报汇总行 + 分型行（W44~W46，W43 之前无数据）
        conn.executemany(
            "INSERT INTO dws_taptap_ad_aggr_v6_periodic_report "
            "(period_type, crawled_at, category_type, category_value, app_cnt, "
            " total_views, ad_views, pcebid_views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-44", "all", "ALL", 109, 4597, 4373, 224,
                 "2026-11-16 11:35:09"),
                ("weekly", "2026-45", "all", "ALL", 96, 5534, 5114, 420,
                 "2026-11-16 11:35:09"),
                ("weekly", "2026-46", "all", "ALL", 120, 5969, 5328, 641,
                 "2026-11-16 11:35:09"),
                ("weekly", "2026-45", "dist", "0", 92, 5180, None, None, None),
                ("weekly", "2026-45", "dist", "1", 3, 320, None, None, None),
                ("weekly", "2026-45", "dist", "2", 1, 34, None, None, None),
                ("weekly", "2026-46", "dist", "0", 104, 5402, None, None, None),
                ("weekly", "2026-46", "dist", "1", 2, 468, None, None, None),
                ("weekly", "2026-46", "dist", "2", 0, 0, None, None, None),
                ("weekly", "2026-46", "dist", "(未分类)", 14, 99, None, None, None),
            ],
        )
        # is_active=0 的下线残留行：接口必须过滤掉（DWS 的规矩是置 0 不删数据）
        conn.execute(
            "UPDATE dws_taptap_ad_aggr_v6_periodic_report SET is_active = 1"
        )
        conn.execute(
            "INSERT INTO dws_taptap_ad_aggr_v6_periodic_report "
            "(period_type, crawled_at, category_type, category_value, app_cnt, "
            " total_views, is_active) "
            "VALUES ('weekly', '2026-46', 'dist', '(未分类)', 7, 500, 0)"
        )

        # 定期报告：概览周表（含一列已下线字段的值，验证接口不取）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_periodic_report_overview_weekly (
                id INTEGER PRIMARY KEY,
                week_key TEXT NOT NULL,
                week_start TEXT NULL,
                week_end TEXT NULL,
                online_game_cnt INTEGER NULL,
                online_game_cnt_hour TEXT NULL,
                online_peak_players INTEGER NULL,
                online_peak_pc INTEGER NULL,
                online_peak_pc_emulator INTEGER NULL,
                online_peak_creative INTEGER NULL,
                download_total INTEGER NULL,
                download_pc INTEGER NULL,
                download_app INTEGER NULL,
                download_dist_type_0 INTEGER NULL,
                download_dist_type_1 INTEGER NULL,
                download_dist_type_2 INTEGER NULL,
                ad_total INTEGER NULL,
                ad_app INTEGER NULL,
                ad_pc INTEGER NULL,
                ad_kw INTEGER NULL,
                torchlight_download_total INTEGER NULL,
                torchlight_avg_online_players REAL NULL,
                heartopia_download_total INTEGER NULL,
                heartopia_avg_online_players REAL NULL,
                torchlight_app_id INTEGER NULL,
                torchlight_icon TEXT NULL,
                heartopia_app_id INTEGER NULL,
                heartopia_icon TEXT NULL,
                online_peak_dist_0 INTEGER NULL,
                online_peak_dist_1 INTEGER NULL,
                online_peak_dist_2 INTEGER NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_periodic_report_overview_weekly "
            "(week_key, week_start, week_end, online_game_cnt, online_game_cnt_hour, "
            " online_peak_players, "
            " torchlight_download_total, torchlight_avg_online_players, "
            " heartopia_download_total, heartopia_avg_online_players, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # 合成的 14 倍放量：只为覆盖 ≥3× 告警逻辑（线上 W45 已修正为 66,379）
                ("2026-44", "2026-10-26", "2026-11-01", None, None, None,
                 86842, 11813.4, 228910, 8123.5, "2026-11-02 11:35:10"),
                # W45 概览的在线列是旧快照（763 / 133480），线上实测与在线周期表不符
                ("2026-45", "2026-11-02", "2026-11-08", 763, "2026-11-07 22", 133480,
                 1224138, 12150.5, 233456, 8231.2, "2026-11-12 18:24:27"),
                # W46：MAX(game_cnt)=671 在 11-15 22，而在线峰值（人数）在 11-13 22
                ("2026-46", "2026-11-09", "2026-11-15", 671, "2026-11-15 22", 146354,
                 47319, 10402.1, 254189, 8102.7, "2026-11-16 09:55:05"),
            ],
        )
        # 重点游戏真图与 app_id（后加列）：W46 有值，更早的期留 NULL
        conn.execute(
            "UPDATE dws_periodic_report_overview_weekly SET "
            "torchlight_app_id = 172664, "
            "torchlight_icon = 'https://img-tc.tapimg.com/torchlight.jpg', "
            "heartopia_app_id = 45213, "
            "heartopia_icon = 'https://img-tc.tapimg.com/heartopia.jpg' "
            "WHERE week_key = '2026-46'"
        )

        # 逐日趋势源表（日增量）：下载 v2_daily / 广告 v6_daily / 在线明细小时
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_download_v2_daily (
                id INTEGER PRIMARY KEY,
                crawled_at TEXT NOT NULL,
                app_id INTEGER NOT NULL,
                distribution_type INTEGER NULL,
                download_count INTEGER NULL,
                app_download_count INTEGER NULL,
                pc_download_count INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_download_v2_daily "
            "(crawled_at, app_id, distribution_type, download_count) VALUES (?, ?, ?, ?)",
            [
                # W46：11-09 / 11-10 有数据，11-11 故意整天缺（验证「缺天不出点」）
                ("2026-11-09", 1, 0, 600000), ("2026-11-09", 2, 1, 300000),
                ("2026-11-09", 3, 2, 200000),
                ("2026-11-10", 1, 0, 500000), ("2026-11-10", 2, 1, 250000),
                ("2026-11-10", 3, 2, 150000),
                ("2026-11-12", 1, 0, 400000),
                # 上一期 W45：只给 11-08 一天
                ("2026-11-08", 1, 0, 333000),
            ],
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_ad_aggr_v6_daily (
                id INTEGER PRIMARY KEY,
                crawled_at TEXT NOT NULL,
                app_id INTEGER NOT NULL,
                distribution_type INTEGER NULL,
                ad_views INTEGER NULL,
                pcebid_views INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_ad_aggr_v6_daily "
            "(crawled_at, app_id, distribution_type, ad_views, pcebid_views) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-11-09", 1, 0, 100, 20), ("2026-11-09", 2, 1, 30, 0),
                ("2026-11-10", 1, 0, 90, 10), ("2026-11-10", 2, 1, 30, 0),
                ("2026-11-08", 1, 0, 70, 0),
            ],
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_pc_online_peak_players_details_hourly (
                id INTEGER PRIMARY KEY,
                crawled_at TEXT NOT NULL,
                app_id INTEGER NOT NULL,
                distribution_type INTEGER NULL,
                online_players INTEGER NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_pc_online_peak_players_details_hourly "
            "(crawled_at, app_id, distribution_type, online_players) VALUES (?, ?, ?, ?)",
            [
                # 11-13 22 点：两 app 合计 1,000；20 点 900 → 当天峰值取 22 点
                ("2026-11-13 20:00:00", 1, 0, 600),
                ("2026-11-13 20:00:00", 2, 1, 300),
                ("2026-11-13 22:00:00", 1, 0, 700),
                ("2026-11-13 22:00:00", 2, 1, 300),
                ("2026-11-14 22:00:00", 1, 0, 500),
            ],
        )

        # 预约报表（DWS 2026-11-16 落库，采集 2026-11-08 才有）：W45 只 1 天、W46 满周、月报 9/17 天
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_reserve_periodic_report (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                category_type TEXT NOT NULL,
                category_value TEXT NOT NULL,
                reserve_add INTEGER NULL,
                reserve_game_cnt INTEGER NULL,
                reserve_game_cnt_roster INTEGER NULL,
                reserve_total_end INTEGER NULL,
                day_cnt INTEGER NULL,
                first_day TEXT NULL,
                last_day TEXT NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_reserve_periodic_report "
            "(period_type, crawled_at, category_type, category_value, reserve_add, "
            " reserve_game_cnt, reserve_game_cnt_roster, reserve_total_end, day_cnt, "
            " first_day, last_day, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-45", "all", "ALL", 163381, 199, 200, 96404047, 1,
                 "2026-11-08", "2026-11-08", "2026-11-16 09:00:00"),
                ("weekly", "2026-45", "dist", "0", 132827, 124, 125, 61496318, 1,
                 "2026-11-08", "2026-11-08", None),
                ("weekly", "2026-45", "dist", "1", 30554, 75, 75, 34907729, 1,
                 "2026-11-08", "2026-11-08", None),
                ("weekly", "2026-46", "all", "ALL", 1131061, 162, 163, 69613606, 7,
                 "2026-11-09", "2026-11-15", "2026-11-17 09:00:00"),
                ("weekly", "2026-46", "dist", "0", 959971, 110, 111, 49034036, 7,
                 "2026-11-09", "2026-11-15", None),
                ("weekly", "2026-46", "dist", "1", 171001, 52, 52, 20579277, 7,
                 "2026-11-09", "2026-11-15", None),
                ("weekly", "2026-46", "dist", "2", 89, 1, 1, 293, 4,
                 "2026-11-12", "2026-11-15", None),
                # W47 进行中（只 1 天，期内到今天 2 天）：日均环比必须被挡住
                ("weekly", "2026-47", "all", "ALL", 128150, 100, 100, 66535597, 1,
                 "2026-11-16", "2026-11-16", "2026-11-17 09:30:00"),
                ("monthly", "2026-11", "all", "ALL", 1422592, 200, 201, 69613606, 9,
                 "2026-11-08", "2026-11-16", "2026-11-17 09:00:00"),
                ("monthly", "2026-11", "dist", "0", 1202710, 140, 141, 49034036, 9,
                 "2026-11-08", "2026-11-16", None),
                ("monthly", "2026-11", "dist", "1", 219767, 60, 60, 20579277, 9,
                 "2026-11-08", "2026-11-16", None),
                ("monthly", "2026-11", "dist", "2", 115, 1, 1, 293, 5,
                 "2026-11-12", "2026-11-16", None),
            ],
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dws_taptap_reserve_periodic_rank (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                list_type TEXT NOT NULL,
                rank_type TEXT NOT NULL,
                rank_no INTEGER NOT NULL,
                app_id INTEGER NOT NULL,
                app_name TEXT NULL,
                icon TEXT NULL,
                distribution_type INTEGER NULL,
                reserve_add INTEGER NULL,
                total_reserve_count INTEGER NULL,
                updated_at TEXT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO dws_taptap_reserve_periodic_rank "
            "(period_type, crawled_at, list_type, rank_type, rank_no, app_id, app_name, "
            " icon, distribution_type, reserve_add, total_reserve_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("weekly", "2026-46", "all", "gain", 1, 43639, "白银之城",
                 "https://img-tc.tapimg.com/gain-43639.jpg", 0, 114809, 2452558),
                ("weekly", "2026-46", "all", "gain", 2, 730674, "第二款",
                 None, 0, 62032, 3055517),
                ("weekly", "2026-46", "all", "total", 1, 777320, "累计第一",
                 "https://img-tc.tapimg.com/total-777320.jpg", 1, 5000, 9900000),
            ],
        )

        # 访问统计表（MySQL 侧见 sql/visit_event.sql，这里放 SQLite 等价结构）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visit_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                visitor TEXT NOT NULL,
                visitor_src TEXT NOT NULL DEFAULT 'fp',
                referrer TEXT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def clear_visit_event() -> None:
    """清空访问明细，并让本周统计的进程内缓存失效。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.execute("DELETE FROM visit_event")
        conn.commit()
    finally:
        conn.close()

    from app.stats import clear_stats_cache

    clear_stats_cache()


@pytest.fixture(scope="session")
def client():
    _seed()
    os.environ["API_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
    os.environ["API_MAX_ROWS"] = "100"
    # 访问统计：测试用固定盐，写库指向同一个 SQLite
    os.environ["API_STATS_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
    os.environ["API_STATS_SALT"] = "test-salt"
    os.environ["API_STATS_CACHE_SECONDS"] = "0"

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stats_db_path():
    """测试库文件路径，方便用例直接造历史数据。"""
    return _DB_PATH


@pytest.fixture
def clean_stats():
    """每条统计用例开始前清空访问明细，保证 PV/UV 断言是确定值。"""
    clear_visit_event()
    yield
    clear_visit_event()
