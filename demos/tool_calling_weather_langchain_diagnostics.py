"""LangChain Tool Calling 故障观测 Demo。

这个 Demo 不请求真实 LLM，而是使用一个确定性的脚本模型，专门观察
LangChain 0.3.x 在不同故障下的行为。

运行交互菜单：
    python demos/tool_calling_weather_langchain_diagnostics.py

直接运行某个场景：
    python demos/tool_calling_weather_langchain_diagnostics.py --case normal
    python demos/tool_calling_weather_langchain_diagnostics.py --case missing-arg-strict
    python demos/tool_calling_weather_langchain_diagnostics.py --case missing-arg-handled
    python demos/tool_calling_weather_langchain_diagnostics.py --case unknown-tool
    python demos/tool_calling_weather_langchain_diagnostics.py --case value-error
    python demos/tool_calling_weather_langchain_diagnostics.py --case tool-exception

需要观察的重点：
    1. 参数 Schema 校验发生在工具函数执行之前。
    2. 未知工具是工具路由错误，不是参数错误。
    3. handle_parsing_errors 只处理 Agent 输出解析错误。
    4. 工具异常是否回到 Agent Loop，取决于工具自己的错误配置。
"""

from __future__ import annotations

import argparse
import json
import traceback
from typing import Any

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder


def weather_result(city: str) -> dict[str, Any]:
    """返回固定天气数据。"""

    return {
        "city": city,
        "temperature": 26,
        "unit": "celsius",
        "condition": "晴",
        "humidity": 45,
    }


@tool
def weather_ok(city: str) -> dict[str, Any]:
    """查询指定城市的固定天气数据。"""

    print(f"[工具函数开始执行] weather_ok(city={city!r})")
    return weather_result(city)


def weather_requires_city(city: str) -> dict[str, Any]:
    """需要 city 参数的工具实现。"""

    print(f"[工具函数开始执行] weather_requires_city(city={city!r})")
    return weather_result(city)


def weather_value_error(city: str) -> dict[str, Any]:
    """模拟工具函数抛出普通 ValueError。"""

    print(f"[工具函数开始执行] weather_value_error(city={city!r})")
    raise ValueError("上游天气服务返回了无法解析的数据")


def weather_tool_exception(city: str) -> dict[str, Any]:
    """模拟可转换为工具 observation 的 ToolException。"""

    print(f"[工具函数开始执行] weather_tool_exception(city={city!r})")
    raise ToolException("上游天气服务暂时不可用")


class ScriptedChatModel(BaseChatModel):
    """按场景返回固定 tool_call，避免错误演示受真实 LLM 随机性影响。"""

    scenario: str
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        """模拟 ChatOpenAI.bind_tools；本 Demo 不需要真的发送 Schema。"""

        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.call_count += 1
        print(f"\n[脚本模型第 {self.call_count} 次被调用]")
        print(f"[脚本模型收到 {len(messages)} 条消息]")

        if self.call_count == 1:
            tool_call = self._first_tool_call()
            response = AIMessage(content="", tool_calls=[tool_call])
        else:
            response = AIMessage(
                content=self._recovery_answer(),
            )

        print("[脚本模型返回]")
        print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2, default=str))
        return ChatResult(generations=[ChatGeneration(message=response)])

    def _first_tool_call(self) -> dict[str, Any]:
        calls = {
            "normal": {
                "name": "weather_ok",
                "args": {"city": "北京"},
                "id": "call-normal-001",
            },
            "missing-arg-strict": {
                "name": "get_weather",
                "args": {},
                "id": "call-missing-001",
            },
            "missing-arg-handled": {
                "name": "get_weather",
                "args": {},
                "id": "call-missing-002",
            },
            "unknown-tool": {
                "name": "query_weather_v2",
                "args": {"city": "北京"},
                "id": "call-unknown-001",
            },
            "value-error": {
                "name": "weather_value_error",
                "args": {"city": "北京"},
                "id": "call-value-error-001",
            },
            "tool-exception": {
                "name": "weather_tool_exception",
                "args": {"city": "北京"},
                "id": "call-tool-exception-001",
            },
        }
        return calls[self.scenario]

    def _recovery_answer(self) -> str:
        answers = {
            "normal": "工具执行成功，返回北京天气。",
            "missing-arg-handled": "工具参数校验失败，模型收到了错误 observation。",
            "unknown-tool": "工具名称错误，模型收到了可用工具列表。",
            "tool-exception": "工具执行失败，但异常被转换成了 observation。",
        }
        return answers.get(self.scenario, "脚本模型没有为这个场景提供第二次回答。")


