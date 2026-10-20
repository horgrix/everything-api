"""统一响应与错误码。"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

# 0 表示成功；其余为业务错误码
CODE_OK = 0
CODE_BAD_REQUEST = 40000          # 参数不合法
CODE_SQL_GUARD = 40300            # 被只读 / 单语句检查拒绝
CODE_SQL_ERROR = 40001            # SQL 执行失败（语法、表不存在等）
CODE_DB_UNAVAILABLE = 50300       # 连接失败或配置缺失
CODE_INTERNAL = 50000


def ok(data: Any = None, total: int | None = None, truncated: bool | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": CODE_OK, "message": "success", "data": data}
    if total is not None:
        payload["total"] = total
    if truncated is not None:
        payload["truncated"] = truncated
    return payload


def error_response(code: int, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "data": None},
    )
