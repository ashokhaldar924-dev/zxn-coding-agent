"""
一个最小可用的编程 agent：读写文件、执行命令，自主完成编程任务。

核心循环（这是整个项目的心脏，面试会重点问这里）：
    1. 把当前对话历史 + 工具 schema 发给 DeepSeek
    2. 模型要么直接给文本回答（视为"完成"），要么返回 tool_calls
    3. 如果是 tool_calls：本地执行对应工具，把结果作为 role="tool" 消息塞回历史
    4. 回到第 1 步，直到模型给出纯文本回答，或达到最大迭代次数

用法：
    export DEEPSEEK_API_KEY=sk-xxxx
    python agent.py                       # 进入交互模式
    python agent.py "帮我写一个冒泡排序"      # 直接给一个任务
"""
import json
import sys

import config
import llm_client
import tools
from context_manager import ConversationContext


SYSTEM_PROMPT = f"""你是一个可以直接操作文件系统和执行命令的编程助手（coding agent）。

你的工作区根目录是：{config.WORKSPACE_DIR}
所有文件路径都请使用相对于工作区根目录的相对路径。

可用工具：read_file, write_file, list_dir, search_text, run_command。
- 修改代码前，建议先用 read_file / list_dir / search_text 了解现状，不要凭空猜测文件内容。
- 执行命令（比如运行测试）来验证你的修改是否正确，不要假设代码能跑。
- 如果一个工具调用返回了错误信息，请根据错误信息调整后重试，而不是重复同样的调用。
- write_file 和 run_command 执行前会给用户展示 diff / 命令内容并请求确认，用户可能会拒绝；
  如果工具结果显示"用户拒绝了..."，这不是错误，请据此调整方案（比如换一种更小的改动再试，
  或直接询问用户希望怎么做），不要机械地重复同一个调用。
- 当你已经完成用户交给你的任务时，直接输出一段纯文本总结你做了什么，
  不要再调用任何工具——这段纯文本回答会被视为"任务完成"的信号。
"""


def _execute_tool_call(tool_call: dict) -> str:
    """执行单个 tool_call，返回要塞回给模型的字符串结果（成功或错误信息都是字符串）"""
    name = tool_call["function"]["name"]
    raw_args = tool_call["function"].get("arguments", "{}")

    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return f"错误：无法解析工具参数 JSON：{e}。原始参数：{raw_args}"

    func = tools.TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"错误：未知工具 '{name}'，可用工具有：{list(tools.TOOL_FUNCTIONS.keys())}"

    try:
        return func(args)
    except Exception as e:  # noqa: BLE001 - 任何工具内部异常都要喂回模型，而不是让 agent 崩溃
        return f"错误：执行工具 '{name}' 时出现异常：{type(e).__name__}: {e}"


def run_task(context: ConversationContext) -> str:
    """
    跑一轮完整的"任务解决循环"，直到模型给出纯文本回答或达到最大迭代次数。
    返回模型的最终文本回答。
    """
    for iteration in range(1, config.MAX_ITERATIONS + 1):
        print(f"\n--- 第 {iteration} 轮 ---")
        message = llm_client.call_llm(context.get_messages(), tools.TOOL_SCHEMAS)
        context.add_assistant_message(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            final_text = message.get("content", "") or "(模型未返回内容)"
            print(f"[模型] {final_text}")
            return final_text

        for call in tool_calls:
            fname = call["function"]["name"]
            fargs = call["function"].get("arguments", "{}")
            print(f"[工具调用] {fname}({fargs})")
            result = _execute_tool_call(call)
            print(f"[工具结果] {result[:300]}{'...' if len(result) > 300 else ''}")
            context.add_tool_result(call["id"], result)

    warning = f"（已达到最大迭代次数 {config.MAX_ITERATIONS}，强制停止，任务可能未完全完成）"
    print(f"\n[系统] {warning}")
    return warning


def main() -> None:
    config.get_api_key()  # 提前校验，key 缺失时尽早报错而不是等到第一次网络请求

    if len(sys.argv) > 1:
        first_task = " ".join(sys.argv[1:])
    else:
        print(f"工作区目录：{config.WORKSPACE_DIR}")
        first_task = input("请输入你的编程任务：\n> ")

    context = ConversationContext(SYSTEM_PROMPT, first_task)
    run_task(context)

    # 交互模式：任务完成后允许继续追加指令，复用同一份上下文（体现"多轮对话"能力）
    while True:
        try:
            follow_up = input("\n继续输入指令（回车/exit 退出）：\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not follow_up or follow_up.lower() in ("exit", "quit"):
            break
        context.add_user_message(follow_up)
        run_task(context)


if __name__ == "__main__":
    try:
        main()
    except (llm_client.LLMError, RuntimeError) as e:
        print(f"\n[致命错误] {e}")
        sys.exit(1)
