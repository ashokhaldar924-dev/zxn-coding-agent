# 设计说明

本项目的统一原则是：**模型提出动作，本地 Runtime 执行动作、维护状态并控制结束。** 目标不是复制 Claude Code、Pi 或 Aider，而是把题目要求的五项核心逻辑和少量真正有用的工程机制做完整、做透明。

## 1. 为什么自行实现 Tool-calling Agent Loop

问题：Agent 框架会隐藏消息协议、工具调度和终止条件，而题目要求这些核心逻辑自行实现。

选择：`agent.py` 直接执行 `LLM -> tool_calls -> local results -> LLM` 循环。

实现：`llm.py` 只完成一次普通 Chat Completions HTTP 请求；`agent.py` 自行校验 assistant message、补全 call ID、解析 JSON arguments、分发本地工具并构造 `role="tool"` 结果。正常 final、max steps、wall time、连续 Runtime errors、Ctrl+C 和 fatal model error 都有显式终止路径。

取舍：没有 orchestration framework、streaming tool-call parser 或规划器。循环更容易逐行解释和离线测试，但不提供大型框架的编排功能。

## 2. 为什么模型只提议、本地 Runtime 执行

问题：模型输出只是调用意图，不是可信执行结果；题目也禁止依赖服务端托管的代码/文件工具。

选择：API 只接收本项目定义的普通 function schemas。文件、命令、权限、checkpoint 和验证全部在本地运行。

实现：`tools.py` 集中放置 schema、registry、dispatcher 和 9 个实现。`ToolRes.ok=False` 只代表本地 Runtime/tool error；被测程序返回非零 exit code 仍是成功取得的 observation。

取舍：接口透明且可测，但 shell 安全性仍取决于本机权限边界；本项目不声称它是 OS 沙箱。

## 3. 为什么文件边界使用 resolved workspace path

问题：模型可能生成 `../`、绝对路径或通过符号链接访问 workspace 外部。

选择：所有文件入口先解析真实路径，再确认结果仍位于 workspace。

实现：`tools._resolve_safe_path()` 和 checkpoint 的独立检查都使用 resolved path；递归搜索对每个候选文件再次检查。`.agent` 私有数据不出现在普通目录/搜索结果中，`@file` 也拒绝引用它。

取舍：这只保护本项目的文件工具。`shell=True` 命令只以 workspace 为 cwd，仍能访问外部路径。

## 4. 为什么 Exact Edit 要唯一匹配

问题：模型可能基于陈旧上下文编辑；宽泛 replace-all 容易改错位置。

选择：`edit_file(path, old, new)` 要求 `old` 非空且恰好出现一次。

实现：0 次匹配要求重新读取，多次匹配要求提供更多上下文；唯一匹配后先在内存生成 proposed content 和 unified diff，权限通过后才写入。CRLF 会在匹配和写入时保留。

取舍：没有 AST patch 或行号 patch。模型需要提供更精确的 old fragment，但行为简单、可预测且能防 stale-context overwrite。

## 5. 为什么 PermissionManager 独立于 Agent Loop

问题：每次 Effect tool 都弹 `y/N` 难以持续使用，但无条件自动执行又削弱用户控制。

选择：使用小型 `ALLOW / ASK / DENY` policy，并只记住当前进程内的批准。

实现：Observe tools 直接执行；干净文件编辑可以批准一次或整类允许；命令只按完整字符串记忆；初始 Git dirty 文件需要单独批准；少量高置信度破坏性命令直接 DENY。拒绝和阻止都作为正常 observation 返回模型。

取舍：不实现复杂 command AST、规则语言或 permission mode 矩阵。`--yes` 只跳过 ASK，不能绕过 DENY；这仍不是 sandbox。

## 6. 为什么 GitGuard 与 Checkpoint 分开

问题：启动前用户已有改动和启动后 Agent 新增改动是两类不同风险。

选择：`GitGuard` 负责识别“原本就存在的用户工作”，`CheckpointManager` 负责恢复“Agent 文件工具后来造成的变化”。

实现：

- `GitGuard.scan()` 只读调用 `git status --porcelain=v1 -z`，dirty 文件写入前必须单独批准；
- 文件工具获批后、真正写入前，checkpoint 先保存原始字节和 before/after SHA-256；
- restore 只有在当前文件仍等于 Agent after-hash 时才执行；
- 原文件恢复 before-image，Agent 新文件删除，多次修改按 LIFO 撤回。

