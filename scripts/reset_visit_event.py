"""一次性重置脚本：清空访问明细并按 v2 结构重建 visit_event。

v1 → v2 的差异：新增 `visitor_src`（访客身份来源 cid / fp），
并去掉写入时的 30 分钟去重（PV 改为明细行数）。旧表结构不兼容，
所以这里直接 DROP 重建 —— **会丢掉全部历史访问数据**。

用法（在 everything-api 目录下，.env 已配好写库账号）：

    python scripts/reset_visit_event.py            # 只体检，打印当前行数/结构
    python scripts/reset_visit_event.py --yes      # 真正清空重建
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.as_posix()))

from app.config import ConfigError  # noqa: E402
from app.stats import StatsError, get_stats_engine, table_name  # noqa: E402

DDL_FILE = ROOT / "sql" / "visit_event.sql"


def _statements() -> list[str]:
    raw = DDL_FILE.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if not line.strip().startswith("--")]
    body = "\n".join(lines)
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def _exists(conn, table: str, dialect: str) -> bool:
    stmt = (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :t"
        if dialect == "mysql"
        else "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"
    )
    return conn.execute(text(stmt), {"t": table}).first() is not None


def main(argv: list[str]) -> int:
    confirm = "--yes" in argv
    table = table_name()
    engine = get_stats_engine()
    dialect = engine.dialect.name
    print(f"目标库：{engine.url.render_as_string(hide_password=True)}（{dialect}）")
    print(f"表名：{table}")

    if dialect != "mysql":
        print("非 MySQL 方言：请手动执行 sql/visit_event.sql")
        return 1

    with engine.begin() as conn:
        if _exists(conn, table, dialect):
            rows = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
            columns = [row[0] for row in conn.execute(text(f"SHOW COLUMNS FROM {table}")).all()]
            print(f"现有行数：{rows}")
            print(f"现有列：{', '.join(columns)}")
            if "visitor_src" in columns:
                print("已经是 v2 结构（含 visitor_src），无需重建。")
                return 0
        else:
            print("表不存在，将直接建表。")

        if not confirm:
            print("这是体检模式：加 --yes 才会 DROP 并重建（会清空全部历史访问数据）。")
            return 0

        if _exists(conn, table, dialect):
            conn.exec_driver_sql(f"DROP TABLE {table}")
            print(f"已 DROP {table}")

        for stmt in _statements():
            conn.exec_driver_sql(stmt)

    print("已按 v2 结构重建完成（历史访问数据已清空）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, StatsError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
