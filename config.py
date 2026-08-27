"""
全局配置。所有敏感信息（API key）一律从环境变量读取，
绝不写入代码或提交到仓库。

使用方式：
    export DEEPSEEK_API_KEY="sk-xxxxxxxx"
    python agent.py
"""
import os


def get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(
            "未找到环境变量 DEEPSEEK_API_KEY。\n"
            "请先执行：export DEEPSEEK_API_KEY=你的key\n"
            "（Windows PowerShell: $env:DEEPSEEK_API_KEY=\"你的key\"）"
        )
    return key


# DeepSeek 是 OpenAI 兼容网关，直接用标准 chat/completions 接口即可
API_BASE_URL = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# --- agent 循环的可调参数，都留了默认值但允许覆盖，方便你在面试时演示"这个策略是可配置的" ---

# 防止模型陷入死循环，超过这个轮数强制停止
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "20"))

# 单次工具输出（比如 run_command 的 stdout、read_file 的内容）超过这个长度就截断，
# 防止一次读一个大文件把整个上下文窗口撑爆
MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("AGENT_MAX_TOOL_OUTPUT_CHARS", "4000"))

# 对话历史消息条数上限，超过后做裁剪（见 context_manager.py 的策略）
MAX_HISTORY_MESSAGES = int(os.environ.get("AGENT_MAX_HISTORY_MESSAGES", "40"))

# 单个 shell 命令的超时时间（秒），防止 run_command 卡死整个 agent
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("AGENT_COMMAND_TIMEOUT", "30"))

# 工具只允许在这个目录（及子目录）内读写文件/执行命令，防止模型跑出工作区
WORKSPACE_DIR = os.path.abspath(os.environ.get("AGENT_WORKSPACE", os.getcwd()))

# 写文件 / 执行命令前是否需要用户在终端确认（类似 Claude Code 的 permission prompt）。
# 单元测试、CI 或想无人值守跑一遍 demo 时可以设为 false。
REQUIRE_CONFIRMATION = os.environ.get("AGENT_REQUIRE_CONFIRM", "true").lower() != "false"
