# JARVIS-Win

一个常驻 Windows 的 AI 助手，带终端 TUI。能对话，也能真的干活：执行命令、读写文件、体检系统、看/改剪贴板、管理进程、记住你的偏好、按计划多步执行、准点自动跑任务、上网查资料。

- **Phase 1**：Textual TUI + 可插拔 OpenAI 兼容模型 + 工具调用循环 + 破坏性操作确认 + 会话历史落库
- **Phase 2**：长期记忆、计划模式（可执行的多步任务）、定时任务、全局热键常驻守护
- **Phase 3**：自定义 provider（含内网/自建端点）、多模型热切换与故障自动切换、联网搜索与网页抓取、系统托盘图标
- **Phase 4**：剪贴板读写（纯 ctypes，零依赖）、进程管理（列表 / 详情 / 结束 / 挂起恢复）；顺带修掉了 psutil 并发与系统面板卡界面的隐患，并把横幅与侧栏改成按终端「格」对齐
- **界面**：左侧命令菜单（`Ctrl+B`）+ 右侧系统面板（`Ctrl+S`），两侧栏默认收起，键盘可全程操作
- **Phase 5**：并行子任务。主 Agent 把可拆的 N 个子任务交给 `delegate_subagents`，子 Agent 独立对话、共享工具与数据；超时（默认 30s）后先把已完成的部分输出，剩下的继续跑完再追加最终汇总
- **v0.6.1**：思考过程展示。推理模型的 `reasoning_content` / `reasoning` 流以「思考过程」折叠块显示在回答上方，默认收起、每轮一块，不进上下文
- **v0.6.2**：工具调用折叠概览。`⚙ 工具名 + 关键参数` 一行常驻，点击展开完整参数与输出；无 start 的结果（拒绝确认/子代理汇总）自成完成态块

## 快速开始

```bash
cd D:/code/workbuddy-projects/project1/jarvis
uv sync                            # 安装依赖
uv run jarvis --selftest           # 不开 TUI，自检配置、工具与调度器
uv run jarvis --selftest --ping    # 额外发一次真实模型请求
uv run jarvis                      # 启动 TUI
uv run jarvis --daemon             # 常驻：全局热键 + 定时任务 + 托盘图标（另开一个终端跑）
```

首次运行会生成 `config.toml`（已 git-ignore）。填入 API Key：

```toml
[models.deepseek]
api_key = "sk-..."      # 或者留空，改设环境变量 DEEPSEEK_API_KEY
```

## 交互

直接输入问题，回车发送。JARVIS 自行决定是否调用工具。

### 界面布局

窗口分三栏：左侧命令菜单、中间聊天区、右侧系统面板。

```text
┌─ 菜单 (Ctrl+B) ──┬────────── 聊天 ──────────┬─ 系统面板 (Ctrl+S) ─┐
│ ── 会话 ────      │  ██╗ █████╗ ██████╗ ...   │ SYSTEM             │
│ 帮助与命令   F1   │                          │ CPU  12.2% · 16 核 │
│ 清屏对话     Ctrl+L│ › 搜一下今天的 AI 动态   │ MEM  57% · 18/31GB │
│ ── 模型 ────      │                          │ TOP 进程           │
│ 切换模型…    /model│ ⚙ web_search …           │  python.exe  5.0%  │
└───────────────────┴──────────────────────────┴────────────────────┘
```

- **两个侧栏默认都是收起的**，聊天区先占满窗口；左侧只留一条 `≡` 边栏提示菜单的存在。面板收起时连进程采样都停掉，不白烧 CPU。
- **`Ctrl+B` 打开左侧命令菜单**：`↑`/`↓` 移动，`Enter` 执行，`Esc` 收起；执行完自动收起并把光标交回输入框。右列写的是等价的命令或快捷键，所以菜单本身就是一张速查表。
- **`Ctrl+S` 打开右侧系统面板**：CPU（含每核）+ 内存 / 交换 / 网络 / 电池 + 磁盘 + 占资源最多的进程 + 本会话模型与记忆信息。展开时立刻重新采样一次，所以看到的不是上次留下的旧数据。

命令菜单里能做的事：

