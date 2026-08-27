"""
工具的"定义"（给模型看的 JSON Schema）和"执行"（本地真正跑的 Python 函数）
放在一起，方便对照，也方便面试时讲清楚"模型只是发起请求，真正干活的是这里"。

安全边界：
1. 所有文件/命令类工具都被限制在 config.WORKSPACE_DIR 内，
   防止模型（或者模型被诱导后）读写/执行工作区之外的东西。
2. write_file 会先算出 unified diff 展示给用户看，run_command 会展示要执行的
   命令，两者默认都需要用户在终端确认（y/N）才会真正落地——类似 Claude Code
   的 permission prompt。可通过 AGENT_REQUIRE_CONFIRM=false 关闭（比如跑自动化
   测试或无人值守 demo 时）。用户拒绝时不会抛异常，而是把"被拒绝"这个结果当作
   正常的工具返回值喂回模型，让模型据此调整方案——这也是"错误处理"设计的延伸：
   拒绝和报错走的是同一条"喂回模型自我修正"的路径。
"""
import difflib
import os
import re
import subprocess

import config


def _confirm(prompt: str) -> bool:
    """
    统一的确认入口。config.REQUIRE_CONFIRMATION=False 时直接放行
    （单元测试、CI、无人值守场景用）；否则在终端阻塞询问用户。
    """
    if not config.REQUIRE_CONFIRMATION:
        return True
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def _resolve_safe_path(relative_path: str) -> str:
    """
    把用户/模型给的相对路径解析成绝对路径，并确保它没有跑出 WORKSPACE_DIR。
    这是所有文件类工具共用的安全检查。
    """
    target = os.path.abspath(os.path.join(config.WORKSPACE_DIR, relative_path))
    if not (target == config.WORKSPACE_DIR or target.startswith(config.WORKSPACE_DIR + os.sep)):
        raise PermissionError(
            f"拒绝访问：路径 '{relative_path}' 解析后跑出了工作区 {config.WORKSPACE_DIR}"
        )
    return target


def _truncate(text: str) -> str:
    if len(text) > config.MAX_TOOL_OUTPUT_CHARS:
        return (
            text[: config.MAX_TOOL_OUTPUT_CHARS]
            + f"\n...(输出已截断，原始长度 {len(text)} 字符，超过上限 {config.MAX_TOOL_OUTPUT_CHARS})"
        )
    return text


# ---------- 具体工具实现 ----------

def tool_read_file(args: dict) -> str:
    path = _resolve_safe_path(args["path"])
    if not os.path.isfile(path):
        return f"错误：文件不存在 - {args['path']}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return _truncate(content)


def tool_write_file(args: dict) -> str:
    path = _resolve_safe_path(args["path"])
    new_content = args["content"]
    append = bool(args.get("append"))

    old_content = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            old_content = f.read()

    preview_content = (old_content + new_content) if append else new_content
    diff = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            preview_content.splitlines(keepends=True),
            fromfile=f"a/{args['path']}",
            tofile=f"b/{args['path']}",
        )
    )
    if diff:
        print(f"\n--- 即将修改 {args['path']} ---\n{diff}")
    else:
        print(f"\n--- 即将创建新文件 {args['path']}（内容与旧文件相同或为新建）---")

    if not _confirm(f"是否应用对 {args['path']} 的以上修改？"):
        return f"用户拒绝了对 {args['path']} 的写入，文件未被修改"

    mode = "a" if append else "w"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write(new_content)
    action = "追加写入" if append else "覆盖写入"
    return f"{action}成功：{args['path']}（{len(new_content)} 字符）"


def tool_list_dir(args: dict) -> str:
    path = _resolve_safe_path(args.get("path", "."))
    if not os.path.isdir(path):
        return f"错误：目录不存在 - {args.get('path', '.')}"
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append(("[DIR] " if os.path.isdir(full) else "      ") + name)
    return _truncate("\n".join(entries) if entries else "(空目录)")


def tool_search_text(args: dict) -> str:
    """在工作区内按正则搜索文本，类似简化版 grep -rn"""
    pattern = re.compile(args["pattern"])
    root = _resolve_safe_path(args.get("path", "."))
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过常见的大目录，避免搜索 .git / node_modules 浪费上下文
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", ".venv")]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, start=1):
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, config.WORKSPACE_DIR)
                            matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                            if len(matches) >= 200:
                                break
            except (UnicodeDecodeError, OSError):
                continue
        if len(matches) >= 200:
            break
    return _truncate("\n".join(matches) if matches else "(未找到匹配)")


def tool_run_command(args: dict) -> str:
    command = args["command"]

    if not _confirm(f"是否执行命令：{command}"):
        return f"用户拒绝执行命令：{command}"

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=config.COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超过 {config.COMMAND_TIMEOUT_SECONDS} 秒，已终止"

    output = f"退出码: {proc.returncode}\n"
    if proc.stdout:
        output += f"stdout:\n{proc.stdout}\n"
    if proc.stderr:
        output += f"stderr:\n{proc.stderr}\n"
    return _truncate(output)


# ---------- 注册表：name -> 执行函数 ----------
TOOL_FUNCTIONS = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "list_dir": tool_list_dir,
    "search_text": tool_search_text,
    "run_command": tool_run_command,
}

# ---------- 注册表：给模型看的 JSON Schema（OpenAI / DeepSeek 通用格式） ----------
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内某个文本文件的完整内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作区根目录的文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件内容。默认覆盖整个文件；append=true 时追加到文件末尾。父目录不存在会自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作区根目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                    "append": {"type": "boolean", "description": "true 表示追加，false 或省略表示覆盖"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作区内某个目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作区根目录的目录路径，省略则为根目录"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "在工作区内递归搜索匹配正则表达式的代码/文本行，类似 grep -rn",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 正则表达式"},
                    "path": {"type": "string", "description": "搜索起始目录，省略则为工作区根目录"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在工作区目录下执行一条 shell 命令（比如运行测试、安装依赖、执行脚本），"
                f"返回退出码、stdout、stderr。超时时间 {os.environ.get('AGENT_COMMAND_TIMEOUT', 30)} 秒。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                },
                "required": ["command"],
            },
        },
    },
]
