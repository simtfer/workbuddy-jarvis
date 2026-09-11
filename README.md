# JARVIS-Win

一个常驻 Windows 的 AI 助手，带终端 TUI。能对话，也能真的干活：执行命令、读写文件、体检系统、记住你的偏好、按计划多步执行、准点自动跑任务。

- **Phase 1**：Textual TUI + 可插拔 OpenAI 兼容模型 + 工具调用循环 + 破坏性操作确认 + 会话历史落库
- **Phase 2**：长期记忆、计划模式（可执行的多步任务）、定时任务、全局热键常驻守护

## 快速开始

```bash
cd D:/code/workbuddy-projects/project1/jarvis
uv sync                            # 安装依赖
uv run jarvis --selftest           # 不开 TUI，自检配置、工具与调度器
uv run jarvis --selftest --ping    # 额外发一次真实模型请求
uv run jarvis                      # 启动 TUI
uv run jarvis --daemon             # 常驻：全局热键 + 定时任务（另开一个终端跑）
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
| `/model [名字]` | 查看 / 切换模型（deepseek、qwen、doubao、ollama） |
| `/tools` | 列出工具及其确认策略 |
| `/sys` | 立刻输出系统体检报告 |
| `/history` | 最近会话列表 |
| `/resume <id>` | 载入某个历史会话作为上下文 |
| `/reset` | 清空当前上下文（历史与记忆仍在 `data/`） |
| `/clear` · `/theme` · `/quit` | 清屏 · 切主题 · 退出 |

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

### 常驻与全局热键

```bash
uv run jarvis --daemon                 # 守护：注册热键 + 跑定时任务
uv run jarvis --daemon --hotkey ctrl+alt+k
uv run jarvis --no-scheduler           # TUI 不接管调度器（热键拉起时用）
uv run jarvis --tasks                  # 只打印任务列表
```

按 `Ctrl+Alt+J`（`daemon.hotkey` 可改）会新开一个终端窗口运行 TUI。守护进程用 pid 文件
（`data/jarvis.pid`）防重复启动，日志写在 `data/daemon.log`。

快捷键：`Ctrl+Q` 退出 · `Ctrl+L` 清屏 · `Ctrl+S` 系统面板 · `F1` 帮助 · `F2` 任务 · `Ctrl+X` 取消任务

## 架构

```
TUI (Textual)  →  Agent 内核  →  工具层  →  LLM 适配 / 记忆
                    ↓
              计划器 · 调度器 · 守护进程（热键）
```

- `src/jarvis/tui/` — 界面：聊天流、系统面板、确认弹窗、帮助面板、计划面板
- `src/jarvis/core/` — `agent.py` 对话循环（流式 + 工具调度 + 计划执行 + 记忆提炼）、`registry.py` 工具注册与截断、`planner.py` 计划解析、`scheduler.py` 调度与下次执行时间计算、`prompts.py` 人设与系统提示
- `src/jarvis/llm/` — 仅依赖 OpenAI 兼容协议；端点不支持 tools 时自动降级为纯对话
- `src/jarvis/tools/` — `shell`（命令执行 + 黑名单护栏）、`fs`（读写/列目录/搜索）、`sysinfo`（psutil 快照）、`notify`（Windows 通知）
- `src/jarvis/memory/` — SQLite：会话历史 + 长期事实
- `src/jarvis/daemon/` — `hotkey.py`（RegisterHotKey 全局热键）、`service.py`（守护进程、无人值守任务执行）

## 工具与安全

| 工具 | 说明 | 确认 |
|------|------|------|
| `run_shell` | 执行 PowerShell 命令（默认 UTF-8 输出） | ⚠ 需要 |
| `write_file` | 写入 / 追加文件（拒绝写 `C:\Windows`、`Program Files`） | ⚠ 需要 |
| `read_file` | 读文本，支持分段 | 免确认 |
| `list_dir` / `search_files` | 列目录、按通配符递归查找 | 免确认 |
| `sys_report` | CPU / 内存 / 磁盘 / 网络 / 电池 / 高占用进程 | 免确认 |

- 危险工具在 TUI 里弹窗确认，可选「同意 / 拒绝 / 本会话始终允许」。
- `run_shell` 内置硬拦截：格式化磁盘、`del /s`、`rm -rf /`、`reg delete`、`bcdedit`、`diskpart`、`vssadmin delete`、关机等一律拒绝执行。
- 定时任务默认**拒绝**危险操作，除非创建时加了 `--danger`（守护进程与 TUI 行为一致）。
- 确认框 5 分钟无响应自动按拒绝处理，不会把 Agent 卡死。
- API Key 只在 `config.toml` 或环境变量里，`config.toml` 与 `data/` 都在 .gitignore 中。

## 测试

```bash
uv run python -u tests/phase2.py        # 计划解析/记忆/调度/热键（无网络，含真实热键注册）
uv run python -u tests/smoke_tui.py     # 无头 TUI：命令、记忆、任务、计划面板、弹窗
uv run python -u tests/live_agent.py    # 真实模型 + 真实工具端到端（需要 API Key）
uv run python -u tests/live_phase2.py   # 真实模型的计划模式 + 无人值守任务（需要 API Key）
uv run python -u tests/screenshot.py    # 导出 docs/screenshot.svg 界面快照
```

## 已知问题

- 进程退出时可能打印 `httpcore2 ... generator didn't stop after athrow()`。这是 openai 3.x 所带 httpcore2 在事件循环关闭时回收 SSE 生成器的内部噪音，功能不受影响。
- `config.toml` 里 `ollama` 段默认指向本机 11434；未装 Ollama 时切换过去会报连接失败。
- 计划模式与记忆提炼都会额外消耗模型调用：`learn_every` 调大可以省钱，`auto_learn=false` 可完全关掉。

## 路线图

- Phase 3：语音（faster-whisper STT + edge-tts TTS）、多模型热切换、网络搜索工具、托盘图标
