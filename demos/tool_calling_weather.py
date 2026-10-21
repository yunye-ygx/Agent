"""最小 Tool Calling Demo：不使用 Agent 框架。

运行真实模型：
    $env:LLM_API_KEY = "your-api-key"
    python demos/tool_calling_weather.py

离线观察完整链路：
    python demos/tool_calling_weather.py --mock

这个 Demo 使用 OpenAI Chat Completions 兼容格式。
它把两件容易混淆的东西并排放在这里：

1. get_weather：Python 真正执行的函数。
2. WEATHER_TOOL_SCHEMA：发送给 LLM 的工具描述。

LLM 只会返回 tool_call 请求；真正调用 get_weather 的是本文件中的
dispatch_tool，而不是 LLM。
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# -----------------------------
# 1. 真正执行工具的 Python 函数
# -----------------------------

def get_weather(city: str) -> dict[str, Any]:
    """返回固定天气数据，先不引入真实天气 API。"""

    normalized_city = city.strip()
    if not normalized_city:
        raise ValueError("city 不能为空")

    # 这里故意使用固定数据，把网络 API 细节留到下一步学习。
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


# ----------------------------------------
# 2. 发给 LLM 的工具描述，不是 Python 函数本身
# ----------------------------------------

WEATHER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气。只有用户明确提供城市后才能调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "要查询天气的城市名称，例如：北京、上海",
                }
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_weather": get_weather,
}


def dispatch_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """根据 LLM 返回的工具名分发到本地 Python 函数。"""

    tool = TOOL_REGISTRY.get(tool_name)
    if tool is None:
        raise ValueError(f"不允许调用工具：{tool_name}")
    return tool(**arguments)


def dump(label: str, value: Any) -> None:
    """用统一格式打印中间状态，方便观察消息协议。"""

    print(f"\n{'=' * 18} {label} {'=' * 18}")
    print(json.dumps(value, ensure_ascii=False, indent=2))


# -----------------------------
# 3. 一个真实的 OpenAI-compatible LLM 客户端
# -----------------------------

class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        request_body = {
            "model": self.model,
            "messages": messages,
            "tools": [WEATHER_TOOL_SCHEMA],
            "tool_choice": "auto",
            "temperature": 0,
        }
        dump("发送给 LLM 的 HTTP JSON", request_body)

        with httpx.Client(base_url=self.base_url, timeout=30) as client:
            response = client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()

        dump("LLM 原始 HTTP 响应", payload)
        return payload


# ---------------------------------------
# 4. 离线模型：只模拟 LLM，不模拟工具执行
# ---------------------------------------

class MockLLM:
    """模拟模型的两次响应，让 Demo 不配置 API Key 也能跑通。"""

    def __init__(self) -> None:
        self.call_count = 0

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.call_count += 1
        dump(
            f"Mock LLM 第 {self.call_count} 次收到的 messages + tools",
            {"messages": messages, "tools": [WEATHER_TOOL_SCHEMA]},
        )

        user_question = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        city = next((candidate for candidate in ("北京", "上海") if candidate in user_question), None)
        has_tool_result = any(message.get("role") == "tool" for message in messages)

        if not has_tool_result and city is None:
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "请问你想查询哪个城市的天气？",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        elif not has_tool_result:
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_weather_001",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": json.dumps({"city": city}, ensure_ascii=False),
                                        },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        else:
            tool_result_message = next(
                message for message in messages if message.get("role") == "tool"
            )
            tool_result = json.loads(tool_result_message["content"])
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": (
                                f"{tool_result['city']}今天{tool_result['condition']}，"
                                f"气温 {tool_result['temperature']}°C，"
                                f"湿度 {tool_result['humidity']}%。"
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

        dump("Mock LLM 返回的响应", payload)
        return payload


def first_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError(f"LLM 响应缺少 choices：{response}")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError(f"LLM 响应缺少 message：{response}")
    return message


def run_agent(user_question: str, llm: OpenAICompatibleLLM | MockLLM) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "你是一个天气助手。需要城市时先向用户追问，不要猜城市。"},
        {"role": "user", "content": user_question},
    ]
    dump("用户问题 + 初始 messages", messages)

    # 允许一次模型响应产生多个 tool_call，因此这里使用循环。
    for round_number in range(1, 4):
        print(f"\n>>> Agent Loop 第 {round_number} 轮")
        response = llm.complete(messages)
        assistant_message = first_message(response)
        tool_calls = assistant_message.get("tool_calls") or []

        if not tool_calls:
            final_text = assistant_message.get("content") or "LLM 没有返回文本。"
            dump("Agent 最终输出", final_text)
            return final_text

        # 必须把模型原样的 assistant tool_calls 消息放回上下文。
        messages.append(assistant_message)
        dump("追加回 messages 的 assistant tool_call", assistant_message)

        for tool_call in tool_calls:
            tool_call_id = tool_call["id"]
            function = tool_call["function"]
            tool_name = function["name"]
            raw_arguments = function["arguments"]
            dump(
                "Python 解析出的 tool_call",
                {"id": tool_call_id, "name": tool_name, "arguments": raw_arguments},
            )

            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON object")
                tool_result = dispatch_tool(tool_name, arguments)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                arguments = {"raw": raw_arguments}
                tool_result = {"error": str(exc)}

            print(f"Python 正在真正执行：{tool_name}(**{arguments})")
            dump("Python 工具函数返回值", tool_result)

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(tool_result, ensure_ascii=False),
            }
            messages.append(tool_message)
            dump("追加回 messages 的 tool result", tool_message)

    raise RuntimeError("超过最大 Agent Loop 次数，可能发生了工具调用失控。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="观察最小 Tool Calling 流程")
    parser.add_argument("question", nargs="?", help="可选：单次执行的问题；不传则进入交互模式")
    parser.add_argument("--mock", action="store_true", help="不请求真实 LLM，使用离线模型响应")
    return parser.parse_args()


def interactive_session(llm: OpenAICompatibleLLM | MockLLM) -> None:
    print("已经连接到 Tool Calling Demo。输入问题，输入 exit 或 quit 退出。")
    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return

        if question.lower() in {"exit", "quit", "q"}:
            print("已退出。")
            return
        if not question:
            continue

        try:
            answer = run_agent(question, llm)
            print(f"\n助手：{answer}")
        except httpx.HTTPError as exc:
            print(f"\nLLM HTTP 请求失败：{exc}")
        except (RuntimeError, ValueError) as exc:
            print(f"\nDemo 执行失败：{exc}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    args = parse_args()
    if args.mock:
        llm: OpenAICompatibleLLM | MockLLM = MockLLM()
    else:
        api_key = os.getenv("LLM_API_KEY", "")
        if not api_key:
            raise SystemExit("未配置 LLM_API_KEY。先运行 python demos/tool_calling_weather.py --mock")
        llm = OpenAICompatibleLLM(
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            api_key=api_key,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )

    if args.question:
        run_agent(args.question, llm)
    else:
        interactive_session(llm)


if __name__ == "__main__":
    main()
