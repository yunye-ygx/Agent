"""Tool execution boundary, independent of LLM providers and task persistence.

Adapters must use cooperative async I/O (no blocking CPU work on the event loop)
and propagate cancellation. A timeout stops local waiting, not remote effects.
The trusted caller supplies identity/permissions/operation_id; never build that
context from model arguments. Write adapters enforce business idempotency at the
actual write boundary; an operation ID alone does NOT implement idempotency.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class ToolArguments(BaseModel):
    """Subclasses define business fields and may add business validators."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ToolCall(ToolArguments):
    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class ExecutionContext(BaseModel):
    """Backend-owned context, NOT part of the tool schema exposed to the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str = Field(min_length=1)
    permissions: frozenset[str] = frozenset()
    operation_id: str | None = Field(default=None, min_length=1, max_length=200)


class Observation(BaseModel):
    status: Literal["success", "rejected", "failed", "unknown"]
    code: str
    message: str
    call_id: str | None
    tool: str | None
    attempts: int = 0
    data: dict[str, Any] | None = None
    operation_id: str | None = None
    next_action: Literal["continue", "fix_arguments", "report_failure", "stop", "reconcile"]
    issues: list[dict[str, Any]] = Field(default_factory=list)

    def to_tool_message(self) -> dict[str, str]:
        """Serialize one valid call's observation to Chat Completions format."""
        if self.call_id is None:
            raise ValueError("A malformed call without an ID cannot form a tool message")
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": self.model_dump_json(),
        }


class RetryableToolError(Exception):
    """Adapter classified a transient failure; only read operations auto-retry."""


class ToolRejected(Exception):
    """Known business rejection with NO committed effect.

    Adapters must supply a public, sanitized message, never an upstream exception.
    Do not use this for an ambiguous timeout or a write followed by a failure.
    """

    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code, self.public_message = code, public_message


@dataclass(frozen=True)
class ToolSpec:
    name: str
    arguments_model: type[ToolArguments]
    handler: Callable[[Any, ExecutionContext], Awaitable[dict[str, Any]]]
    permission: str
    # Fast local resource check. Real adapters must also enforce ownership in
    # their database/API boundary, especially if authorization data can change.
    authorize: Callable[[Any, ExecutionContext], bool]
    effect: Literal["read", "write"] = "read"
    timeout_seconds: float = 10.0
    max_attempts: int = 1  # Includes the first call; 2 means one retry.
    retry_delay_seconds: float = 0.1
    description: str = ""

    def __post_init__(self):
        if not self.name or not self.permission or self.effect not in {"read", "write"}:
            raise ValueError("Tool name, permission and effect must be explicit")
        if not issubclass(self.arguments_model, ToolArguments):
            raise TypeError("Arguments must extend ToolArguments")
        if (
            self.arguments_model.model_config.get("extra") != "forbid"
            or self.arguments_model.model_config.get("strict") is not True
        ):
            raise ValueError("Tool arguments must forbid extra fields and use strict types")
        if not inspect.iscoroutinefunction(self.handler):
            raise TypeError("Tools must use cooperative async handlers")
        if not callable(self.authorize) or inspect.iscoroutinefunction(self.authorize):
            raise TypeError("authorize must be a fast synchronous predicate")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be an integer between 1 and 5")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be nonnegative and finite")


def _issues(error: ValidationError) -> list[dict[str, Any]]:
    # Pydantic's full errors can contain credentials in input/ctx/msg.
    return [
        {"path": list(item["loc"]), "type": item["type"]}
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    ]


class ToolRuntime:
    def __init__(self, tools: Iterable[ToolSpec]):
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool: {tool.name}")
            self._tools[tool.name] = tool

    async def execute(
        self, raw_call: ToolCall | dict[str, Any], context: ExecutionContext
    ) -> Observation:
        """Validate -> authorize -> execute -> normalize; never creates a task/MQ job."""
        try:
            # Revalidate model instances too; callers may have mutated arguments.
            call = ToolCall.model_validate(
                raw_call.model_dump() if isinstance(raw_call, ToolCall) else raw_call
            )
        except ValidationError as exc:
            return Observation(
                status="rejected", code="INVALID_CALL", message="工具调用结构不合法。",
                call_id=None, tool=None, next_action="fix_arguments", issues=_issues(exc),
            )

        def observation(status, code, message, *, attempts=0, next_action="stop", **extra):
            return Observation(
                status=status, code=code, message=message, call_id=call.call_id,
                tool=call.name, attempts=attempts, next_action=next_action, **extra,
            )

        tool = self._tools.get(call.name)
        if tool is None:
            return observation("rejected", "UNKNOWN_TOOL", "工具不存在或未开放。")
        if tool.permission not in context.permissions:
            return observation("rejected", "FORBIDDEN", "当前用户无权执行此操作。")
        try:
            args = tool.arguments_model.model_validate(call.arguments)
        except ValidationError as exc:
            return observation(
                "rejected", "INVALID_ARGUMENTS", "参数不合法，请核对字段与类型。",
                next_action="fix_arguments", issues=_issues(exc),
            )
        try:
            allowed = tool.authorize(args, context) is True
        except Exception:
            # Never turn an authorization infrastructure failure into permission.
            logger.warning("Tool authorization failed (%s)", tool.name)
            return observation("failed", "AUTHORIZATION_UNAVAILABLE", "暂时无法验证权限。")
        if not allowed:
            return observation("rejected", "FORBIDDEN", "无权访问指定资源。")
        if tool.effect == "write" and context.operation_id is None:
            return observation(
                "rejected", "OPERATION_ID_REQUIRED", "后端尚未提供业务操作标识。",
            )

        limit = tool.max_attempts if tool.effect == "read" else 1
        for attempt in range(1, limit + 1):
            try:
                async with asyncio.timeout(tool.timeout_seconds):
                    data = await tool.handler(args, context)
                if not isinstance(data, dict):
                    raise TypeError("Tool output must be a JSON object")
                # Reject non-JSON values before handing an observation to the LLM.
                json.dumps(data, allow_nan=False)
                return observation(
                    "success", "OK", "工具执行成功。", attempts=attempt,
                    next_action="continue", data=data,
                    operation_id=context.operation_id if tool.effect == "write" else None,
                )
            except ToolRejected as exc:
                return observation(
                    "rejected", exc.code, exc.public_message, attempts=attempt,
                )
            except (TimeoutError, RetryableToolError) as exc:
                code = "TIMEOUT" if isinstance(exc, TimeoutError) else "TRANSIENT_ERROR"
                if attempt < limit:
                    await asyncio.sleep(tool.retry_delay_seconds * 2 ** (attempt - 1))
                    continue
            except Exception:
                code = "TOOL_ERROR"
                # Log metadata only, never upstream credentials/tracebacks to the model.
                logger.warning("Tool execution failed (%s, attempt %s)", tool.name, attempt)
            # asyncio.CancelledError is intentionally NOT caught or retried.
            if tool.effect == "write":
                return observation(
                    "unknown", code, "写操作结果尚未确认；请核对原操作，不要重复创建。",
                    attempts=attempt, next_action="reconcile", operation_id=context.operation_id,
                )
            return observation(
                "failed", code, "工具未能返回有效结果。", attempts=attempt,
                next_action="report_failure",
            )
        raise AssertionError("Unreachable: at least one attempt is required")
