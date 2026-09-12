"""The slash-command layer, extracted from ``app.py``.

``CommandMixin`` carries every ``/command`` handler - models, providers,
search, memory, clipboard, processes, scheduled tasks - so the App itself
keeps only the chat-stream machinery. The mixin is mixed into
:class:`~jarvis.tui.app.JarvisApp`, which supplies the attributes below
(duck-typed: the project does not run a type checker over the TUI).

Host contract::

    config, agent, store, task_store, registry, session_id
    _busy, _notify_enabled, _turns_since_learn
    _append(widget), _focus_prompt()
    run_plan(task, execute), run_plan_execute(), run_sys_report(),
    run_manual_task(task), action_clear_chat()
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import TYPE_CHECKING

from ..core.scheduler import ScheduleError, parse_task_command
from ..providers import SEARCH_PROVIDERS
from ..tools import build_registry
from ..tools import clipboard as clipboard_mod
from ..tools import procman, web
from .screens import HelpScreen, ModelPickerScreen
from .widgets import Notice

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import JarvisApp


def split_flags(text: str) -> tuple[list[str], dict[str, str]]:
    """``"gpt --provider openai --model gpt-4o"`` -> (["gpt"], {...}).

    ``--flag value`` pairs go into the dict; ``--bare`` becomes ``{"bare": ""}``.
    """

    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    positional: list[str] = []
    flags: dict[str, str] = {}
    pending: str | None = None
    for token in tokens:
        if token.startswith("--"):
            pending = token[2:].lower()
            flags.setdefault(pending, "")
        elif pending is not None:
            flags[pending] = token
            pending = None
        else:
            positional.append(token)
    return positional, flags


class CommandMixin:
    """All ``/``-command handling for the JARVIS app (see module docstring)."""

    # ---------------------------------------------------------------- dispatch
    async def _run_command(self: JarvisApp, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        args = rest.split()

        if command in {"/quit", "/exit", "/q"}:
            self.exit()
        elif command in {"/help", "/?"}:
            self.push_screen(HelpScreen(), callback=lambda _result: self._focus_prompt())
        elif command == "/clear":
            await self.action_clear_chat()
        elif command == "/sys":
            self.run_sys_report()
        elif command == "/clip":
            await self._handle_clip_command(rest)
        elif command == "/ps":
            await self._handle_ps_command(rest)
        elif command == "/tools":
            lines = [
                f"· {tool.name:<14} {'⚠ 需确认' if tool.dangerous else '免确认'}  {tool.description[:60]}"
                for tool in self.registry.tools
            ]
            await self._append(Notice("可用工具：\n" + "\n".join(lines), "info"))
        elif command in {"/model", "/models"}:
            await self._handle_model_command(rest)
        elif command in {"/provider", "/providers"}:
            await self._handle_provider_command(rest)
        elif command == "/search":
            await self._handle_search_command(rest)
        elif command == "/fetch" and rest:
            await self._fetch_page(rest)
        elif command == "/history":
            await self._show_history()
        elif command == "/resume" and rest:
            await self._resume(args[0])
        elif command == "/reset":
            self.agent.reset()
            await self._append(Notice("上下文已清空（历史与记忆仍保存在 data/ 中）。", "info"))
        elif command == "/theme":
            self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
            await self._append(Notice(f"主题：{self.theme}", "info"))
        # ---------------------------------------------------------- memory
        elif command == "/facts":
            await self._show_facts()
        elif command == "/remember" and rest:
            await self._remember(rest)
        elif command == "/forget" and rest:
            await self._forget(args[0])
        elif command == "/learn":
            await self._learn(force=True)
        # ------------------------------------------------------------ plan
        elif command == "/plan" and rest:
            self.run_plan(rest, execute=False)
        elif command == "/auto" and rest:
            self.run_plan(rest, execute=True)
        elif command == "/do":
            if not self.agent.plan:
                await self._append(Notice("当前没有计划，先用 /plan <任务> 生成。", "warn"))
            else:
                self.run_plan_execute()
        # ------------------------------------------------------------ tasks
        elif command == "/task" or command == "/tasks":
            await self._handle_task_command(rest)
        elif command == "/daemon":
            hotkey = self.config.daemon.hotkey
            tray = "开" if self.config.daemon.tray else "关"
            await self._append(
                Notice(
                    "常驻守护：`uv run jarvis --daemon`\n"
                    f"  · 全局热键 {hotkey} 唤起新窗口\n"
                    f"  · 系统托盘图标：{tray}（右键菜单：打开 JARVIS / 定时任务 / 状态 / 退出）\n"
                    "  · 后台跑定时任务（当前窗口若也要接管，直接启动 TUI 即可）",
                    "info",
                )
            )
        else:
            await self._append(Notice(f"未知命令 {command}，/help 查看全部命令。", "warn"))

    # ------------------------------------------------------------------- models
    async def _handle_model_command(self: JarvisApp, rest: str) -> None:
        positional, flags = split_flags(rest)
        # A bare ``/model`` means "let me pick", not "print a table" - so the
        # default here must be empty, not "list".
        action = positional[0].lower() if positional else ""
        payload = positional[1:] if len(positional) > 1 else []

        if action == "":
            await self._pick_model()
        elif action == "list":
            await self._show_models()
        elif action == "add":
            await self._add_model(payload, flags)
        elif action in {"rm", "del", "remove"}:
            await self._remove_model(payload, flags)
        elif action in {"default", "use"}:
            await self._set_default_model(payload)
        elif action == "fallback":
            await self._set_fallbacks(payload)
        else:
            await self._switch_model(" ".join(positional))

    async def _show_models(self: JarvisApp) -> None:
        current = self.agent.model_key
        names = self.config.model_names()
        lines = []
        for index, name in enumerate(names, start=1):
            model = self.config.models[name]
            mark = "▶" if name == current else " "
            key_state = "已配 Key" if model.key_ready else "⚠ 缺 Key"
            provider = f"[{model.provider}]" if model.provider else "[自定义]"
            lines.append(f"{mark} {index}. {name:<10} {provider:<13} {model.model:<26} {key_state}")
        chain = " → ".join(self.config.fallbacks_for(current)) or "（自动挑已配 Key 的）"
        await self._append(
            Notice(
                "模型列表（/model 直接选，或 /model <序号|名字> 热切换）：\n"
                + "\n".join(lines)
                + f"\n\n当前 ▶ {current} · 故障自动切换：{chain}"
                + "\n新增：/model add <名字> --provider <provider> --model <模型ID> [--env VAR]"
                + " [--key sk-xxx] [--url ...] [--timeout 秒] [--default]",
                "info",
            )
        )

    async def _pick_model(self: JarvisApp) -> None:
        """Arrow-key model picker, so switching does not require typing a name."""

        if self._busy:
            await self._append(Notice("正在跑任务，等它结束再切模型（Ctrl+X 可取消）。", "warn"))
            return
        current = self.agent.model_key
        choices: list[tuple[str, str]] = []
        for name in self.config.model_names():
            model = self.config.models[name]
            mark = "▶" if name == current else " "
            key_state = "已配 Key" if model.key_ready else "缺 Key"
            choices.append(
                (
                    name,
                    f"{mark} {name}  ·  {model.provider or '自定义'}  ·  {model.model}  ·  {key_state}",
                )
            )

        def _resolved(picked: str | None) -> None:
            # Do NOT await anything here: this callback fires from the prompt's
            # message handler, and blocking that freezes the whole UI. Hand the
            # switch to a worker instead.
            self._focus_prompt()
            if picked:
                self.run_worker(
                    self._switch_model(picked), name="model-pick", group="model"
                )

        try:
            self.push_screen(ModelPickerScreen(choices, current), callback=_resolved)
        except Exception as exc:  # noqa: BLE001 - never wedge the prompt on a broken modal
            await self._append(Notice(f"模型选择器打开失败（{exc}）。", "bad"))

    async def _switch_model(self: JarvisApp, token: str) -> None:
        if self._busy:
            await self._append(Notice("正在跑任务，等它结束再切模型（Ctrl+X 可取消）。", "warn"))
            return
        previous = self.agent.model_key
        try:
            key = self.agent.set_model(token)
        except KeyError as exc:
            await self._append(Notice(str(exc.args[0]), "warn"))
            return
        model = self.config.models[key]
        if key == previous:
            await self._append(Notice(f"已经是 {key}（{model.model}）了。", "info"))
            return
        # Switching in the TUI means "this is the model I want to use", so it is
        # persisted - reopening JARVIS must not silently fall back to the old one.
        self.config.set_default(key)
        await self._append(
            Notice(
                f"已切换到 {key} · {model.model}"
                f"（{model.base_url}）{' ⚠ 还没配 Key' if not model.key_ready else ''}"
                f"\n已设为默认模型（下次启动仍是它），上下文 {len(self.agent.history())} 条消息保持不变。",
                "info" if model.key_ready else "warn",
            )
        )

    async def _add_model(self: JarvisApp, payload: list[str], flags: dict[str, str]) -> None:
        name = (payload[0] if payload else flags.get("name", "")).strip()
        if not name:
            await self._append(
                Notice(
                    "用法：/model add <名字> --provider <provider> --model <模型ID>"
                    " [--env VAR] [--key sk-xxx] [--url https://...] [--label 显示名] [--timeout 秒] [--default]\n"
                    "例：/model add gpt --provider openai --model gpt-4o-mini --env OPENAI_API_KEY",
                    "warn",
                )
            )
            return
        try:
            cfg = self.config.add_model(
                name,
                provider=flags.get("provider", ""),
                model=flags.get("model", ""),
                label=flags.get("label", ""),
                base_url=flags.get("url", "") or flags.get("base-url", ""),
                api_key=flags.get("key", ""),
                api_key_env=flags.get("env", ""),
                temperature=float(flags.get("temperature", 0.3) or 0.3),
                timeout=float(flags.get("timeout", 180.0) or 180.0),
                make_default="default" in flags,
            )
        except (ValueError, KeyError) as exc:
            await self._append(Notice(str(exc.args[0] if exc.args else exc), "warn"))
            return
        self.agent.config = self.config
        await self._append(
            Notice(
                f"已写入模型 {cfg.name}：{cfg.model} @ {cfg.base_url}\n"
                f"配置已保存到 {self.config.path}。切换：/model {cfg.name}"
                + ("（已设为默认）" if "default" in flags else ""),
                "info",
            )
        )

    async def _remove_model(self: JarvisApp, payload: list[str], flags: dict[str, str]) -> None:
        token = (payload[0] if payload else flags.get("name", "")).strip()
        if not token:
            await self._append(Notice("用法：/model rm <名字>", "warn"))
            return
        key = self.config.find_model(token) or token
        if key == self.agent.model_key:
            await self._append(Notice("正在用的是这个模型，先 /model 切到别的再删。", "warn"))
            return
        try:
            removed = self.config.remove_model(key)
        except ValueError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        await self._append(
            Notice(f"已删除模型 {key}。" if removed else f"没有模型 {key}。",
                   "info" if removed else "warn")
        )

    async def _set_default_model(self: JarvisApp, payload: list[str]) -> None:
        token = payload[0] if payload else ""
        if not token:
            await self._append(Notice("用法：/model default <名字>（下次启动用它）", "warn"))
            return
        try:
            cfg = self.config.set_default(self.config.find_model(token) or token)
        except KeyError as exc:
            await self._append(Notice(str(exc.args[0]), "warn"))
            return
        await self._append(Notice(f"默认模型已设为 {cfg.name}（写回 config.toml）。", "info"))

    async def _set_fallbacks(self: JarvisApp, payload: list[str]) -> None:
        if not payload:
            chain = ", ".join(self.config.fallback_models) or "（未设置，运行时自动挑选）"
            await self._append(
                Notice(f"故障切换链：{chain}\n用法：/model fallback qwen ollama（写回 config.toml）", "info")
            )
            return
        resolved: list[str] = []
        for token in payload:
            key = self.config.find_model(token)
            if key:
                resolved.append(key)
        self.config.fallback_models = resolved
        self.config.save()
        await self._append(
            Notice(f"故障切换链已设为：{' → '.join(resolved) or '（空，自动挑选）'}", "info")
        )

    # ---------------------------------------------------------------- providers
    async def _handle_provider_command(self: JarvisApp, rest: str) -> None:
        positional, flags = split_flags(rest)
        action = positional[0].lower() if positional else "list"
        payload = positional[1:] if len(positional) > 1 else []

        if action in {"list", ""}:
            await self._show_providers()
        elif action == "add":
            await self._add_provider(payload, flags)
        elif action in {"rm", "del", "remove"}:
            await self._remove_provider(payload)
        else:
            await self._show_provider(action)

    async def _show_providers(self: JarvisApp) -> None:
        in_use: dict[str, list[str]] = {}
        for name, model in self.config.models.items():
            if model.provider:
                in_use.setdefault(model.provider, []).append(name)

        lines = ["对话模型（/model add <名字> --provider <名字> --model <模型ID>）："]
        for name in self.config.catalog.names():
            preset = self.config.catalog.require(name)
            custom = "※自定义" if name in self.config.user_providers else ""
            used = f" · 在用：{','.join(in_use[name])}" if name in in_use else ""
            key_state = ""
            if preset.api_key_env:
                ready = bool(os.environ.get(preset.api_key_env, "").strip())
                key_state = f" · {preset.api_key_env}{'✓' if ready else '（未设置）'}"
            lines.append(f"  {name:<12} {preset.label:<22} {preset.base_url}{used}{key_state}{custom}")

        lines.append("")
        lines.append("搜索后端（/search backend <名字>）：")
        for name, preset in sorted(SEARCH_PROVIDERS.items()):
            mark = "▶" if name == self.config.search.provider else " "
            key_state = ""
            if preset.needs_key:
                key_state = " " + ("Key ✓" if self.config.search.resolve_api_key() else "⚠ 缺 Key")
            lines.append(f"{mark} {name:<12} {preset.label}{key_state}")

        lines.append("")
        lines.append(
            "自定义 endpoint：/provider add <名字> <base_url> [--env VAR] [--label 显示名]"
            "\n 例：/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY --label 公司内网"
        )
        await self._append(Notice("\n".join(lines), "info"))

    async def _show_provider(self: JarvisApp, name: str) -> None:
        preset = self.config.catalog.get(name)
        if preset is None:
            await self._append(Notice(f"没有 provider '{name}'，/provider list 看全部。", "warn"))
            return
        using = [n for n, m in self.config.models.items() if m.provider == name]
        await self._append(
            Notice(
                f"{name} · {preset.label}\n"
                f"base_url     {preset.base_url}\n"
                f"api_key_env  {preset.api_key_env or '(不需要)'}\n"
                f"常见模型     {', '.join(preset.models) or '(未列)'}\n"
                f"控制台       {preset.console or '-'}\n"
                f"在用模型     {', '.join(using) or '无'}"
                + (f"\n备注         {preset.note}" if preset.note else ""),
                "info",
            )
        )

    async def _add_provider(self: JarvisApp, payload: list[str], flags: dict[str, str]) -> None:
        name = (payload[0] if payload else flags.get("name", "")).strip()
        base_url = (payload[1] if len(payload) > 1 else flags.get("url", "")).strip()
        if not name or not base_url:
            await self._append(
                Notice(
                    "用法：/provider add <名字> <base_url> [--env VAR] [--label 显示名]\n"
                    "例：/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY",
                    "warn",
                )
            )
            return
        try:
            preset = self.config.add_provider(
                name,
                base_url=base_url,
                label=flags.get("label", ""),
                api_key_env=flags.get("env", ""),
            )
        except ValueError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        await self._append(
            Notice(
                f"已添加 provider {preset.name} · {preset.label} → {preset.base_url}\n"
                f"接着加模型：/model add <名字> --provider {preset.name} --model <模型ID>",
                "info",
            )
        )

    async def _remove_provider(self: JarvisApp, payload: list[str]) -> None:
        name = payload[0] if payload else ""
        if not name:
            await self._append(Notice("用法：/provider rm <名字>（只删自定义的）", "warn"))
            return
        removed = self.config.remove_provider(name)
        await self._append(
            Notice(f"已删除自定义 provider {name}。" if removed else f"{name} 不是自定义 provider，删不掉。",
                   "info" if removed else "warn")
        )

    # -------------------------------------------------------------------- web
    async def _handle_search_command(self: JarvisApp, rest: str) -> None:
        positional, flags = split_flags(rest)
        action = positional[0].lower() if positional else ""

        if action in {"backend", "provider"} and len(positional) > 1:
            name = positional[1].lower()
            if name not in SEARCH_PROVIDERS:
                await self._append(
                    Notice(f"未知搜索后端 '{name}'。可用：{', '.join(sorted(SEARCH_PROVIDERS))}", "warn")
                )
                return
            preset = SEARCH_PROVIDERS[name]
            self.config.search.provider = name
            self.config.search.base_url = ""  # fall back to the preset endpoint
            self.config.search.api_key_env = preset.api_key_env
            self.config.save()
            self.registry = build_registry(self.config.security, str(self.config.workdir), self.config.search)
            await self._append(
                Notice(f"搜索后端已切到 {name} · {preset.label}（已写回 config.toml）", "info")
            )
            return

        if not rest.strip():
            await self._append(
                Notice(
                    "联网搜索\n"
                    f"  当前后端  {web.describe(self.config.search)}\n"
                    "  用法      /search 关键词          直接搜一次\n"
                    "            /search backend tavily 换后端（bocha 中文好、tavily 摘要干净）\n"
                    "            /fetch example.com     打开网页读正文\n"
                    "  会话里直接说「搜一下 xxx」，JARVIS 会自己调 web_search 工具。",
                    "info",
                )
            )
            return

        query = " ".join(positional)
        await self._append(Notice(f"🔍 搜索「{query}」（{self.config.search.provider}）…", "info"))
        output = await self.registry.call(
            "web_search", {"query": query, "max_results": self.config.search.max_results}
        )
        await self._append(Notice(output, "info"))

    async def _fetch_page(self: JarvisApp, url: str) -> None:
        await self._append(Notice(f"🌐 抓取 {url} …", "info"))
        output = await self.registry.call("fetch_url", {"url": url, "max_chars": 6000})
        await self._append(Notice(output, "info"))

    async def _show_history(self: JarvisApp) -> None:
        rows = self.store.sessions(15)
        if not rows:
            await self._append(Notice("还没有历史会话。", "info"))
            return
        lines = [f"{sid}  {title or '(无标题)'}" for sid, title, _ts in rows]
        await self._append(Notice("最近会话（/resume <id> 载入上下文）：\n" + "\n".join(lines), "info"))

    async def _resume(self: JarvisApp, target: str) -> None:
        history = self.store.recent_messages(target, self.config.security.history_limit)
        if not history:
            await self._append(Notice(f"会话 {target} 没有可载入的消息。", "warn"))
            return
        self.agent.load_history(history)
        await self._append(Notice(f"已载入会话 {target} 的 {len(history)} 条消息作为上下文。", "info"))

    # ------------------------------------------------------------------ memory
    async def _show_facts(self: JarvisApp) -> None:
        rows = self.store.facts(50)
        if not rows:
            await self._append(
                Notice("长期记忆还是空的。用 /remember <内容> 手动添加，或正常聊天让它自己沉淀。", "info")
            )
            return
        lines = [f"#{fid:<3} {text}" for fid, text in rows]
        await self._append(
            Notice(f"长期记忆 {len(rows)} 条（/forget <编号> 删除）：\n" + "\n".join(lines), "info")
        )

    async def _remember(self: JarvisApp, text: str) -> None:
        fact_id, created = self.store.add_fact(text, source=f"manual@{self.session_id}")
        self.agent.refresh_system()
        verb = "已记住" if created else "已存在，已刷新"
        await self._append(Notice(f"{verb} #{fact_id}：{text}", "info"))

    async def _forget(self: JarvisApp, raw_id: str) -> None:
        try:
            fact_id = int(raw_id.lstrip("#"))
        except ValueError:
            await self._append(Notice(f"'{raw_id}' 不是有效的编号。", "warn"))
            return
        removed = self.store.delete_fact(fact_id)
        self.agent.refresh_system()
        await self._append(
            Notice(f"已删除记忆 #{fact_id}。" if removed else f"没有编号为 {fact_id} 的记忆。",
                   "info" if removed else "warn")
        )

    async def _learn(self: JarvisApp, force: bool = False) -> None:
        if not force and not self.config.memory.auto_learn:
            return
        self._turns_since_learn += 1
        if not force and self._turns_since_learn < self.config.memory.learn_every:
            return
        self._turns_since_learn = 0
        transcript = self.agent.transcript()
        if not transcript.strip():
            return
        facts = await self.agent.learn(transcript)
        added: list[str] = []
        for fact in facts:
            _fid, created = self.store.add_fact(fact, source=f"auto@{self.session_id}")
            if created:
                added.append(fact)
        if added:
            self.agent.refresh_system()
            await self._append(
                Notice(f"沉淀了 {len(added)} 条长期记忆：\n" + "\n".join(f"+ {f}" for f in added), "info")
            )

    # ------------------------------------------------- clipboard / processes
    async def _handle_clip_command(self: JarvisApp, rest: str) -> None:
        """``/clip`` 看剪贴板，``/clip set <文本>`` 写入，``/clip clear`` 清空。"""

        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        payload = parts[1] if len(parts) > 1 else ""

        if action in {"", "show", "get"}:
            try:
                text = await asyncio.to_thread(clipboard_mod.read_clipboard, 4000)
            except Exception as exc:  # noqa: BLE001 - surface the reason in the UI
                await self._append(Notice(f"读剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice("剪贴板\n" + text, "info"))
        elif action in {"set", "put", "copy"}:
            if not payload:
                await self._append(Notice("用法：/clip set <要复制的文本>", "warn"))
                return
            try:
                result = await asyncio.to_thread(clipboard_mod.write_clipboard, payload)
            except Exception as exc:  # noqa: BLE001
                await self._append(Notice(f"写剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice(result, "info"))
        elif action in {"clear", "empty", "wipe"}:
            try:
                result = await asyncio.to_thread(clipboard_mod.clear_clipboard)
            except Exception as exc:  # noqa: BLE001
                await self._append(Notice(f"清空剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice(result, "info"))
        else:
            await self._append(
                Notice("用法：/clip（查看）· /clip set <文本>（写入）· /clip clear（清空）", "warn")
            )

    async def _handle_ps_command(self: JarvisApp, rest: str) -> None:
        """``/ps [cpu|mem|name|pid] [关键词]`` —— 只读的进程列表。"""

        tokens = rest.split()
        sort_by = "cpu"
        if tokens and tokens[0].lower() in {"cpu", "mem", "memory", "name", "pid"}:
            sort_by = tokens.pop(0).lower()
        needle = " ".join(tokens)
        try:
            report = await asyncio.to_thread(procman.list_processes, sort_by, 15, needle)
        except Exception as exc:  # noqa: BLE001
            await self._append(Notice(f"列进程失败：{exc}", "bad"))
            return
        await self._append(Notice(report, "info"))

    # ------------------------------------------------------------------- tasks
    async def _handle_task_command(self: JarvisApp, rest: str) -> None:
        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        payload = parts[1].strip() if len(parts) > 1 else ""

        if action in {"list", ""}:
            await self._show_tasks()
        elif action == "add":
            await self._add_task(payload)
        elif action in {"rm", "del", "delete"}:
            await self._remove_task(payload)
        elif action in {"on", "off"}:
            await self._toggle_task(payload, action == "on")
        elif action == "run":
            await self._run_task_now(payload)
        else:
            await self._append(
                Notice(
                    "用法：/task list · /task add <内容> @daily 09:00 · /task rm <id>"
                    " · /task on|off <id> · /task run <id>",
                    "warn",
                )
            )

    async def _show_tasks(self: JarvisApp) -> None:
        tasks = self.task_store.list()
        if not tasks:
            await self._append(
                Notice(
                    "还没有定时任务。示例：\n"
                    "/task add 看一下磁盘剩余空间 @daily 09:00\n"
                    "/task add 汇总今天新增的文件 @every 2h --danger（--danger 才允许执行写操作）",
                    "info",
                )
            )
            return
        lines = []
        for task in tasks:
            state = "启用" if task.enabled else "停用"
            next_run = task.next_run or "-"
            lines.append(
                f"#{task.id:<3} [{state}] {task.schedule_text:<12} 下次 {next_run:<17} "
                f"{'⚠' if task.allow_dangerous else ' '} {task.title}"
            )
        await self._append(Notice("定时任务：\n" + "\n".join(lines), "info"))

    async def _add_task(self: JarvisApp, payload: str) -> None:
        if not payload:
            await self._append(Notice("用法：/task add <任务内容> @every 30m | @daily 09:00 | @once 2026-09-12 08:00", "warn"))
            return
        try:
            task = parse_task_command(payload)
        except ScheduleError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        self.task_store.add(task)
        await self._append(
            Notice(
                f"已添加任务 #{task.id}：{task.schedule_text} · {task.title}"
                f"（下次 {task.next_run}）{' · 允许危险操作' if task.allow_dangerous else ''}",
                "info",
            )
        )

    async def _remove_task(self: JarvisApp, payload: str) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        removed = self.task_store.delete(task_id)
        await self._append(
            Notice(f"已删除任务 #{task_id}。" if removed else f"没有编号为 {task_id} 的任务。",
                   "info" if removed else "warn")
        )

    async def _toggle_task(self: JarvisApp, payload: str, enabled: bool) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        ok = self.task_store.set_enabled(task_id, enabled)
        verb = "启用" if enabled else "停用"
        await self._append(
            Notice(f"已{verb}任务 #{task_id}。" if ok else f"没有编号为 {task_id} 的任务。",
                   "info" if ok else "warn")
        )

    async def _run_task_now(self: JarvisApp, payload: str) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        task = self.task_store.get(task_id)
        if task is None:
            await self._append(Notice(f"没有编号为 {task_id} 的任务。", "warn"))
            return
        self.run_manual_task(task)

    def _parse_id(self: JarvisApp, payload: str) -> int | None:
        try:
            return int(payload.strip().lstrip("#"))
        except ValueError:
            self.call_later(self._append, Notice(f"'{payload}' 不是有效的任务编号。", "warn"))
            return None
