"""Phase 3 checks: providers, config write-back, hot switching, web tools, tray.

Offline: no network calls leave this process (the web tests stub ``http_get``).
Run with:  uv run python -u tests/phase3.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from jarvis.config import (
    Config,
    DaemonConfig,
    MemoryConfig,
    ModelConfig,
    SearchConfig,
    SecurityConfig,
    build_default_config,
    load_config,
)
from jarvis.core.agent import Agent
from jarvis.core.registry import ToolRegistry
from jarvis.llm.client import LLMError, StreamResult
from jarvis.providers import PROVIDERS, SEARCH_PROVIDERS, ProviderCatalog, guess_provider
from jarvis.tomlwrite import dumps
from jarvis.tui.app import split_flags

faulthandler.dump_traceback_later(90, exit=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------- fake LLM
class TextClient:
    """Streams a canned reply in chunks."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.tools_supported = True

    async def stream(self, messages, tools=None, on_delta=None):
        if on_delta:
            for index in range(0, len(self.text), 8):
                on_delta(self.text[index : index + 8])
        return StreamResult(content=self.text)

    async def aclose(self) -> None:
        pass


class FailClient:
    """Always blows up, like a dead endpoint or an invalid key."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.tools_supported = True

    async def stream(self, messages, tools=None, on_delta=None):
        raise LLMError(self.message)

    async def aclose(self) -> None:
        pass


def model(name: str, base_url: str = "http://127.0.0.1:1/v1") -> ModelConfig:
    return ModelConfig(name=name, base_url=base_url, model=f"{name}-model", api_key="x")


def make_config(**models: ModelConfig) -> Config:
    return Config(
        default_model=next(iter(models)),
        models=models,
        security=SecurityConfig(max_tool_rounds=2),
        memory=MemoryConfig(learn_every=99),
        daemon=DaemonConfig(),
    )


# -------------------------------------------------------------------- providers
def test_providers() -> None:
    check("内置 provider 数量 >= 15", len(PROVIDERS) >= 15, str(len(PROVIDERS)))
    for required in ("deepseek", "dashscope", "ark", "moonshot", "zhipu", "openai", "ollama"):
        check(f"内置 provider 含 {required}", required in PROVIDERS)
    check(
        "本地 provider 不需要 Key",
        PROVIDERS["ollama"].api_key_env == "" and PROVIDERS["lmstudio"].api_key_env == "",
    )
    check("guess_provider 按 base_url 反查", guess_provider("https://api.deepseek.com/v1") == "deepseek")
    check("guess_provider 兼容尾斜杠", guess_provider("https://api.moonshot.cn/v1/") == "moonshot")
    check("guess_provider 未知返回空", guess_provider("http://10.0.0.9/v1") == "")

    catalog = ProviderCatalog.build({"myvllm": {"base_url": "http://10.0.0.9/v1", "label": "内网"}})
    check("自定义 provider 合并进目录", catalog.get("myvllm") is not None)
    check("自定义 provider 不覆盖内置数量", len(catalog.names()) == len(PROVIDERS) + 1)
    check("覆盖同名内置 provider 生效", catalog.require("deepseek").base_url.startswith("https://api"))

    for name in SEARCH_PROVIDERS:
        check(f"搜索后端 {name} 有端点", bool(SEARCH_PROVIDERS[name].base_url))


# ----------------------------------------------------------------------- toml
def test_tomlwrite() -> None:
    payload = {
        "default_model": "deepseek",
        "fallback_models": ["qwen", "ollama"],
        "flag": True,
        "count": 7,
        "temp": 0.3,
        "empty": "",
        "quoted": 'he said "hi"\nnewline\\slash',
        "models": {
            "a": {"provider": "deepseek", "temperature": 0.3},
            "b": {"provider": "ollama", "supports_tools": False},
        },
        "providers": {"x-y": {"base_url": "http://h/v1", "models": ["m1", "m2"]}},
    }
    text = dumps(payload, header="test header")
    parsed = tomllib.loads(text)
    check("TOML 数值往返", parsed["count"] == 7 and parsed["temp"] == 0.3)
    check("TOML 布尔往返", parsed["flag"] is True and parsed["models"]["b"]["supports_tools"] is False)
    check("TOML 数组往返", parsed["fallback_models"] == ["qwen", "ollama"])
    check("TOML 转义往返", parsed["quoted"] == payload["quoted"], repr(parsed["quoted"]))
    check("TOML 嵌套表往返", parsed["models"]["a"]["provider"] == "deepseek")
    check("TOML 带连字符的键往返", parsed["providers"]["x-y"]["models"] == ["m1", "m2"])
    check("TOML 注释头保留", text.startswith("# test header"))

# ---------------------------------------------------------------------- config
def test_config_providers(tmp: Path) -> None:
    cfg = build_default_config()
    check("默认配置有模型", len(cfg.models) >= 5, str(cfg.model_names()))
    check("默认模型 deepseek", cfg.default_model == "deepseek")
    d = cfg.model("deepseek")
    check("模板模型带 provider 标记", d.provider == "deepseek")
    check("模板模型 base_url 来自预设", d.base_url == PROVIDERS["deepseek"].base_url)
    check("模板模型 key_env 来自预设", d.api_key_env == "DEEPSEEK_API_KEY")

    # render -> parse round trip
    reparsed = load_config(_write(tmp / "round.toml", cfg.render()))
    check("render/parse 往返模型一致", reparsed.model_names() == cfg.model_names())
    check("render/parse 往返 provider 一致", reparsed.model("qwen").provider == "dashscope")
    check("render/parse 往返 fallback 一致", reparsed.fallback_models == cfg.fallback_models)
    check("render/parse 往返 search 一致", reparsed.search.provider == "duckduckgo")

    # provider inheritance: only base_url/api_key_env come from the preset
    cfg.add_provider("myvllm", base_url="http://10.0.0.9:8000/v1", label="内网 vLLM",
                     api_key_env="MYVLLM_KEY", save=False)
    added = cfg.add_model("nei", provider="myvllm", model="qwen3-32b", save=False)
    check("自定义 provider 继承 base_url", added.base_url == "http://10.0.0.9:8000/v1")
    check("自定义 provider 继承 key_env", added.api_key_env == "MYVLLM_KEY")
    check("自定义 provider label 继承", added.label == "内网 vLLM")

    # a model without a provider still works via an explicit base_url
    manual = cfg.add_model("manual", base_url="http://10.0.0.7:1234/v1", model="local",
                           api_key_env="NOPE", save=False)
    check("无 provider 时用显式 base_url", manual.base_url == "http://10.0.0.7:1234/v1")
    check("base_url 反查 provider", manual.provider == "")

    # provider inheritance wins only when the field is empty
    override = cfg.add_model("ovr", provider="deepseek", model="deepseek-reasoner",
                             base_url="https://proxy.internal/v1", save=False)
    check("显式 base_url 覆盖预设", override.base_url == "https://proxy.internal/v1")
    check("覆盖后仍保留 provider 名", override.provider == "deepseek")

    # per-model timeout: default is 180, custom values must survive a round trip
    check("默认超时为 180", cfg.model("deepseek").timeout == 180.0)
    slow = cfg.add_model("slow", provider="deepseek", model="deepseek-chat",
                         timeout=30.0, save=False)
    check("自定义超时写入模型", slow.timeout == 30.0)
    slower = cfg.add_model("slow", provider="deepseek", model="deepseek-chat", save=False)
    check("重复 add 保留已有超时", slower.timeout == 30.0)

    try:
        cfg.add_model("bad", provider="nope", model="x", save=False)
        check("未知 provider 应报错", False)
    except KeyError:
        check("未知 provider 被拦住", True)

    try:
        cfg.add_model("bad2", provider="deepseek", model="", save=False)
        check("缺 model 字段应报错", False)
    except ValueError:
        check("缺 model 字段被拦住", True)

    # persistence: saved file must reload with the custom provider intact
    target = _write(tmp / "custom.toml", cfg.render())
    reloaded = load_config(target)
    check("自定义 provider 落盘", "myvllm" in reloaded.catalog.presets)
    check("自定义模型落盘", reloaded.model("nei").base_url == "http://10.0.0.9:8000/v1")
    check("落盘后 reload 的 provider 目录含自定义", reloaded.catalog.get("myvllm") is not None)
    check("自定义超时落盘", "timeout = 30.0" in target.read_text(encoding="utf-8"))
    check("reload 后超时保留", reloaded.model("slow").timeout == 30.0)
    check("默认超时不写进文件",
          "timeout = 180" not in target.read_text(encoding="utf-8"))

    # lookup helpers
    check("find_model 按序号", reloaded.find_model("1") == reloaded.model_names()[0])
    check("find_model 越界返回 None", reloaded.find_model("99") is None)
    check("find_model 按模型 ID", reloaded.find_model("qwen3-32b") == "nei")
    check("find_model 前缀匹配", reloaded.find_model("deep") == "deepseek")
    check("find_model 空输入", reloaded.find_model("  ") is None)

    # fallback chain + edits
    reloaded.fallback_models = ["nei", "qwen"]
    check("显式 fallback 链", reloaded.fallbacks_for("deepseek") == ["nei", "qwen"])
    check("fallback 链排除自己", "deepseek" not in reloaded.fallbacks_for("deepseek"))

    auto = replace(reloaded, fallback_models=[])
    chain = auto.fallbacks_for("deepseek")
    usable = [n for n in auto.model_names() if n != "deepseek" and auto.models[n].key_ready]
    check("无显式链时按顺序挑可用的，最多两个", chain == usable[:2], str(chain))
    check(
        "自动链排除缺 Key 的云端模型",
        "qwen" not in chain and "doubao" not in chain and "kimi" not in chain,
        str(chain),
    )
    check("自动链包含本地/内网模型", "nei" in chain, str(chain))

    # self-hosted endpoints on the LAN must not be reported as "missing key"
    check("内网地址视为本地", reloaded.model("nei").is_local is True)
    check("内网模型不需要 Key", reloaded.model("nei").key_ready is True)
    check("带 Key 的云端模型不算本地", reloaded.model("deepseek").is_local is False)
    for url, expected in (
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:1234/v1", True),
        ("http://192.168.1.50:8000/v1", True),
        ("http://172.20.3.4:8000/v1", True),
        ("http://mybox.local:8000/v1", True),
        ("http://api.deepseek.com/v1", False),
        ("http://172.32.0.1:8000/v1", False),
        ("", False),
    ):
        probe = replace(reloaded.model("deepseek"), base_url=url)
        check(f"is_local({url or '空'}) == {expected}", probe.is_local is expected)

    reloaded.default_model = "deepseek"
    check("set_default 生效并落盘", reloaded.set_default("nei").name == "nei"
          and load_config(target).default_model == "nei")
    check("remove_model 生效", reloaded.remove_model("nei") and "nei" not in reloaded.models)
    check("remove_provider 生效", reloaded.remove_provider("myvllm"))

    single = make_config(only=model("only"))
    try:
        single.remove_model("only")
        check("不允许删到零个模型", False)
    except ValueError:
        check("最后一个模型删不掉", True)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------- agent
def test_hot_switch() -> None:
    cfg = make_config(alpha=model("alpha"), beta=model("beta"), gamma=model("gamma"))
    agent = Agent(config=cfg, registry=ToolRegistry(cfg.security), model_name="alpha")

    check("切换前是 alpha", agent.model_key == "alpha")
    check("按名字切换", agent.set_model("beta") == "beta" and agent.model_key == "beta")
    check("按序号切换", agent.set_model("3") == "gamma")
    check("按模型 ID 切换", agent.set_model("beta-model") == "beta")

    try:
        agent.set_model("nope")
        check("未知模型应报错", False)
    except KeyError:
        check("未知模型被拦住", True)

    async def run() -> None:
        clients = {"alpha": TextClient("来自 alpha")}
        agent.client_factory = lambda spec: clients[spec.name]
        agent.set_model("alpha")
        first = [event async for event in agent.run("你好")]
        check("切换后能正常对话", any("来自 alpha" in e.get("text", "") for e in first))
        check("上下文保留", len(agent.history()) == 2, str(agent.history()))
        check("客户端被缓存复用", agent._client is clients["alpha"])
        check("缓存记住了所属模型", agent._client_model == "alpha")

    asyncio.run(run())


def test_failover() -> None:
    cfg = make_config(primary=model("primary"), backup=model("backup"))
    cfg.fallback_models = ["backup"]
    agent = Agent(config=cfg, registry=ToolRegistry(cfg.security), model_name="primary")

    check("failover 链含两个模型", agent.failover_chain() == ["primary", "backup"])

    async def recover() -> None:
        clients = {"primary": FailClient("503 service unavailable"), "backup": TextClient("备用模型回答")}
        agent.client_factory = lambda spec: clients[spec.name]
        events = [event async for event in agent.run("在吗")]
        notices = [e["text"] for e in events if e["type"] == "notice"]
        text = "".join(e.get("text", "") for e in events if e["type"] == "text")
        check("失败模型报错提示", any("请求失败" in n for n in notices), str(notices[:1]))
        check("自动切换提示", any("已自动切到 backup" in n for n in notices), str(notices[-1:]))
        check("备用模型给出回答", "备用模型回答" in text)
        check("切换后模型名更新", agent.model_key == "backup")
        check("会话内不再回切", agent.failover_chain() == ["backup", "primary"])
        check("失败客户端已从缓存清掉", agent._client is None or agent._client is clients["backup"])

    asyncio.run(recover())

    async def total_failure() -> None:
        cfg2 = make_config(a=model("a"), b=model("b"))
        cfg2.fallback_models = ["b"]
        agent2 = Agent(config=cfg2, registry=ToolRegistry(cfg2.security), model_name="a")
        agent2.client_factory = lambda spec: FailClient(f"{spec.name} 挂了")
        events = [event async for event in agent2.run("在吗")]
        errors = [e["message"] for e in events if e["type"] == "error"]
        check("全部失败时给出错误", bool(errors), str(errors[:1]))
        check("错误里列出尝试过的模型", "a → b" in (errors[0] if errors else ""))

    asyncio.run(total_failure())

    async def bad_key() -> None:
        cfg3 = make_config(x=model("x"), y=model("y"))
        cfg3.fallback_models = ["y"]
        agent3 = Agent(config=cfg3, registry=ToolRegistry(cfg3.security), model_name="x")

        def builder(spec):
            if spec.name == "x":
                raise LLMError("模型 'x' 未配置 API Key。")
            return TextClient("y 顶上")

        agent3.client_factory = builder
        events = [event async for event in agent3.run("在吗")]
        text = "".join(e.get("text", "") for e in events if e["type"] == "text")
        check("缺 Key 也能切到有 Key 的模型", "y 顶上" in text and agent3.model_key == "y")

    asyncio.run(bad_key())


def test_fallback_config() -> None:
    cfg = make_config(a=model("a"), b=model("b"), c=model("c"))
    cfg.fallback_models = ["b", "x", "b", "c"]
    check("fallback 链过滤未知与重复", cfg.fallbacks_for("a") == ["b", "c"])
    check("fallback 链排除当前模型", "a" not in cfg.fallbacks_for("a"))
    cfg.fallback_models = []
    check("无链时按顺序挑两个", cfg.fallbacks_for("a") == ["b", "c"])


# ------------------------------------------------------------------------- web
DDG_FIXTURE = """
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=abc">第一条 &amp; 结果</a>
    </h2>
    <div class="result__extras"><span class="result__url">example.com</span></div>
    <a class="result__snippet" href="//x">摘要一 &lt;带标签&gt;</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="https://direct.example.org/two">第二条</a>
    </h2>
    <a class="result__snippet" href="//y">摘要二</a>
  </div>
