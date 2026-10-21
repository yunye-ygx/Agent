"""动态计划错误恢复 Demo。

这个文件演示生产环境中一个很重要的边界：LLM 可以提出错误计划，
但不能直接获得执行副作用工具的权限。

运行：
    python demos/dynamic_plan_recovery.py

第一次规划会故意先调用 send_email，再调用 query_order。Runtime 会在
发送邮件前发现 order.status 尚未存在，把错误写成 observation，然后把
当前状态交回 Planner。第二次规划改为先查订单，再发送邮件。

这里的 ScriptedPlanner 只用于让错误稳定复现；真实项目中可以把它替换
成调用智谱/OpenAI 兼容接口的 Planner，Runtime 不需要因此改变。
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class QueryOrderArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)


class SendEmailArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


class PlanAction(BaseModel):
    """LLM 提出的一个动作；这是我们定义的计划 Schema。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    """LLM 每次重新规划时必须返回的结构。"""

    model_config = ConfigDict(extra="forbid")

    actions: list[PlanAction] = Field(default_factory=list)
    finish: bool = False


@dataclass
class ToolResult:
    data: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """Runtime 需要知道的工具契约，而不是只知道一个 Python 函数。"""

    name: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel], ToolResult]
    required_facts: frozenset[str] = frozenset()
    side_effect: bool = False


@dataclass
class Observation:
    kind: str
    message: str
    action_id: str | None = None
    tool: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskState:
    request: str
    facts: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_actions: set[str] = field(default_factory=set)
    observations: list[Observation] = field(default_factory=list)

    def planner_context(self) -> dict[str, Any]:
        """生成下一次规划需要看到的状态；不把 Python 对象直接塞给 LLM。"""

        return {
            "request": self.request,
            "facts": self.facts,
            "completed_actions": sorted(self.completed_actions),
            "outputs": self.outputs,
            "observations": [asdict(item) for item in self.observations],
        }


@dataclass
class RunResult:
    status: str
    state: TaskState
    plan_calls: int
    failure_reason: str | None = None


class Planner(Protocol):
    def plan(self, context: dict[str, Any]) -> Any:
        """返回 LLM 原始结构；Runtime 会再次做 Pydantic 校验。"""

    def final_answer(self, context: dict[str, Any]) -> str:
        """根据已经执行的结果生成最终回答。"""


class PlanningIssue(Exception):
    def __init__(self, observation: Observation) -> None:
        super().__init__(observation.message)
        self.observation = observation


class ScriptedPlanner:
    """确定性模拟 LLM，专门用于复现错误规划和修正规划。"""

    def __init__(self, first_tool: str = "send_email") -> None:
        self.first_tool = first_tool
        self.plan_calls = 0

    def plan(self, context: dict[str, Any]) -> dict[str, Any]:
        self.plan_calls += 1
        print(f"\n[Planner 第 {self.plan_calls} 次调用]")
        print("Planner 收到的状态：")
        print(json.dumps(context, ensure_ascii=False, indent=2))

        if self.first_tool == "send_emails":
            return {
                "actions": [
                    {
                        "id": "send_email",
                        "tool": "send_emails",
                        "arguments": {"recipient": "colleague@example.com"},
                    }
                ]
            }

        if self.first_tool == "query_order_with_bad_args":
            return {
                "actions": [
                    {
                        "id": "query_order",
                        "tool": "query_order",
                        "arguments": {"order_id": 100},
                    }
                ]
            }

        if self.plan_calls == 1:
            # 故意制造错误：副作用工具在 order.status 产生前执行。
            return {
                "actions": [
                    {
                        "id": "send_email",
                        "tool": "send_email",
                        "arguments": {
                            "recipient": "colleague@example.com",
                            "subject": "订单状态更新",
                            "body": "订单状态未知，但请先通知同事。",
                        },
                    },
                    {
                        "id": "query_order",
                        "tool": "query_order",
                        "arguments": {"order_id": "A-100"},
                    },
                ]
            }

        # observation 告诉模型 order.status 缺失后，模型重新规划并声明依赖。
        return {
            "finish": True,
            "actions": [
                {
                    "id": "query_order",
                    "tool": "query_order",
                    "arguments": {"order_id": "A-100"},
                },
                {
                    "id": "send_email",
                    "tool": "send_email",
                    "depends_on": ["query_order"],
                    "arguments": {
                        "recipient": "colleague@example.com",
                        "subject": "订单状态更新",
                        "body": "订单 A-100 当前状态：${order.status}",
                    },
                },
            ]
        }

    def final_answer(self, context: dict[str, Any]) -> str:
        status = context["facts"].get("order.status", "未知")
        return f"订单 A-100 的状态是：{status}，并已发送邮件给同事。"