def strict_weather_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=weather_requires_city,
        name="get_weather",
        description="查询天气，city 是必填字符串参数。",
    )


def handled_weather_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=weather_requires_city,
        name="get_weather",
        description="查询天气，city 是必填字符串参数。",
        handle_validation_error=True,
    )


def value_error_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=weather_value_error,
        name="weather_value_error",
        description="模拟普通 ValueError。",
    )


def tool_exception_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=weather_tool_exception,
        name="weather_tool_exception",
        description="模拟可转换为工具 observation 的 ToolException。",
        handle_tool_error=True,
    )


def build_executor(scenario: str) -> AgentExecutor:
    model = ScriptedChatModel(scenario=scenario)

    if scenario == "normal":
        tools = [weather_ok]
    elif scenario == "missing-arg-strict":
        tools = [strict_weather_tool()]
    elif scenario == "missing-arg-handled":
        tools = [handled_weather_tool()]
    elif scenario == "unknown-tool":
        tools = [weather_ok]
    elif scenario == "value-error":
        tools = [value_error_tool()]
    elif scenario == "tool-exception":
        tools = [tool_exception_tool()]
    else:
        raise ValueError(f"未知场景：{scenario}")

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "你是一个用于错误观测的天气 Agent。"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )
    agent = create_tool_calling_agent(model, tools, prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        max_iterations=3,
        handle_parsing_errors=True,
        return_intermediate_steps=True,
    )


def print_chunk(chunk: dict[str, Any]) -> None:
    print(f"\n{'=' * 18} AgentExecutor 中间状态 {'=' * 18}")

    for action in chunk.get("actions", []):
        print("[AgentAction] 模型请求工具")
        print(f"工具名：{action.tool}")
        print(f"参数：{json.dumps(action.tool_input, ensure_ascii=False)}")
        print(f"日志：{action.log}")

    for step in chunk.get("steps", []):
        print("[AgentStep] 工具 observation")
        print(f"工具名：{step.action.tool}")
        print(f"结果：{step.observation}")

    if "output" in chunk:
        print("[AgentFinish] 最终输出")
        print(chunk["output"])

    if "intermediate_steps" in chunk:
        print(f"[中间步骤总数] {len(chunk['intermediate_steps'])}")


def run_case(scenario: str, show_traceback: bool) -> None:
    questions = {
        "normal": "查询北京天气",
        "missing-arg-strict": "查询天气，但模型漏传 city",
        "missing-arg-handled": "查询天气，但模型漏传 city，并把校验错误反馈给模型",
        "unknown-tool": "模型请求一个不存在的工具",
        "value-error": "工具函数抛出普通 ValueError",
        "tool-exception": "工具函数抛出配置为可恢复的 ToolException",
    }
    print(f"\n######## 场景：{scenario} ########")
    print(f"问题：{questions[scenario]}")

    executor = build_executor(scenario)
    try:
        result: dict[str, Any] = {}
        for chunk in executor.stream({"input": questions[scenario]}):
            print_chunk(chunk)
            result.update(chunk)

        print("\n######## 场景结束 ########")
        print(f"最终结果：{result.get('output')}")
        print(f"intermediate_steps：{len(result.get('intermediate_steps', []))} 条")
    except Exception as exc:
        print("\n######## 场景抛出异常 ########")
        print(f"异常类型：{type(exc).__name__}")
        print(f"异常信息：{exc}")
        if show_traceback:
            traceback.print_exc()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="观察 LangChain Tool Calling 错误边界")
    parser.add_argument(
        "--case",
        choices=[
            "normal",
            "missing-arg-strict",
            "missing-arg-handled",
            "unknown-tool",
            "value-error",
            "tool-exception",
        ],
        help="直接运行指定场景；不传时进入菜单",
    )
    parser.add_argument("--traceback", action="store_true", help="异常时打印完整调用栈")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = [
        "normal",
        "missing-arg-strict",
        "missing-arg-handled",
        "unknown-tool",
        "value-error",
        "tool-exception",
    ]

    if args.case:
        run_case(args.case, args.traceback)
        return

    print("LangChain Tool Calling 故障观测 Demo")
    print("这个 Demo 使用脚本模型，不会请求真实 LLM。")
    for index, scenario in enumerate(cases, start=1):
        print(f"{index}. {scenario}")
    print("q. 退出")

    while True:
        choice = input("\n请选择场景：").strip().lower()
        if choice in {"q", "quit", "exit"}:
            return
        if choice.isdigit() and 1 <= int(choice) <= len(cases):
            run_case(cases[int(choice) - 1], args.traceback)
        else:
            print("请输入菜单编号，或者输入 q 退出。")


if __name__ == "__main__":
    main()
