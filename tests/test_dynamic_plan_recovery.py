from __future__ import annotations

from demos.dynamic_plan_recovery import (
    ScriptedPlanner,
    build_tool_registry,
    run_task,
)


def test_wrong_plan_is_rejected_before_email_side_effect() -> None:
    sent_emails: list[dict[str, str]] = []
    tools = build_tool_registry(sent_emails=sent_emails)
    planner = ScriptedPlanner()

    result = run_task(
        request="查询订单 A-100 的状态，然后发邮件给同事",
        planner=planner,
        tools=tools,
        max_replans=3,
    )

    assert result.status == "completed"
    assert planner.plan_calls == 2
    assert len(sent_emails) == 1
    assert sent_emails[0]["body"] == "订单 A-100 当前状态：已发货"
    assert any(
        item.kind == "precondition_failed"
        and item.action_id == "send_email"
        for item in result.state.observations
    )


def test_unknown_tool_is_feedback_and_never_executed() -> None:
    sent_emails: list[dict[str, str]] = []
    tools = build_tool_registry(sent_emails=sent_emails)
    planner = ScriptedPlanner(first_tool="send_emails")

    result = run_task(
        request="查询订单 A-100 的状态，然后发邮件给同事",
        planner=planner,
        tools=tools,
        max_replans=1,
    )

    assert result.status == "failed"
    assert result.failure_reason == "planning_retry_exhausted"
    assert sent_emails == []
    assert any(item.kind == "unknown_tool" for item in result.state.observations)


def test_plan_argument_error_is_feedback_not_tool_execution() -> None:
    sent_emails: list[dict[str, str]] = []
    tools = build_tool_registry(sent_emails=sent_emails)
    planner = ScriptedPlanner(first_tool="query_order_with_bad_args")

    result = run_task(
        request="查询订单 A-100 的状态，然后发邮件给同事",
        planner=planner,
        tools=tools,
        max_replans=1,
    )

    assert result.status == "failed"
    assert sent_emails == []
    assert any(
        item.kind == "argument_validation_failed"
        for item in result.state.observations
    )