| 分区 | 条目 |
|------|------|
| 会话 | 帮助与命令 F1 · 清屏对话 Ctrl+L · 历史会话 · 重置上下文 |
| 模型 | 切换模型… · 模型列表 · 服务商列表 |
| 工具 | 系统体检 · 进程管理 · 剪贴板 · 工具清单 |
| 任务 | 定时任务 |
| 记忆 | 长期记忆 · 提炼记忆 |
| 其他 | 切换主题 · 退出 Ctrl+Q |

### 基础

| 命令 | 作用 |
|------|------|
| `/help` | 帮助面板 |
| `/tools` | 列出工具及其确认策略 |
| `/sys` | 立刻输出系统体检报告 |
| `/clip` | 查看剪贴板（`/clip set <文本>` 写入 · `/clip clear` 清空） |
| `/ps` | 进程列表（`/ps mem` 按内存 · `/ps chrome` 按名字过滤） |
| `/history` · `/resume <id>` | 最近会话列表 · 载入某个历史会话作为上下文 |
| `/reset` · `/clear` · `/theme` · `/quit` | 清空上下文 · 清屏 · 切主题 · 退出 |

### 模型与 provider（热切换）

内置 18 个 provider 预设（DeepSeek / 通义 / 豆包 / Kimi / 智谱 / OpenAI / Ollama / LM Studio / vLLM ……）。
一个 *provider* 是「端点 + Key 放哪」，一个 *model* 是「provider + 模型 ID」，模型可以只写 provider 继承端点。

```text
/model                      打开交互式选择器（↑↓ 选 · Enter 切换 · Esc 取消）
/model list                 打印模型表（带序号 / provider / Key 状态）
/model 2                    按序号热切换；也可以 /model qwen、/model deepseek-chat
/model add nei --provider myvllm --model qwen3-32b   新增模型
/model rm nei               删除模型（在用中的会拒绝）
/model default qwen         设为默认模型（/model 切换时也会自动记住）
/model fallback deepseek ollama     指定故障切换链
/provider list              列出 provider 与密钥状态
/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY --label 公司内网
/provider rm myvllm
```

- **切换会被记住**：切换模型的同时会写成默认模型，下次启动仍是它（想临时试一个，试完切回来即可）。
- **切换保留上下文**：换模型不会清空对话历史。
- **故障自动切换**：当前模型网络失败 / 配额用尽 / Key 失效时，自动按 `fallback` 链试下一个，
  并在界面上说明「哪个模型挂了、切到了哪个」。没显式配链时会自动挑其它「可用」的模型（最多两个）。
- **本地/内网端点不要求 Key**：`localhost`、`127.0.0.1`、`192.168.*`、`10.*`、`172.16-31.*`、`*.local`
  会识别为自建端点，不会误报「缺 Key」（Ollama / LM Studio / 公司 vLLM 开箱即用）。
- **单模型超时**：`/model add ... --timeout 30`（默认 180 秒）。端点挂死时不用干等。

### 手动添加自定义 provider（改配置文件）

`config.toml` 就在项目根目录（已 git-ignore）。除了 `/provider add`，也可以直接手写，改完**重启 JARVIS** 生效。
两种写法：

**写法一：先定义 provider，多个模型共享（推荐）**

```toml
# 端点与 Key 来源集中写一次
[providers.myapi]
label = "我的中转"                              # 显示名；省略则用条目名（覆盖内置时继承内置名）
base_url = "https://api.example.com/v1"         # 必填，OpenAI 兼容端点
api_key_env = "MYAPI_KEY"                       # 从环境变量读 Key
models = ["gpt-4o-mini", "claude-sonnet-4"]     # 可选，仅供 /provider list 展示

# 模型只写 provider + 模型 ID，端点与 Key 自动继承
[models.myapi]
provider = "myapi"
model = "gpt-4o-mini"
```

**写法二：不定义 provider，直接写全（最简）**

```toml
[models.quick]
base_url = "https://api.example.com/v1"
model = "gpt-4o-mini"
api_key = "sk-xxxx"          # 也可以换成 api_key_env = "MYAPI_KEY"
```