def build_tool_registry(
    sent_emails: list[dict[str, str]] | None = None,
) -> dict[str, ToolSpec]:
    """构造工具注册表；真实服务中 handler 会调用数据库或外部 API。"""

    email_log = sent_emails if sent_emails is not None else []

    def query_order(arguments: QueryOrderArguments) -> ToolResult:
        print(f"[Tool 执行] query_order(order_id={arguments.order_id!r})")
        return ToolResult(
            data={"order_id": arguments.order_id, "status": "已发货"},
            facts={
                "order.id": arguments.order_id,
                "order.status": "已发货",
            },
        )

    def send_email(arguments: SendEmailArguments) -> ToolResult:
        print(f"[Tool 执行] send_email(recipient={arguments.recipient!r})")
        email_log.append(arguments.model_dump())
        return ToolResult(
            data={"message_id": "message-001", "recipient": arguments.recipient},
            facts={"email.sent": True},
        )

    return {
        "query_order": ToolSpec(
            name="query_order",
            arguments_model=QueryOrderArguments,
            handler=query_order,
        ),
        "send_email": ToolSpec(
            name="send_email",
            arguments_model=SendEmailArguments,
            handler=send_email,
            required_facts=frozenset({"order.status"}),
            side_effect=True,
        ),
    }


def _resolve_references(value: Any, facts: dict[str, Any]) -> Any:
    """解析 ${fact.name}；引用不存在时拒绝调用，不把未解析模板传给工具。"""

    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in facts:
                raise KeyError(key)
            return str(facts[key])

        return pattern.sub(replace, value)

    if isinstance(value, dict):
        return {key: _resolve_references(item, facts) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_references(item, facts) for item in value]
    return value


def _validate_plan_structure(plan: TaskPlan, tools: dict[str, ToolSpec]) -> None:
    action_ids = [action.id for action in plan.actions]
    if len(action_ids) != len(set(action_ids)):
        raise PlanningIssue(
            Observation(
                kind="duplicate_action_id",
                message="计划包含重复 action id，无法建立可靠的执行状态。",
            )
        )

    known_ids = set(action_ids)
    for action in plan.actions:
        if action.tool not in tools:
            raise PlanningIssue(
                Observation(
                    kind="unknown_tool",
                    message=f"工具 {action.tool!r} 不在 Runtime 工具注册表中。",
                    action_id=action.id,
                    tool=action.tool,
                )
            )
        missing_dependencies = set(action.depends_on) - known_ids
        if missing_dependencies:
            raise PlanningIssue(
                Observation(
                    kind="unknown_dependency",
                    message=f"动作 {action.id!r} 依赖不存在的动作。",
                    action_id=action.id,
                    tool=action.tool,
                    details={"missing_dependencies": sorted(missing_dependencies)},
                )
            )


