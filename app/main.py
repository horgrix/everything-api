"""FastAPI 应用入口。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import run_query
from .errors import (
    CODE_BAD_REQUEST,
    CODE_DB_UNAVAILABLE,
    CODE_INTERNAL,
    error_response,
    ok,
)
from .routers import data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="Everything API",
    description="Everything 数据平台查询接口：直接执行 SQL 或按表名查询 MySQL 数据。",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(data.router)


@app.get("/", include_in_schema=False)
def index() -> dict:
    return ok({"name": "everything-api", "docs": "/docs", "health": "/health"})


@app.get("/health", tags=["health"], summary="健康检查")
def health() -> dict:
    """不触达数据库，只反映进程状态与配置是否完整。"""
    try:
        settings.mysql_url()
        configured, detail = True, "ok"
    except Exception as exc:  # noqa: BLE001 - 配置缺失也要正常返回健康状态
        configured, detail = False, str(exc)
    return ok({"status": "ok", "database_configured": configured, "detail": detail})


@app.get("/health/db", tags=["health"], summary="数据库连通性检查")
def health_db() -> object:
    try:
        run_query("SELECT 1", max_rows=1)
    except Exception as exc:  # noqa: BLE001 - 探测失败统一转 503
        logger.warning("数据库探测失败：%s", exc)
        return error_response(CODE_DB_UNAVAILABLE, f"数据库不可用：{exc}", 503)
    return ok({"status": "ok"})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(CODE_BAD_REQUEST, f"请求参数不合法：{exc.errors()}", 400)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常：%s", exc)
    return error_response(CODE_INTERNAL, "服务内部错误", 500)
