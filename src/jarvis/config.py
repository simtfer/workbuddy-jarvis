"""Configuration loading for JARVIS-Win.

A single ``config.toml`` at the project root keeps model endpoints, security
policy and runtime tuning. On first launch the file is generated from a
template so the user can simply fill in an API key.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.toml"
DATA_DIR = PROJECT_ROOT / "data"

CONFIG_TEMPLATE = """\
# JARVIS-Win configuration
# Docs: see README.md.  This file is git-ignored - keep API keys out of git.

default_model = "deepseek"

# ----------------------------- models ---------------------------------------
# Any OpenAI-compatible endpoint works: fill base_url / model / api_key.
# Leave api_key empty to read it from the environment variable api_key_env.

[models.deepseek]
label = "DeepSeek"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key = ""
api_key_env = "DEEPSEEK_API_KEY"
supports_tools = true
temperature = 0.3

[models.qwen]
label = "Qwen (DashScope)"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-plus"
api_key = ""
api_key_env = "DASHSCOPE_API_KEY"
supports_tools = true
temperature = 0.3

[models.doubao]
label = "Doubao (Volcengine)"
base_url = "https://ark.cn-beijing.volces.com/api/v3"
model = "doubao-seed-1-6-250615"
api_key = ""
api_key_env = "ARK_API_KEY"
supports_tools = true
temperature = 0.3

[models.ollama]
label = "Ollama (local)"
base_url = "http://127.0.0.1:11434/v1"
model = "qwen3:8b"
api_key = "ollama"
supports_tools = true
temperature = 0.3

# ---------------------------- security --------------------------------------
[security]
# Tools listed here run without asking for confirmation (fnmatch patterns).
auto_approve = []
shell_timeout = 60
max_tool_rounds = 8
max_tool_output = 6000
history_limit = 30
# Default working directory for shell tools; empty = project root.
workdir = ""

# ---------------------------- long-term memory ------------------------------
[memory]
# Let JARVIS distil durable facts (preferences, project conventions) by itself.
auto_learn = true
# Run the extraction pass every N completed turns.
learn_every = 3
# How many facts to inject into the system prompt.
max_facts_in_prompt = 20

# ------------------------------- daemon -------------------------------------
[daemon]
# Global hotkey that opens a JARVIS window (jarvis --daemon must be running).
hotkey = "ctrl+alt+j"
# Let the daemon / TUI fire scheduled tasks.
scheduler = true
# Windows toast notifications.
notify = true
"""


@dataclass
class ModelConfig:
    """One OpenAI-compatible endpoint."""

    name: str
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    supports_tools: bool = True
    temperature: float = 0.3

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
class DaemonConfig:
    """Resident-process behaviour: hotkey, scheduler, notifications."""

    hotkey: str = "ctrl+alt+j"
    scheduler: bool = True
    notify: bool = True


@dataclass
class Config:
    default_model: str
    models: dict[str, ModelConfig]
    security: SecurityConfig
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    path: Path = CONFIG_PATH
    created: bool = False

    def model(self, name: str | None = None) -> ModelConfig:
        key = name or self.default_model
        if key not in self.models:
            raise KeyError(
                f"未知模型 '{key}'，可选：{', '.join(sorted(self.models)) or '(无)'}"
            )
        return self.models[key]

    @property
    def workdir(self) -> Path:
        raw = self.security.workdir.strip()
        return Path(raw) if raw else PROJECT_ROOT


def _parse(raw: dict) -> Config:
    models: dict[str, ModelConfig] = {}
    for name, item in (raw.get("models") or {}).items():
        if not isinstance(item, dict):
            continue
        models[name] = ModelConfig(
            name=name,
            label=str(item.get("label", "")),
            base_url=str(item.get("base_url", "")),
            model=str(item.get("model", "")),
            api_key=str(item.get("api_key", "")),
            api_key_env=str(item.get("api_key_env", "")),
            supports_tools=bool(item.get("supports_tools", True)),
            temperature=float(item.get("temperature", 0.3)),
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
    )

    return Config(
        default_model=default_model,
        models=models,
        security=security,
        memory=memory,
        daemon=daemon,
    )


def load_config(path: Path | None = None) -> Config:
    """Load ``config.toml``, generating it from the template when missing."""

    target = Path(path) if path else CONFIG_PATH
    created = False
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        created = True

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    config = _parse(raw)
    config.path = target
    config.created = created
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config
