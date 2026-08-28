# 设计说明

这个项目追求“小而完整、可真实运行、每个关键机制能逐行解释”。统一原则是：模型提出动作，本地 Runtime 执行动作、记录状态并控制结束。

## 1. 自行实现 tool-calling loop

问题：Agent 框架会隐藏消息协议和循环控制，而题目要求关键逻辑自行实现。

选择：`agent.py` 直接执行 `LLM -> tool_calls -> local results -> LLM` 循环，并自行处理每个 call 的 ID、名称和 JSON arguments。

实现：`llm.py` 只返回一次 assistant message 和 usage；`agent.py` 解析后调用 `tools.run_tool()`，再构造对应 `role="tool"` 消息。

取舍：不加入 orchestration framework、planning tool、streaming 或多 Agent。循环短、协议透明，代价是没有复杂编排能力。

## 2. 模型只提议，本地 Runtime 真正执行

问题：模型输出不是可信执行结果，也不能把代码/文件执行托管给 API 服务端。

选择：模型只能使用普通 function schemas；文件和命令都由本地 Python 实现执行。

实现：`tools.py` 同时放 schema、registry、dispatcher 和本地函数，方便逐项对照。`ToolRes.ok` 只描述 Runtime 是否工作；被测命令返回 `rc != 0` 仍是成功取得的 observation。命令工具会告知模型当前平台和既有 workspace cwd，运行时私有的 `.agent` trajectory 不应作为任务代码读取。

取舍：本地执行便于观察和测试，但安全性取决于本机边界和用户确认。

## 3. 文件工具限制 workspace

问题：模型生成的路径可能误读、误写工作区外文件，也可能通过符号链接越界。

选择：所有文件入口先解析真实路径并检查其仍位于 workspace；递归搜索对每个候选文件重复检查。

实现：`tools._resolve_safe_path()` 使用 resolved path 与 workspace 的相对关系判断边界。

取舍：这是文件工具边界，不是完整沙箱。`shell=True` 的命令只设置 `cwd=workspace`，命令仍可能访问外部路径；项目不声称 OS-level sandbox。

## 4. exact edit 必须唯一匹配

问题：模型可能基于陈旧上下文编辑；宽泛 replace-all 容易修改错误位置。

选择：`edit_file(path, old, new)` 要求 `old` 非空且恰好出现一次。

实现：0 次匹配要求重新读取，多个匹配要求加入更多上下文；唯一匹配后先在内存生成 proposed content 和 unified diff，经确认才写回。Windows CRLF 会被识别并保持，不因为换行风格制造伪变化。

取舍：不做 AST patch 或行号编辑，接口更容易解释；模型需要提供足够精确的上下文。

## 5. 独立 PermissionManager 与会话批准

问题：写文件和运行命令具有真实副作用，用户需要最终控制权。

选择：Observe tools 直接执行；Effect tools 交给独立 `PermissionManager`，结果只有 `ALLOW / ASK / DENY`。用户可以批准一次，也可以在本次运行中记住干净文件编辑或一条精确命令。

实现：`permissions.py` 保存会话级批准。用户拒绝返回 `ToolRes(ok=True, rejected=True)`；策略阻止返回 `ToolRes(ok=True, blocked=True)`。少量高置信度破坏性命令直接 DENY，`--yes` 也不会绕过。

取舍：不解析任意 shell 的完整语义，也不实现 Claude Code 式规则语言；命令只按完整字符串记忆，参数变化就重新询问。它减少重复弹窗，但不是 OS 沙箱。

## 6. model context 与完整 history 分离

问题：按单条消息裁剪可能保留 tool result 却丢掉对应 assistant tool call，破坏协议；无限历史又会撑大上下文。

选择：`Ctx` 保存固定 head 和最近若干完整 logical groups，一个 assistant tool-call 与其全部 results 是不可拆分单位；额外使用可配置字符预算做 cheap-first 裁剪。

