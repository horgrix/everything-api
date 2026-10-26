"""建表脚本：初始化访问统计需要的 visit_event 表。

用法（在 everything-api 目录下，.env 已配好写库账号）：

    python scripts/init_visit_event.py          # 执行建表
    python scripts/init_visit_event.py --check  # 只检查表是否存在

DDL 在 sql/visit_event.sql，重复执行安全（CREATE TABLE IF NOT EXISTS）。
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


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    table = table_name()
    engine = get_stats_engine()
    dialect = engine.dialect.name
    print(f"目标库：{engine.url.render_as_string(hide_password=True)}（{dialect}）")
    print(f"表名：{table}")

    with engine.begin() as conn:
        exists = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            )
            if dialect == "mysql"
            else text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"),
            {"t": table},
        ).first()

        if check_only:
            print("存在" if exists else "不存在")
            return 0 if exists else 1

        if exists:
            print("表已存在，跳过建表")
            return 0

        if dialect != "mysql":
            print("非 MySQL 方言：请手动执行 sql/visit_event.sql")
            return 1

        for stmt in _statements():
            conn.exec_driver_sql(stmt)
    print("建表完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, StatsError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
