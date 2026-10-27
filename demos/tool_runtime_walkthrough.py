"""Run with: python -X utf8 -m demos.tool_runtime_walkthrough --case invalid-arguments

Model calls and business data are explicit fixtures, not real model responses or
real customer orders. Validation, execution, retries and timeouts use the actual
app.tool_runtime implementation. No credentials, database or network required.
"""

import argparse
import asyncio
import json

from pydantic import Field

from app.tool_runtime import (
    ExecutionContext,
    RetryableToolError,
    ToolArguments,
    ToolCall,
    ToolRuntime,
    ToolSpec,
)


class OrderArguments(ToolArguments):
    order_id: str = Field(min_length=1)


CASES = {
    "success": "合法查询：执行工具并返回数据",
    "invalid-arguments": "参数错误：order_id 传了数字，工具不应执行",
    "forbidden": "权限不足：用户没有查询权限，工具不应执行",
    "unknown-tool": "工具名错误：拒绝未知工具",
    "retry": "第一次发生暂时故障：执行器重试一次后成功",
    "read-timeout": "查询一直没有返回：两次尝试后报告超时",
    "write-timeout": "写入已成功但响应迟到：报告结果未知，不自动重写",
}


async def run_case(case: str):
    print(f"\n场景：{CASES[case]}")
    attempts = []
    written_operations = []

    async def handler(args, context):
        attempts.append(1)
        print(f"  [真正进入工具] 第 {len(attempts)} 次执行")
        if case == "retry" and len(attempts) == 1:
            raise RetryableToolError("simulated upstream unavailable")
        if case == "write-timeout":
            # A test fixture models an already-committed external write. The
            # timeout cancels local waiting, but does not undo this append.
            written_operations.append(context.operation_id)
            print("  [模拟业务服务] 已创建工单 T100，尚未返回响应")
        if case in {"read-timeout", "write-timeout"}:
            await asyncio.sleep(10)
        return {"order_id": args.order_id, "status": "shipped", "source": "fixture"}

    writing = case == "write-timeout"
    name = "create_ticket" if writing else "query_order"
    permission = "tickets:create" if writing else "orders:read"
    runtime = ToolRuntime([
        ToolSpec(
            name=name,
            arguments_model=OrderArguments,
            handler=handler,
            permission=permission,
            authorize=lambda args, ctx: args.order_id == "A-100" and ctx.user_id == "user-1",
            effect="write" if writing else "read",
            timeout_seconds=0.05,  # Short on purpose so the demonstration is quick.
            max_attempts=2,
            retry_delay_seconds=0.01,
        )
    ])
    request = ToolCall(
        call_id="model-call-1",
        name="not_registered" if case == "unknown-tool" else name,
        arguments={"order_id": 100 if case == "invalid-arguments" else "A-100"},
    )
    trusted = ExecutionContext(
        user_id="user-1",
        permissions=frozenset() if case == "forbidden" else frozenset({permission}),
        operation_id="backend-operation-1" if writing else None,
    )
    print("  [模拟 LLM 输出] " + request.model_dump_json())
    result = await runtime.execute(request, trusted)
    print("  [Runtime -> LLM 的 observation]")
    print(json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False, indent=2))
    print(f"  实际工具执行次数：{len(attempts)}")
    if writing:
        print(f"  模拟业务写入次数：{len(written_operations)}；不是零次，也没有自动重试")
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[*CASES, "all"], default="success")
    args = parser.parse_args()
    for case in CASES if args.case == "all" else [args.case]:
        await run_case(case)


if __name__ == "__main__":
    asyncio.run(main())
