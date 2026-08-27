"""
最薄的一层：把"发一次带 tools 的对话请求"封装成一个函数。
不做任何循环、不做任何工具执行——那些逻辑都在 agent.py 里，
这样面试时你可以清楚地说："这一层只负责 HTTP，agent 的大脑在别处"。
"""
import requests

import config


class LLMError(RuntimeError):
    """DeepSeek API 返回非 200 或响应格式不符合预期时抛出"""


def call_llm(messages: list[dict], tools: list[dict]) -> dict:
    """
    发送一次 chat completion 请求（非流式，简单可靠，足够 demo 用）。

    参数:
        messages: OpenAI 格式的消息列表
        tools:    OpenAI 格式的 tool schema 列表

    返回:
        本轮模型返回的 assistant message（dict），
        可能包含 "content"（文本）和/或 "tool_calls"（工具调用请求）
    """
    url = f"{config.API_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.get_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.MODEL_NAME,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
    except requests.RequestException as e:
        raise LLMError(f"请求 DeepSeek API 失败（网络层）：{e}") from e

    if resp.status_code != 200:
        raise LLMError(
            f"DeepSeek API 返回错误状态码 {resp.status_code}：{resp.text[:500]}"
        )

    try:
        data = resp.json()
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"无法解析 DeepSeek API 响应：{e}\n原始响应：{resp.text[:500]}") from e

    return message