**字段说明**

| 字段 | 位置 | 必填 | 说明 |
|------|------|------|------|
| `base_url` | `[providers.*]` · `[models.*]` | 是 | OpenAI 兼容端点，一般以 `/v1` 结尾 |
| `model` | `[models.*]` | 是 | 服务商文档里的模型 ID |
| `provider` | `[models.*]` | 否 | 填了就继承该 provider 的端点与 Key 来源 |
| `api_key` | 两处皆可 | 否 | 明文 Key；与 `api_key_env` 都写时以它为准 |
| `api_key_env` | 两处皆可 | 否 | 环境变量名，如 `MYAPI_KEY` |
| `label` | 两处皆可 | 否 | 显示名；不写时自定义项用条目名，覆盖内置时继承内置名 |
| `note` | `[providers.*]` | 否 | 备忘说明，`/provider list` 里显示 |
| `models` | `[providers.*]` | 否 | 只给 `/provider list` 展示用，**不会自动生成模型** |
| `supports_tools` | `[models.*]` | 否 | 默认 `true`；端点不支持函数调用就设 `false`，会走纯对话 |
| `temperature` | `[models.*]` | 否 | 默认 `0.3` |
| `timeout` | `[models.*]` | 否 | 默认 `180` 秒；端点容易挂死就调小 |
| `default_model` | 文件顶部 | 否 | 设为默认模型，如 `default_model = "myapi"`（也可用 `/model default myapi`）|

**几点注意**

- **覆盖内置 provider**：只写想改的字段即可，其余继续继承内置预设。例如把内置 OpenAI 指到你的代理：

  ```toml
  [providers.openai]
  base_url = "https://my-proxy.example.com/v1"
  ```

  显示名仍是 `OpenAI`，Key 仍读 `OPENAI_API_KEY`。想恢复用 `/provider rm openai`，或删掉这段手工配置。
- **本地 / 内网端点不要求 Key**：`localhost`、`127.0.0.1`、`192.168.*`、`10.*`、`172.16-31.*`、`*.local`
  会被认作自建端点，不会误报「缺 Key」。
- **`console` 字段无效**：那是内置预设用来标「去哪申请 Key」的，手写的 provider 不读它。
- **JARVIS 会重写这个文件**：执行 `/provider add`、`/model` 等命令后 `config.toml` 会被重新生成
  （注释与字段顺序会变），但只写出你实际设置过的字段，语义不变。手写后建议用 `/provider list` 和 `/model`
  确认读到了。

### 并行子任务

主 Agent 在系统提示里被教了：当用户的问题天然可拆成 N 个**互不依赖**的子任务时，调
`delegate_subagents(prompts=[...])` 一次派给多个子 Agent 并行执行。每个子 Agent
独立对话、独立 LLM 连接池，但**共享** Config / ToolRegistry / 数据库（`/remember`
之类会写入同一个 `data/jarvis.db`）。子 Agent 走和主 Agent 同样的工具，**但**
危险工具会被一个内置的「总是拒绝」confirm handler 拦住，不会阻塞主对话。

**典型场景**：并行搜索多组关键词、并行抓多个 URL、同时读 / 总结多份文件、并行跑多个
独立实验；任何「A、B、C 一起做」的请求。

**两阶段输出**（在 TUI 里看得最清楚）：
1. 派发后立刻显示 `→ 派发了 N 个子任务：…`。
2. 每个子 Agent 完成时显示 `✓ 子任务 #i done · X 工具 · Y.Ys`（或 `✗` / `⏱`）。
3. 跑满 `Config.subagents.default_timeout`（默认 30 秒）时立刻输出**部分快照**：
   `⏱ 默认超时 30s：已完成 X/N，仍在跑：#i、#j。先把已有结果继续推进，剩下的完成后
   会自动追加最终汇总。` —— 主 Agent **不会**被这里打断，它仍在等真正的最终结果。
4. 所有子 Agent 都落地后输出 `🏁 子任务全部结束：…`，并把完整汇总作为工具结果喂给
   主 Agent；主 Agent 据此做最终综合回答。

