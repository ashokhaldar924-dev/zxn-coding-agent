# mini-coding-agent

一个从基础组件自行实现的本地编程 Agent。模型通过 OpenAI-compatible Chat Completions 原生 `tool_calls` 提议下一步；本地 Runtime 负责读写文件、执行命令、维护上下文、记录轨迹并决定何时允许结束。项目不依赖任何 Agent 框架或服务端代码/文件执行工具。

## 架构

```text
user task
  -> Ctx.build() 提供固定任务 + 最近完整 logical groups
  -> llm.call() 发起一次普通 Chat Completions 请求
  -> agent.py 自行解析 tool_calls
  -> tools.run_tool() 在本地工作区执行
  -> tool result 回到同一 logical group
  -> 循环，直到 final 通过 Runtime verification gate
```

| 文件 | 职责 |
| --- | --- |
| `agent.py` | Tool-call 解析、Agent Loop、终止条件、verification gate、CLI |
| `llm.py` | 一次非流式 OpenAI-compatible HTTP 请求与有限重试 |
| `ctx.py` | 固定 head + 最近完整 logical groups |
| `tools.py` | 7 个工具的 schema、本地实现、registry 与 dispatcher |
| `state.py` | `State` 和 `ToolRes` 的明确运行语义 |
| `log.py` | 与模型上下文分离的完整 JSONL trajectory |
| `ui.py` | TTY 下的轻量 ANSI 状态和 diff 配色 |
| `config.py` | 只从环境变量读取配置和凭据 |

## 七个本地工具

- Observe tools：`read_file`、`list_dir`、`search_text`，直接执行。
- Effect tools：`write_file`、`edit_file`、`run_command`、`check_command`，默认先展示 diff 或完整命令，再等待 `y/N`。

`read_file` 按 1-based 行范围读取，每次最多 200 行。`search_text` 做简单 substring 搜索，最多展示 30 条。`edit_file` 要求 `old` 非空且恰好匹配一次；0 次提示重新读取，多个匹配提示扩大上下文。`run_command` 是普通观察，命令返回非零退出码不代表 Runtime 崩溃。`check_command` 只有在未拒绝、未超时且退出码为 0 时才验证当前 revision。

用户拒绝副作用操作会得到 `ToolRes(ok=True, rejected=True)`，作为正常 observation 回传模型，不计入连续 Runtime error。

## Verification Gate

每次真实文件修改都会增加 `rev`。成功的 `check_command` 把 `ok_rev` 更新为当时的 `rev`。模型尝试给出 final 时：

- 任务未修改文件，可以直接结束；
- 修改过文件，则只有 `ok_rev == rev` 才允许结束；
- 验证成功后再次修改会得到新 revision，旧验证自动失效。

这只能证明当前版本成功执行过一次模型选择的检查，不能证明测试覆盖充分，也不是形式化正确性证明。

## Context 与 trajectory

`Ctx` 永远保留 system prompt 和原始任务，只按完整 logical group 保留最近窗口。一个 assistant tool-call 与它的全部 tool results 不会被拆开，因此不会产生 orphan tool result。若 provider 返回 thinking-mode 的 `reasoning_content`（例如 DeepSeek），该字段也会在后续工具轮次中原样回传。

完整运行轨迹单独写入工作区 `.agent/run-*.jsonl`，记录任务、模型响应、token usage、工具调用/结果、用户拒绝、verification gate、final 和 fatal error。`.agent/` 已被 Git 忽略，日志也会脱敏环境中的 `AGENT_API_KEY`。

## 终止与错误处理

Runtime 明确处理：正常 final、最大步数、最大墙钟时间、连续工具错误上限、Ctrl+C 和 fatal LLM error。工具参数 JSON 错误、未知工具、路径错误和工具异常会成为 observation，使模型有机会修正。网络异常、HTTP 429 和 5xx 最多重试两次。

命令输出超过限制时保留 head 和 tail，避免丢失尾部 traceback/test summary。API 返回 usage 时显示并累计 token；provider 不返回时安静跳过，不估算价格。

## 安装与运行

要求 Python 3.10+。

```bash
python -m pip install -r requirements.txt
```

设置环境变量，不要把真实 key 写入源码、文档或提交记录：

```powershell
$env:AGENT_API_KEY="your-key"
$env:AGENT_BASE_URL="https://your-openai-compatible-endpoint/v1"
$env:AGENT_MODEL="your-model-name"
python agent.py --workspace D:\path\to\project "修复这个项目的失败测试并验证"
```

默认 effect tools 需要确认。`--yes` 仅对本次运行关闭确认，启动时会显示醒目提示：

```bash
python agent.py --yes "分析并修复当前项目"
```

其余可选环境变量见 `.env.example`：`AGENT_WORKSPACE`、`AGENT_MAX_STEPS`、`AGENT_MAX_TIME`、`AGENT_MAX_TOOL_CHARS`、`AGENT_MAX_GROUPS`、`AGENT_CMD_TIMEOUT`、`AGENT_CONFIRM`。

## 测试

测试不需要 API key，也不会调用网络：

```bash
python -m unittest discover -s tests -v
python -m py_compile *.py
```

测试覆盖工具边界、diff/confirmation、CRLF exact edit、timeout、head/tail 截断、logical groups、trajectory 脱敏、HTTP 协议/重试，以及 FakeLLM Agent Loop 与 revision-aware verification gate。

## 安全边界与已知局限

- 文件工具会解析真实路径并限制在 workspace 内；搜索也跳过越界符号链接。
- shell 仅以 `cwd=workspace` 启动并要求确认，**不是 OS-level sandbox**；命令本身仍可能访问工作区外资源。
- `AGENT_CONFIRM=false` 或 `--yes` 会取消人工确认，只应在受信环境使用。
- 当前不做 streaming、RAG、多 Agent、MCP、持久 shell、GUI/TUI 或复杂权限系统。
- 模型选择的成功检查不等于完整正确性证明。

## 设计参考

- [How to Build an Agent](https://ampcode.com/notes/how-to-build-an-agent)
- [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent)
- [SWE-agent / ACI paper](https://arxiv.org/abs/2405.15793)
- [OpenCode](https://github.com/anomalyco/opencode)
- [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Billet](https://github.com/stefanwille/billet)

更完整的取舍说明见 `DESIGN.md`。
