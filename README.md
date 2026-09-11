# JARVIS-Win

一个常驻 Windows 的 AI 助手，带终端 TUI。能对话，也能真的干活：执行命令、读写文件、体检系统、记住你的偏好、按计划多步执行、准点自动跑任务、上网查资料。

- **Phase 1**：Textual TUI + 可插拔 OpenAI 兼容模型 + 工具调用循环 + 破坏性操作确认 + 会话历史落库
- **Phase 2**：长期记忆、计划模式（可执行的多步任务）、定时任务、全局热键常驻守护
- **Phase 3**：自定义 provider（含内网/自建端点）、多模型热切换与故障自动切换、联网搜索与网页抓取、系统托盘图标

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

### 基础

| 命令 | 作用 |
|------|------|
| `/help` | 帮助面板 |
| `/tools` | 列出工具及其确认策略 |
| `/sys` | 立刻输出系统体检报告 |
| `/history` · `/resume <id>` | 最近会话列表 · 载入某个历史会话作为上下文 |
| `/reset` · `/clear` · `/theme` · `/quit` | 清空上下文 · 清屏 · 切主题 · 退出 |

### 模型与 provider（热切换）

内置 18 个 provider 预设（DeepSeek / 通义 / 豆包 / Kimi / 智谱 / OpenAI / Ollama / LM Studio / vLLM ……）。
一个 *provider* 是「端点 + Key 放哪」，一个 *model* 是「provider + 模型 ID」，模型可以只写 provider 继承端点。

```text
/model                      列出全部模型（按序号切换）
/model 2                    按序号热切换；也可以 /model qwen、/model deepseek-chat
/model add nei --provider myvllm --model qwen3-32b   新增模型
/model rm nei               删除模型（在用中的会拒绝）
/model default qwen         设为默认模型
/model fallback deepseek ollama     指定故障切换链
/provider list              列出 provider 与密钥状态
/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY --label 公司内网
/provider rm myvllm
```

- **切换保留上下文**：换模型不会清空对话历史。
- **故障自动切换**：当前模型网络失败 / 配额用尽 / Key 失效时，自动按 `fallback` 链试下一个，
  并在界面上说明「哪个模型挂了、切到了哪个」。没显式配链时会自动挑其它「可用」的模型（最多两个）。
- **本地/内网端点不要求 Key**：`localhost`、`127.0.0.1`、`192.168.*`、`10.*`、`172.16-31.*`、`*.local`
  会识别为自建端点，不会误报「缺 Key」（Ollama / LM Studio / 公司 vLLM 开箱即用）。
- **单模型超时**：`/model add ... --timeout 30`（默认 180 秒）。端点挂死时不用干等。

### 联网搜索

```text
/search                    查看当前后端与用法
/search 关键词              直接搜一次
/search backend tavily     换后端（duckduckgo 免 Key / bocha 中文好 / tavily 摘要干净 / serper / searxng）
/fetch example.com         抓网页正文
```

也可以直接说「搜一下 xxx」，模型会自己调 `web_search` 工具；需要细节时它还会追加 `fetch_url` 读正文。
免 Key 的 DuckDuckGo 后端默认可用，搜索与抓取都走标准库，没有新增依赖。

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

快捷键：`Ctrl+Q` 退出 · `Ctrl+L` 清屏 · `Ctrl+S` 系统面板 · `F1` 帮助 · `F2` 任务 · `Ctrl+X` 取消任务

## 架构

```
TUI (Textual)  →  Agent 内核  →  工具层  →  LLM 适配 / 记忆
                    ↓
        计划器 · 调度器 · provider 目录 · 守护进程（热键 + 托盘）
```

- `src/jarvis/tui/` — 界面：聊天流、系统面板、确认弹窗、帮助面板、计划面板
- `src/jarvis/core/` — `agent.py` 对话循环（流式 + 工具调度 + 计划执行 + 记忆提炼 + 故障切换）、`registry.py` 工具注册与截断、`planner.py` 计划解析、`scheduler.py` 调度与下次执行时间计算、`prompts.py` 人设与系统提示
- `src/jarvis/llm/` — 仅依赖 OpenAI 兼容协议；端点不支持 tools 时自动降级为纯对话
- `src/jarvis/providers.py` — 内置 provider / 搜索后端预设目录 + base_url 反查
- `src/jarvis/tomlwrite.py` — 最小 TOML 序列化（配置可写回）
- `src/jarvis/tools/` — `shell`（命令执行 + 黑名单护栏）、`fs`（读写/列目录/搜索）、`sysinfo`（psutil 快照）、`web`（搜索 + 正文抓取）、`notify`（Windows 通知）
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

- 危险工具在 TUI 里弹窗确认，可选「同意 / 拒绝 / 本会话始终允许」。
- `run_shell` 内置硬拦截：格式化磁盘、`del /s`、`rm -rf /`、`reg delete`、`bcdedit`、`diskpart`、`vssadmin delete`、关机等一律拒绝执行。
- 定时任务默认**拒绝**危险操作，除非创建时加了 `--danger`（守护进程与 TUI 行为一致）。
- 确认框 5 分钟无响应自动按拒绝处理，不会把 Agent 卡死。
- API Key 只在 `config.toml` 或环境变量里，`config.toml` 与 `data/` 都在 .gitignore 中。
- 联网工具只做「搜」和「读」，不会自动提交表单或发帖。

## 测试

```bash
uv run python -u tests/phase2.py        # 计划/记忆/调度/热键（无网络，含真实热键注册）
uv run python -u tests/phase3.py        # provider 目录/配置读写/热切换/搜索解析/托盘结构（无网络）
uv run python -u tests/smoke_tui.py     # 无头 TUI：命令、模型切换、provider 增删、任务、计划面板、弹窗
uv run python -u tests/daemon_check.py  # 守护进程：真实启动 + 热键注册 + 真实按键触发 + pid 防重
uv run python -u tests/tray_check.py    # 真实托盘：加入通知区、点击回调、气泡通知、任务栏重启后重挂
uv run python -u tests/live_agent.py    # 真实模型 + 真实工具端到端（需要 API Key）
uv run python -u tests/live_phase2.py   # 真实模型的计划模式 + 无人值守任务（需要 API Key）
uv run python -u tests/live_phase3.py   # 真实联网搜索 + 真实网页抓取 + 真实故障切换（需要 API Key）
uv run python -u tests/screenshot.py    # 导出 docs/screenshot.svg 界面快照
```

离线测试（phase2 / phase3 / smoke_tui）不需要网络也不需要 API Key，全程用临时数据库与临时配置，
不会动你的 `data/` 和 `config.toml`。

## 已知问题

- 进程退出时可能打印 `httpcore2 ... generator didn't stop after athrow()`。这是 openai 3.x 所带 httpcore2 在事件循环关闭时回收 SSE 生成器的内部噪音，功能不受影响。
- `ollama` 段默认指向本机 11434；未装 Ollama 时切换过去会报连接失败（会自动切到别的可用模型，如果配了 fallback）。
- 计划模式与记忆提炼都会额外消耗模型调用：`learn_every` 调大可以省钱，`auto_learn=false` 可完全关掉。
- 联网搜索依赖本机能出网。走代理时按系统 `HTTPS_PROXY` 环境变量；DuckDuckGo 偶发限流会返回空结果并提示换后端。

## 路线图

- Phase 4：语音（faster-whisper STT + edge-tts TTS）—— 按你的要求暂缓