**配置**（`config.toml` 的 `[subagents]`）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `default_timeout` | 30.0 | 触发「部分快照」的秒数（仅影响 UX，不取消子 Agent） |
| `max_runtime` | 600.0 | 单个子 Agent 的绝对最长用时，超过会被中止 |
| `max_concurrent` | 4 | 同时在跑的子 Agent 上限（asyncio 信号量） |
| `max_per_call` | 8 | 一次 `delegate_subagents` 调用允许的最多的 prompt 数 |

子 Agent 跑出来的工具调用和记忆写入都会进主数据库——它们之间是真正「在干同一个项目的
一队人」，不是相互隔离的沙箱。

### 思考过程（推理模型）

推理模型（DeepSeek-R1 / deepseek-reasoner、OpenRouter 上的 reasoning 模型等）会在给出
答案之前先流式输出一段「思考」：客户端读取 SSE delta 里的 `reasoning_content`（DeepSeek
风格）或 `reasoning`（OpenRouter 风格）字段，透传成 `thinking` 事件流。

在 TUI 里，每轮模型输出的思考显示在回答上方的「思考过程」折叠块中：

- **默认折叠**，只占一行标题，不干扰阅读；点击标题（或聚焦后回车）展开。
- 结束后标题会带上字数，如 `思考过程（1234 字）`。
- **每一轮工具调用各有一块**——模型每轮都会重新思考，块也各自独立。
- 思考流**不进对话上下文**（不写入 messages 历史），也不进长期记忆，只用于展示。

普通（非推理）模型没有这个流，界面与行为完全不变。

### 工具调用（折叠概览）

工具调用同样默认折叠成**一行概览**：`✓ ⚙ run_shell  echo hi  →  hi`（状态 mark + 工具名 +
最有信息量的参数 + 结果首行摘要）。点击标题展开后可看到完整参数 JSON 与结果输出（超过
12 行截断并标注剩余行数）。要点：

- 运行中概览带 `…` 标记，完成后变 `✓` / `✗`（失败整行标红），并追加结果摘要（`→  首行`，
  按 48 格截断）。
- 没有对应 start 的结果（用户拒绝确认、子代理汇总）会自成一块，直接以完成态显示。
- 概览行常驻可见，输出细节按需展开——长输出不再刷屏；连续折叠块之间无空行，紧凑排列。

### 联网搜索

```text
/search                    查看当前后端与用法
/search 关键词              直接搜一次
/search backend tavily     换后端（duckduckgo 免 Key / bocha 中文好 / tavily 摘要干净 / serper / searxng）
/fetch example.com         抓网页正文
```

也可以直接说「搜一下 xxx」，模型会自己调 `web_search` 工具；需要细节时它还会追加 `fetch_url` 读正文。
免 Key 的 DuckDuckGo 后端默认可用，搜索与抓取都走标准库，没有新增依赖。

### 剪贴板

```text
/clip                      看看现在剪贴板里有什么（文本 / 文件列表 / 格式）
/clip set <文本>           把这段文本放进剪贴板，之后直接 Ctrl+V
/clip clear                清空剪贴板
```

对话里同样好使：「总结一下我复制的内容」「把刚才的结论复制到剪贴板」「我在资源管理器里复制了哪些文件」。
直接用 Win32 剪贴板 API（ctypes，无新增依赖），也能识别「复制文件」产生的 `CF_HDROP` 文件列表。
读取免确认；**写入和清空会覆盖你剪贴板里的东西，所以每次都会弹窗确认**。
剪贴板同一时刻只能被一个程序打开，遇到别的程序（截图工具、剪贴板管理器、聊天软件）占用时会自动重试，
仍失败则明确告诉你原因，而不是静默失败。

### 进程管理

```text
/ps                        占用 CPU 最高的 15 个进程
/ps mem                    按内存排序；/ps chrome 只看名字含 chrome 的
```

