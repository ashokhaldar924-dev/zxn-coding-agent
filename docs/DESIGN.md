# 设计说明

本项目的统一原则是：**模型提出动作，本地 Runtime 执行动作、维护状态并控制结束。** 设计聚焦 Coding Agent 的核心职责：上下文管理、本地工具执行、tool-call 解析、循环终止和错误处理，并只加入能改善可靠性的工程机制。

## 1. 为什么自行实现 Tool-calling Agent Loop

问题：Agent 框架会隐藏消息协议、工具调度和终止条件，使关键状态边界难以检查、测试和解释。

选择：`agent.py` 直接执行 `LLM -> tool_calls -> local results -> LLM` 循环。

实现：`llm.py` 只完成一次普通 Chat Completions HTTP 请求，并把 provider 的 `finish_reason` 交给 Runtime；`agent.py` 自行校验 assistant message、补全 call ID、解析 JSON arguments、分发本地工具并构造 `role="tool"` 结果。第一次 `length` 只允许一次不执行 partial tool call 的 continuation；连续第二次以 `INCOMPLETE_MODEL_OUTPUT` 停止。`content_filter` 独立终止，不重试也不执行过滤或截断的调用。正常 final、max steps、wall time、可选 task token budget、连续 Runtime errors、Ctrl+C 和 fatal model error 都有显式终止路径。

取舍：没有 orchestration framework、streaming tool-call parser 或重型 Plan Mode。循环更容易逐行解释和离线测试，但不提供大型框架的编排功能。

## 2. 为什么模型只提议、本地 Runtime 执行

问题：模型输出只是调用意图，不是可信执行结果；托管式代码或文件工具也无法提供本地 Runtime 所需的工作区状态保证。

选择：API 只接收本项目定义的普通 function schemas。文件、命令、权限、checkpoint 和验证全部在本地运行。

实现：`tools.py` 集中放置 schema、registry、dispatcher 和 12 个实现。命令是否允许运行由 `PermissionManager` 判断；`command_runtime.py` 只负责 subprocess、timeout、stdout/stderr 和结果存储，使 policy 与 execution 的职责分开。命令使用独立进程组/进程树，stdout 与 stderr 先进入临时文件；超时后清理普通后代进程，长结果再以流式方式脱敏并保存，避免 `capture_output` 把任意大小的日志先装入 Python 内存。Agent 自身的 API Key 不传入命令环境。`ToolRes.ok=False` 只代表本地 Runtime/tool error；被测程序返回非零 exit code 仍是成功取得的 observation。

取舍：接口透明且可测，但仍用 `shell=True` 保持测试、pipe 和平台命令的兼容性，没有引入容易误判的简化 shell parser。Shell 安全性仍取决于本机权限边界；本项目不声称它是 OS 沙箱。

## 3. 为什么文件边界使用 resolved workspace path

问题：模型可能生成 `../`、绝对路径或通过符号链接访问 workspace 外部。

选择：所有文件入口先解析真实路径，再确认结果仍位于 workspace。

实现：`tools._resolve_safe_path()` 和 checkpoint 的独立检查都使用 resolved path；递归搜索对每个候选文件再次检查。`.agent` 私有数据不出现在普通目录/搜索结果中，`@file` 也拒绝引用它。

取舍：这只保护本项目的文件工具。`shell=True` 命令只以 workspace 为 cwd，仍能访问外部路径。

## 4. 为什么 Exact Edit 要唯一匹配

问题：模型可能基于陈旧上下文编辑；宽泛 replace-all 容易改错位置。

选择：`edit_file(path, old, new)` 要求 `old` 非空且恰好出现一次；同一文件内有多个相关修改时，`multi_edit(path, edits)` 把 2–20 个同样的精确替换作为一个事务。

