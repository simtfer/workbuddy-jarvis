"""Command line entry point: TUI, selftest, daemon, provider/model management."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .config import Config, load_config
from .providers import SEARCH_PROVIDERS


def _print_tasks() -> int:
    """List scheduled tasks without starting anything else."""

    from .core.scheduler import TaskStore
    from .memory.store import HistoryStore, default_db_path

    store = HistoryStore(default_db_path())
    try:
        tasks = TaskStore(store.conn).list()
        if not tasks:
            print("没有定时任务。")
            return 0
        for task in tasks:
            state = "启用" if task.enabled else "停用"
            print(
                f"#{task.id:<3} [{state}] {task.schedule_text:<14} "
                f"下次 {task.next_run or '-':<17} {'⚠' if task.allow_dangerous else ' '} {task.prompt}"
            )
    finally:
        store.close()
    return 0


def _print_models(config: Config) -> int:
    current = config.default_model
    for index, name in enumerate(config.model_names(), start=1):
        model = config.models[name]
        mark = "▶" if name == current else " "
        key = "已配 Key" if model.key_ready else "缺 Key"
        provider = model.provider or "自定义"
        print(f"{mark} {index}. {name:<12} [{provider:<12}] {model.model:<30} {key}")
    chain = config.fallbacks_for(current)
    print(f"\n故障切换链：{' → '.join(chain) or '（自动挑选已配 Key 的模型）'}")
    print(f"配置文件：{config.path}")
    return 0


def _print_providers(config: Config) -> int:
    in_use: dict[str, list[str]] = {}
    for name, model in config.models.items():
        if model.provider:
            in_use.setdefault(model.provider, []).append(name)

    print("对话模型 provider（--add-model <名字> --provider <provider> --model <模型ID>）")
    for name in config.catalog.names():
        preset = config.catalog.require(name)
        tag = " ※自定义" if name in config.user_providers else ""
        used = f"  在用: {','.join(in_use[name])}" if name in in_use else ""
        env = f"  {preset.api_key_env}" if preset.api_key_env else "  (无需 Key)"
        print(f"  {name:<12} {preset.label:<24} {preset.base_url}{env}{used}{tag}")

    print("\n搜索后端（--search-provider <名字>）")
    for name, preset in sorted(SEARCH_PROVIDERS.items()):
        mark = "▶" if name == config.search.provider else " "
        env = f"  {preset.api_key_env}" if preset.needs_key else "  (无需 Key)"
        print(f"{mark} {name:<12} {preset.label}{env}")
    return 0


def _add_model(config: Config, args: argparse.Namespace) -> int:
    from .providers import guess_provider

    provider = args.provider or guess_provider(args.base_url or "")
    try:
        if provider and config.catalog.get(provider) is None:
            raise KeyError(
                f"未知 provider '{provider}'。先定义：--add-provider {provider} <base_url>"
            )
        model = config.add_model(
            args.add_model,
            provider=provider,
            model=args.model_name or "",
            label=args.label or "",
            base_url=args.base_url or "",
            api_key=args.api_key or "",
            api_key_env=args.api_key_env or "",
            temperature=float(args.temperature),
            timeout=float(args.timeout),
            make_default=args.make_default,
        )
    except (ValueError, KeyError) as exc:
        print(f"错误：{exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    print(f"已写入模型 {model.name}：{model.model} @ {model.base_url}")
    print(f"provider={model.provider or '-'} key_env={model.api_key_env or '-'} "
          f"默认={config.default_model}")
    print(f"配置文件：{config.path}")
    return 0


def _add_provider(config: Config, args: argparse.Namespace) -> int:
    try:
        preset = config.add_provider(
            args.add_provider,
            base_url=args.base_url or "",
            label=args.label or "",
            api_key_env=args.api_key_env or "",
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已写入 provider {preset.name} · {preset.label} → {preset.base_url}")
    print(f"接着加模型：jarvis --add-model <名字> --provider {preset.name} --model <模型ID>")
    return 0


def _remove_model(config: Config, name: str) -> int:
    try:
        removed = config.remove_model(name)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已删除模型 {name}。" if removed else f"没有模型 {name}。")
    return 0 if removed else 1


def _run_search(config: Config, query: str, provider: str) -> int:
    from .tools import web

    if provider:
        if provider not in SEARCH_PROVIDERS:
            print(f"未知搜索后端 '{provider}'。可用：{', '.join(sorted(SEARCH_PROVIDERS))}",
                  file=sys.stderr)
            return 1
        config.search.provider = provider
        config.search.base_url = ""
    print(web.search(query, config.search))
    return 0


def _selftest(ping: bool = False) -> int:
    """Headless sanity check: config, tools, registry, scheduler, optional live call.

    Lives here (not in the TUI package) because it never opens the interface -
    it is a CLI health check over the same building blocks.
    """

    from .core.scheduler import ScheduledTask, TaskStore
    from .memory.store import HistoryStore, default_db_path
    from .tools import build_registry, clipboard as clipboard_mod
    from .tools import fs, procman, sysinfo, web

    config = load_config()
    print(f"config      : {config.path} (created={config.created})")
    print(f"default     : {config.default_model}")
    registry = build_registry(config.security, str(config.workdir), config.search)
    print(f"tools       : {', '.join(t.name for t in registry.tools)}")
    print(f"workdir     : {config.workdir}")
    print(
        f"memory      : auto_learn={config.memory.auto_learn} every={config.memory.learn_every} "
        f"max_in_prompt={config.memory.max_facts_in_prompt}"
    )
    print(f"daemon      : hotkey={config.daemon.hotkey} scheduler={config.daemon.scheduler} "
          f"tray={config.daemon.tray}")
    print(f"providers   : {len(config.catalog.presets)} 个内置 + "
          f"{len(config.user_providers)} 个自定义；search={web.describe(config.search)}")
    for name in config.model_names():
        model = config.models[name]
        print(
            f"  model {name:<10} provider={model.provider or '-':<12} {model.model:<28} "
            f"key={'yes' if model.key_ready else 'MISSING'}"
        )
    print(f"fallback    : {' → '.join(config.fallbacks_for(config.default_model)) or '(无)'}")

    print("--- sysinfo ---")
    print(sysinfo.sys_report(include_processes=3))
    print("--- clipboard ---")
    preview = clipboard_mod.clipboard_text(80)
    print(f"text        : {preview or '(剪贴板没有文本)'}")
    print("--- processes ---")
    print(procman.list_processes(sort_by="cpu", limit=3))
    print("--- fs ---")
    print(fs.list_dir(str(config.workdir))[:400])

    store = HistoryStore(default_db_path())
    print(f"--- store ---\n{store.path} · facts={store.fact_count()} · sessions/messages={store.stats()}")
    task_store = TaskStore(store.conn)
    demo = ScheduledTask(id=None, kind="interval", spec="30", prompt="selftest")

    now = datetime(2026, 9, 11, 12, 0)
    print(
        f"--- scheduler ---\ninterval next={demo.next_after(now)} · "
        f"tasks={len(task_store.list())}"
    )
    store.close()

    model = config.model()
    key = model.resolve_api_key()
    print(f"--- model {model.name}: key={'yes' if key else 'MISSING'} base_url={model.base_url}")
    if ping and key:
        from .llm.client import LLMClient

        async def _ping() -> None:
            client = LLMClient(model)
            result = await client.stream([{"role": "user", "content": "只回复两个字：在线"}])
            print(f"reply       : {result.content.strip()[:80]}")
            await client.aclose()

        asyncio.run(_ping())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS-Win · Windows 超级 AI 助手")
    parser.add_argument("--selftest", action="store_true", help="不开 TUI，自检配置、工具与调度器")
    parser.add_argument("--ping", action="store_true", help="自检时额外发一次真实模型请求")
    parser.add_argument("--daemon", action="store_true", help="常驻模式：全局热键 + 托盘 + 定时任务")
    parser.add_argument("--hotkey", default=None, help="覆盖全局热键，例如 ctrl+alt+j")
    parser.add_argument("--no-scheduler", action="store_true", help="本次运行不启动调度器")
    parser.add_argument("--no-tray", action="store_true", help="常驻时不显示系统托盘图标")
    parser.add_argument("--no-open-tui", action="store_true", help="守护模式下热键不拉起新窗口")
    parser.add_argument("--tasks", action="store_true", help="打印定时任务列表后退出")
    # ------------------------------------------------------------ providers
    parser.add_argument("--models", action="store_true", help="列出已配置模型后退出")
    parser.add_argument("--providers", action="store_true", help="列出内置 provider 与搜索后端后退出")
    parser.add_argument("--add-model", metavar="名字", help="新增/更新一个模型并写回 config.toml")
    parser.add_argument("--rm-model", metavar="名字", help="删除一个模型")
    parser.add_argument("--add-provider", metavar="名字", help="定义一个自定义 OpenAI 兼容 provider")
    parser.add_argument("--rm-provider", metavar="名字", help="删除一个自定义 provider")
    parser.add_argument("--provider", default="", help="provider 名（配合 --add-model）")
    parser.add_argument("--model-name", dest="model_name", default="", help="模型 ID，如 deepseek-chat")
    parser.add_argument("--base-url", default="", help="覆盖 provider 的 base_url")
    parser.add_argument("--api-key", default="", help="直接写入 api_key（会存进 config.toml）")
    parser.add_argument("--api-key-env", default="", help="读取 Key 的环境变量名")
    parser.add_argument("--label", default="", help="显示名")
    parser.add_argument("--temperature", default=0.3, help="采样温度，默认 0.3")
    parser.add_argument("--timeout", default=180.0, help="单次请求超时秒数，默认 180")
    parser.add_argument("--make-default", action="store_true", help="同时设为默认模型")
    # --------------------------------------------------------------- search
    parser.add_argument("--search", metavar="关键词", help="直接跑一次联网搜索后退出")
    parser.add_argument("--search-provider", default="", help="搜索后端：duckduckgo/bocha/tavily/serper/searxng")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    args = parser.parse_args()

    if args.tasks:
        sys.exit(_print_tasks())

    config = load_config()

    if args.providers:
        sys.exit(_print_providers(config))
    if args.models:
        sys.exit(_print_models(config))
    if args.add_provider:
        sys.exit(_add_provider(config, args))
    if args.rm_provider:
        removed = config.remove_provider(args.rm_provider)
        print(f"已删除自定义 provider {args.rm_provider}。" if removed
              else f"{args.rm_provider} 不是自定义 provider，删不掉。")
        sys.exit(0 if removed else 1)
    if args.add_model:
        sys.exit(_add_model(config, args))
    if args.rm_model:
        sys.exit(_remove_model(config, args.rm_model))
    if args.search:
        sys.exit(_run_search(config, args.search, args.search_provider))

    if args.selftest or args.ping:
        sys.exit(_selftest(ping=args.ping))

    if args.daemon:
        from .daemon.service import run_daemon

        print("JARVIS 守护进程启动中…（Ctrl+C 退出，或右键托盘图标退出）")
        try:
            sys.exit(
                asyncio.run(
                    run_daemon(
                        config,
                        hotkey=args.hotkey,
                        with_scheduler=not args.no_scheduler,
                        open_tui=not args.no_open_tui,
                        tray=not args.no_tray,
                    )
                )
            )
        except KeyboardInterrupt:
            sys.exit(0)

    from .tui.app import JarvisApp

    app = JarvisApp(config, scheduler_enabled=not args.no_scheduler)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
