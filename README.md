# JARVIS-Win

一个常驻 Windows 的 AI 助手，带终端 TUI。能对话，也能真的干活：执行命令、读写文件、体检系统。

Phase 1 已实现：Textual TUI + 可插拔 OpenAI 兼容模型 + 工具调用循环 + 破坏性操作确认 + 会话历史落库。

## 快速开始

```bash
cd D:/code/workbuddy-projects/project1/jarvis
uv sync                       # 安装依赖
uv run jarvis --selftest      # 不开 TUI，自检配置与工具
uv run jarvis --selftest --ping   # 额外发一次真实模型请求
uv run jarvis                 # 启动
```

首次运行会生成 `config.toml`（已 git-ignore）。填入 API Key：

```toml
[models.deepseek]
api_key = "sk-..."      # 或者留空，改设环境变量 DEEPSEEK_API_KEY
```

## 交互

直接输入问题，回车发送。JARVIS 自行决定是否调用工具。

| 命令 | 作用 |
|------|------|
| `/help` | 帮助面板 |
| `/model [名字]` | 查看 / 切换模型（deepseek、qwen、doubao、ollama） |
| `/tools` | 列出工具及其确认策略 |
| `/sys` | 立刻输出系统体检报告 |
| `/history` | 最近会话列表 |
| `/resume <id>` | 载入某个历史会话作为上下文 |
| `/reset` | 清空当前上下文（历史仍在 `data/history.db`） |
| `/clear` | 清屏 |
| `/theme` | 深浅主题切换 |
| `/quit` | 退出 |

快捷键：`Ctrl+Q` 退出 · `Ctrl+L` 清屏 · `Ctrl+S` 系统面板 · `Ctrl+X` 取消任务 · `F1` 帮助 · `Esc` 中断生成

## 架构

```
TUI (Textual)  →  Agent 内核  →  工具层  →  LLM 适配 / 记忆
```

- `src/jarvis/tui/` — 界面：聊天流、系统面板、确认弹窗、帮助面板
- `src/jarvis/core/` — `agent.py` 对话循环（流式 + 工具调度）、`registry.py` 工具注册与截断、`prompts.py` 人设与系统提示
- `src/jarvis/llm/` — 仅依赖 OpenAI 兼容协议；端点不支持 tools 时自动降级为纯对话
- `src/jarvis/tools/` — `shell`（命令执行 + 黑名单护栏）、`fs`（读写/列目录/搜索）、`sysinfo`（psutil 快照）
- `src/jarvis/memory/` — SQLite 会话历史

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
- 想在 config.toml 里放宽：`security.auto_approve = ["run_shell"]`（不建议；`sys_report` 之类本来就免确认）。
- 确认框 5 分钟无响应自动按拒绝处理，不会把 Agent 卡死。

## 测试

```bash
uv run python -u tests/smoke_tui.py     # 无头启动 TUI、斜杠命令、确认弹窗、面板开关
uv run python -u tests/live_agent.py    # 真实模型 + 真实工具端到端（需要 API Key）
```

## 已知问题

- 进程退出时可能打印 `httpcore2 ... generator didn't stop after athrow()`。这是 openai 3.x 所带 httpcore2 在事件循环关闭时回收 SSE 生成器的内部噪音，功能不受影响。
- `config.toml` 里 `ollama` 段默认指向本机 11434；未装 Ollama 时切换过去会报连接失败。

## 路线图

- Phase 2：长期记忆（事实/偏好）、规划模式（多步任务可视化）、定时任务、全局热键常驻 + 托盘
- Phase 3：语音（faster-whisper STT + edge-tts TTS）、多模型热切换、网络搜索工具、Windows 通知
