"""Durable, fenced tool-call execution and replay."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select

from app.business.utils import utc_now
from app.chat import events, lifecycle, store
from app.chat.contracts import (
    RunCancelled,
    RunLease,
    RunStatus,
    ToolRequiresAttention,
)
from app.chat.models import ToolExecution, Turn
from app.tool_runtime import Observation


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


async def prepare_tool_batch(
    db,
    *,
    run_id: str,
    worker_id: str,
    lease_generation: int,
    model_response_id: str,
    calls: list[dict],
) -> list[ToolExecution]:
    """Persist every model tool call before any external side effect occurs."""

    await lifecycle.assert_run_ownership(
        db,
        run_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
    )
    existing = {
        row.tool_call_id: row
        for row in (
            await db.execute(select(ToolExecution).where(ToolExecution.run_id == run_id))
        ).scalars()
    }
    for index, call in enumerate(calls):
        arguments = call.get("args") or {}
        arguments_hash = _value_hash(arguments)
        row = existing.get(call["id"])
        if row is not None:
            if row.tool_name != call["name"] or row.arguments_hash != arguments_hash:
                raise ValueError(f"tool call identity changed during replay: {call['id']}")
            continue
        row = ToolExecution(
            run_id=run_id,
            model_response_id=model_response_id,
            tool_batch_id=model_response_id,
            tool_call_id=call["id"],
            call_index=index,
            tool_name=call["name"],
            tool_version=call.get("version", "v1"),
            arguments_json=arguments,
            arguments_hash=arguments_hash,
            status="pending",
        )
        db.add(row)
        existing[row.tool_call_id] = row
    await db.commit()
    return list(
        (
            await db.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.run_id == run_id,
                    ToolExecution.model_response_id == model_response_id,
                )
                .order_by(ToolExecution.call_index)
            )
        ).scalars()
    )


async def execute_tool_batch(
    db,
    *,
    run_id: str,
    worker_id: str,
    lease_generation: int,
    model_response_id: str,
    calls: list[dict],
    runner,
) -> list:
    """Resume a tool batch, reusing succeeded calls and preserving call order."""

    rows = await prepare_tool_batch(
        db,
        run_id=run_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
        model_response_id=model_response_id,
        calls=calls,
    )
    calls_by_id = {call["id"]: call for call in calls}
    observations = []
    for row in rows:
        if row.status == "succeeded":
            observations.append(row.result_json)
            continue
        if row.attempt_count >= row.max_attempts:
            raise RuntimeError(f"tool call exhausted attempts: {row.tool_call_id}")

        await lifecycle.assert_run_ownership(
            db,
            run_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
        )
        row.status = "running"
        row.attempt_count += 1
        row.started_at = utc_now()
        row.updated_at = row.started_at
        await db.commit()

        try:
            result = await runner(calls_by_id[row.tool_call_id])
        except Exception as exc:
            await lifecycle.assert_run_ownership(
                db,
                run_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
            row = await db.get(ToolExecution, row.id)
            row.status = "failed"
            row.error_code = type(exc).__name__
            row.error_message = str(exc)[:1000]
            row.finished_at = utc_now()
            row.updated_at = row.finished_at
            await db.commit()
            raise

        await lifecycle.assert_run_ownership(
            db,
            run_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
        )
        row = await db.get(ToolExecution, row.id)
        row.status = "succeeded"
        row.result_json = result
        row.result_hash = _value_hash(result)
        row.finished_at = utc_now()
        row.updated_at = row.finished_at
        await db.commit()
        observations.append(result)
    return observations


_STATUS_MAP = {
    "success": "succeeded",
    "rejected": "rejected",
    "failed": "failed",
    "unknown": "unknown",
}


class DurableToolRecorder:
    """Replays committed tool results and persists every new side effect.

    The recorder is called from the LangGraph tool middleware, so it must be
    fenced: a stale worker may not write a tool result or protocol message.
    """

    def __init__(
        self,
        session_factory,
        *,
        lease: RunLease,
        max_attempts: int = 3,
    ):
        self.session_factory = session_factory
        self.lease = lease
        self.max_attempts = max_attempts
        self._model_response_id = f"response-{uuid.uuid4().hex}"
        self._tool_batch_id = self._model_response_id
        self._call_order: dict[str, int] = {}

    def begin_response(self, model_response_id: str | None, calls: list[dict]) -> None:
        if model_response_id:
            self._model_response_id = model_response_id
            self._tool_batch_id = model_response_id
        self._call_order = {call["id"]: index for index, call in enumerate(calls)}

    async def before(self, call: dict, context) -> Observation | None:
        call_id = call["id"]
        requires_attention = False
        async with self.session_factory() as db:
            run = await lifecycle.assert_run_ownership(
                db,
                self.lease.run_id,
                worker_id=self.lease.worker_id,
                lease_generation=self.lease.lease_generation,
            )
            if run.status == RunStatus.CANCELLING:
                raise RunCancelled(f"run {run.id} is cancelling")
            row = (
                await db.execute(
                    select(ToolExecution).where(
                        ToolExecution.run_id == run.id,
                        ToolExecution.tool_call_id == call_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.status == "succeeded" and row.result_json is not None:
                return Observation.model_validate(row.result_json)
            if row is not None and row.status in ("unknown", "requires_attention"):
                raise ToolRequiresAttention(
                    f"tool call {call_id} needs reconciliation before it can run again"
                )
            if row is not None and row.status == "running":
                # A previous worker may have completed an external side effect
                # after writing `running` but before it saved the outcome.
                # Repeating it is unsafe until an operator/reconciler verifies it.
                now = utc_now()
                row.status = "requires_attention"
                row.error_code = "TOOL_RESULT_UNKNOWN"
                row.error_message = "工具调用中断，外部结果尚未确认"
                row.finished_at = now
                row.updated_at = now
                await db.commit()
                requires_attention = True
            if (
                not requires_attention
                and row is not None
                and row.attempt_count >= row.max_attempts
            ):
                raise ToolRequiresAttention(
                    f"tool call {call_id} exhausted its persisted attempts"
                )
            if not requires_attention:
                now = utc_now()
                arguments = call.get("args") or {}
                if row is None:
                    row = ToolExecution(
                        run_id=run.id,
                        model_response_id=self._model_response_id,
                        tool_batch_id=self._tool_batch_id,
                        tool_call_id=call_id,
                        call_index=self._call_order.get(call_id, 0),
                        tool_name=call["name"],
                        tool_version=call.get("version", "v1"),
                        arguments_json=arguments,
                        arguments_hash=_value_hash(arguments),
                        status="pending",
                        attempt_count=0,
                        max_attempts=self.max_attempts,
                        idempotency_key=f"{run.id}:{call_id}",
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                row.status = "running"
                row.attempt_count += 1
                row.started_at = now
                row.updated_at = now
                await db.commit()
        if requires_attention:
            await events.emit_event(
                self.session_factory,
                run_id=self.lease.run_id,
                event_type="tool.requires_attention",
                payload={
                    "run_id": self.lease.run_id,
                    "turn_id": self.lease.turn_id,
                    "tool_call_id": call_id,
                    "tool_name": call["name"],
                    "code": "TOOL_RESULT_UNKNOWN",
                },
                lease=self.lease,
                model_response_id=self._model_response_id,
            )
            raise ToolRequiresAttention(
                f"tool call {call_id} may have completed before the worker crashed"
            )
        await events.emit_event(
            self.session_factory,
            run_id=self.lease.run_id,
            event_type="tool.started",
            payload={
                "run_id": self.lease.run_id,
                "turn_id": self.lease.turn_id,
                "tool_call_id": call_id,
                "tool_name": call["name"],
                "call_index": self._call_order.get(call_id, 0),
            },
            lease=self.lease,
            model_response_id=self._model_response_id,
        )
        return None

    async def after(self, call: dict, context, observation: Observation) -> None:
        call_id = call["id"]
        status = _STATUS_MAP.get(observation.status, "failed")
        async with self.session_factory() as db:
            run = await lifecycle.assert_run_ownership(
                db,
                self.lease.run_id,
                worker_id=self.lease.worker_id,
                lease_generation=self.lease.lease_generation,
            )
            turn = await db.get(Turn, run.turn_id)
            row = (
                await db.execute(
                    select(ToolExecution).where(
                        ToolExecution.run_id == run.id,
                        ToolExecution.tool_call_id == call_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ToolExecution(
                    run_id=run.id,
                    model_response_id=self._model_response_id,
                    tool_batch_id=self._tool_batch_id,
                    tool_call_id=call_id,
                    call_index=self._call_order.get(call_id, 0),
                    tool_name=call["name"],
                    tool_version=call.get("version", "v1"),
                    arguments_json=call.get("args") or {},
                    arguments_hash=_value_hash(call.get("args") or {}),
                    status=status,
                    attempt_count=1,
                    max_attempts=self.max_attempts,
                    created_at=utc_now(),
                )
                db.add(row)
            now = utc_now()
            row.status = status
            row.result_json = observation.model_dump()
            row.result_hash = _value_hash(observation.model_dump())
            row.error_code = observation.code
            row.error_message = observation.message
            row.finished_at = now
            row.updated_at = now
            await store.project_run_message(
                db,
                run=run,
                turn=turn,
                role="tool",
                content=observation.model_dump_json(),
                source_key=f"tool:{call_id}",
                tool_call_id=call_id,
                tool_name=call["name"],
            )
            run = await events.lock_run(db, run.id, lease=self.lease)
            event_type = (
                "tool.completed"
                if status == "succeeded"
                else "tool.requires_attention"
                if status == "unknown"
                else "tool.failed"
            )
            await events.append_event(
                db,
                run=run,
                event_type=event_type,
                payload={
                    "run_id": run.id,
                    "turn_id": run.turn_id,
                    "tool_call_id": call_id,
                    "tool_name": call["name"],
                    "call_index": self._call_order.get(call_id, 0),
                    "status": status,
                    "code": observation.code,
                },
                lease=self.lease,
                model_response_id=self._model_response_id,
            )
            await db.commit()
        if status == "unknown":
            raise ToolRequiresAttention(
                f"tool call {call_id} has an unknown external result"
            )
