"""Web access tools: search the internet and read a page as text.

Deliberately dependency-free: ``urllib`` for HTTP (it honours the system proxy
env vars, which matters on machines behind a corporate/local proxy) and
``html.parser`` for text extraction.

Backends
--------
``duckduckgo``  HTML endpoint, no API key - the default.
``bocha`` / ``tavily`` / ``serper``  API backends, need a key.
``searxng``  your own instance.

Configure with the ``[search]`` section of ``config.toml`` or the TUI's
``/search`` command.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any

from ..config import SearchConfig
from ..providers import SEARCH_PROVIDERS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Free HTML backends throttle bursts; wait this long before the single retry.
RETRY_DELAY = 0.9

BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
    "ul", "ol", "dl", "dt", "dd", "hr", "form",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe", "nav", "aside"}


class WebError(RuntimeError):
    """Network or backend failure, phrased for the model to read."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def line(self, index: int) -> str:
        parts = [f"{index}. {self.title}", f"   {self.url}"]
        if self.snippet:
            parts.append(f"   {self.snippet}")
        return "\n".join(parts)


# ----------------------------------------------------------------------- http
def _decode(raw: bytes, content_type: str, sample: bytes = b"") -> str:
    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        match = re.search(rb'charset=["\']?([\w\-]+)', sample[:2048] or raw[:2048], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8", "gb18030", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, str, str]:
    """GET/POST a URL, returning ``(status, body_text, content_type)``."""

    merged = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            elif "deflate" in encoding:
                try:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except zlib.error:
                    pass
            content_type = response.headers.get("Content-Type", "")
            return response.status, _decode(raw, content_type), content_type
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise WebError(f"HTTP {exc.code} {exc.reason}（{url}）{(' · ' + body) if body else ''}") from exc
    except urllib.error.URLError as exc:
        raise WebError(f"请求失败（{url}）：{exc.reason}") from exc
    except TimeoutError as exc:
        raise WebError(f"请求超时（{url}）") from exc


def http_post_json(
    url: str, payload: dict[str, Any], *, timeout: float, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", **(headers or {})}
    _status, text, _ctype = http_get(
        url, timeout=timeout, headers=merged, data=body, method="POST"
    )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WebError(f"搜索结果不是合法 JSON：{text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise WebError("搜索结果格式不符合预期")
    return parsed


# ----------------------------------------------------------------- extraction
class _TextExtractor(HTMLParser):
    """Turn HTML into readable-ish plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "title":
            self._in_title = True
        elif tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
        if tag == "title":
            self._in_title = False
        elif tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title += data
            return
        if self._skip_depth:
            return
        if data.strip():
            self.parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(self._title.split())

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        kept: list[str] = []
        for line in lines:
            if not line:
                if kept and kept[-1] != "":
                    kept.append("")
                continue
            kept.append(line)
        return "\n".join(kept).strip()


def strip_html(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not break the tool
        pass
    return parser.title, parser.text()


def _clean(text: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", text or "")).split())


# -------------------------------------------------------------------- backends
_DDG_LINK_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _unwrap_ddg(url: str) -> str:
    if "duckduckgo.com/l/" not in url and not url.startswith("/l/"):
        return url
    query = urllib.parse.urlparse(url).query or url.split("?", 1)[-1]
    params = urllib.parse.parse_qs(query)
    target = params.get("uddg", [""])[0]
    return urllib.parse.unquote(target) if target else url


def _parse_duckduckgo_html(html: str, limit: int) -> list[SearchResult]:
    """Pull results out of the DuckDuckGo HTML endpoint (no JS, stable markup)."""

    results: list[SearchResult] = []
    blocks = html.split('class="result ')[1:]
    for block in blocks:
        link = _DDG_LINK_RE.search(block)
        if not link:
            continue
        title = _clean(link.group(2))
        href = unescape(link.group(1))
        if href.startswith("//"):
            href = "https:" + href
        snippet = ""
        found = _DDG_SNIPPET_RE.search(block)
        if found:
            snippet = _clean(found.group(1))
        if title and href:
            results.append(SearchResult(title=title, url=_unwrap_ddg(href), snippet=snippet))
        if len(results) >= limit:
            break
    return results


def _search_duckduckgo(query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    endpoint = cfg.base_url or SEARCH_PROVIDERS["duckduckgo"].base_url
    url = f"{endpoint}?q={urllib.parse.quote_plus(query)}"
    _status, html, _ctype = http_get(url, timeout=cfg.timeout)
    return _parse_duckduckgo_html(html, limit)


def _search_searxng(query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    endpoint = cfg.base_url or SEARCH_PROVIDERS["searxng"].base_url
    url = f"{endpoint}?q={urllib.parse.quote_plus(query)}&format=json"
    _status, text, _ctype = http_get(url, timeout=cfg.timeout)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WebError(
            "SearXNG 没返回 JSON：确认实例开启了 format=json（settings.yml 里的 search.formats）。"
        ) from exc
    results = []
    for item in (payload.get("results") or [])[:limit]:
        results.append(
            SearchResult(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("url", "")).strip(),
                snippet=_clean(str(item.get("content", ""))),
            )
        )
    return results


def _search_tavily(query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    key = cfg.resolve_api_key()
    if not key:
        raise WebError(f"Tavily 需要 API Key：配置 search.api_key 或环境变量 {cfg.api_key_env or 'TAVILY_API_KEY'}")
    payload = http_post_json(
        cfg.base_url or SEARCH_PROVIDERS["tavily"].base_url,
        {"query": query, "max_results": limit, "search_depth": "basic"},
        timeout=cfg.timeout,
        headers={"Authorization": f"Bearer {key}"},
    )
    return [
        SearchResult(
            title=str(item.get("title", "")).strip(),
            url=str(item.get("url", "")).strip(),
            snippet=_clean(str(item.get("content", ""))),
        )
        for item in (payload.get("results") or [])[:limit]
    ]


def _search_bocha(query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    key = cfg.resolve_api_key()
    if not key:
        raise WebError(f"博查需要 API Key：配置 search.api_key 或环境变量 {cfg.api_key_env or 'BOCHA_API_KEY'}")
    payload = http_post_json(
        cfg.base_url or SEARCH_PROVIDERS["bocha"].base_url,
        {"query": query, "count": limit, "summary": True},
        timeout=cfg.timeout,
        headers={"Authorization": f"Bearer {key}"},
    )
    data = payload.get("data") or {}
    pages = ((data.get("webPages") or {}).get("value")) or []
    return [
        SearchResult(
            title=str(item.get("name", "")).strip(),
            url=str(item.get("url", "")).strip(),
            snippet=_clean(str(item.get("summary") or item.get("snippet") or "")),
        )
        for item in pages[:limit]
    ]


def _search_serper(query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    key = cfg.resolve_api_key()
    if not key:
        raise WebError(f"Serper 需要 API Key：配置 search.api_key 或环境变量 {cfg.api_key_env or 'SERPER_API_KEY'}")
    payload = http_post_json(
        cfg.base_url or SEARCH_PROVIDERS["serper"].base_url,
        {"q": query, "num": limit},
        timeout=cfg.timeout,
        headers={"X-API-KEY": key},
    )
    return [
        SearchResult(
            title=str(item.get("title", "")).strip(),
            url=str(item.get("link", "")).strip(),
            snippet=_clean(str(item.get("snippet", ""))),
        )
        for item in (payload.get("organic") or [])[:limit]
    ]


BACKENDS = {
    "duckduckgo": _search_duckduckgo,
    "searxng": _search_searxng,
    "tavily": _search_tavily,
    "bocha": _search_bocha,
    "serper": _search_serper,
}


# ------------------------------------------------------------------------ tui
def search(query: str, cfg: SearchConfig, *, max_results: int | None = None,
           provider: str | None = None) -> str:
    """Human/model readable search results."""

    query = query.strip()
    if not query:
        return "[错误] 搜索关键词不能为空。"
    if not cfg.enabled:
        return "[错误] 网络搜索已被配置关闭（config.toml 的 search.enabled = false）。"

    name = (provider or cfg.provider or "duckduckgo").lower()
    backend = BACKENDS.get(name)
    if backend is None:
        return f"[错误] 不认识的搜索后端 '{name}'。可用：{', '.join(sorted(BACKENDS))}"

    limit = max(1, min(int(max_results or cfg.max_results), 20))
    try:
        results = _run_backend(backend, query, limit, cfg)
    except WebError as exc:
        return (
            f"[错误] {exc}\n"
            f"（后端 {name} 不可用；可以换一个：/search backend "
            f"{'tavily' if name == 'duckduckgo' else 'duckduckgo'}，或稍后重试。）"
        )
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 搜索失败：{type(exc).__name__}: {exc}"

    if not results:
        return (
            f"搜索「{query}」（{name}）没有返回结果。"
            "免费后端偶尔会限流或对某些问题无结果，可以换个说法、换后端（/search backend），"
            "或用 fetch_url 直接打开已知网址。"
        )

    header = f"搜索「{query}」（{name}，{len(results)} 条）："
    return "\n".join([header, ""] + [r.line(i) for i, r in enumerate(results, start=1)])


def _run_backend(backend: Any, query: str, limit: int, cfg: SearchConfig) -> list[SearchResult]:
    """Call a backend, retrying once on a transient failure.

    The key-free HTML backends throttle bursts, and the agent often fires two
    searches back to back. One short retry turns most of those into successes.
    """

    try:
        return backend(query, limit, cfg)
    except WebError:
        time.sleep(RETRY_DELAY)
        return backend(query, limit, cfg)


def fetch(url: str, *, max_chars: int = 8000, timeout: float = 25.0) -> str:
    """Download a URL and return it as readable text."""

    target = url.strip()
    if not target:
        return "[错误] 网址不能为空。"
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    try:
        _status, body, content_type = http_get(target, timeout=timeout)
    except WebError as exc:
        return f"[错误] {exc}"

    if "json" in content_type:
        try:
            pretty = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            return _truncate(f"{target}\n\n{pretty}", max_chars)
        except json.JSONDecodeError:
            pass

    if "html" in content_type or "<html" in body[:600].lower():
        title, text = strip_html(body)
        head = f"{title}\n{target}" if title else target
        return _truncate(f"{head}\n\n{text}", max_chars)
    return _truncate(f"{target}\n\n{body.strip()}", max_chars)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（已截断，原文共 {len(text)} 字符）"


def describe(cfg: SearchConfig) -> str:
    """One-line summary of the current backend, for /search and the sidebar."""

    preset = SEARCH_PROVIDERS.get(cfg.provider)
    label = preset.label if preset else cfg.provider
    key_state = ""
    if preset and preset.needs_key:
        key_state = "（Key 已配置）" if cfg.resolve_api_key() else "（⚠ 缺 API Key）"
    state = "" if cfg.enabled else "（已关闭）"
    return f"{cfg.provider} · {label}{key_state}{state} · 每次 {cfg.max_results} 条"