实现：0 次匹配要求重新读取，多次匹配要求提供更多上下文；`multi_edit` 中每一步都基于上一步的内存结果，任一步失败时不写入任何内容。全部成功后只展示一次 unified diff、请求一次权限、建立一次 checkpoint，并推进一次 revision。Runtime 维护最近已知内容摘要，若文件随后被 shell 或用户改变，会拒绝陈旧编辑并要求重新读取；权限确认结束后、真正写入前还会再次逐字节核对，关闭“展示 diff 到落盘”之间的覆盖窗口。实际落盘使用同目录临时文件、`fsync` 和 `os.replace`，写入失败保留完整原文件；CRLF、UTF-8 BOM 和权限位会保留。

取舍：没有 fuzzy matching、AST patch、行号 patch 或跨文件事务。模型需要提供更精确的 old fragment，但行为简单、可预测且能防 stale-context overwrite。未知新内容不会自动合并；重新读取后由模型基于新事实生成下一次修改。

## 5. 为什么 PermissionManager 独立于 Agent Loop

问题：每次 Effect tool 都弹 `y/N` 难以持续使用，但无条件自动执行又削弱用户控制。

选择：使用小型 `ALLOW / ASK / DENY` policy，默认采用低打扰的 `balanced` 模式，同时保留 `manual` 模式和当前进程内的精确授权状态。

实现：Observe tools 和普通 workspace 文件编辑直接执行；初始 Git dirty、密钥类文件和 Git 元数据按文件 ASK。只读命令、常见 verifier 与用户配置的精确 final verifier 自动放行；未知命令按可见命令族提供允许一次、会话允许或拒绝。`python -c`、`bash -c`、`node -e` 等通用解释器入口只能记忆精确命令，不能获得可执行任意代码的宽泛 family 授权。外部状态、安装、删除与 Git 写操作始终 ASK，高置信度破坏操作直接 DENY。拒绝和阻止都作为正常 observation 返回模型。

取舍：命令识别是保守的确定性规则，会拒绝对复合 shell、重定向、敏感路径和工作区外路径的自动批准；它不是完整 shell AST 或 OS sandbox。`--yes` 只跳过 ASK，不能绕过 DENY。

## 6. 为什么 GitGuard 与 Checkpoint 分开

问题：启动前用户已有改动和启动后 Agent 新增改动是两类不同风险。

选择：`GitGuard` 负责识别“原本就存在的用户工作”，`CheckpointManager` 负责恢复“Agent 文件工具后来造成的变化”。

实现：

- `GitGuard.scan()` 只读调用 `git status --porcelain=v1 -z`，dirty 文件写入前必须单独批准；
- 文件工具获批后、真正写入前，checkpoint 先保存原始字节和 before/after SHA-256；
- restore 只有在当前文件仍等于 Agent after-hash 时才执行；
- 原文件恢复 before-image，Agent 新文件删除，多次修改按 LIFO 撤回；
- GUI 的本轮恢复先对所有目标做冲突预检，再整体逆序恢复，避免一个文件冲突时只撤回一半；恢复后推进 revision 并使旧 verification 失效。

取舍：不自动 commit、stash、reset 或 rollback。Checkpoint 不跟踪 shell、副进程、权限位、重命名或任意外部副作用，也不替代 Git。冲突时宁可拒绝，也不覆盖用户后续修改。最终 Changes 从当前 user turn 开始时的 checkpoint cursor 计算，不把同一 Session 更早任务的修改冒充成本轮成果；shell 或用户后续改变导致无法证明行数时明确显示 unavailable。

## 7. 为什么 Session 与 Trajectory 是两种数据

问题：审计日志可以解释一次运行，但不能可靠恢复对话；而模型 context 又不能无限增长。

选择：保留两条本地追加式 JSONL：session 用于恢复，trajectory 用于审计。

实现：

- `session.py` 保存 session header、每个 user task、完整 logical groups 和必要 State；
- `log.py` 保存每次进程的模型响应、usage、工具事件、门禁和终止；
- 两者都在 `.agent/`，都替换当前 API key；session 每次追加后 flush + fsync；
- 读取时只容忍一个可能由进程中断产生的残缺末行，不忽略中间损坏。

