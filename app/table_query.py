"""按表名 + 条件构造查询 SQL（不使用裸字符串拼接用户输入）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Dialect

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# 聚合表达式只允许有限字符集，禁止引号、分号、注释符
_AGGREGATE = re.compile(r"^[A-Za-z0-9_$(),.*+\-/<> ]+$")

_OPS = {
    "=", "!=", "<>", ">", ">=", "<", "<=",
    "like", "not like", "in", "not in",
    "is null", "is not null", "between",
}


class IdentifierError(ValueError):
    """表名 / 字段名 / 表达式不合法。"""


@dataclass(slots=True)
class BuiltQuery:
    sql: str
    params: dict[str, Any]
    count_sql: str
    count_params: dict[str, Any] = field(default_factory=dict)


def _quote(dialect: Dialect, name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise IdentifierError(f"非法的标识符：{name}")
    return dialect.identifier_preparer.quote(name)


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_fields(dialect: Dialect, raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return ["*"]
    columns: list[str] = []
    for token in _split_list(raw):
        if token == "*":
            columns.append("*")
        else:
            columns.append(_quote(dialect, token))
    return columns


def _parse_order(dialect: Dialect, raw: str | None) -> list[str]:
    if not raw:
        return []
    parts: list[str] = []
    for token in _split_list(raw):
        pieces = token.split()
        if not pieces or len(pieces) > 2:
            raise IdentifierError(f"非法的排序表达式：{token}")
        column = _quote(dialect, pieces[0])
        if len(pieces) == 2:
            direction = pieces[1].upper()
            if direction not in {"ASC", "DESC"}:
                raise IdentifierError(f"非法的排序方向：{pieces[1]}")
            column = f"{column} {direction}"
        parts.append(column)
    return parts


def _parse_aggregate(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    if not _AGGREGATE.match(raw):
        raise IdentifierError(f"非法的聚合表达式：{raw}")
    return raw.strip()


def _parse_where(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IdentifierError(f"where 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(parsed, list):
        raise IdentifierError("where 必须是 JSON 数组")
    for item in parsed:
        if not isinstance(item, dict) or "col" not in item:
            raise IdentifierError('where 每项必须是 {"col": ..., "op": ..., "value": ...}')
    return parsed


def _where_clause(
    dialect: Dialect, conditions: list[dict[str, Any]], params: dict[str, Any]
) -> str:
    fragments: list[str] = []
    for index, condition in enumerate(conditions):
        column = _quote(dialect, str(condition["col"]))
        op = str(condition.get("op", "=")).strip().lower()
        if op not in _OPS:
            raise IdentifierError(f"不支持的比较符：{op}")

        if op == "is null":
            fragments.append(f"{column} IS NULL")
            continue
        if op == "is not null":
            fragments.append(f"{column} IS NOT NULL")
            continue

        value = condition.get("value")
        if op in {"in", "not in"}:
            if not isinstance(value, (list, tuple)) or not value:
                raise IdentifierError(f"{op} 需要非空数组 value")
            keys = []
            for offset, element in enumerate(value):
                key = f"w{index}_{offset}"
                params[key] = element
                keys.append(f":{key}")
            fragments.append(f"{column} {op.upper()} ({', '.join(keys)})")
        elif op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise IdentifierError("between 需要 [下限, 上限]")
            low, high = f"w{index}_low", f"w{index}_high"
            params[low], params[high] = value[0], value[1]
            fragments.append(f"{column} BETWEEN :{low} AND :{high}")
        elif value is None:
            fragments.append(f"{column} IS {'NOT ' if op in {'!=', '<>'} else ''}NULL")
        else:
            key = f"w{index}"
            params[key] = value
            rendered = op if op in {"like", "not like"} else op.upper()
            fragments.append(f"{column} {rendered} :{key}")
    return " AND ".join(fragments)


def build_table_query(
    dialect: Dialect,
    table: str,
    *,
    data: dict[str, Any],
    max_rows: int,
) -> BuiltQuery:
    """根据表名与条件构造 SELECT 与对应的 COUNT 语句。"""
    quoted_table = _quote(dialect, table)
    fields = _parse_fields(dialect, data.get("fields"))
    aggregate = _parse_aggregate(data.get("aggregate"))
    group_by = _parse_order(dialect, data.get("group_by"))
    order_by = _parse_order(dialect, data.get("order_by"))
    conditions = _parse_where(data.get("where"))

    select_items: list[str] = []
    if aggregate:
        select_items.append(aggregate)
        if fields != ["*"]:
            select_items.extend(fields)
    else:
        select_items.extend(fields)

    params: dict[str, Any] = {}
    clauses = [f"SELECT {', '.join(select_items)} FROM {quoted_table}"]

    where_sql = _where_clause(dialect, conditions, params)
    if where_sql:
        clauses.append(f"WHERE {where_sql}")
    if group_by:
        clauses.append(f"GROUP BY {', '.join(group_by)}")
    if order_by:
        clauses.append(f"ORDER BY {', '.join(order_by)}")

    base_sql = " ".join(clauses)

    limit = int(data.get("limit") or max_rows)
    limit = max(1, min(limit, max_rows))
    offset = max(0, int(data.get("offset") or 0))

    sql = f"{base_sql} LIMIT :_limit OFFSET :_offset"
    params["_limit"] = limit
    params["_offset"] = offset

    count_sql = f"SELECT COUNT(*) AS total FROM ({base_sql}) AS _t"
    return BuiltQuery(
        sql=sql,
        params=params,
        count_sql=count_sql,
        count_params={k: v for k, v in params.items() if not k.startswith("_")},
    )
