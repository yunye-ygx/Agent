"""Exercise the execution boundary, without an LLM or external services."""

import asyncio

import pytest
from pydantic import Field

from app.tool_runtime import (
    ExecutionContext,
    RetryableToolError,
    ToolArguments,
    ToolCall,
    ToolRejected,
    ToolRuntime,
    ToolSpec,
)


class OrderArguments(ToolArguments):
    order_id: str = Field(min_length=1)


def call(**arguments):
    return ToolCall(call_id="call-1", name="query_order", arguments=arguments)


def context(*, permissions=frozenset({"orders:read"}), operation_id=None):
    return ExecutionContext(
        user_id="user-1", permissions=permissions, operation_id=operation_id
    )


def spec(handler, **kwargs):
    return ToolSpec(
        name="query_order",
        arguments_model=OrderArguments,
        handler=handler,
        permission="orders:read",
        authorize=lambda args, ctx: args.order_id == "A-100" and ctx.user_id == "user-1",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "ctx", "code"),
    [
        ({"order_id": 100}, context(), "INVALID_ARGUMENTS"),
        ({"order_id": "A-100", "tenant_id": "other"}, context(), "INVALID_ARGUMENTS"),
        ({}, context(), "INVALID_ARGUMENTS"),
        ({"order_id": "A-100"}, context(permissions=frozenset()), "FORBIDDEN"),
        ({"order_id": "OTHER"}, context(), "FORBIDDEN"),
    ],
)
async def test_rejected_arguments_or_access_never_execute_tool(arguments, ctx, code):
    executed = []

    async def handler(args, trusted):
        executed.append(args.order_id)
        return {"status": "shipped"}

    result = await ToolRuntime([spec(handler)]).execute(call(**arguments), ctx)
    assert (result.status, result.code, result.attempts) == ("rejected", code, 0)
    assert executed == []


@pytest.mark.asyncio
async def test_unknown_tool_and_malformed_call_are_observations():
    runtime = ToolRuntime([])
    unknown = await runtime.execute(call(order_id="A-100"), context())
    malformed = await runtime.execute({"name": "x", "arguments": "secret"}, context())
    assert unknown.code == "UNKNOWN_TOOL"
    assert malformed.code == "INVALID_CALL"
    assert "secret" not in malformed.model_dump_json()


@pytest.mark.asyncio
async def test_read_retries_transient_error_then_returns_actual_result():
    attempts = []

    async def handler(args, trusted):
        attempts.append(trusted.user_id)
        if len(attempts) == 1:
            raise RetryableToolError("internal upstream URL and credential")
        return {"order_id": args.order_id, "status": "shipped"}

    runtime = ToolRuntime([spec(handler, max_attempts=2, retry_delay_seconds=0)])
    result = await runtime.execute(call(order_id="A-100"), context())
    assert result.status == "success" and result.attempts == 2
    assert result.data == {"order_id": "A-100", "status": "shipped"}
    assert attempts == ["user-1", "user-1"]
    message = result.to_tool_message()
    assert message["role"] == "tool" and message["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_read_timeout_has_finite_attempts():
    attempts = []

    async def handler(args, trusted):
        attempts.append(1)
        await asyncio.sleep(10)
        return {}

    runtime = ToolRuntime([
        spec(handler, timeout_seconds=0.01, max_attempts=2, retry_delay_seconds=0)
    ])
    result = await runtime.execute(call(order_id="A-100"), context())
    assert (result.status, result.code, result.attempts) == ("failed", "TIMEOUT", 2)
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_write_timeout_preserves_unknown_outcome_and_never_auto_retries():
    committed = []

    async def handler(args, trusted):
        committed.append(trusted.operation_id)
        await asyncio.sleep(10)  # Business write succeeded, response not received yet.
        return {"ticket_id": "T100"}

    runtime = ToolRuntime([
        spec(handler, effect="write", timeout_seconds=0.01, max_attempts=3)
    ])
    result = await runtime.execute(call(order_id="A-100"), context(operation_id="op-1"))
    assert (result.status, result.code, result.attempts) == ("unknown", "TIMEOUT", 1)
    assert result.next_action == "reconcile" and result.operation_id == "op-1"
    assert committed == ["op-1"]


@pytest.mark.asyncio
async def test_write_requires_backend_operation_id_before_execution():
    async def handler(args, trusted):
        pytest.fail("write must not run without its operation identity")

    result = await ToolRuntime([spec(handler, effect="write")]).execute(
        call(order_id="A-100"), context()
    )
    assert result.code == "OPERATION_ID_REQUIRED" and result.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("effect,expected", [("read", "failed"), ("write", "unknown")])
async def test_unexpected_errors_are_redacted_and_not_retried(effect, expected):
    attempts = []

    async def handler(args, trusted):
        attempts.append(1)
        raise RuntimeError("secret-token=abc123")

    result = await ToolRuntime([spec(handler, effect=effect, max_attempts=3)]).execute(
        call(order_id="A-100"), context(operation_id="op-1")
    )
    assert result.status == expected and result.attempts == 1
    assert "abc123" not in result.model_dump_json()
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_business_rejection_is_known_no_effect_and_not_retried():
    async def handler(args, trusted):
        raise ToolRejected("DEVICE_NOT_FOUND", "设备不存在，请核对设备编号。")

    result = await ToolRuntime([spec(handler, effect="write", max_attempts=3)]).execute(
        call(order_id="A-100"), context(operation_id="op-1")
    )
    assert result.status == "rejected" and result.code == "DEVICE_NOT_FOUND"
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_retry():
    entered = asyncio.Event()

    async def handler(args, trusted):
        entered.set()
        await asyncio.sleep(10)
        return {}

    runtime = ToolRuntime([spec(handler, max_attempts=3)])
    running = asyncio.create_task(runtime.execute(call(order_id="A-100"), context()))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


def test_duplicate_registration_and_sync_handlers_are_rejected():
    async def handler(args, trusted):
        return {}

    with pytest.raises(ValueError):
        ToolRuntime([spec(handler), spec(handler)])
    with pytest.raises(TypeError):
        spec(lambda args, trusted: {})


@pytest.mark.asyncio
async def test_permission_check_failure_is_closed_and_redacted():
    async def handler(args, trusted):
        pytest.fail("authorization failed")

    def broken_authorizer(args, trusted):
        raise RuntimeError("private auth connection string")

    tool = ToolSpec(
        name="query_order", arguments_model=OrderArguments, handler=handler,
        permission="orders:read", authorize=broken_authorizer,
    )
    result = await ToolRuntime([tool]).execute(call(order_id="A-100"), context())
    assert result.code == "AUTHORIZATION_UNAVAILABLE" and result.attempts == 0
    assert "private" not in result.model_dump_json()