恢复会带回 task、assistant/tool messages、revision、改动文件、当前 Plan 和 token totals，但重新创建 PermissionManager、RepetitionGuard、error counters 和 wall-clock deadline。`ok_rev` 始终重置为 `-1`；恢复时还会对账 active checkpoint，把已落盘但尚未进入最后一条 session state 的 Agent 文件修改合并回来。Session 同时保存创建或上次确认时的 Git HEAD；同一路径已经切换代码基线时必须由用户确认，接受后才更新后续恢复基线。由于退出期间文件可能被人工修改，恢复后的现有文件在首次编辑前必须先由 `read_file` 建立本进程的新观察，避免旧对话直接覆盖停机期间的新内容。

取舍：Session 是线性历史，没有 tree、branch 或 fork；多进程并发写同一 session 不在保证范围内。

## 8. 为什么 CLI 同时保留 One-shot 与 Interactive

问题：一次任务启动一次进程适合自动化，却不适合持续编码；完整 TUI 则会显著扩大界面层的复杂度。

选择：有 task 参数时 one-shot；无 task 参数时进入简单 REPL。

实现：交互层提供 `/status`、`/sessions`、`/resume`、`/new`、`/undo`、`/restore`、`/exit`。`@file` 在 workspace 边界内按需注入有界文本；`!command` 是用户明确发起的 shell 模式，执行后由用户选择是否加入模型历史。`ui.py` 以 append-only 时间线显示任务、Plan、工具动作、命令结果和 final；文件 `+N/-N` 由 `changes.py` 对真实 before/after 计算，多次直接编辑的最终净变化从 Checkpoint 最早 before-image 与当前文件得出。UI 只读取 `ToolRes`、`PlanState` 和 `verification_current()`，不建立第二套完成状态。

取舍：没有 curses、Rich、全屏 TUI、动画、后台 shell 或图片上传。普通自动批准编辑只显示变化摘要；真正需要用户确认的编辑仍在提示前展示完整 diff。TTY 使用少量 ANSI，非 TTY 或不兼容编码自动退化为纯文本。`!command` 是用户动作，因此不经过 Agent permission policy；有 active session 时仍会经过同一工作区变化跟踪并使旧 verification 失效。

### 为什么桌面 GUI 仍然只有一套 Runtime 状态

问题：桌面界面适合演示持续执行流，但如果 GUI 自己维护 workspace revision、Plan 或 `verified=true`，就可能与真实 Agent 状态分叉。

选择：PySide6 GUI 只是现有同步 Runtime 的可选观察与输入层。Agent Loop 在 `QThread` Worker 中按原顺序运行；`RunLog` 将已经脱敏的结构化事件可选转发给 `gui_presenter.py`，再由窗口渲染 Activity、Plan 和 Verification。

实现：工具事件携带真实 `FileChange` 与命令耗时；`State.verification_data()` 由 verification freshness、adequacy、workspace revision、fingerprint 和 Runtime completion 共同生成 Runtime-owned snapshot。Presenter 只投影这个 snapshot，即使模型正文声称完成，也不能把 STALE、PARTIAL 或 STOPPED 变为 FINAL VERIFIED。Plan 只来自持久化的 `PlanState`。权限请求通过可注入 answer callback 进入 Qt modal，三个按钮仍映射到 PermissionManager 原来的允许一次、当前进程记忆和拒绝分支；GUI 不直接修改 State。

`gui_data.py` 为窗口提供受 workspace 边界约束的只读项目树、文件预览和显式 recent-workspace 列表。切换工作区只允许在空闲状态进行，并同时更新 Runtime 的真实 workspace 配置，而不是只换标题。History 直接读取 `SessionStore` 的持久化摘要；Resume 仍调用同一 `_resume_active()`，因此旧 verification 失效、Git HEAD 变化确认和进程内权限重建都不会被 GUI 绕过。Changes 的可点击 Diff 由当前任务 Checkpoint 的 before-image、after-hash 和当前字节共同证明；shell-only 或后来发生冲突的变化只显示 unavailable，不伪造 diff。Plan 的 Evidence Hint 只从已完成的工具事件提取，模型正文不能写入。`evidence_report.py` 同样只汇总 Runtime 保存的任务、计划、文件变化、检查、revision/fingerprint、Token、调用和终止原因；GUI 导出的是这份确定性报告，不再请求模型生成交付证明。