</div>
"""


def test_web_parsing() -> None:
    from jarvis.tools import web

    check("DDG 重定向还原", web._unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fb") == "https://a.com/b")
    check("非重定向链接原样返回", web._unwrap_ddg("https://a.com/b") == "https://a.com/b")

    results = web._parse_duckduckgo_html(DDG_FIXTURE, limit=5)
    check("DDG 解析出 2 条", len(results) == 2, str(len(results)))
    check("DDG 标题解实体", results[0].title == "第一条 & 结果", results[0].title)
    check("DDG 链接已还原", results[0].url == "https://example.com/one", results[0].url)
    check("DDG 摘要清理标签", results[0].snippet == "摘要一 <带标签>", results[0].snippet)
    check("DDG 直链保留", results[1].url == "https://direct.example.org/two")
    check("DDG 限流生效", len(web._parse_duckduckgo_html(DDG_FIXTURE, limit=1)) == 1)

    html = """
    <html><head><title>文章标题</title><style>body{color:red}</style>
    <script>var x=1;</script></head>
    <body><h1>大标题</h1><p>第一段   文字</p><div><p>第二段</p></div>
    <ul><li>项目一</li><li>项目二</li></ul></body></html>
    """
    title, text = web.strip_html(html)
    check("HTML 标题提取", title == "文章标题", title)
    check("HTML 正文提取", "第一段 文字" in text, text[:60])
    check("HTML 去掉 script/style", "var x" not in text and "color:red" not in text)
    check("HTML 保留列表项", "项目一" in text and "项目二" in text)
    check("HTML 压缩空白", "    " not in text)


def test_web_backends() -> None:
    from jarvis.tools import web

    original = web.http_get
    calls: list[tuple[str, str]] = []

    def fake_get(url, *, timeout=20.0, headers=None, data=None, method=None):
        calls.append((url, method or "GET"))
        if "tavily" in url:
            return 200, '{"results":[{"title":"T","url":"https://t","content":"C"}]}', "application/json"
        if "bochaai" in url:
            return 200, ('{"data":{"webPages":{"value":[{"name":"B","url":"https://b",'
                         '"snippet":"S"}]}}}'), "application/json"
        if "serper" in url:
            return 200, '{"organic":[{"title":"G","link":"https://g","snippet":"SS"}]}', "application/json"
        if "example.org" in url:
            return 200, "<html><title>抓取标题</title><body><p>网页正文内容</p></body></html>", "text/html"
        return 200, DDG_FIXTURE, "text/html"

    web.http_get = fake_get
    try:
        cfg = SearchConfig()
        out = web.search("测试", cfg, max_results=2)
        check("默认后端走 duckduckgo", "html.duckduckgo.com" in calls[0][0])
        check("搜索结果含标题与链接", "第一条 & 结果" in out and "https://example.com/one" in out)
        check("搜索结果带来源标注", "duckduckgo" in out)

        tavily = web.search("测试", replace(cfg, provider="tavily", api_key="k"))
        check("tavily 结果解析", "T" in tavily and "https://t" in tavily)
        bocha = web.search("测试", replace(cfg, provider="bocha", api_key="k"))
        check("bocha 结果解析", "B" in bocha and "https://b" in bocha)
        serper = web.search("测试", replace(cfg, provider="serper", api_key="k"))
        check("serper 结果解析", "G" in serper and "https://g" in serper)

        check("搜索 POST 方法", any(m == "POST" for _u, m in calls if "tavily" in _u))

        no_key = web.search("测试", replace(cfg, provider="tavily"))
        check("缺 Key 时给出可读错误", no_key.startswith("[错误]") and "API Key" in no_key, no_key[:60])

        unknown = web.search("测试", replace(cfg, provider="nope"))
        check("未知后端被拦住", unknown.startswith("[错误]") and "不认识的搜索后端" in unknown)

        disabled = web.search("测试", replace(cfg, enabled=False))
        check("关闭搜索时拒绝执行", disabled.startswith("[错误]"))

        fetch_out = web.fetch("example.org")
        check("fetch_url 提取正文", "网页正文内容" in fetch_out and "抓取标题" in fetch_out)
        check("fetch_url 自动补 https", calls[-1][0].startswith("https://example.org"))

        # transient failures are retried once, so a throttled backend still answers
        web.RETRY_DELAY = 0
        attempts: list[int] = []

        def flaky(url, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise web.WebError("HTTP 429 Too Many Requests")
            return 200, DDG_FIXTURE, "text/html"

        web.http_get = flaky
        retried = web.search("测试", cfg, max_results=2)
        check("限流后重试成功", "https://example.com/one" in retried and len(attempts) == 2,
              f"{len(attempts)} 次请求")

        def boom(url, **_kwargs):
            raise web.WebError("连接被拒绝")

        web.http_get = boom
        dead = web.search("测试", cfg)
        check("网络异常转成可读文本", dead.startswith("[错误]"))
        check("错误里给出换后端的提示", "/search backend" in dead, dead.splitlines()[-1][:70])
        check("抓取异常转成可读文本", web.fetch("https://a.com").startswith("[错误]"))
    finally:
        web.http_get = original
        web.RETRY_DELAY = 0.9


def test_web_registry() -> None:
    from jarvis.tools import build_registry

    registry = build_registry(SecurityConfig(), ".", SearchConfig())
    names = [t.name for t in registry.tools]
    check("web_search 已注册", "web_search" in names, ",".join(names))
    check("fetch_url 已注册", "fetch_url" in names)
    check("web_search 免确认", not registry.needs_confirmation(registry.get("web_search")))  # type: ignore[arg-type]
    check("fetch_url 免确认", not registry.needs_confirmation(registry.get("fetch_url")))  # type: ignore[arg-type]
    check("run_shell 仍需确认", registry.needs_confirmation(registry.get("run_shell")))  # type: ignore[arg-type]


# ------------------------------------------------------------------ tui parsing
def test_split_flags() -> None:
    positional, flags = split_flags("gpt --provider openai --model gpt-4o-mini --default")
    check("位置参数解析", positional == ["gpt"], str(positional))
    check("带值 flag 解析", flags["provider"] == "openai" and flags["model"] == "gpt-4o-mini")
    check("无值 flag 解析", flags["default"] == "")
    positional, flags = split_flags('"带 空格" name --label "我的 模型"')
    check("引号参数解析", positional == ["带 空格", "name"], str(positional))
    check("引号 flag 值解析", flags["label"] == "我的 模型")
    check("空输入", split_flags("") == ([], {}))


# ------------------------------------------------------- handwritten providers
def test_handwritten_providers(tmp: Path) -> None:
    """A [providers.*] block written by hand must parse, inherit, and survive a rewrite.

    This is the path a user takes when they edit config.toml directly instead of
    using /provider add, so the guarantees here are what the README promises.
    """

    text = """
