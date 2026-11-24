"""Tests for built-in web_fetch and web_search tools, including SSRF protections and HTML parsing."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from src.harness.agent_loop.run_turn import ModelReply
from src.harness.tools.base import ToolCall, ToolFailure, schema_size
from src.harness.tools.builtins import create_builtin_registry
from src.harness.tools.builtins.web import (
    FetchedResponse,
    SearchHit,
    WebFetchTool,
    WebSearchTool,
    extract_readable_html,
    validate_public_url,
)
from src.harness.tools.executor import ToolExecutor
from src.runtime.session.chat_session import ChatSession


class WebToolsTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_builtin_registry_includes_web_search_and_web_fetch_within_schema_budget(self) -> None:
        registry = create_builtin_registry(self.root)
        names = [tool.name for tool in registry.all()]
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)

        plan_names = [tool.name for tool in registry.all(mode="plan")]
        self.assertIn("web_search", plan_names)
        self.assertIn("web_fetch", plan_names)

        visible = ToolExecutor().offered_tools(registry.all())
        self.assertEqual(visible, registry.all())
        self.assertLessEqual(schema_size(visible), 3_600)

    def test_ssrf_validation_blocks_non_http_credentials_and_private_addresses(self) -> None:
        for forbidden_url in (
            "file:///etc/passwd",
            "ftp://example.com/data.txt",
            "javascript:alert(1)",
            "https://user:secret@example.com/private",
            "http://localhost:8080/admin",
            "http://127.0.0.1:9000/status",
            "http://[::1]:8000/health",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.10/service",
            "http://0.0.0.0:5000/",
        ):
            with self.subTest(url=forbidden_url):
                with self.assertRaises(ToolFailure) as ctx:
                    validate_public_url(forbidden_url)
                self.assertEqual(ctx.exception.code, "BLOCKED_URL")

    def test_ssrf_validation_blocks_domain_resolving_to_private_ip(self) -> None:
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=fake_addrinfo):
            with self.assertRaises(ToolFailure) as ctx:
                validate_public_url("https://rebind.example.com/secret")
            self.assertEqual(ctx.exception.code, "BLOCKED_URL")

        public_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=public_addrinfo):
            parsed = validate_public_url("https://example.com/docs?page=1")
            self.assertEqual(parsed.hostname, "example.com")

    def test_extract_readable_html_strips_scripts_styles_and_formats_links_and_headings(self) -> None:
        html_doc = """
        <!DOCTYPE html>
        <html>
          <head>
            <title>MiniCC Documentation</title>
            <style>body { color: red; }</style>
            <script>console.log("secret token");</script>
          </head>
          <body>
            <nav>Home | About</nav>
            <main>
              <h1>Getting Started</h1>
              <p>Welcome to <strong>MiniCC</strong>! Read the <a href="/guide">Quick Guide</a>.</p>
              <ul>
                <li>First item</li>
                <li>Second item</li>
              </ul>
              <pre><code>pip install minicc</code></pre>
            </main>
          </body>
        </html>
        """
        title, text = extract_readable_html(html_doc, base_url="https://docs.example.com/intro/index.html")
        self.assertEqual(title, "MiniCC Documentation")
        self.assertNotIn("color: red", text)
        self.assertNotIn("secret token", text)
        self.assertIn("# Getting Started", text)
        self.assertIn("Quick Guide (https://docs.example.com/guide)", text)
        self.assertIn("- First item", text)
        self.assertIn("pip install minicc", text)

    def test_web_fetch_formats_html_json_and_paginates_long_content(self) -> None:
        long_body = "Line of documentation content.\n" * 400
        html_payload = f"<html><head><title>Long Doc</title></head><body><p>{long_body}</p></body></html>"

        def fake_fetcher(url: str) -> FetchedResponse:
            if url.endswith(".json"):
                return FetchedResponse(
                    url=url,
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=b'{"name":"MiniCC","version":1}',
                )
            return FetchedResponse(
                url=url,
                status=200,
                content_type="text/html; charset=utf-8",
                body=html_payload.encode("utf-8"),
            )

        tool = WebFetchTool(fetcher=fake_fetcher)
        page1 = tool.execute({"url": "https://example.com/doc", "max_chars": 1000}, lambda: None)
        self.assertIn("标题：Long Doc", page1)
        self.assertIn("start_char=1000", page1)

        page2 = tool.execute({"url": "https://example.com/doc", "start_char": 1000, "max_chars": 1000}, lambda: None)
        self.assertIn("1000-", page2)

        json_out = tool.execute({"url": "https://example.com/api.json"}, lambda: None)
        self.assertIn('"name": "MiniCC"', json_out)

    def test_web_fetch_rejects_binary_content_type(self) -> None:
        def binary_fetcher(url: str) -> FetchedResponse:
            return FetchedResponse(
                url=url,
                status=200,
                content_type="image/png",
                body=b"\x89PNG\r\n\x1a\n",
            )

        tool = WebFetchTool(fetcher=binary_fetcher)
        with self.assertRaises(ToolFailure) as ctx:
            tool.execute({"url": "https://example.com/logo.png"}, lambda: None)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_CONTENT_TYPE")

    def test_web_search_formats_results_and_handles_empty_or_invalid_queries(self) -> None:
        hits = (
            SearchHit(
                title="Python 3.14 What's New",
                url="https://docs.python.org/3.14/whatsnew/3.14.html",
                snippet="Summary of release highlights in Python 3.14.",
                source="duckduckgo",
            ),
            SearchHit(
                title="PEP 749 – Implementing PEP 649",
                url="https://peps.python.org/pep-0749/",
                snippet="Deferred evaluation of annotations.",
                source="duckduckgo",
            ),
        )
        tool = WebSearchTool(searcher=lambda q, limit, cp: hits[:limit])
        output = tool.execute({"query": "python 3.14 whats new", "limit": 2}, lambda: None)
        self.assertIn("Python 3.14 What's New", output)
        self.assertIn("https://docs.python.org/3.14/whatsnew/3.14.html", output)
        self.assertIn("PEP 749", output)

        empty_tool = WebSearchTool(searcher=lambda q, limit, cp: ())
        empty_output = empty_tool.execute({"query": "nonexistent xyz123"}, lambda: None)
        self.assertIn("未找到", empty_output)

    def test_duckduckgo_and_bing_html_parsers_extract_search_hits(self) -> None:
        from src.harness.tools.builtins.web import parse_bing_html, parse_duckduckgo_html

        ddg_sample = """
        <html><body>
          <div class="result results_links results_links_deep web-result">
            <h2 class="result__title">
              <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle&amp;rut=abc">
                Example <b>Article</b> Title
              </a>
            </h2>
            <a class="result__snippet" href="#">This is a <b>helpful</b> snippet from DuckDuckGo.</a>
          </div>
        </body></html>
        """
        ddg_hits = parse_duckduckgo_html(ddg_sample, limit=5)
        self.assertEqual(len(ddg_hits), 1)
        self.assertEqual(ddg_hits[0].title, "Example Article Title")
        self.assertEqual(ddg_hits[0].url, "https://example.com/article")
        self.assertEqual(ddg_hits[0].snippet, "This is a helpful snippet from DuckDuckGo.")

        bing_sample = """
        <html><body>
          <ol id="b_results">
            <li class="b_algo">
              <h2><a href="https://example.org/guide">Example Org Guide</a></h2>
              <div class="b_caption"><p>Detailed guide on how to use Example Org.</p></div>
            </li>
          </ol>
        </body></html>
        """
        bing_hits = parse_bing_html(bing_sample, limit=5)
        self.assertEqual(len(bing_hits), 1)
        self.assertEqual(bing_hits[0].title, "Example Org Guide")
        self.assertEqual(bing_hits[0].url, "https://example.org/guide")
        self.assertEqual(bing_hits[0].snippet, "Detailed guide on how to use Example Org.")

    def test_redirect_handler_blocks_redirect_to_private_ip(self) -> None:
        from src.harness.tools.builtins.web import _SafeRedirectHandler
        from urllib.request import Request

        handler = _SafeRedirectHandler()
        req = Request("https://example.com/start")
        with self.assertRaises(ToolFailure) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/internal")
        self.assertEqual(ctx.exception.code, "BLOCKED_URL")

    def test_chat_session_executes_web_search_and_web_fetch_without_approval_pause(self) -> None:
        registry = create_builtin_registry(self.root)
        # Replace with deterministic mock instances
        registry.unregister("web_search")
        registry.unregister("web_fetch")
        registry.register(
            WebSearchTool(
                searcher=lambda q, limit, cp: (
                    SearchHit("Official Docs", "https://example.com/docs", "Read official documentation."),
                )
            )
        )
        registry.register(
            WebFetchTool(
                fetcher=lambda url: FetchedResponse(
                    url=url,
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=b"<html><head><title>Docs</title></head><body><h1>API</h1><p>Use v2 endpoint.</p></body></html>",
                )
            )
        )

        replies = iter((
            ModelReply(None, (ToolCall("s1", "web_search", json.dumps({"query": "example api"})),)),
            ModelReply(None, (ToolCall("f1", "web_fetch", json.dumps({"url": "https://example.com/docs"})),)),
            ModelReply("According to https://example.com/docs, you should use the v2 endpoint."),
        ))
        seen_results = []
        session = ChatSession(
            lambda m, t, to: next(replies),
            registry,
            on_tool_result=lambda call, res: seen_results.append((call.name, res.error_code, res.content)),
        )
        answer = session.send("Check example API docs")
        self.assertIn("v2 endpoint", answer)
        self.assertEqual(len(seen_results), 2)
        self.assertEqual(seen_results[0][0:2], ("web_search", None))
        self.assertEqual(seen_results[1][0:2], ("web_fetch", None))
        self.assertIn("Use v2 endpoint.", seen_results[1][2])