Stop 使用 `State` 中不持久化的 process-local cancellation event。Agent Loop 在模型等待、工具组边界和 final 前检查它；命令执行层轮询同一事件并终止普通子进程树。若 HTTP provider 不支持取消，等待线程可继续到 provider timeout，但其迟到响应不会再进入 Context 或触发工具。已开始的原子文件替换不会被异步强杀，避免留下半写文件。结束卡片只根据 Runtime 的 completion、verification snapshot、真实 Changes 和计数生成，停止状态永远优先于最近一次成功检查。

取舍：没有 Web 服务、SSE、浏览器前端、可写编辑器、Settings 页面、Git 面板或第二套异步 Runtime。项目树和预览刻意保持只读；历史是线性本地 Session，不伪装成分支式会话中心。基础安装不引入 Qt，CLI 仍可独立安装。

## 9. 为什么 Context 同时保存完整历史和有界模型视图

问题：按单条消息裁剪会产生 orphan tool result；固定最近 N 组又无法优先处理巨大旧输出；多轮 session 还需要持续保留当前任务。

选择：`Ctx` 在内存中保留完整多轮 logical groups，但 `build()` 每次只生成有界模型视图。

实现：

1. 当前用户 task 单独锚定；
2. assistant tool call 与全部 results 是不可拆组；
3. 同时检查字符预算和 `UTF-8 bytes / 4` 的近似 token 估算，并把固定 tool schemas 与输出预留算入 context window；
4. 优先把较旧 tool output 替换为带原长度的占位符；
5. 仍超预算才删除最旧完整 groups；
6. 当前 task 和配置数量的最新 groups 不裁；
7. 每轮重新注入 Runtime 自己生成的确定性状态。

RuntimeState 包含 revision、验证状态、已跟踪变化文件、最新 check、当前 Plan、外部 shell 变更标记和快照是否完整。它不是模型摘要，因此不会因历史裁剪而“记错”运行状态。

为减少无意义的重复输入，`read_file` 对“同一路径、同一实际显示范围、同一输出摘要”做 observation 去重。工具每次仍重新读取并计算当前结果，真实性不依赖缓存。Runtime 另外维护一个仅限当前进程、按范围数和总字符数双重限制的精确源码 working set；普通最近组窗口淘汰某次读取后，`Ctx` 会固定包含该结果的最新原始 logical group，而不是合成新的 assistant 消息。这样 `assistant` 的 `reasoning_content`、tool call 和 result 都保持 provider 返回的原始协议，源码也不会被提升到 system role。若保留一个小片段需要额外携带超过 6k 字符的 reasoning 或其他结果，则不固定该高开销组；`Ctx` 会把本轮实际仍可见的精确结果反馈给 `State`，不可见项立即退出 compact cache，下次读取必须重新发送全文。只有读取前已经存在精确证据时才能返回 compact hit。新用户 turn 会清空 working set；文件被 Agent、shell 或用户修改时，只使对应路径的证据和 inspected-range ledger 立即失效，不牵连未变化文件。

命令输出超过 `MAX_TOOL_CHARS` 时，Runtime 把脱敏后的全文流式保存到当前 session 的 `.agent/outputs/`，tool result 只返回 head/tail preview、总字符数和 opaque id。`read_command_output` 只能读取当前 session（以及用户显式 shell scope）的指定 id，并直接按字符范围读取，不会先把全文重新装入内存；因此模型不必为了找回被截断的错误信息重跑测试，也不会获得读取其他 `.agent` 私有文件的通道。`read_file` 对超长单行做显式截断并保留续读行号；`list_dir`、`glob_files` 与 `search_text` 使用 `offset/limit` 分页，使 observation 在单轮内有界，同时仍能继续取得完整证据。

