"""LangChain 版本的天气 Tool Calling Demo。

安装框架依赖：
    python -m pip install -e ".[agent]"

运行：
    python demos/tool_calling_weather_langchain.py

这里固定使用 LangChain 0.3.27 这一条版本线，原因是当前项目中的
MinerU 依赖 openai<3；先保持依赖兼容，暂不混入 LangChain 1.x 的 API。

这个文件故意打印 AgentExecutor 的中间步骤，方便和
tool_calling_weather.py 的手写 Agent Loop 对照。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


@tool
def get_weather(city: str) -> dict[str, Any]:
    """查询指定城市的当前天气。只有用户明确提供城市后才能调用。"""

    print(f"\n[框架正在执行 Python 工具] get_weather(city={city!r})")

    normalized_city = city.strip()
    if not normalized_city:
        raise ValueError("city 不能为空")

    weather_by_city = {
        "北京": {
            "city": "北京",
            "temperature": 26,
            "unit": "celsius",
            "condition": "晴",
            "humidity": 45,
        },
        "上海": {
            "city": "上海",
            "temperature": 28,
            "unit": "celsius",
            "condition": "多云",
            "humidity": 68,
        },
    }

    return weather_by_city.get(
        normalized_city,
        {
            "city": normalized_city,
            "temperature": 24,
            "unit": "celsius",
            "condition": "晴",
            "humidity": 50,
        },
    )


def build_agent() -> AgentExecutor:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise SystemExit("未配置 LLM_API_KEY，请检查项目根目录下的 .env 文件。")

    model = ChatOpenAI(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        temperature=0,
        timeout=30,
        max_retries=0,
    )

    tools = [get_weather]
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是一个天气助手。需要城市时先向用户追问，不要猜城市。",
            ),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    # create_tool_calling_agent 负责创建“模型 + 工具调用协议”的 Agent。
    # AgentExecutor 负责执行 Agent Loop、调用工具、回填工具结果并继续请求模型。
    agent = create_tool_calling_agent(model, tools, prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        max_iterations=3,
        handle_parsing_errors=True,
    )


def print_framework_chunk(chunk: dict[str, Any]) -> None:
    """打印 AgentExecutor 流程中的一个中间结果。"""

    print(f"\n{'=' * 18} LangChain 中间状态 {'=' * 18}")

    if "actions" in chunk:
        for action in chunk["actions"]:
            print("模型请求调用工具：")
            print(f"  工具名：{action.tool}")
            print(f"  参数：{json.dumps(action.tool_input, ensure_ascii=False)}")

    if "steps" in chunk:
        for step in chunk["steps"]:
            print("工具执行结果：")
            print(f"  工具名：{step.action.tool}")
            print(f"  结果：{step.observation}")

    if "output" in chunk:
        print(f"模型最终回答：{chunk['output']}")

    if not any(key in chunk for key in ("actions", "steps", "output")):
        print(json.dumps(chunk, ensure_ascii=False, indent=2, default=str))


def run_once(agent: AgentExecutor, question: str) -> str:
    print(f"\n用户问题：{question}")
    final_output = ""

    for chunk in agent.stream({"input": question}):
        print_framework_chunk(chunk)
        if "output" in chunk:
            final_output = str(chunk["output"])

    return final_output


def main() -> None:
    agent = build_agent()
    print("LangChain Tool Calling Demo 已启动。输入 exit 或 quit 退出。")

    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return

        if question.lower() in {"exit", "quit", "q"}:
            print("已退出。")
            return
        if question:
            run_once(agent, question)


if __name__ == "__main__":
    main()
