"""统一错误体：{"code": ..., "message": ..., "detail": {...}}（§11 约定）。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("ui_web")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def body(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def not_found(what: str, key: object = "") -> ApiError:
    return ApiError(404, "not_found", f"{what}不存在", {"key": key})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": "请求参数不合法",
                     "detail": {"errors": exc.errors()}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": f"http_{exc.status_code}", "message": str(exc.detail), "detail": {}},
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常：%s", exc)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "服务内部错误", "detail": {"error": str(exc)}},
        )
