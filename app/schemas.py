"""请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SqlQueryRequest(BaseModel):
    """直接执行 SQL 的请求体。"""

    sql: str = Field(
        ...,
        description="要执行的 SQL，单条语句；只读模式下仅允许 SELECT/WITH/SHOW/DESC/EXPLAIN",
        examples=[
            "SELECT game_name, download_count FROM dws_taptap_download_daily "
            "ORDER BY dt DESC LIMIT 10"
        ],
    )
    params: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description="绑定参数。dict 对应 %(name)s 占位符，list 对应 %s 占位符",
    )
    max_rows: int | None = Field(
        default=None,
        ge=1,
        description="本次返回的最大行数，不超过服务端上限 API_MAX_ROWS",
    )


class TableQueryRequest(BaseModel):
    """按表名 + 条件查询的请求参数（查询串形式传入）。"""

    fields: str = Field(default="*", description="逗号分隔的字段列表，默认 *")
    where: str | None = Field(
        default=None,
        description='JSON 数组：[{"col":"price","op":">","value":100}]',
    )
    group_by: str | None = Field(default=None, description="逗号分隔的分组字段")
    order_by: str | None = Field(default=None, description="排序，如 dt DESC 或 COUNT(*) DESC")
    aggregate: str | None = Field(
        default=None,
        description='聚合表达式，如 SUM(download_count) AS total, COUNT(*) AS cnt',
    )
    limit: int | None = Field(default=None, ge=1, description="行数限制")
    offset: int = Field(default=0, ge=0, description="偏移量")
    with_count: bool = Field(default=False, description="是否额外统计满足条件的总行数")


class Envelope(BaseModel):
    """对外统一响应结构。"""

    code: int = 0
    message: str = "success"
    data: Any = None
    total: int | None = None
    truncated: bool | None = None