取舍：working set 只保存已经真实返回给模型的有限精确文本，不做语义合并，也不写入 session；默认上限为 12 个范围、34k 字符，同时不超过字符窗口的 9/16。这个比例允许中小项目的一组核心源码跨裁剪保留，但总请求仍受原有 60k 字符、32k Token 预算及 tool schema / 输出预留共同约束，不以扩大每轮输入换取更少调用。超出限额按最近使用顺序淘汰，模型仍可按需读取缺失的小范围。它减少的是裁剪导致的重读，不缓存文件真实性，也不取代 stale-read 检查。估算不等同于具体 provider tokenizer；最新 group 单独超预算时会记录 `over_budget`，而不是破坏协议。GUI、Evidence Report 和 eval 会在 provider 提供相应字段时记录 prompt、completion、cache hit/miss 与 reasoning tokens，字段缺失时不伪造为 0。暂不做 LLM compaction summary、RAG、embedding 或 vector DB，避免额外调用和不可确定的信息损失。

## 10. 为什么 Repo Map 使用 Python AST 加轻量多语言声明匹配

问题：glob + regex 能找到文本，但模型仍需大量机械搜索才能知道中型仓库里有哪些核心符号。

选择：增加按需 `repo_map` Observe tool。Python 使用标准库 AST；JavaScript/TypeScript、Go、Rust、Java、C/C++、Ruby 和 Shell 使用行锚定的保守声明规则。

实现：Python 提取顶层 class/function、class method、async 标记、参数名和行号；其他语言只提取明确的类型、函数等声明并保留行号。文件按目录深度、声明密度、入口文件和测试文件权重排序，再受单文件大小、文件数、每文件符号数、总符号数和字符预算约束。Python 语法错误时只退回声明 regex 并显式标注。每次调用即时构建，不维护可能陈旧的后台索引。

取舍：非 Python 结果只是导航提示，不是假装精确的语义索引；模型编辑前仍必须读取文件。不使用 graph ranking、tree-sitter、LSP 或后台索引，保持依赖为零、行为可解释，并避免修改后读到旧 cache。

## 11. 为什么 Planner 只做导航

问题：复杂任务跨越多个模型回合后容易偏离目标，但把计划变成审批或完成门禁会重复现有 Permission 与 Verifier 的职责。

选择：只保留一个快照式 `update_plan`。每次提交 1–8 个 `pending / in_progress / completed` 步骤，最多一个步骤进行中；简单任务不要求创建计划。对于已有仓库，首次计划前先完成最少必要的只读调查；空仓库可以直接按技术问题规划。

实现：`planner.py` 保存短小、可序列化的 `PlanState`，并提供保守的生成策略检查。Runtime 只拒绝两类高置信度问题：已有仓库在没有至少两种有效只读观察时立即创建首次计划，以及由“实现功能 / 写测试 / 写 README / 跑测试”等通用活动构成的模板。System prompt 给出调度器和成绩分布两组任务特有示例，要求通常用 3–7 个可独立验证的技术里程碑，并要求计划、进度和最终答复跟随用户语言；测试步骤必须说明验证的行为或边界，文档只有在用户明确要求时才单独进入计划。当前计划以紧凑确定性文本进入 Runtime context，通过 Session JSONL 恢复，并记录独立 `plan_update` trajectory event；真实文件改动或验证后，Runtime 会在下一模型回合静默要求同步计划状态，事件到达后 CLI 与 GUI 立即重绘。若模型在最终答复前仍遗漏同步，Runtime 只会在独立 Verification Gate 已接受完成后关闭残留导航状态；这个收口不能产生或替代验证。只有新证据、失败根因或实现路线改变时才改写步骤文本。Planner 不触碰 workspace revision、fingerprint、Checkpoint 或 verification；任务因 step/time/token limit 停止时，界面保留真实未完成步骤。

取舍：策略检查有意不尝试理解任意自然语言计划，也不生成或重写模型的步骤；它只拦截明确的时机和模板错误，避免把 Planner 变成另一套语义引擎。没有 `create_plan / finish_task`、用户审批式 Plan Mode、第二个模型或独立 Planner Agent，也没有为展示层加入 Web 服务、通用 EventBus 或独立业务状态。计划表达“Agent 打算如何前进”，Verifier 才表达“当前代码是否真的通过”。

