# mini-coding-agent

一个从零实现的最小可用编程 agent：通过 DeepSeek API 的原生 tool calling，
自主读写文件、执行 shell 命令，完成编程任务。不依赖任何 agent 框架/SDK。

## 快速开始

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY=sk-你的key
python agent.py "在当前目录写一个 fizzbuzz.py，并写一个 pytest 测试文件验证它，然后运行测试"
```

或不带参数进入交互模式：

```bash
python agent.py
```

本地跑单元测试（不需要 API key，不需要网络）：

```bash
python -m unittest discover -s tests -v
```

## 架构

```
agent.py            主循环：调 LLM → 判断是否有 tool_calls → 执行工具 → 回传结果 → 循环
llm_client.py        对 DeepSeek /chat/completions 的最薄封装（纯 requests，无 SDK）
tools.py              5 个工具的 schema 定义 + 本地执行实现，含路径越权防护
context_manager.py   对话历史管理，超长时做结构化裁剪
config.py             全部可调参数 + API key 读取（环境变量）
tests/                本地单元测试，覆盖工具逻辑和上下文裁剪逻辑
```

## 关键设计决策

- **为什么用 `requests` 而不是官方 SDK？** DeepSeek 兼容 OpenAI 格式，直接手写 HTTP
  请求可以让每一步（请求体长什么样、tool_calls 怎么解析）都完全透明、可讲解，
  不依赖 SDK 内部帮你做了什么。
- **上下文裁剪策略**：不做摘要（省时间和 token），而是永远锚定"系统提示 + 最初任务"，
  中间部分超限后整体丢弃并插入裁剪提示，保留最近 N 条消息。取舍是可能丢失中间步骤的
  细节，但保证 agent 不会忘记自己在做什么任务。
- **循环终止条件**：模型返回不带 `tool_calls` 的纯文本消息即视为完成；同时设置
  `MAX_ITERATIONS` 硬上限防止死循环或反复失败重试耗尽预算。
- **错误处理哲学**：工具执行中的任何异常都被捕获并转换成字符串错误信息回传给模型，
  而不是让程序崩溃——这样模型有机会根据错误信息自我纠正（比如路径写错、命令语法错）。
- **安全边界的取舍**：只做了"路径不能跑出工作区"这一条硬限制，没有做命令白名单或
  执行前用户确认。这是刻意的简化，目的是在 demo 场景里保持可用性，面试时可以谈
  如果要用于生产环境还需要加哪些防护（sandbox、审批流等）。

## 特色功能：diff 预览 + 执行前确认

`write_file` 在真正落盘前，会用 `difflib` 计算 unified diff 并打印出来；
`run_command` 在真正执行前会打印出完整命令。两者默认都会在终端等待用户输入
`y`/`N` 确认，拒绝时不会报错崩溃，而是把"用户拒绝了此操作"作为正常的工具
结果喂回给模型，模型据此调整方案（比如换个更小的改动、或直接询问用户）。
这类似 Claude Code / Codex 的 permission prompt 机制。

- 演示 / 自动化测试时可以设 `AGENT_REQUIRE_CONFIRM=false` 跳过确认。
- 这是一个刻意做的"人在回路"（human-in-the-loop）取舍：牺牲一点自主性，
  换来"用户对每一次落盘/执行都有最终否决权"，我认为这个取舍在编程 agent
  场景下是值得的，因为写文件和跑命令是唯一有真实副作用的操作。

## 已知局限（可作为后续迭代方向）

- 单个工具调用串行执行，未支持并行 tool_calls
- 没有 token 级别的精确上下文控制，只做消息条数裁剪
- 确认机制是终端阻塞式 input()，没有做超时或批量放行选项
