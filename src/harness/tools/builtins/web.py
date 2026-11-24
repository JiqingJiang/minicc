"""Built-in web_search and web_fetch tools with SSRF protection and readable HTML extraction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import socket
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, SplitResult
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.harness.tools.base import BaseTool, ToolFailure

_MAX_FETCH_BYTES = 2 * 1024 * 1024
_DEFAULT_TIMEOUT = 15
_MAX_REDIRECTS = 5
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MiniCC/1.0"
)
_TEXT_MIMES = (
    "text/",
    "application/json",
    "application/ld+json",
    "application/problem+json",
    "application/xml",
    "application/xhtml+xml",
    "application/atom+xml",
    "application/rss+xml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
)
_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}


@dataclass(frozen=True)
class FetchedResponse:
    url: str
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    source: str = "web"


Fetcher = Callable[[str], FetchedResponse]
Searcher = Callable[[str, int, Callable[[], None]], tuple[SearchHit, ...]]


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    # Explicitly block CGNAT 100.64.0.0/10 and AWS/cloud metadata 169.254.0.0/16
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("169.254.0.0/16"):
            return False
    return True


def validate_url_syntax_and_host(raw_url: str) -> tuple[SplitResult, str, int]:
    """Validate URL syntax, HTTP(S) scheme, no embedded credentials, and non-private hostname/literal IP."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ToolFailure("INVALID_ARGUMENTS", "请提供非空的 URL。")
    cleaned = raw_url.strip()
    if any(ord(ch) < 32 or ch in {"\x7f", " "} for ch in cleaned):
        raise ToolFailure("BLOCKED_URL", "URL 包含无效的控制字符或空白。")
    try:
        parsed = urlsplit(cleaned)
    except ValueError as error:
        raise ToolFailure("BLOCKED_URL", "URL 格式无效。") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ToolFailure("BLOCKED_URL", "仅支持 http:// 或 https:// 协议的公开网页。")
    if parsed.username or parsed.password:
        raise ToolFailure("BLOCKED_URL", "拒绝包含用户名或密码凭证的 URL。")
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ToolFailure("BLOCKED_URL", "URL 缺少有效的主机名。")
    if hostname in _BLOCKED_HOSTS or hostname.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")):
        raise ToolFailure("BLOCKED_URL", f"安全边界拦截：禁止访问本地或内网主机 {hostname}。")

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise ToolFailure("BLOCKED_URL", "URL 端口号无效。") from error

    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip and not _is_public_ip(hostname):
        raise ToolFailure("BLOCKED_URL", f"安全边界拦截：禁止访问私有或保留 IP 地址 {hostname}。")

    return parsed, hostname, port