取舍：不自动 commit、stash、reset 或 rollback。Checkpoint 不跟踪 shell、副进程、权限位、重命名或任意外部副作用，也不替代 Git。冲突时宁可拒绝，也不覆盖用户后续修改。

## 7. 为什么 Session 与 Trajectory 是两种数据

问题：审计日志可以解释一次运行，但不能可靠恢复对话；而模型 context 又不能无限增长。

选择：保留两条本地追加式 JSONL：session 用于恢复，trajectory 用于审计。

实现：

- `session.py` 保存 session header、每个 user task、完整 logical groups 和必要 State；
- `log.py` 保存每次进程的模型响应、usage、工具事件、门禁和终止；
- 两者都在 `.agent/`，都替换当前 API key；session 每次追加后 flush + fsync；
- 读取时只容忍一个可能由进程中断产生的残缺末行，不忽略中间损坏。

恢复会带回 task、assistant/tool messages、revision、改动文件和 token totals，但重新创建 PermissionManager、RepetitionGuard、error counters 和 wall-clock deadline。`ok_rev` 始终重置为 `-1`，因为退出期间 workspace 可能已变化。

取舍：Session 是线性历史，没有 tree、branch 或 fork；多进程并发写同一 session 不在保证范围内。

## 8. 为什么 CLI 同时保留 One-shot 与 Interactive

问题：一次任务启动一次进程适合自动化，却不适合持续编码；直接造完整 TUI 又会转移考核重点。

选择：有 task 参数时 one-shot；无 task 参数时进入简单 REPL。

实现：交互层提供 `/status`、`/sessions`、`/resume`、`/new`、`/undo`、`/restore`、`/exit`。`@file` 在 workspace 边界内按需注入有界文本；`!command` 是用户明确发起的 shell 模式，执行后由用户选择是否加入模型历史。

取舍：没有 curses、Rich、流式 UI、后台 shell 或图片上传。`!command` 是用户动作，因此不经过 Agent permission policy；它会保守地令旧 verification 失效。

## 9. 为什么 Context 同时保存完整历史和有界模型视图

问题：按单条消息裁剪会产生 orphan tool result；固定最近 N 组又无法优先处理巨大旧输出；多轮 session 还需要持续保留当前任务。

选择：`Ctx` 在内存中保留完整多轮 logical groups，但 `build()` 每次只生成有界模型视图。

实现：

1. 当前用户 task 单独锚定；
2. assistant tool call 与全部 results 是不可拆组；
3. 同时检查字符预算和 `UTF-8 bytes / 4` 的保守 token 估算；
4. 优先把较旧 tool output 替换为带原长度的占位符；
5. 仍超预算才删除最旧完整 groups；
6. 当前 task 和配置数量的最新 groups 不裁；
7. 每轮重新注入 Runtime 自己生成的确定性状态。

RuntimeState 包含 revision、验证状态、Agent 改动文件、最新 check 和外部 shell 变更标记。它不是模型摘要，因此不会因历史裁剪而“记错”运行状态。

取舍：估算不等同于具体 provider tokenizer；最新 group 单独超预算时会记录 `over_budget`，而不是破坏协议。暂不做 LLM compaction summary、RAG、embedding 或 vector DB，避免额外调用和不可确定的信息损失。

## 10. 为什么 Repo Map 先只支持 Python AST

问题：glob + regex 能找到文本，但模型仍需大量机械搜索才能知道中型仓库里有哪些核心符号。

选择：增加按需 `repo_map` Observe tool，先覆盖 Python。

实现：标准库 `ast` 提取顶层 class/function、class method、async 标记、参数名和行号；扫描文件数、输出符号数和字符数都有上限；语法错误文件跳过并计数。每次调用即时构建，不维护可能陈旧的后台索引。

取舍：不使用 Aider 的 graph ranking、tree-sitter、LSP 或跨语言索引。能力较小，但依赖为零、实现可解释、修改后不会读到旧 cache。

## 11. 为什么保留确定性 RepetitionGuard

问题：模型可能连续重复完全相同的读取或命令，浪费轮次和 token。

