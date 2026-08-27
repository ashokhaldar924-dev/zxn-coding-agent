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

## 5. effect tools 需要人工确认

问题：写文件和运行命令具有真实副作用，用户需要最终控制权。

选择：Observe tools 直接执行；write/edit/run/check 默认展示 diff 或完整命令并询问 `y/N`。

实现：统一 `_confirm()`；拒绝返回 `ToolRes(ok=True, rejected=True)` 并回传模型，不计 Runtime error，也不修改 revision。

取舍：终端确认会降低无人值守程度。测试/受信环境可用 `AGENT_CONFIRM=false` 或 `--yes`，但启动时明确提示。

## 6. model context 与完整 history 分离

问题：按单条消息裁剪可能保留 tool result 却丢掉对应 assistant tool call，破坏协议；无限历史又会撑大上下文。

选择：`Ctx` 保存固定 head 和最近若干完整 logical groups，一个 assistant tool-call 与其全部 results 是不可拆分单位。

实现：`Ctx.add_group()` 只在组边界裁剪。完整 trajectory 由 `RunLog` 逐事件写入 JSONL，不依赖模型上下文保存。

取舍：不做 RAG、embedding 或 LLM summary，早期细节可能离开模型窗口，但数据流简单且不会产生 orphan tool messages。

## 7. `rev / ok_rev` Verification Gate

问题：模型可能修改代码后未经验证就宣布完成，或拿旧版本的成功测试证明新修改正确。

选择：每次真实文件修改增加 `rev`；成功 `check_command` 记录当时的 `ok_rev`；final 只有在未修改或 `ok_rev == rev` 时通过。

实现：普通 `run_command` 不更新验证状态；`check_command` 只有未拒绝、未超时且 `rc == 0` 才更新。验证后再次编辑自然使 `rev > ok_rev`，Runtime 会拒绝 final 并要求重新检查。

取舍：gate 证明的是“当前 revision 执行过一次模型选择的成功检查”，不能保证检查选择合理、测试覆盖充分或程序形式化正确。
