"""Built-in provider presets.

A *provider* is "an OpenAI-compatible endpoint + where its API key lives".
A *model* is "one provider + one model id + tuning". Keeping the two apart
means adding a new model for an endpoint you already use is one line of TOML:

    [models.gpt-mini]
    provider = "openai"      # inherits base_url / api_key_env
    model = "gpt-4o-mini"

Anything not in this catalogue can still be configured by hand - either by
writing ``[providers.<name>]`` yourself, or with ``/provider add`` in the TUI
(``jarvis --add-model`` from the shell), which writes it back to config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderPreset:
    """A known OpenAI-compatible endpoint."""

    name: str
    label: str
    base_url: str
    api_key_env: str = ""
    models: tuple[str, ...] = ()
    console: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }


def _p(*args, **kwargs) -> ProviderPreset:  # tiny alias to keep the table readable
    return ProviderPreset(*args, **kwargs)


# --------------------------------------------------------------------------- llm
PROVIDERS: dict[str, ProviderPreset] = {
    # ---------------------------------------------------------------- 国内
    "deepseek": _p(
        "deepseek",
        "DeepSeek 深度求索",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        ("deepseek-chat", "deepseek-reasoner"),
        "https://platform.deepseek.com/api_keys",
    ),
    "dashscope": _p(
        "dashscope",
        "通义千问（阿里云百炼）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        ("qwen-plus", "qwen-max", "qwen-turbo", "qwen3-coder-plus"),
        "https://bailian.console.aliyun.com/",
    ),
    "ark": _p(
        "ark",
        "豆包（火山方舟）",
        "https://ark.cn-beijing.volces.com/api/v3",
        "ARK_API_KEY",
        ("doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"),
        "https://console.volcengine.com/ark",
        "model 填方舟上的「接入点 ID」或模型 ID",
    ),
    "moonshot": _p(
        "moonshot",
        "Kimi（月之暗面）",
        "https://api.moonshot.cn/v1",
        "MOONSHOT_API_KEY",
        ("kimi-k2-0905-preview", "moonshot-v1-128k"),
        "https://platform.moonshot.cn/console/api-keys",
    ),
    "zhipu": _p(
        "zhipu",
        "智谱 GLM",
        "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPUAI_API_KEY",
        ("glm-4-plus", "glm-4-flash", "glm-4.5"),
        "https://bigmodel.cn/usercenter/apikeys",
    ),
    "siliconflow": _p(
        "siliconflow",
        "硅基流动 SiliconFlow",
        "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY",
        ("deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-32B"),
        "https://cloud.siliconflow.cn/account/ak",
        "一个 key 通吃很多开源模型",
    ),
    "minimax": _p(
        "minimax",
        "MiniMax",
        "https://api.minimax.chat/v1",
        "MINIMAX_API_KEY",
        ("MiniMax-Text-01", "abab6.5s-chat"),
        "https://platform.minimaxi.com/",
    ),
    "qianfan": _p(
        "qianfan",
        "百度千帆",
        "https://qianfan.baidubce.com/v2",
        "QIANFAN_API_KEY",
        ("ernie-4.5-turbo-128k", "ernie-speed-128k"),
        "https://console.bce.baidu.com/qianfan/",
    ),
    "hunyuan": _p(
        "hunyuan",
        "腾讯混元",
        "https://api.hunyuan.cloud.tencent.com/v1",
        "HUNYUAN_API_KEY",
        ("hunyuan-turbos-latest", "hunyuan-standard"),
        "https://console.cloud.tencent.com/hunyuan/api-key",
    ),
    # ---------------------------------------------------------------- 海外
    "openai": _p(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        ("gpt-4o", "gpt-4o-mini"),
        "https://platform.openai.com/api-keys",
    ),
    "openrouter": _p(
        "openrouter",
        "OpenRouter（聚合）",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        ("anthropic/claude-sonnet-4", "google/gemini-2.5-pro"),
        "https://openrouter.ai/keys",
        "一个 key 用上百个模型",
    ),
    "groq": _p(
        "groq",
        "Groq（超快推理）",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        ("llama-3.3-70b-versatile",),
        "https://console.groq.com/keys",
    ),
    "mistral": _p(
        "mistral",
        "Mistral AI",
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
        ("mistral-large-latest", "mistral-small-latest"),
        "https://console.mistral.ai/api-keys/",
    ),
    "xai": _p(
        "xai",
        "xAI Grok",
        "https://api.x.ai/v1",
        "XAI_API_KEY",
        ("grok-4", "grok-3-mini"),
        "https://console.x.ai/",
    ),
    "gemini": _p(
        "gemini",
        "Google Gemini（OpenAI 兼容层）",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        ("gemini-2.5-pro", "gemini-2.5-flash"),
        "https://aistudio.google.com/apikey",
    ),
    # ---------------------------------------------------------------- 本地
    "ollama": _p(
        "ollama",
        "Ollama（本机）",
        "http://127.0.0.1:11434/v1",
        "",
        ("qwen3:8b", "llama3.1:8b"),
        "https://ollama.com/download",
        "本地推理，不需要 API Key",
    ),
    "lmstudio": _p(
        "lmstudio",
        "LM Studio（本机）",
        "http://127.0.0.1:1234/v1",
        "",
        ("local-model",),
        "https://lmstudio.ai/",
        "本地推理，不需要 API Key；model 填 LM Studio 里显示的模型名",
    ),
    "vllm": _p(
        "vllm",
        "vLLM / 自建服务",
        "http://127.0.0.1:8000/v1",
        "",
        ("",),
        "",
        "任何 OpenAI 兼容的自建端点，把 base_url 改成你的地址",
    ),
}


# ----------------------------------------------------------------------- search
@dataclass(frozen=True)
class SearchPreset:
    """A web-search backend."""

    name: str
    label: str
    kind: str  # scrape | api
    base_url: str = ""
    api_key_env: str = ""
    needs_key: bool = False
    console: str = ""
    note: str = ""


SEARCH_PROVIDERS: dict[str, SearchPreset] = {
    "duckduckgo": SearchPreset(
        name="duckduckgo",
        label="DuckDuckGo（免 Key，默认）",
        kind="scrape",
        base_url="https://html.duckduckgo.com/html/",
        note="无需注册即可用；结果质量中等，偶尔会被限流",
    ),
    "bocha": SearchPreset(
        name="bocha",
        label="博查 Bocha（中文友好）",
        kind="api",
        base_url="https://api.bochaai.com/v1/web-search",
        api_key_env="BOCHA_API_KEY",
        needs_key=True,
        console="https://open.bochaai.com/",
        note="国内合规搜索 API，中文结果好",
    ),
    "tavily": SearchPreset(
        name="tavily",
        label="Tavily（为 LLM 设计）",
        kind="api",
        base_url="https://api.tavily.com/search",
        api_key_env="TAVILY_API_KEY",
        needs_key=True,
        console="https://app.tavily.com/home",
        note="返回干净摘要，适合喂给模型",
    ),
    "serper": SearchPreset(
        name="serper",
        label="Serper（Google 结果）",
        kind="api",
        base_url="https://google.serper.dev/search",
        api_key_env="SERPER_API_KEY",
        needs_key=True,
        console="https://serper.dev/",
    ),
    "searxng": SearchPreset(
        name="searxng",
        label="SearXNG（自建元搜索）",
        kind="scrape",
        base_url="http://127.0.0.1:8080/search",
        note="需要你自建实例，把 base_url 改成实例地址",
    ),
}


# --------------------------------------------------------------------- helpers
@dataclass
class ProviderCatalog:
    """Presets merged with whatever the user wrote in ``[providers.*]``."""

    presets: dict[str, ProviderPreset] = field(default_factory=dict)

    @classmethod
    def build(cls, user_providers: dict[str, dict[str, str]] | None = None) -> "ProviderCatalog":
        merged = dict(PROVIDERS)
        for name, item in (user_providers or {}).items():
            base = merged.get(name)
            merged[name] = ProviderPreset(
                name=name,
                label=str(item.get("label") or (base.label if base else name)),
                base_url=str(item.get("base_url") or (base.base_url if base else "")),
                api_key_env=str(item.get("api_key_env") or (base.api_key_env if base else "")),
                models=tuple(item.get("models") or (base.models if base else ())),
                console=str(item.get("console") or (base.console if base else "")),
                note=str(item.get("note") or (base.note if base else "")),
            )
        return cls(presets=merged)

    def get(self, name: str) -> ProviderPreset | None:
        return self.presets.get(name.strip().lower())

    def require(self, name: str) -> ProviderPreset:
        preset = self.get(name)
        if preset is None:
            known = ", ".join(sorted(self.presets))
            raise KeyError(f"未知 provider '{name}'。内置可用：{known}")
        return preset

    def names(self) -> list[str]:
        return sorted(self.presets)

    def is_builtin(self, name: str) -> bool:
        return name.strip().lower() in PROVIDERS


def guess_provider(base_url: str) -> str:
    """Best-effort reverse lookup: which preset does this base_url belong to?"""

    target = (base_url or "").strip().rstrip("/").lower()
    if not target:
        return ""
    for name, preset in PROVIDERS.items():
        if preset.base_url.rstrip("/").lower() == target:
            return name
    for name, preset in PROVIDERS.items():
        host = preset.base_url.split("//")[-1].split("/")[0]
        if host and host in target:
            return name
    return ""
