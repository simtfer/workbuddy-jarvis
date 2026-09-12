"""Configuration loading for JARVIS-Win.

A single ``config.toml`` at the project root keeps provider endpoints, security
policy and runtime tuning. On first launch the file is generated from a
template so the user can simply fill in an API key.

Phase 3 splits *providers* (an OpenAI-compatible endpoint + where its key
lives) from *models* (one provider + one model id). A model can inherit
everything endpoint-related:

    [models.gpt-mini]
    provider = "openai"
    model = "gpt-4o-mini"

The file is also writable: ``/provider add`` in the TUI (or ``jarvis
--add-model``) updates the dataclasses and calls :meth:`Config.save`.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import tomlwrite
from .providers import ProviderCatalog, ProviderPreset, guess_provider

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.toml"
DATA_DIR = PROJECT_ROOT / "data"

# Hostnames that never need an API key.
LOCAL_HOSTS = {"localhost", "host.docker.internal", "host.containers.internal"}


def _host_of(url: str) -> str:
    """Extract a lowercase hostname from a base URL (tolerates missing scheme)."""

    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip("[]")

CONFIG_HEADER = """\
JARVIS-Win configuration
本文件由 JARVIS 读取；用 /provider add 或 /model 修改时会自动重写。
API Key 只放在这里（已 git-ignore），不要提交到仓库。
Docs: README.md
"""

_TEMPLATE_MODELS: list[tuple[str, str, str]] = [
    ("deepseek", "deepseek", "deepseek-chat"),
    ("qwen", "dashscope", "qwen-plus"),
    ("doubao", "ark", "doubao-seed-1-6-250615"),
    ("kimi", "moonshot", "kimi-k2-0905-preview"),
    ("glm", "zhipu", "glm-4-plus"),
    ("ollama", "ollama", "qwen3:8b"),
]


@dataclass
class ModelConfig:
    """One OpenAI-compatible endpoint + model id."""

    name: str
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    supports_tools: bool = True
    temperature: float = 0.3
    timeout: float = 180.0
    provider: str = ""

    def resolve_api_key(self) -> str:
        if self.api_key.strip():
            return self.api_key.strip()
        if self.api_key_env:
            value = os.environ.get(self.api_key_env.strip(), "")
            if value.strip():
                return value.strip()
        return ""

    @property
    def display(self) -> str:
        return self.label or self.name

    @property
    def is_local(self) -> bool:
        """True when the endpoint lives on this machine or the LAN.

        Self-hosted endpoints (Ollama, LM Studio, a company vLLM box) rarely
        need an API key, so they must not be reported as "missing key".
        """

        host = _host_of(self.base_url)
        if not host:
            return False
        if host in LOCAL_HOSTS or host.endswith(".local") or host.endswith(".internal"):
            return True
        if host.startswith("127.") or host == "::1":
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local

    @property
    def key_required(self) -> bool:
        return not self.is_local

    @property
    def key_ready(self) -> bool:
        return bool(self.resolve_api_key()) or self.is_local

    def public(self) -> dict[str, Any]:
        """Serialisable view (never exposes the key itself)."""

        return {
            "name": self.name,
            "label": self.display,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "has_key": bool(self.resolve_api_key()),
            "supports_tools": self.supports_tools,
            "temperature": self.temperature,
            "timeout": self.timeout,
        }


@dataclass
class SecurityConfig:
    auto_approve: list[str] = field(default_factory=list)
    shell_timeout: int = 60
    max_tool_rounds: int = 8
    max_tool_output: int = 6000
    history_limit: int = 30
    workdir: str = ""


@dataclass
class MemoryConfig:
    """Long-term memory behaviour."""

    auto_learn: bool = True
    learn_every: int = 3
    max_facts_in_prompt: int = 20


@dataclass
class SearchConfig:
    """Web search backend."""

    provider: str = "duckduckgo"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    max_results: int = 5
    timeout: int = 20
    enabled: bool = True

    def resolve_api_key(self) -> str:
        if self.api_key.strip():
            return self.api_key.strip()
        if self.api_key_env:
            return os.environ.get(self.api_key_env.strip(), "").strip()
        return ""


@dataclass
class DaemonConfig:
    """Resident-process behaviour: hotkey, scheduler, notifications, tray."""

    hotkey: str = "ctrl+alt+j"
    scheduler: bool = True
    notify: bool = True
    tray: bool = True
    tray_tooltip: str = "JARVIS-Win · AI 助手"


@dataclass
class SubAgentConfig:
    """Settings for ``delegate_subagents``: how the main agent farms out work.

    ``default_timeout`` is the wall-clock budget after which the manager surfaces a
    partial snapshot to the UI; it does *not* cancel the still-running sub-agents -
    they keep going until ``max_runtime`` (or they finish naturally), and the final
    summary arrives when they are all back. The point is to keep the chat moving:
    the user gets something to read while the slowest task is still cooking.
    """

    default_timeout: float = 30.0   # seconds before partial output (UI only)
    max_runtime: float = 600.0      # absolute cap per sub-agent (model call + tools)
    max_concurrent: int = 4         # asyncio semaphore; >=1
    max_per_call: int = 8           # max prompts in one delegate_subagents call


@dataclass
class Config:
    default_model: str
    models: dict[str, ModelConfig]
    security: SecurityConfig
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    subagents: SubAgentConfig = field(default_factory=SubAgentConfig)
    fallback_models: list[str] = field(default_factory=list)
    user_providers: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path = CONFIG_PATH
    created: bool = False

    # ----------------------------------------------------------------- lookup
    def model(self, name: str | None = None) -> ModelConfig:
        key = name or self.default_model
        if key not in self.models:
            raise KeyError(
                f"未知模型 '{key}'，可选：{', '.join(sorted(self.models)) or '(无)'}"
            )
        return self.models[key]

    def model_names(self) -> list[str]:
        return sorted(self.models)

    def find_model(self, token: str) -> str | None:
        """Resolve ``"2"``, ``"deepseek"`` or ``"deepseek-chat"`` to a model key."""

        token = token.strip()
        if not token:
            return None
        names = self.model_names()
        if token.isdigit():
            index = int(token) - 1
            return names[index] if 0 <= index < len(names) else None
        lowered = token.lower()
        for name in names:
            if name.lower() == lowered:
                return name
        for name in names:
            if name.lower().startswith(lowered):
                return name
        for name in names:
            cfg = self.models[name]
            if cfg.model.lower() == lowered:
                return name
        for name in names:
            if lowered in name.lower() or lowered in self.models[name].model.lower():
                return name
        return None

    @property
    def catalog(self) -> ProviderCatalog:
        return ProviderCatalog.build(self.user_providers)

    def fallbacks_for(self, current: str) -> list[str]:
        """The failover chain for ``current``, filtered to usable models."""

        chain: list[str] = []
        for name in self.fallback_models:
            key = self.find_model(name)
            if key and key != current and key not in chain:
                chain.append(key)
        if not chain:
            # No explicit chain: any other model that already has a key will do.
            for name in self.model_names():
                if name != current and self.models[name].key_ready:
                    chain.append(name)
            chain = chain[:2]
        return chain

    @property
    def workdir(self) -> Path:
        raw = self.security.workdir.strip()
        return Path(raw) if raw else PROJECT_ROOT

    # ------------------------------------------------------------------ edits
    def add_model(
        self,
        name: str,
        *,
        provider: str = "",
        model: str = "",
        label: str = "",
        base_url: str = "",
        api_key: str = "",
        api_key_env: str = "",
        temperature: float = 0.3,
        timeout: float = 180.0,
        supports_tools: bool = True,
        make_default: bool = False,
        save: bool = True,
    ) -> ModelConfig:
        """Add or update a model entry, filling the endpoint in from a provider."""

        key = name.strip()
        if not key:
            raise ValueError("模型名不能为空")

        provider_key = provider.strip().lower()
        preset = self.catalog.get(provider_key) if provider_key else None
        if provider_key and preset is None:
            raise KeyError(
                f"未知 provider '{provider_key}'。用 /provider list 查看内置列表，"
                "或先用 /provider add 定义一个。"
            )
        if not provider_key:
            provider_key = guess_provider(base_url)

        entry = self.models.get(key)
        cfg = ModelConfig(
            name=key,
            label=(label.strip() or (preset.label if preset else "")
                   or (entry.label if entry else "")),
            base_url=(base_url.strip() or (preset.base_url if preset else "")
                      or (entry.base_url if entry else "")),
            model=model.strip() or (entry.model if entry else ""),
            api_key=api_key.strip() or (entry.api_key if entry else ""),
            api_key_env=(api_key_env.strip() or (preset.api_key_env if preset else "")
                         or (entry.api_key_env if entry else "")),
            supports_tools=supports_tools,
            temperature=temperature,
            timeout=timeout if timeout != 180.0 else (entry.timeout if entry else 180.0),
            provider=provider_key,
        )
        if not cfg.model:
            raise ValueError(f"模型 '{key}' 缺少 model 字段（例如 deepseek-chat）")
        if not cfg.base_url:
            raise ValueError(f"模型 '{key}' 缺少 base_url，也没指定 provider")

        self.models[key] = cfg
        if make_default or not self.default_model:
            self.default_model = key
        if save:
            self.save()
        return cfg

    def add_provider(
        self,
        name: str,
        *,
        base_url: str,
        label: str = "",
        api_key_env: str = "",
        models: list[str] | None = None,
        note: str = "",
        save: bool = True,
    ) -> ProviderPreset:
        """Define (or override) a provider that is not in the built-in catalogue."""

        key = name.strip().lower()
        if not key:
            raise ValueError("provider 名不能为空")
        if not base_url.strip():
            raise ValueError(f"provider '{key}' 必须给出 base_url")
        if key in self.catalog.presets and key not in self.user_providers:
            # Overriding a built-in: keep it as a user entry so it survives a reload.
            pass
        self.user_providers[key] = {
            "label": label.strip(),
            "base_url": base_url.strip(),
            "api_key_env": api_key_env.strip(),
            "models": list(models or []),
            "note": note.strip(),
        }
        if save:
            self.save()
        return self.catalog.require(key)

    def remove_model(self, name: str) -> bool:
        key = self.find_model(name) or name
        if key not in self.models:
            return False
        if len(self.models) == 1:
            raise ValueError("至少要保留一个模型")
        del self.models[key]
        self.fallback_models = [m for m in self.fallback_models if m != key]
        if self.default_model == key:
            self.default_model = self.model_names()[0]
        self.save()
        return True

    def remove_provider(self, name: str) -> bool:
        key = name.strip().lower()
        if key not in self.user_providers:
            return False
        del self.user_providers[key]
        self.save()
        return True

    def set_default(self, name: str) -> ModelConfig:
        cfg = self.model(name)
        self.default_model = cfg.name
        self.save()
        return cfg

    # ------------------------------------------------------------------ write
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"default_model": self.default_model}
        if self.fallback_models:
            data["fallback_models"] = list(self.fallback_models)

        models: dict[str, Any] = {}
        for name, cfg in sorted(self.models.items()):
            models[name] = _model_to_dict(cfg, self.catalog.get(cfg.provider))
        data["models"] = models

        if self.user_providers:
            data["providers"] = {
                name: dict(item) for name, item in sorted(self.user_providers.items())
            }

        data["security"] = {
            "auto_approve": list(self.security.auto_approve),
            "shell_timeout": self.security.shell_timeout,
            "max_tool_rounds": self.security.max_tool_rounds,
            "max_tool_output": self.security.max_tool_output,
            "history_limit": self.security.history_limit,
            "workdir": self.security.workdir,
        }
        data["memory"] = {
            "auto_learn": self.memory.auto_learn,
            "learn_every": self.memory.learn_every,
            "max_facts_in_prompt": self.memory.max_facts_in_prompt,
        }
        data["search"] = {
            "provider": self.search.provider,
            "base_url": self.search.base_url,
            "api_key": self.search.api_key,
            "api_key_env": self.search.api_key_env,
            "max_results": self.search.max_results,
            "timeout": self.search.timeout,
            "enabled": self.search.enabled,
        }
        data["daemon"] = {
            "hotkey": self.daemon.hotkey,
            "scheduler": self.daemon.scheduler,
            "notify": self.daemon.notify,
            "tray": self.daemon.tray,
            "tray_tooltip": self.daemon.tray_tooltip,
        }
        return data

    def render(self) -> str:
        """Human-readable config.toml (comments regenerated on every save)."""

        lines: list[str] = []
        lines.extend(f"# {line}".rstrip() for line in CONFIG_HEADER.splitlines())
        lines.append("")
        lines.append(f'default_model = "{self.default_model}"')
        rendered_fallback = ", ".join(f'"{m}"' for m in self.fallback_models)
        lines.append(f"fallback_models = [{rendered_fallback}]")
        lines.append("# fallback_models：主模型连不上时按顺序自动切换（留空 = 自动挑选已配 Key 的模型）")

        lines.append("")
        lines.append("# ------------------------------- models -------------------------------------")
        lines.append("# provider 决定 base_url / api_key_env，可用 /provider list 查看全部内置项。")
        for name, cfg in sorted(self.models.items()):
            preset = self.catalog.get(cfg.provider)
            lines.append("")
            lines.append(f"[models.{name}]")
            if cfg.provider:
                lines.append(f'provider = "{cfg.provider}"')
            lines.append(f'model = "{cfg.model}"')
            inherited: list[str] = []
            for field_name, value in (("label", cfg.label), ("base_url", cfg.base_url),
                                      ("api_key_env", cfg.api_key_env)):
                default = getattr(preset, field_name, "") if preset else ""
                if value and value != default:
                    lines.append(f'{field_name} = "{value}"')
                elif value and preset is not None:
                    inherited.append(field_name)
            if inherited:
                lines.append(f"# {' / '.join(inherited)} 继承自 provider 「{cfg.provider}」")
            if cfg.api_key.strip():
                lines.append(f'api_key = "{cfg.api_key}"')
            lines.append(f"supports_tools = {_bool(cfg.supports_tools)}")
            lines.append(f"temperature = {cfg.temperature}")
            if cfg.timeout != 180.0:
                lines.append(f"timeout = {cfg.timeout}")

        if self.user_providers:
            lines.append("")
            lines.append("# ----------------------------- providers ------------------------------------")
            lines.append("# 自定义 provider（/provider add 写入），会覆盖同名的内置预设。")
            for name, item in sorted(self.user_providers.items()):
                lines.append("")
                lines.append(f"[providers.{name}]")
                # Only write what the user actually set. An omitted field means
                # "inherit from the built-in preset of the same name", so
                # emitting blanks would be noise - and on the next read a blank
                # label would shadow the built-in one.
                if item.get("label"):
                    lines.append(f'label = "{item["label"]}"')
                lines.append(f'base_url = "{item.get("base_url", "")}"')
                if item.get("api_key_env"):
                    lines.append(f'api_key_env = "{item["api_key_env"]}"')
                if item.get("note"):
                    lines.append(f'note = "{item["note"]}"')
                models = item.get("models") or []
                if models:
                    joined = ", ".join(f'"{m}"' for m in models)
                    lines.append(f"models = [{joined}]")

        return "\n".join(lines) + "\n" + _render_tail(self)

    def to_toml(self) -> str:
        """Machine-style dump of every value (used by tests and tooling)."""

        return tomlwrite.dumps(self.to_dict(), header=CONFIG_HEADER.splitlines()[0])

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render(), encoding="utf-8")
        return target


# ------------------------------------------------------------------- rendering
def _bool(value: bool) -> str:
    return "true" if value else "false"


def _render_tail(config: "Config") -> str:
    sec = config.security
    mem = config.memory
    search = config.search
    daemon = config.daemon
    approval = ", ".join(f'"{p}"' for p in sec.auto_approve)
    parts = [
        "# ---------------------------- security --------------------------------------",
        "[security]",
        "# 列在这里的工具免确认执行（支持通配符），慎用：run_shell / write_file 不要加。",
        f"auto_approve = [{approval}]",
        f"shell_timeout = {sec.shell_timeout}",
        f"max_tool_rounds = {sec.max_tool_rounds}",
        f"max_tool_output = {sec.max_tool_output}",
        f"history_limit = {sec.history_limit}",
        "# shell 工具的默认工作目录，留空 = 项目根目录。",
        f'workdir = "{sec.workdir}"',
        "",
        "# ------------------------- long-term memory ---------------------------------",
        "[memory]",
        "# 让 JARVIS 自己沉淀长期记忆（偏好、路径、既定结论）。",
        f"auto_learn = {_bool(mem.auto_learn)}",
        "# 每 N 轮对话做一次提炼。",
        f"learn_every = {mem.learn_every}",
        "# 注入系统提示的记忆条数上限。",
        f"max_facts_in_prompt = {mem.max_facts_in_prompt}",
        "",
        "# ----------------------------- web search -----------------------------------",
        "[search]",
        "# duckduckgo（免 Key）| bocha | tavily | serper | searxng，详见 /provider list。",
        f'provider = "{search.provider}"',
        "# 留空用 provider 默认端点；自建 SearXNG 时填你的实例地址。",
        f'base_url = "{search.base_url}"',
        f'api_key = "{search.api_key}"',
        f'api_key_env = "{search.api_key_env}"',
        f"max_results = {search.max_results}",
        f"timeout = {search.timeout}",
        f"enabled = {_bool(search.enabled)}",
        "",
        "# ------------------------------- daemon -------------------------------------",
        "[daemon]",
        "# 全局热键（jarvis --daemon 常驻时生效）。",
        f'hotkey = "{daemon.hotkey}"',
        "# 由 TUI / 守护进程触发定时任务。",
        f"scheduler = {_bool(daemon.scheduler)}",
        "# Windows 通知中心弹窗。",
        f"notify = {_bool(daemon.notify)}",
        "# 常驻时显示系统托盘图标（右键菜单：打开 / 任务 / 状态 / 退出）。",
        f"tray = {_bool(daemon.tray)}",
        f'tray_tooltip = "{daemon.tray_tooltip}"',
        "",
        "# ------------------------------- subagents --------------------------------",
        "[subagents]",
        "# 默认超时（秒）：超时后把已完成的部分先输出给用户，剩下的继续跑完再追加最终汇总。",
        f"default_timeout = {config.subagents.default_timeout}",
        "# 单个子 Agent 的绝对最长用时（秒）。",
        f"max_runtime = {config.subagents.max_runtime}",
        "# 同时在跑的子 Agent 数量上限（信号量）。",
        f"max_concurrent = {config.subagents.max_concurrent}",
        "# 一次 delegate_subagents 调用允许的最多 prompt 数。",
        f"max_per_call = {config.subagents.max_per_call}",
    ]
    return "\n".join(parts) + "\n"


def _model_to_dict(cfg: ModelConfig, preset: ProviderPreset | None) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if cfg.provider:
        item["provider"] = cfg.provider
    if cfg.label and (preset is None or cfg.label != preset.label):
        item["label"] = cfg.label
    if cfg.base_url and (preset is None or cfg.base_url != preset.base_url):
        item["base_url"] = cfg.base_url
    item["model"] = cfg.model
    item["api_key"] = cfg.api_key
    if cfg.api_key_env and (preset is None or cfg.api_key_env != preset.api_key_env):
        item["api_key_env"] = cfg.api_key_env
    item["supports_tools"] = cfg.supports_tools
    item["temperature"] = cfg.temperature
    if cfg.timeout != 180.0:
        item["timeout"] = cfg.timeout
    return item


# ---------------------------------------------------------------------- parsing
def _parse(raw: dict) -> Config:
    user_providers: dict[str, dict[str, str]] = {}
    for name, item in (raw.get("providers") or {}).items():
        if not isinstance(item, dict):
            continue
        models = item.get("models") or []
        user_providers[str(name)] = {
            # Leave label empty when the user did not set one, so that
            # ProviderCatalog.build can fall back to the built-in preset's
            # label (or the entry name) instead of shadowing it with the key.
            "label": str(item.get("label", "")),
            "base_url": str(item.get("base_url", "")),
            "api_key_env": str(item.get("api_key_env", "")),
            "models": [str(m) for m in models] if isinstance(models, list) else [],
            "note": str(item.get("note", "")),
        }

    catalog = ProviderCatalog.build(user_providers)
    models: dict[str, ModelConfig] = {}
    for name, item in (raw.get("models") or {}).items():
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", "")).lower()
        base_url = str(item.get("base_url", ""))
        if not provider:
            provider = guess_provider(base_url)
        preset = catalog.get(provider) if provider else None
        models[name] = ModelConfig(
            name=str(name),
            label=str(item.get("label") or (preset.label if preset else "")),
            base_url=base_url or (preset.base_url if preset else ""),
            model=str(item.get("model", "")),
            api_key=str(item.get("api_key", "")),
            api_key_env=str(item.get("api_key_env") or (preset.api_key_env if preset else "")),
            supports_tools=bool(item.get("supports_tools", True)),
            temperature=float(item.get("temperature", 0.3)),
            timeout=float(item.get("timeout", 180.0)),
            provider=provider,
        )

    sec_raw = raw.get("security") or {}
    security = SecurityConfig(
        auto_approve=[str(p) for p in (sec_raw.get("auto_approve") or [])],
        shell_timeout=int(sec_raw.get("shell_timeout", 60)),
        max_tool_rounds=int(sec_raw.get("max_tool_rounds", 8)),
        max_tool_output=int(sec_raw.get("max_tool_output", 6000)),
        history_limit=int(sec_raw.get("history_limit", 30)),
        workdir=str(sec_raw.get("workdir", "")),
    )

    default_model = str(raw.get("default_model") or (next(iter(models)) if models else "deepseek"))

    mem_raw = raw.get("memory") or {}
    memory = MemoryConfig(
        auto_learn=bool(mem_raw.get("auto_learn", True)),
        learn_every=max(1, int(mem_raw.get("learn_every", 3))),
        max_facts_in_prompt=int(mem_raw.get("max_facts_in_prompt", 20)),
    )

    daemon_raw = raw.get("daemon") or {}
    daemon = DaemonConfig(
        hotkey=str(daemon_raw.get("hotkey", "ctrl+alt+j")),
        scheduler=bool(daemon_raw.get("scheduler", True)),
        notify=bool(daemon_raw.get("notify", True)),
        tray=bool(daemon_raw.get("tray", True)),
        tray_tooltip=str(daemon_raw.get("tray_tooltip", "JARVIS-Win · AI 助手")),
    )
    sub_raw = raw.get("subagents") or {}
    subagents = SubAgentConfig(
        default_timeout=float(sub_raw.get("default_timeout", 30.0)),
        max_runtime=float(sub_raw.get("max_runtime", 600.0)),
        max_concurrent=max(1, int(sub_raw.get("max_concurrent", 4))),
        max_per_call=max(1, int(sub_raw.get("max_per_call", 8))),
    )

    return Config(
        default_model=default_model,
        models=models,
        security=security,
        memory=memory,
        daemon=daemon,
        search=_parse_search(raw.get("search") or {}),
        subagents=subagents,
        fallback_models=[str(m) for m in (raw.get("fallback_models") or [])],
        user_providers=user_providers,
    )


def _parse_search(raw: dict) -> SearchConfig:
    from .providers import SEARCH_PROVIDERS

    name = str(raw.get("provider", "duckduckgo")).lower()
    preset = SEARCH_PROVIDERS.get(name)
    return SearchConfig(
        provider=name,
        base_url=str(raw.get("base_url") or (preset.base_url if preset else "")),
        api_key=str(raw.get("api_key", "")),
        api_key_env=str(raw.get("api_key_env") or (preset.api_key_env if preset else "")),
        max_results=max(1, int(raw.get("max_results", 5))),
        timeout=max(3, int(raw.get("timeout", 20))),
        enabled=bool(raw.get("enabled", True)),
    )


def build_default_config() -> Config:
    """The config written on first launch (no keys, no facts)."""

    catalog = ProviderCatalog.build({})
    models: dict[str, ModelConfig] = {}
    for name, provider, model in _TEMPLATE_MODELS:
        preset = catalog.require(provider)
        models[name] = ModelConfig(
            name=name,
            label=preset.label,
            base_url=preset.base_url,
            model=model,
            api_key_env=preset.api_key_env,
            provider=provider,
        )
    return Config(
        default_model="deepseek",
        models=models,
        security=SecurityConfig(),
        fallback_models=["qwen", "ollama"],
    )


def load_config(path: Path | None = None) -> Config:
    """Load ``config.toml``, generating it when missing."""

    target = Path(path) if path else CONFIG_PATH
    created = False
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(build_default_config().render(), encoding="utf-8")
        created = True

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    config = _parse(raw)
    config.path = target
    config.created = created
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config


__all__ = [
    "CONFIG_PATH",
    "DATA_DIR",
    "PROJECT_ROOT",
    "Config",
    "DaemonConfig",
    "MemoryConfig",
    "ModelConfig",
    "SearchConfig",
    "SecurityConfig",
    "build_default_config",
    "load_config",
]