def validate_public_url(raw_url: str) -> SplitResult:
    """Validate that a URL is HTTP(S), carries no credentials, and resolves only to global public IPs."""
    parsed, hostname, port = validate_url_syntax_and_host(raw_url)
    try:
        ipaddress.ip_address(hostname)
        return parsed
    except ValueError:
        pass

    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ToolFailure("WEB_FETCH_FAILED", f"无法解析域名 {hostname}：{error}") from error

    if not addr_infos:
        raise ToolFailure("WEB_FETCH_FAILED", f"域名 {hostname} 没有返回可用地址。")

    for family, _, _, _, sockaddr in addr_infos:
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr:
            resolved_ip = str(sockaddr[0]).split("%", 1)[0]
            if not _is_public_ip(resolved_ip):
                raise ToolFailure(
                    "BLOCKED_URL",
                    f"安全边界拦截：域名 {hostname} 解析到了非公网地址 {resolved_ip}。",
                )
    return parsed


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > _MAX_REDIRECTS:
            raise HTTPError(req.full_url, code, "重定向次数超过上限（5 次）。", headers, fp)
        resolved = urljoin(req.full_url, newurl)
        validate_public_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def default_fetch_url(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    accept: str = "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5",
    extra_headers: dict[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    checkpoint: Callable[[], None] = lambda: None,
) -> FetchedResponse:
    checkpoint()
    validate_public_url(url)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(_SafeRedirectHandler())
    deadline = monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = getattr(response, "url", None) or response.geturl() or url
            validate_public_url(final_url)
            status = getattr(response, "status", None) or response.getcode() or 200
            content_type = response.headers.get("Content-Type", "text/plain; charset=utf-8")
            chunks: list[bytes] = []
            total = 0
            while total <= _MAX_FETCH_BYTES:
                checkpoint()
                if monotonic() >= deadline:
                    raise TimeoutError("stream read deadline exceeded")
                chunk = response.read(65_536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
    except ToolFailure:
        raise
    except HTTPError as error:
        raise ToolFailure("WEB_FETCH_FAILED", f"HTTP {error.code} 错误：{error.reason}") from error
    except (TimeoutError, socket.timeout) as error:
        raise ToolFailure("TOOL_TIMEOUT", f"请求 {url} 超时（>{timeout}s）。") from error
    except URLError as error:
        if isinstance(error.reason, ToolFailure):
            raise error.reason from error
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise ToolFailure("TOOL_TIMEOUT", f"请求 {url} 超时（>{timeout}s）。") from error
        raise ToolFailure("WEB_FETCH_FAILED", f"网络请求失败：{error.reason}") from error
    except OSError as error:
        raise ToolFailure("WEB_FETCH_FAILED", f"网络读取错误：{error}") from error

    if len(raw) > _MAX_FETCH_BYTES:
        raw = raw[:_MAX_FETCH_BYTES]
    return FetchedResponse(url=final_url, status=int(status), content_type=content_type, body=raw)


def _decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    candidates = []
    if charset_match:
        candidates.append(charset_match.group(1).strip("\"'"))
    candidates.extend(["utf-8", "gb18030", "latin-1"])
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


class _ReadableHTMLParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}
    _BLOCK_TAGS = {
        "p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
        "blockquote", "pre", "table", "tr", "ul", "ol", "dl", "dt", "dd", "hr", "br",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "figcaption", "details", "summary",
    }
    _HEADING_LEVELS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.in_title = False
        self.skip_depth = 0
        self.in_pre = 0
        self.chunks: list[str] = []
        self._link_stack: list[tuple[str, int]] = []

    def _newline(self, count: int = 1) -> None:
        if not self.chunks:
            return
        tail = "".join(self.chunks[-4:])
        existing = len(tail) - len(tail.rstrip("\n"))
        if existing < count:
            self.chunks.append("\n" * (count - existing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth > 0:
            return
        if lower == "title":
            self.in_title = True
            return
        if lower in self._HEADING_LEVELS:
            self._newline(2)
            self.chunks.append(f"{self._HEADING_LEVELS[lower]} ")
        elif lower == "li":
            self._newline(1)
            self.chunks.append("- ")
        elif lower == "br":
            self._newline(1)
        elif lower == "hr":
            self._newline(2)
            self.chunks.append("---\n")
        elif lower == "pre":
            self._newline(2)
            self.chunks.append("```\n")
            self.in_pre += 1
        elif lower in {"td", "th"}:
            if self.chunks and not self.chunks[-1].endswith(("\n", " | ")):
                self.chunks.append(" | ")
        elif lower in self._BLOCK_TAGS:
            self._newline(2)
        elif lower == "a":
            href = dict(attrs).get("href") or ""
            href = href.strip()
            if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                if self.base_url:
                    href = urljoin(self.base_url, href)
                self._link_stack.append((href, len(self.chunks)))
            else:
                self._link_stack.append(("", len(self.chunks)))

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth > 0:
            return
        if lower == "title":
            self.in_title = False
            return
        if lower == "pre":
            if self.in_pre > 0:
                self.in_pre -= 1
            self._newline(1)
            self.chunks.append("```\n")
        elif lower == "a":
            if self._link_stack:
                href, start_idx = self._link_stack.pop()
                anchor_text = "".join(self.chunks[start_idx:]).strip()
                if href and anchor_text and href not in anchor_text:
                    self.chunks.append(f" ({href})")
        elif lower in self._HEADING_LEVELS or lower in self._BLOCK_TAGS:
            self._newline(2 if lower not in {"li", "tr"} else 1)

    def handle_data(self, data: str) -> None:
        if self.skip_depth > 0:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.in_pre > 0:
            self.chunks.append(data)
            return
        collapsed = re.sub(r"[ \t\r\f\v]+", " ", data.replace("\n", " "))
        if not collapsed.strip():
            if self.chunks and not self.chunks[-1].endswith((" ", "\n", "- ")):
                self.chunks.append(" ")
            return
        if self.chunks and self.chunks[-1].endswith(("\n", "- ", "# ")):
            collapsed = collapsed.lstrip()
        self.chunks.append(collapsed)


def extract_readable_html(html_text: str, *, base_url: str = "") -> tuple[str, str]:
    """Convert raw HTML into a clean (title, readable_markdown_text) pair."""
    parser = _ReadableHTMLParser(base_url=base_url)
    parser.feed(html_text)
    parser.close()
    title = " ".join("".join(parser.title_parts).split()).strip()
    raw_text = "".join(parser.chunks)
    lines = [line.rstrip() for line in raw_text.splitlines()]
    cleaned_lines: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1 and cleaned_lines:
                cleaned_lines.append("")
        else:
            blank_run = 0
            cleaned_lines.append(line)
    return title, "\n".join(cleaned_lines).strip()


def _strip_tags(fragment: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(unescape(no_tags).split()).strip()


def _unwrap_ddg_url(raw_href: str) -> str:
    href = unescape(raw_href).strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlsplit(href)
    if parsed.hostname and "duckduckgo.com" in parsed.hostname and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return qs["uddg"][0].strip()
    return href


def parse_duckduckgo_html(html_text: str, *, limit: int = 5) -> tuple[SearchHit, ...]:
    """Extract search results from DuckDuckGo HTML/Lite endpoint."""
    hits: list[SearchHit] = []
    seen_urls: set[str] = set()
    # Pattern 1: standard html.duckduckgo.com result blocks
    link_pattern = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|'
        r'<div[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    snippets = [
        _strip_tags(m.group(1) or m.group(2) or "")
        for m in snippet_pattern.finditer(html_text)
    ]
    for idx, match in enumerate(link_pattern.finditer(html_text)):
        if len(hits) >= limit:
            break
        raw_url, raw_title = match.group(1), match.group(2)
        if "ad_provider=" in raw_url or "duckduckgo.com/y.js" in raw_url:
            continue
        url = _unwrap_ddg_url(raw_url)
        title = _strip_tags(raw_title)
        if not title or not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        snippet = snippets[idx] if idx < len(snippets) else ""
        hits.append(SearchHit(title=title, url=url, snippet=snippet, source="duckduckgo"))
    return tuple(hits)


def parse_bing_html(html_text: str, *, limit: int = 5) -> tuple[SearchHit, ...]:
    """Extract search results from Bing HTML search page as a fallback."""
    hits: list[SearchHit] = []
    seen_urls: set[str] = set()
    block_pattern = re.compile(
        r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
        re.IGNORECASE | re.DOTALL,
    )
    title_link = re.compile(r"<h2[^>]*>\s*<a[^>]*href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
    caption_p = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
    for block_match in block_pattern.finditer(html_text):
        if len(hits) >= limit:
            break
        block = block_match.group(1)
        t_match = title_link.search(block)
        if not t_match:
            continue
        url = unescape(t_match.group(1)).strip()
        title = _strip_tags(t_match.group(2))
        if not title or not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        p_match = caption_p.search(block)
        snippet = _strip_tags(p_match.group(1)) if p_match else ""
        hits.append(SearchHit(title=title, url=url, snippet=snippet, source="bing"))
    return tuple(hits)


def _search_via_tavily(query: str, limit: int, api_key: str) -> tuple[SearchHit, ...]:
    payload = json.dumps(
        {"api_key": api_key, "query": query, "max_results": limit, "include_answer": False}
    ).encode("utf-8")
    resp = default_fetch_url(
        "https://api.tavily.com/search",
        accept="application/json",
        extra_headers={"Content-Type": "application/json"},
        method="POST",
        data=payload,
    )
    data = json.loads(_decode_body(resp.body, resp.content_type))
    results = data.get("results", []) if isinstance(data, dict) else []
    hits: list[SearchHit] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = " ".join(str(item.get("content") or "").split())
        if title and url.startswith(("http://", "https://")):
            hits.append(SearchHit(title=title, url=url, snippet=snippet[:320], source="tavily"))
    return tuple(hits)


def _search_via_brave(query: str, limit: int, api_key: str) -> tuple[SearchHit, ...]:
    url = f"https://api.search.brave.com/res/v1/web/search?{urlencode({'q': query, 'count': limit})}"
    resp = default_fetch_url(
        url,
        accept="application/json",
        extra_headers={"X-Subscription-Token": api_key},
    )
    data = json.loads(_decode_body(resp.body, resp.content_type))
    web_block = data.get("web", {}) if isinstance(data, dict) else {}
    results = web_block.get("results", []) if isinstance(web_block, dict) else []
    hits: list[SearchHit] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        item_url = str(item.get("url") or "").strip()
        snippet = _strip_tags(str(item.get("description") or ""))
        if title and item_url.startswith(("http://", "https://")):
            hits.append(SearchHit(title=title, url=item_url, snippet=snippet[:320], source="brave"))
    return tuple(hits)


def default_web_search(query: str, limit: int, checkpoint: Callable[[], None]) -> tuple[SearchHit, ...]:
    """Search the web using optional configured API keys (Tavily/Brave) or zero-config DuckDuckGo/Bing HTML."""
    checkpoint()
    errors: list[str] = []

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            hits = _search_via_tavily(query, limit, tavily_key)
            if hits:
                return hits
        except Exception as error:
            errors.append(f"Tavily: {error}")

    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        try:
            checkpoint()
            hits = _search_via_brave(query, limit, brave_key)
            if hits:
                return hits
        except Exception as error:
            errors.append(f"Brave: {error}")

    # Zero-config DuckDuckGo HTML endpoint
    try:
        checkpoint()
        ddg_url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
        resp = default_fetch_url(ddg_url, timeout=12)
        html_text = _decode_body(resp.body, resp.content_type)
        hits = parse_duckduckgo_html(html_text, limit=limit)
        if hits:
            return hits
    except Exception as error:
        errors.append(f"DuckDuckGo: {error}")

    # Zero-config Bing HTML fallback (also works smoothly in mainland China networks)
    for bing_host in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            checkpoint()
            bing_url = f"{bing_host}?{urlencode({'q': query, 'count': limit})}"
            resp = default_fetch_url(bing_url, timeout=12)
            html_text = _decode_body(resp.body, resp.content_type)
            hits = parse_bing_html(html_text, limit=limit)
            if hits:
                return hits
        except Exception as error:
            errors.append(f"Bing({bing_host}): {error}")

    if errors:
        raise ToolFailure("WEB_SEARCH_FAILED", "搜索引擎请求失败：" + "；".join(errors[:2]))
    return ()


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web for up-to-date information, documentation, and links."
    read_only = True
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 400},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, *, searcher: Searcher | None = None) -> None:
        self._searcher = searcher or default_web_search

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        query = arguments["query"].strip()
        if len(query) < 2:
            raise ToolFailure("INVALID_ARGUMENTS", "搜索关键词至少需要 2 个字符。")
        limit = int(arguments.get("limit", 5))
        hits = self._searcher(query, limit, checkpoint)
        checkpoint()
        if not hits:
            return f"未找到与 {query!r} 相关的网页搜索结果。"
        lines = [f"网页搜索结果（关键词：{query}，共 {len(hits)} 条）："]
        for idx, hit in enumerate(hits[:limit], start=1):
            lines.append(f"\n{idx}. {hit.title}\n   URL: {hit.url}")
            if hit.snippet:
                lines.append(f"   摘要: {hit.snippet}")
        lines.append("\n提示：如需阅读某个链接的完整正文，请调用 web_fetch(url=...)。")
        return "\n".join(lines)


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "Fetch a public HTTP/HTTPS URL and extract readable text, markdown, or JSON."
    read_only = True
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 2000},
            "start_char": {"type": "integer", "minimum": 0, "default": 0},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 7500, "default": 6000},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, *, fetcher: Fetcher | None = None) -> None:
        self._fetcher = fetcher

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        url = arguments["url"].strip()
        start_char = int(arguments.get("start_char", 0))
        max_chars = int(arguments.get("max_chars", 6000))
        validate_url_syntax_and_host(url)
        response = self._fetcher(url) if self._fetcher is not None else default_fetch_url(url, checkpoint=checkpoint)
        checkpoint()

        content_type = (response.content_type or "text/plain").strip()
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime and not any(mime.startswith(prefix) for prefix in _TEXT_MIMES):
            raise ToolFailure(
                "UNSUPPORTED_CONTENT_TYPE",
                f"不支持读取非文本内容类型：{mime}（仅支持 HTML、JSON、XML、Markdown 与纯文本）。",
            )

        raw_text = _decode_body(response.body, content_type)
        title = ""
        if "html" in mime or raw_text.lstrip()[:64].lower().startswith(("<!doctype html", "<html")):
            title, readable = extract_readable_html(raw_text, base_url=response.url)
        elif "json" in mime:
            try:
                parsed_json = json.loads(raw_text)
                readable = json.dumps(parsed_json, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                readable = raw_text.strip()
        else:
            readable = raw_text.strip()

        total_chars = len(readable)
        if total_chars == 0:
            header = f"URL: {response.url}" + (f"\n标题：{title}" if title else "")
            return f"{header}\n（页面未提取到可读文本内容）"

        if start_char >= total_chars:
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                f"start_char ({start_char}) 超出页面总字符数 ({total_chars})。",
            )

        end_char = min(total_chars, start_char + max_chars)
        excerpt = readable[start_char:end_char]
        header_lines = [f"URL: {response.url}"]
        if title:
            header_lines.append(f"标题：{title}")
        header_lines.append(f"范围：第 {start_char}-{end_char} 字符（共 {total_chars} 字符）")
        if end_char < total_chars:
            header_lines.append(f"提示：尚有未读内容，可调用 web_fetch(url={response.url!r}, start_char={end_char}) 继续读取。")
        return "\n".join(header_lines) + "\n\n" + excerpt