## 12. 为什么使用确定性的效率边界

问题：模型可能连续重复完全相同的读取或命令，浪费轮次和 token。

选择：第三次连续相同的已注册工具调用返回 stagnation observation，用于阻止完全相同的无效调用循环。对普通只读调查不再设置额外回合上限或弹出警告；Runtime 保留已读文件区间账本并在上下文裁剪后继续提供，模型据此避免重建广泛文件视图。`check_command` 另有只面向验证失败的 Repair Progress；重复的未变化 `read_file` 输出由第 9 节的短缓存减少 token；`AGENT_MAX_TASK_TOKENS` 可选限制一次用户任务的累计 usage。

实现：工具名和排序后的 JSON 参数形成 fingerprint；工具或参数变化立即重置。失败检查会去除路径前缀、时间戳、耗时、地址、行号和 pytest 进度噪声，再对保留的 failing test、exception、assertion/error 做 SHA-256。相同失败第二次给出根因复查警告，第三次标记 `NO_PROGRESS` 并停止；失败身份变化或验证成功会重置 streak。它只评价 repair 是否推进，不写入 `ok_rev`，也不替代 final gate。任务 Token 计数与 session 累计计数分开：达到配置预算 80% 后 RuntimeState 提醒模型减少探索，达到上限后在完整 tool-call/result 组边界停止，Session 仍可恢复；新用户 task 重置任务计数。默认值 0 表示关闭，provider 不返回 usage 时不伪造估算值。

取舍：不做“低收益思考”语义判断，也不让另一个模型承担规划判断；这类启发式容易把必要的诊断误判为浪费。失败指纹是有界、确定性的近似，误归一化时宁可提前停止并保留可恢复 Session，也不会把失败误报为成功。预算只约束模型 usage，不假装限制本地命令成本。

## 13. 为什么 Final Verifier 可以由用户/项目固定

问题：单纯要求“某个 `check_command` 返回 0”仍允许模型选择过弱的检查，例如只运行一个无关测试。

选择：默认保留模型选择；用户或项目可通过 `AGENT_FINAL_VERIFIER` 或根目录 `.agent-verifier` 固定精确命令。Agent 自建测试只是中间证据，不能替代已经配置的 oracle。

实现：`workspace_state.py` 在进程开始、命令执行前后建立有界快照；检测到一组 added/changed/deleted 文件时，把它作为一次 workspace transition 增加 `rev`、记录路径并使旧验证失效。每个文件记录 size、mtime、ctime、文件标识和 SHA-256；后续扫描只有前四项全部相同时才复用已有摘要，变化或不确定时重新读取。这样普通测试前后仍遍历目录，但不再反复读取未变化文件。文件数、单文件和总哈希字节都有上限，超过后明确写入 `workspace_tracking_complete = false`，不把局部观察伪装成完整结果。

可再生产物采用三层保守规则：少量内置 Runtime/依赖/缓存噪声；仅在确认处于 Git 仓库时读取根 `.gitignore` 的常用 glob/negation 规则，并始终保留 Git 已跟踪文件；可选根 `.agentignore` 显式排除项目特有的覆盖率、构建或临时输出。两个项目规则文件本身始终被跟踪，规则在进程启动时固定；`.agentignore` 作为 Runtime 策略修改时需要单独确认，不能在当前运行中即时隐藏文件。没有配置的 untracked 文件不会被一概排除。

`check_command` 只有在精确 verifier（如已配置）返回 0 且检查自身没有改变非忽略文件时，才把 `ok_rev` 与当时的 workspace fingerprint 一起保存。模型返回 final 后，Runtime 会再次捕获工作区：revision、fingerprint 任一不匹配都拒绝结束并要求重新验证。因此通过的是一个具体的已跟踪代码状态，不只是一个历史 exit code。`.agent-verifier` 属于受保护文件，当前进程仍使用启动时加载的原命令。

