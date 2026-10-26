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


class HitRequest(BaseModel):
    """上报一次页面浏览。"""

    path: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="页面路径，只取 pathname；服务端会再归一化（去 query/hash，限长 128）",
        examples=["/taptap"],
    )
    title: str | None = Field(
        default=None, max_length=255, description="页面标题，可选（仅校验，不入库）"
    )
    referrer: str | None = Field(default=None, max_length=512, description="来源页，可选")


class Envelope(BaseModel):
    """对外统一响应结构。"""

    code: int = 0
    message: str = "success"
    data: Any = None
    total: int | None = None
    truncated: bool | None = None
