"""FastAPI 应用组装入口。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.routes import router
from .errors import ErrorCode, GovernanceError


def create_app() -> FastAPI:
    app = FastAPI(title="Multi-Tenant API Gateway")
    app.include_router(router)

    @app.exception_handler(GovernanceError)
    async def governance_error_handler(request: Request,
                                      exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {
                "code": "invalid_request",
                "message": "请求参数校验失败。",
                "details": {"errors": exc.errors()},
            }},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request,
                                      exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "服务内部错误。",
                "details": {"type": type(exc).__name__},
            }},
        )

    return app


app = create_app()
