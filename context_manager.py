"""
对话历史管理。

设计取舍（面试时可以直接讲）：
- 不做语义摘要（省成本、省时间），而是做"结构化裁剪"：
  永远保留 [0] 系统提示 + [1] 最初的用户任务，
  这两条锚定了"agent 是谁、要干什么"；
  中间超出上限的部分整体丢弃，插入一条系统提示告知模型"早期历史被裁剪"，
  避免模型产生"我之前做过这件事"的幻觉；
  始终保留最近 N 条消息，因为最近的上下文对下一步决策最重要。
- 每条工具返回结果在写入前已经在 tools.py 里做过长度截断，
  这里只处理"消息条数"维度的裁剪。
"""
import config


class ConversationContext:
    def __init__(self, system_prompt: str, user_task: str):
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self._trimmed_once = False

    def add_assistant_message(self, message: dict) -> None:
        # message 可能同时带 content 和 tool_calls，原样保留，因为回传给 API
        # 时需要完整的 assistant 消息（包括 tool_calls）才能后续正确关联 tool 结果
        entry = {"role": "assistant"}
        if message.get("content") is not None:
            entry["content"] = message["content"]
        else:
            entry["content"] = ""
        if message.get("tool_calls"):
            entry["tool_calls"] = message["tool_calls"]
        self.messages.append(entry)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def get_messages(self) -> list[dict]:
        self._trim_if_needed()
        return self.messages

    def _trim_if_needed(self) -> None:
        if len(self.messages) <= config.MAX_HISTORY_MESSAGES:
            return

        anchor = self.messages[:2]  # system + 最初任务
        tail_len = config.MAX_HISTORY_MESSAGES - 3  # 留一条给裁剪提示
        tail = self.messages[-tail_len:]

        notice = {
            "role": "system",
            "content": "[注意：更早的对话历史已被裁剪以控制上下文长度，只保留了最初任务和最近的消息]",
        }
        self.messages = anchor + [notice] + tail