对话里：「谁在吃 CPU」「Chrome 开了几个进程、占多少内存」「把那个卡住的进程关掉」「先把它冻住别占 CPU」。
四个工具：`list_processes`（列表）、`process_info`（详情：命令行 / 父子进程 / 网络连接 / 启动时间）、
`kill_process`（结束，温和失败可 `force`）、`freeze_process`（挂起 / 恢复）。
**结束和挂起都会弹窗确认**，并且有一层硬拦截：`System` / `smss` / `csrss` / `lsass` / `services` / `winlogon`
等系统关键进程，以及 JARVIS 自己所在的进程链，一律拒绝——不依赖模型自觉。
列表里的 CPU% 已按逻辑核数归一化，和任务管理器口径一致。

### 并行子任务

模型自动按需调用 `delegate_subagents(prompts=[...])`。详见
[并行子任务](#并行子任务) 段。

### 长期记忆

| 命令 | 作用 |
|------|------|
| `/facts` | 查看已沉淀的事实 |
| `/remember <内容>` | 手动记住一条（例如「我习惯用 PowerShell」） |
| `/forget <编号>` | 删除某条记忆 |
| `/learn` | 立刻从最近对话提炼记忆 |

开启 `memory.auto_learn`（默认开）后，每 N 轮对话（`learn_every`，默认 3）会自动做一次提炼，
把偏好、项目约定、已定结论写进记忆，并在系统提示里以「关于这位用户的长期记忆」注入。
重复内容会自动合并，不会越攒越乱。

### 计划模式

```text
/plan 把 D:\code\demo 里所有 .tmp 文件找出来并汇报   → 先出计划
/do                                              → 按计划逐步执行（面板实时打勾）
/auto <任务>                                      → 出计划并立即执行
```

计划请求不带工具、只要求 JSON，解析失败时会退化为「读取编号列表」，不会因为模型格式漂移而整体失败。

### 定时任务

```text
/task add 检查磁盘剩余空间 @daily 09:00
/task add 汇总今天新增的日志 @every 2h
/task add 清理临时目录 @once 2026-09-12 08:00 --danger
/task list | /task on <id> | /task off <id> | /task rm <id> | /task run <id>
```

- 三种调度：`@every 30m|2h` · `@daily HH:MM` · `@once 2026-09-12 08:00`
- 加 `--danger` 该任务才被允许执行写操作；默认只放行只读工具，危险调用会被拒绝并记进日志。
- 任务存在 SQLite 里，TUI 和守护进程共用；错过整段时间不会「补跑风暴」，会从当下重新计时。
- 跑完会弹 Windows 通知（`daemon.notify`，默认开），结果同时落库。

### 常驻、热键与托盘

```bash
uv run jarvis --daemon                 # 守护：热键 + 托盘 + 定时任务
uv run jarvis --daemon --hotkey ctrl+alt+k
uv run jarvis --daemon --no-tray       # 不要托盘图标
uv run jarvis --no-scheduler           # TUI 不接管调度器（热键拉起时用）
uv run jarvis --tasks                  # 只打印任务列表
uv run jarvis --providers              # 只打印 provider 列表
```

- 按 `Ctrl+Alt+J`（`daemon.hotkey` 可改）新开一个终端窗口运行 TUI。
- 通知区出现 JARVIS 图标，右键菜单：**打开 JARVIS / 定时任务 / 状态 / 退出**，左键单击也能直接唤出窗口；
  任务跑完会用图标气泡提示。
- 守护进程用 pid 文件（`data/jarvis.pid`）防重复启动，日志写在 `data/daemon.log`。
- 托盘用 ctypes 直接调 `Shell_NotifyIconW`，没有引入新依赖；图标资源由 `scripts/make_icon.py` 生成。

快捷键：`Ctrl+Q` 退出 · `Ctrl+L` 清屏 · `Ctrl+B` 命令菜单 · `Ctrl+S` 系统面板 · `F1` 帮助 · `F2` 任务 · `Ctrl+X` 取消任务（两个侧栏默认收起）

## 架构

```
TUI (Textual)  →  Agent 内核  →  工具层  →  LLM 适配 / 记忆
                    ↓
        计划器 · 调度器 · provider 目录 · 守护进程（热键 + 托盘）
```

- `src/jarvis/tui/` — 界面：聊天流、左侧命令菜单、系统面板、确认弹窗、帮助面板、计划面板
- `src/jarvis/core/` — `agent.py` 对话循环（流式 + 工具调度 + 计划执行 + 记忆提炼 + 故障切换 + delegate_subagents 拦截）、`registry.py` 工具注册与截断、`planner.py` 计划解析、`scheduler.py` 调度与下次执行时间计算、`prompts.py` 人设与系统提示、`subagent.py` SubAgentManager（并发执行 + 部分/最终双输出）
- `src/jarvis/llm/` — 仅依赖 OpenAI 兼容协议；端点不支持 tools 时自动降级为纯对话
- `src/jarvis/providers.py` — 内置 provider / 搜索后端预设目录 + base_url 反查
- `src/jarvis/tomlwrite.py` — 最小 TOML 序列化（配置可写回）
- `src/jarvis/textwidth.py` — 终端「格」宽计算（汉字算 2 格）与裁剪 / 补白：横幅、侧栏、进程表的对齐都走它
- `src/jarvis/tools/` — `shell`（命令执行 + 黑名单护栏）、`fs`（读写/列目录/搜索）、`sysinfo`（psutil 快照 + 采样串行化）、`clipboard`（ctypes 剪贴板）、`procman`（进程管理）、`web`（搜索 + 正文抓取）、`notify`（Windows 通知）
- `src/jarvis/memory/` — SQLite：会话历史 + 长期事实
- `src/jarvis/daemon/` — `hotkey.py`（RegisterHotKey 全局热键）、`tray.py`（通知区图标 + 右键菜单）、`service.py`（守护进程、无人值守任务执行）

## 工具与安全

| 工具 | 说明 | 确认 |
|------|------|------|
| `run_shell` | 执行 PowerShell 命令（默认 UTF-8 输出） | ⚠ 需要 |
| `write_file` | 写入 / 追加文件（拒绝写 `C:\Windows`、`Program Files`） | ⚠ 需要 |
| `read_file` | 读文本，支持分段 | 免确认 |
| `list_dir` / `search_files` | 列目录、按通配符递归查找 | 免确认 |
| `sys_report` | CPU / 内存 / 磁盘 / 网络 / 电池 / 高占用进程 | 免确认 |
| `web_search` | 联网搜索（默认 DuckDuckGo，免 Key） | 免确认 |
| `fetch_url` | 抓取网页并提取正文 | 免确认 |
| `read_clipboard` | 读剪贴板文本（附带格式与文件列表） | 免确认 |
| `write_clipboard` | 写入 / 追加剪贴板（`text=""` 等于清空） | ⚠ 需要 |
| `clear_clipboard` | 清空剪贴板 | ⚠ 需要 |
| `list_processes` | 进程列表（PID / CPU% / 内存 / 状态 / 用户） | 免确认 |
| `process_info` | 单个进程详情（命令行、父子进程、端口…） | 免确认 |
| `kill_process` | 结束进程（可 `force` 强杀） | ⚠ 需要 |
| `freeze_process` | 挂起 / 恢复进程 | ⚠ 需要 |

- 危险工具在 TUI 里弹窗确认，可选「同意 / 拒绝 / 本会话始终允许」。
- `run_shell` 内置硬拦截：格式化磁盘、`del /s`、`rm -rf /`、`reg delete`、`bcdedit`、`diskpart`、`vssadmin delete`、关机等一律拒绝执行。
- `kill_process` / `freeze_process` 另有硬拦截：`System` / `Idle` / `smss` / `csrss` / `wininit` / `services` / `lsass` / `winlogon` / `fontdrvhost` 等关键进程，以及 JARVIS 自身与其祖先进程链，无论怎么要求都拒绝。
- 定时任务默认**拒绝**危险操作，除非创建时加了 `--danger`（守护进程与 TUI 行为一致）。
- 确认框 5 分钟无响应自动按拒绝处理，不会把 Agent 卡死。
- API Key 只在 `config.toml` 或环境变量里，`config.toml` 与 `data/` 都在 .gitignore 中。
- 联网工具只做「搜」和「读」，不会自动提交表单或发帖。

## 测试

```bash
uv run python -u tests/phase2.py        # 计划/记忆/调度/热键（无网络，含真实热键注册）
uv run python -u tests/phase3.py        # provider 目录/配置读写/热切换/搜索解析/托盘结构（无网络）
uv run python -u tests/phase4.py        # 剪贴板往返（自动备份还原）+ 进程列表/详情/护栏/真实杀进程（无网络）
uv run python -u tests/phase5.py        # 并行子任务：manager 事件序列 / max_concurrent / 校验 / 端到端 Agent.run（无网络）
uv run python -u tests/smoke_tui.py     # 无头 TUI：命令、模型切换、provider 增删、任务、计划面板、子任务事件、弹窗
uv run python -u tests/daemon_check.py  # 守护进程：真实启动 + 热键注册 + 真实按键触发 + pid 防重
uv run python -u tests/tray_check.py    # 真实托盘：加入通知区、点击回调、气泡通知、任务栏重启后重挂
uv run python -u tests/live_agent.py    # 真实模型 + 真实工具端到端（需要 API Key）
uv run python -u tests/live_phase2.py   # 真实模型的计划模式 + 无人值守任务（需要 API Key）
uv run python -u tests/live_phase3.py   # 真实联网搜索 + 真实网页抓取 + 真实故障切换（需要 API Key）
uv run python -u tests/screenshot.py    # 导出 docs/screenshot.svg 界面快照
```

离线测试（phase2 / phase3 / phase4 / phase5 / smoke_tui）不需要网络也不需要 API Key，全程用临时数据库与临时配置，
不会动你的 `data/` 和 `config.toml`。`phase4.py` 会临时用一下系统剪贴板，但会先备份原内容并在结束时还原
（若剪贴板里有图片或文件列表这类无法还原的内容，则自动跳过写入测试）。

## 已知问题

- 进程退出时可能打印 `httpcore2 ... generator didn't stop after athrow()`。这是 openai 3.x 所带 httpcore2 在事件循环关闭时回收 SSE 生成器的内部噪音，功能不受影响。
- `ollama` 段默认指向本机 11434；未装 Ollama 时切换过去会报连接失败（会自动切到别的可用模型，如果配了 fallback）。
- 计划模式与记忆提炼都会额外消耗模型调用：`learn_every` 调大可以省钱，`auto_learn=false` 可完全关掉。
- 联网搜索依赖本机能出网。走代理时按系统 `HTTPS_PROXY` 环境变量；DuckDuckGo 偶发限流会返回空结果并提示换后端。
- 剪贴板是「同一时刻只能被一个程序打开」的系统资源：截图工具 / 剪贴板管理器 / 聊天软件占用时读写会失败，
  JARVIS 会重试约 1 秒再报明确错误；稍等重试即可。
- Windows 上遍历进程比想象中贵（这台机器约 380 个进程，取一次 CPU+内存要 1–2 秒，`status` 字段单项就要 1.7 秒）。
  因此：进程列表只对**要显示的几行**取昂贵字段；侧栏的 CPU/内存/磁盘每 2 秒刷新、进程表每 10 秒刷新，
  且都在后台线程里采样；`psutil` 的采样被一把全局锁串行化（psutil 的 Windows 扩展在多线程同时枚举时会互相卡死）。
- 终端按**格**排版，不看字符数：一个汉字占 2 格，所以 `len()` 补白一定会歪。两个侧栏的行都按格裁剪到自己的宽度
  再显示（`#side` 46 − 左右各 1 格内边距 − 1 格左边框 = **43 格**；`#menu` 30 − 2 − 1 = **27 格**）；
  超宽的行在栏里会折行，列就散了。改 `#side` / `#menu` 宽度时记得同步
  `sysinfo.PANEL_WIDTH` / `widgets.MENU_CONTENT`（CSS 里的宽度是从常量拼进去的，行宽是量出来的，测试会拦住不一致）。
- 横幅（`widgets.BANNER`）是 44 格宽的 figlet 原稿，别手动重排——`Banner` 会在聊天气泡窄于 44 格时
  自动降级成一行小字（两个侧栏都打开的 120 列终端就属于这种情况）。

## 路线图

- Phase 5：语音（faster-whisper STT + edge-tts TTS）—— 按你的要求暂缓