default_model = "myapi"

[providers.myapi]
label = "我的中转"
base_url = "https://api.example.com/v1"
api_key_env = "MYAPI_KEY"
models = ["gpt-4o-mini"]
note = "手写测试"

[models.myapi]
provider = "myapi"
model = "gpt-4o-mini"

[models.quick]
base_url = "https://quick.example.com/v1"
model = "gpt-4o-mini"
api_key = "sk-quick"

[providers.openai]
base_url = "https://my-proxy.example.com/v1"

[providers.nolabel]
base_url = "https://nl.example.com/v1"
"""
    path = _write(tmp / "hand.toml", text)
    cfg = load_config(path)

    handed = cfg.catalog.get("myapi")
    check("手写 provider 被解析", handed is not None)
    check("手写 provider 字段完整",
          handed is not None and handed.base_url == "https://api.example.com/v1")
    check("手写 provider 的 label 保留", handed is not None and handed.label == "我的中转")
    check("手写 provider 的 note 保留", handed is not None and handed.note == "手写测试")
    check("手写 provider 的 models 保留", handed is not None and handed.models == ("gpt-4o-mini",))

    inherits = cfg.model("myapi")
    check("模型继承手写 provider 的端点", inherits.base_url == "https://api.example.com/v1")
    check("模型继承手写 provider 的 key_env", inherits.api_key_env == "MYAPI_KEY")
    check("模型继承手写 provider 的 label", inherits.label == "我的中转")

    quick = cfg.model("quick")
    check("不写 provider 也能配模型",
          quick.base_url == "https://quick.example.com/v1" and quick.api_key == "sk-quick")

    overridden = cfg.catalog.require("openai")
    check("覆盖内置 provider 的端点", overridden.base_url == "https://my-proxy.example.com/v1")
    check("覆盖内置时仍继承内置 label", overridden.label == PROVIDERS["openai"].label)
    check("覆盖内置时仍继承内置 key_env", overridden.api_key_env == PROVIDERS["openai"].api_key_env)
    check("未写 label 的自定义 provider 退回名字",
          cfg.catalog.require("nolabel").label == "nolabel")

    cfg.save(path)
    again = load_config(path)
    check("重写后自定义 provider 仍在", again.catalog.require("myapi").note == "手写测试")
    check("重写后覆盖内置仍生效",
          again.catalog.require("openai").base_url == "https://my-proxy.example.com/v1")
    check("重写后内置 label 不回退",
          again.catalog.require("openai").label == PROVIDERS["openai"].label)
    check("重写后最简写法的模型仍可用", again.model("quick").api_key == "sk-quick")
    check("重写后默认模型保留", again.default_model == "myapi")


# ------------------------------------------------------------------------- tray
def test_tray() -> None:
    from jarvis.daemon import tray

    if not tray.IS_WINDOWS:
        print("[warn] 非 Windows，跳过托盘结构检查")
        return
    check("托盘结构体大小符合 V4", tray.ctypes.sizeof(tray.NOTIFYICONDATA) == 976,
          str(tray.ctypes.sizeof(tray.NOTIFYICONDATA)))
    check("V2 尺寸 = guidItem 偏移", tray.NOTIFYICONDATA.guidItem.offset == 952)
    check("V3 尺寸 = hBalloonIcon 偏移", tray.NOTIFYICONDATA.hBalloonIcon.offset == 968)
    check("托盘图标文件存在", tray.ICON_PATH.exists(), str(tray.ICON_PATH))
    check("托盘图标是 ICO", tray.ICON_PATH.read_bytes()[:4] == b"\x00\x00\x01\x00")
    check(
        "菜单项各不相同",
        len({tray.MENU_OPEN, tray.MENU_TASKS, tray.MENU_STATUS, tray.MENU_QUIT}) == 4,
    )
    check("回调消息在 WM_APP 区间", 0x8000 <= tray.WM_TRAY < 0xC000)
    check("未启动时状态可读", "未启用" in tray.describe_state(None))


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        test_providers()
        test_tomlwrite()
        test_config_providers(tmp)
        test_handwritten_providers(tmp)
        test_hot_switch()
        test_failover()
        test_fallback_config()
        test_web_parsing()
        test_web_backends()
        test_web_registry()
        test_split_flags()
        test_tray()

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nPHASE3 TEST FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nPHASE3 TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
