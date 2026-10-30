"""Shared HTTP error rendering for business services."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.business.errors import BusinessError


async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    detail = exc.to_detail()
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        detail["request_id"] = request_id
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


def install_business_error_handler(app: FastAPI) -> None:
    app.add_exception_handler(BusinessError, business_error_handler)