选择：第三次连续相同的已注册工具调用返回 stagnation observation。

实现：工具名和排序后的 JSON 参数形成 fingerprint；工具或参数变化立即重置；新用户 turn 也重置。未知工具仍走 Runtime error，不被 guard 掩盖。

取舍：不判断语义相似，不让另一个模型做 planner。可能阻止少数确有必要的三连调用，但行为确定、可配置且易测试。

## 12. 为什么 Final Verifier 可以由用户/项目固定

问题：单纯要求“某个 `check_command` 返回 0”仍允许模型选择过弱的检查，例如只运行一个无关测试。

选择：默认保留模型选择；用户或项目可通过 `AGENT_FINAL_VERIFIER` 或根目录 `.agent-verifier` 固定精确命令。

实现：每次 Agent 文件修改增加 `rev`。`check_command` 总会记录 command、exit code 和 revision；若配置 verifier，只有字符串规范化后与它精确一致且 exit code 为 0 才设置 `ok_rev = rev`。任何后来执行的失败 check 会把 `ok_rev` 设回 `-1`。Final gate 只在未修改或当前 revision 已验证时通过。

取舍：精确命令匹配牺牲了一点命令等价灵活性，换来清楚的 oracle。Verifier 仍只能证明该命令成功执行，不能证明测试充分或程序形式化正确。

## 13. 为什么加入独立 Eval Harness

问题：单元测试证明 Runtime 机制工作，却不能说明真实模型是否会完成 coding task、是否会篡改测试、需要多少步骤和 token。

选择：提供 8 个可重复的小型真实修复任务和一个 opt-in 模型评测脚本。

实现：`evals/run_eval.py` 为每个 case 建立隔离临时 workspace，先确认 oracle 初始失败，再运行真实 Agent；完成后哈希检查 tests 未改变，重新运行固定 verifier，并从 trajectory 统计 success、steps、tokens、tool calls、tool errors、failed checks 和 failure recovery。结果写入 Git-ignore 的 JSON。

取舍：这些任务比 SWE-bench 小，目的是项目级回归和演示证据，不声称是通用 benchmark。无 API 配置时只运行 dry-run fixture validation，绝不伪造模型成功率。

## 14. 为什么没有做通用 Hooks / Plugins

问题：permission、GitGuard、repetition、checkpoint、verification 和 session 都围绕 Agent Loop，继续硬编码可能让主循环膨胀。

选择：使用职责明确的小 manager 和一个 `persist_group` 回调，而不是通用 middleware/plugin framework。

实现：Agent Loop 只保留协议编排；权限、Git、checkpoint、repetition、context 和持久化分别在独立模块中。新增逻辑通过 State 所有权和窄接口连接。

取舍：没有动态 hook 注册、第三方工具加载或 extension lifecycle。对当前规模而言，明确依赖比抽象扩展性更容易解释和测试。

## 15. 明确不做什么

当前版本不实现：

- Agent framework / SDK；
- 服务端 Code Interpreter、Files 或 Shell；
- session tree、branch、fork；
- LLM 自动 compaction summary；
- RAG、embedding、vector database；
- 完整 TUI、streaming tool calls；
- Skills、MCP、通用插件市场；
- 多写入 Agent 或并发 Subagent；
- 自动 Git commit/stash/reset；
- OS-level sandbox；
- 图片输入和多 provider 视觉协议。

这些功能并非没有价值，而是当前考核更需要证明：基础 Agent Loop、上下文、工具、解析、终止和错误处理确实由项目自行实现，并且每项增强都有清楚边界。

## 16. 设计资料如何影响本项目

- Pi：完整 session 与有界 model context 分离、cheap-first compaction、最小核心与外围职责分离；
- Claude Code：session resume、临时 permission 不跨 branch/session、只恢复文件工具改动的 checkpoint 边界；
- Aider Repo Map：先给模型符号概览，再按需读取具体代码；
- SWE-agent ACI：工具参数和反馈格式本身会影响 Agent 能力；
- Anthropic Effective Agents：先采用简单、可组合、能解释的机制，复杂性由真实需求驱动。

本项目只吸收这些公开设计思想，没有复制参考项目源码，也没有将 Agent Loop 委托给它们。
