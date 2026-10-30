"""Debug page API: directory plus one bounded tool execution.

The body cannot supply user_id, role, permissions, timeouts, retries, SQL or
imports; identity and policy come from the authenticated server-side session.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import get_current_principal, require_csrf
from app.business.principal import Principal
from app.tool_runtime import ExecutionContext, ToolCall
from app.tools import catalog

router = APIRouter(prefix="/api/v1/tool-lab", tags=["tool-lab"])


class ToolLabExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    operation_id: str | None = Field(default=None, min_length=1, max_length=36)


@router.get("/tools")
async def list_tools(principal: Principal = Depends(get_current_principal)) -> dict:
    return {
        "identity": {
            "user_id": principal.user_id,
            "username": principal.username,
            "display_name": principal.display_name,
            "role": principal.role,
            "permissions": sorted(principal.permissions),
        },
        "tools": catalog.tool_directory(principal),
    }


@router.post("/execute")
async def execute_tool(
    payload: ToolLabExecuteRequest,
    request: Request,
    principal: Principal = Depends(require_csrf),
) -> dict:
    context = ExecutionContext(
        user_id=principal.user_id,
        permissions=principal.permissions,
        operation_id=payload.operation_id,
    )
    call = ToolCall(call_id=payload.call_id, name=payload.name, arguments=payload.arguments)
    started = time.perf_counter()
    observation = await catalog.runtime.execute(call, context)
    return {
        "observation": observation.model_dump(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "attempts": observation.attempts,
        "request_id": getattr(request.state, "request_id", None),
        "identity": {
            "user_id": principal.user_id,
            "username": principal.username,
            "role": principal.role,
        },
    }