验证还区分 freshness 与 adequacy。前者回答“成功检查对应的是否仍是当前代码”，后者回答“检查范围是否满足本轮明确要求”。`verification.py` 只在用户明确写出“全部/所有/全量测试”或对应英文表达时提高范围要求；常见的 `pytest tests`、`unittest discover`、`npm test` 等可识别为 full suite，单个测试文件、`-k` 选择器或无法确定的复合命令只作为 targeted/unknown 中间证据。局部检查成功仍会保留 revision 与 fingerprint 事实，但 final gate 继续要求全量检查。用户/项目配置的精确 final verifier 仍是更强、更明确的 oracle。只有 Runtime 正常接受 final、验证仍新鲜且范围充分时，界面才显示 `FINAL VERIFIED`；limit/error/interruption 停止最多显示 Last Verification 或 `PARTIALLY VERIFIED`。

取舍：增量复用依赖普通文件系统维护的元数据，不声称抵抗恶意保留/伪造全部元数据的篡改；快照也不是 OS 级文件监控。极大文件或仓库仍会降级为有标记的 partial 跟踪。Ignore 有意只支持根规则文件的常用语义，不实现完整 Git exclude 引擎；不确定时选择纳入快照。无精确 oracle 时的 full-suite 识别是保守命令规则，不做自然语言测试充分性证明；无法识别时宁可要求更明确的全量命令。精确命令匹配牺牲一点命令等价灵活性，换来清楚的 oracle。真正的隐藏测试应由评测者放在 Agent workspace 之外并独立重跑；workspace 内的 `.agent-verifier` 只是可见配置。Verifier 仍只能证明该命令成功执行，不能证明测试充分或程序形式化正确。

## 14. 为什么加入独立 Eval Harness

问题：单元测试证明 Runtime 机制工作，却不能说明真实模型是否会完成 coding task、是否会篡改测试、需要多少步骤和 token。

选择：提供 8 个可重复的小型真实修复任务和一个 opt-in 模型评测脚本。

实现：`evals/run_eval.py` 为每个 case 建立隔离临时 workspace，先确认可见 oracle 初始失败，再运行真实 Agent；Agent 退出后才在另一个临时目录物化 hidden grader。Harness 哈希检查可见 tests 未改变，运行可见与隐藏 verifier，并从 trajectory 统计 success、false completion、steps、tokens、model/tool calls、tool errors、首次 check、verification attempts、failed checks、failure recovery、`NO_PROGRESS`、工作区变化事件和耗时。`--repeat` 保留全部重复结果；结果写入 Git-ignore 的 JSON。`evals/BENCHMARK_PROTOCOL.md` 另行固定 BigCodeBench 30-case 与 SWE-bench Verified 5-case pilot 的版本、确定性选题和报告口径，不把小样本冒充官方完整分数。

取舍：这些任务比 SWE-bench 小，目的是项目级回归和演示证据，不声称是通用 benchmark。hidden grader 在 Agent 工作区之外，但不是针对本地恶意进程的安全隔离。无 API 配置时只运行 dry-run fixture validation；真实模型或公共 benchmark 结果只有在冻结配置并实际运行后才记录。

## 15. 为什么没有做通用 Hooks / Plugins

问题：permission、GitGuard、repetition、checkpoint、verification 和 session 都围绕 Agent Loop，继续硬编码可能让主循环膨胀。

选择：使用职责明确的小 manager 和一个 `persist_group` 回调，而不是通用 middleware/plugin framework。

实现：Agent Loop 只保留协议编排；权限、命令执行、Git、checkpoint、repetition、context 和持久化分别在独立模块中。新增逻辑通过 State 所有权和窄接口连接。

取舍：没有动态 hook 注册、第三方工具加载或 extension lifecycle。对当前规模而言，明确依赖比抽象扩展性更容易解释和测试。

## 16. 明确不做什么

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

这些功能并非没有价值；当前设计优先保持核心 Runtime 清晰、可靠且可测试，并要求每项增强都有明确收益和边界。