实现：`Ctx.add_group()` 只在组边界裁剪。构建上下文时先把较旧 tool output 替换为包含原长度的占位符，仍超预算再丢弃最旧完整 groups；最新 groups 始终完整保留。完整 trajectory 由 `RunLog` 逐事件写入 JSONL，不依赖模型上下文保存。

取舍：字符数只是 token 的透明近似；不做 RAG、embedding 或 LLM summary。最新 group 单独超过预算时宁可记录 `over_budget`，也不破坏最新协议消息。

## 7. `rev / ok_rev` Verification Gate

问题：模型可能修改代码后未经验证就宣布完成，或拿旧版本的成功测试证明新修改正确。

选择：每次真实文件修改增加 `rev`；成功 `check_command` 记录当时的 `ok_rev`；final 只有在未修改或 `ok_rev == rev` 时通过。

实现：普通 `run_command` 不更新验证状态；`check_command` 只有未拒绝、未超时且 `rc == 0` 才更新。验证后再次编辑自然使 `rev > ok_rev`，Runtime 会拒绝 final 并要求重新检查。

取舍：gate 证明的是“当前 revision 执行过一次模型选择的成功检查”，不能保证检查选择合理、测试覆盖充分或程序形式化正确。

## 8. Git 初始脏文件保护

问题：Agent 启动前，用户可能已经有未提交或未跟踪的工作；普通编辑批准不应被理解为可以无提示覆盖这些内容。

选择：启动时只读记录 Git 初始 dirty set，不自动 commit、stash、reset 或 rollback。

实现：`GitGuard.scan()` 调用 `git status --porcelain=v1 -z`，把路径转为绝对路径集合。写入前再次按 resolved path 判断；初始 dirty 文件必须获得针对该文件的单独批准。

取舍：非 Git 工作区安静降级；它只能警告和提高授权粒度，不能恢复被允许后产生的错误修改。

## 9. 有界 AGENTS.md 项目上下文

问题：测试命令、代码规范和禁止修改范围不应每次都靠模型重新探索。

选择：只加载 workspace 根目录的 `AGENTS.md`，作为 system prompt 中受边界标记的项目上下文。

实现：`project_context.py` 拒绝二进制文件并限制字符数；提示词明确说明项目上下文从属于用户任务、工具权限和 Runtime 安全。

取舍：不做层级规则合并、自动记忆或写回，避免引入隐式优先级和持久状态。

## 10. 最小代码导航增强

问题：单层目录列表和字面搜索不足以快速定位中型仓库中的文件与代码模式。

选择：保留原 `search_text` 默认字面语义，只在 `regex=true` 时启用 Python regex；新增只返回文件路径的 `glob_files`。

实现：两个工具复用 workspace real-path 边界、噪声目录过滤和固定结果上限。glob 拒绝绝对路径和 `..` pattern。

取舍：不加入 LSP、AST、symbol index 或 repo map；导航能力提升可测，工具仍可逐行解释。

## 11. 确定性停滞护栏

问题：模型可能连续重复完全相同的读取或命令，浪费轮次和 token。

选择：第三次连续相同的已注册工具调用不执行，直接返回要求换策略的 observation。

实现：`RepetitionGuard` 使用工具名与排序后的 JSON 参数生成 fingerprint；不同工具或参数会立即重置计数。未知工具仍按 Runtime error 处理，不被停滞护栏掩盖。

取舍：不判断语义相似调用，也不自动规划下一步；简单规则可能阻止少数确有必要的三连调用，但行为确定、可测试、可配置。

## 12. 有意不做的扩展

Pi 的 core/extension 分层、session tree 和 compaction，以及 Claude Code 的 hooks、skills、subagents 和复杂权限规则都很有价值，但本项目暂不实现。当前增强分别落在 `permissions.py`、`gitguard.py`、`guards.py`、`project_context.py` 和 `ctx.py`，Agent Loop 只负责协议编排。这样既吸收“最小 core + 外围治理”的思想，又不引入难以在考核中解释和验证的通用框架。
