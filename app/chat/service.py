"""HTTP-facing projections for chat conversations, turns and runs."""

from __future__ import annotations

from app.business.utils import iso_datetime
from app.chat.contracts import SETTLED_RUN_STATUSES, RunStatus


def message_payload(row):
    return {
        "id": row.id,
        "sequence": row.sequence,
        "turn_id": row.turn_id,
        "run_id": row.run_id,
        "role": row.role,
        "content": row.content,
        "tool_calls": row.tool_calls,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "status": row.status,
        "created_at": iso_datetime(row.created_at),
    }


def conversation_payload(row):
    return {
        "id": row.id,
        "title": row.title,
        "busy": row.active_turn is not None,
        "active_turn_id": row.active_turn,
        "updated_at": iso_datetime(row.updated_at),
    }


def run_payload(row):
    settled = row.status in SETTLED_RUN_STATUSES
    return {
        "run_id": row.id,
        "turn_id": row.turn_id,
        "status": row.status,
        "attempt_no": row.attempt_no,
        "trigger_reason": row.trigger_reason,
        "created_at": iso_datetime(row.created_at),
        "queued_at": iso_datetime(row.queued_at),
        "started_at": iso_datetime(row.started_at),
        "finished_at": iso_datetime(row.finished_at),
        "error": (
            {"code": row.error_code, "message": row.error_message}
            if row.error_code
            else None
        ),
        "can_cancel": row.status
        in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.RECOVERING),
        "can_retry": settled,
        "last_event_seq": max(0, (row.next_event_seq or 1) - 1),
        "events_url": f"/api/v1/chat/runs/{row.id}/events",
    }


def turn_payload(turn, latest_run=None):
    return {
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "turn_no": turn.turn_no,
        "status": turn.status,
        "user_message_id": turn.user_message_id,
        "latest_run_id": turn.latest_run_id,
        "active_run_id": turn.active_run_id,
        "run_count": turn.run_count,
        "created_at": iso_datetime(turn.created_at),
        "closed_at": iso_datetime(turn.closed_at),
        "latest_run": run_payload(latest_run) if latest_run is not None else None,
    }


def submission_payload(submission):
    return {
        "conversation_id": submission.conversation_id,
        "turn_id": submission.turn_id,
        "run_id": submission.run_id,
        "status": submission.status,
        "events_url": f"/api/v1/chat/runs/{submission.run_id}/events",
    }