def _execute_plan(plan: TaskPlan, state: TaskState, tools: dict[str, ToolSpec]) -> None:
    pending = {action.id: action for action in plan.actions}

    while pending:
        progress = False
        for action_id, action in list(pending.items()):
            if not set(action.depends_on).issubset(state.completed_actions):
                continue

            spec = tools[action.tool]
            try:
                arguments = spec.arguments_model.model_validate(action.arguments)
            except ValidationError as exc:
                raise PlanningIssue(
                    Observation(
                        kind="argument_validation_failed",
                        message=f"工具 {action.tool!r} 的参数没有通过 Schema 校验。",
                        action_id=action.id,
                        tool=action.tool,
                        details={"errors": exc.errors(include_url=False)},
                    )
                ) from exc

            missing_facts = sorted(spec.required_facts - state.facts.keys())
            if missing_facts:
                raise PlanningIssue(
                    Observation(
                        kind="precondition_failed",
                        message=(
                            f"禁止执行工具 {action.tool!r}：运行时事实尚未满足前置条件。"
                        ),
                        action_id=action.id,
                        tool=action.tool,
                        details={"missing_facts": missing_facts},
                    )
                )

            try:
                resolved_arguments = _resolve_references(
                    arguments.model_dump(), state.facts
                )
                arguments = spec.arguments_model.model_validate(resolved_arguments)
            except (KeyError, ValidationError) as exc:
                details: dict[str, Any] = {}
                if isinstance(exc, KeyError):
                    details["missing_fact_reference"] = exc.args[0]
                else:
                    details["errors"] = exc.errors(include_url=False)
                raise PlanningIssue(
                    Observation(
                        kind="argument_reference_failed",
                        message="计划引用了当前状态中不存在的事实。",
                        action_id=action.id,
                        tool=action.tool,
                        details=details,
                    )
                ) from exc

            try:
                result = spec.handler(arguments)
            except Exception as exc:
                raise PlanningIssue(
                    Observation(
                        kind="tool_execution_failed",
                        message=f"工具 {action.tool!r} 执行异常，未把异常伪装成成功。",
                        action_id=action.id,
                        tool=action.tool,
                        details={"exception_type": type(exc).__name__},
                    )
                ) from exc

            state.outputs[action.id] = result.data
            state.facts.update(result.facts)
            state.completed_actions.add(action.id)
            del pending[action_id]
            progress = True

        if pending and not progress:
            raise PlanningIssue(
                Observation(
                    kind="dependency_deadlock",
                    message="计划中剩余动作没有可执行项，可能存在循环依赖。",
                    details={"pending_actions": sorted(pending)},
                )
            )


def run_task(
    request: str,
    planner: Planner,
    tools: dict[str, ToolSpec],
    max_replans: int = 3,
) -> RunResult:
    """执行“规划 -> 校验 -> 工具 -> observation -> 重新规划”闭环。"""

    state = TaskState(request=request)

    for _ in range(max_replans + 1):
        try:
            raw_plan = planner.plan(state.planner_context())
            print("Planner 返回的原始计划：")
            print(json.dumps(raw_plan, ensure_ascii=False, indent=2))
            plan = TaskPlan.model_validate(raw_plan)
            _validate_plan_structure(plan, tools)
            _execute_plan(plan, state, tools)
        except ValidationError as exc:
            observation = Observation(
                kind="plan_schema_invalid",
                message="Planner 返回的数据不符合 TaskPlan Schema。",
                details={"errors": exc.errors(include_url=False)},
            )
            state.observations.append(observation)
            print(f"[Runtime 拒绝计划] {observation.message}")
            continue
        except PlanningIssue as exc:
            state.observations.append(exc.observation)
            print(f"[Runtime 拒绝或中断] {exc.observation.message}")
            print("[Runtime -> Planner] observation：")
            print(json.dumps(asdict(exc.observation), ensure_ascii=False, indent=2))
            continue

        if not plan.finish:
            print("[Runtime] 本轮动作成功，但 Planner 尚未声明目标完成，继续规划。")
            continue

        answer = planner.final_answer(state.planner_context())
        print(f"[最终回答] {answer}")
        return RunResult(
            status="completed",
            state=state,
            plan_calls=getattr(planner, "plan_calls", 0),
        )

    return RunResult(
        status="failed",
        state=state,
        plan_calls=getattr(planner, "plan_calls", 0),
        failure_reason="planning_retry_exhausted",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="动态计划错误恢复 Demo")
    parser.add_argument(
        "--case",
        choices=["wrong-order", "unknown-tool", "bad-args"],
        default="wrong-order",
        help="选择一个错误计划场景",
    )
    args = parser.parse_args()

    first_tool = {
        "wrong-order": "send_email",
        "unknown-tool": "send_emails",
        "bad-args": "query_order_with_bad_args",
    }[args.case]
    result = run_task(
        request="查询订单 A-100 的状态，然后发邮件给同事",
        planner=ScriptedPlanner(first_tool=first_tool),
        tools=build_tool_registry(),
    )
    print("\n=== 运行结果 ===")
    print(f"status={result.status}")
    print(f"plan_calls={result.plan_calls}")
    print(f"completed_actions={sorted(result.state.completed_actions)}")
    print(f"observations={len(result.state.observations)}")
    if result.failure_reason:
        print(f"failure_reason={result.failure_reason}")


if __name__ == "__main__":
    main()
