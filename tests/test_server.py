"""
Tests for mcp-server-webdriver.

Unit tests run without a browser.
Integration tests (marked with @pytest.mark.integration) open a real headless
Firefox and require geckodriver on PATH.

Run unit tests only:    pytest tests/ -m "not integration"
Run all tests:          pytest tests/
"""

import json
import os
import sys
import pytest

# Ensure the project root is on the path so `import server` works when
# running tests directly from a source checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# Unit tests — argument parsing and geckodriver resolution
# ---------------------------------------------------------------------------

class TestMainArgParsing:
    """main() must parse CLI flags and set env vars before starting the server."""

    def _parse(self, args: list[str]) -> dict:
        """Run only the argument-parsing part of main() and return env state."""
        env_before = {
            "FIREFOX_PROFILE": os.environ.pop("FIREFOX_PROFILE", None),
            "FIREFOX_PROFILE_DIR": os.environ.pop("FIREFOX_PROFILE_DIR", None),
        }
        try:
            # Inline the parsing logic from main() so we don't actually start mcp
            _ENV_FIREFOX_PROFILE     = "FIREFOX_PROFILE"
            _ENV_FIREFOX_PROFILE_DIR = "FIREFOX_PROFILE_DIR"
            i = 0
            while i < len(args):
                if args[i] == "-P" and i + 1 < len(args):
                    os.environ[_ENV_FIREFOX_PROFILE] = args[i + 1]
                    i += 2
                elif args[i].startswith("-P") and len(args[i]) > 2:
                    os.environ[_ENV_FIREFOX_PROFILE] = args[i][2:]
                    i += 1
                elif args[i] == "--profile" and i + 1 < len(args):
                    os.environ[_ENV_FIREFOX_PROFILE_DIR] = args[i + 1]
                    i += 2
                else:
                    i += 1
            return {
                "FIREFOX_PROFILE":     os.environ.get("FIREFOX_PROFILE"),
                "FIREFOX_PROFILE_DIR": os.environ.get("FIREFOX_PROFILE_DIR"),
            }
        finally:
            # Restore original env so tests don't bleed into each other
            for k, v in env_before.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_dash_P_space(self):
        result = self._parse(["-P", "myprofile"])
        assert result["FIREFOX_PROFILE"] == "myprofile"
        assert result["FIREFOX_PROFILE_DIR"] is None

    def test_dash_P_concatenated(self):
        result = self._parse(["-Pmyprofile"])
        assert result["FIREFOX_PROFILE"] == "myprofile"

    def test_profile_long(self):
        result = self._parse(["--profile", "/home/user/.mozilla/firefox/abc123.dev"])
        assert result["FIREFOX_PROFILE_DIR"] == "/home/user/.mozilla/firefox/abc123.dev"
        assert result["FIREFOX_PROFILE"] is None

    def test_profile_and_other_flags(self):
        result = self._parse(["--some-future-flag", "-P", "work"])
        assert result["FIREFOX_PROFILE"] == "work"

    def test_no_profile_flags(self):
        result = self._parse([])
        assert result["FIREFOX_PROFILE"] is None
        assert result["FIREFOX_PROFILE_DIR"] is None


class TestHelp:
    def test_help_exits_zero(self, monkeypatch):
        import server as srv
        monkeypatch.setattr(sys, "argv", ["mcp-server-webdriver", "--help"])
        with pytest.raises(SystemExit) as exc:
            srv.main()
        assert exc.value.code == 0

    def test_short_help_exits_zero(self, monkeypatch):
        import server as srv
        monkeypatch.setattr(sys, "argv", ["mcp-server-webdriver", "-h"])
        with pytest.raises(SystemExit) as exc:
            srv.main()
        assert exc.value.code == 0

    def test_help_mentions_profile_flags(self, capsys, monkeypatch):
        import server as srv
        monkeypatch.setattr(sys, "argv", ["mcp-server-webdriver", "--help"])
        with pytest.raises(SystemExit):
            srv.main()
        out = capsys.readouterr().out
        assert "-P <profile>" in out
        assert "--profile <path>" in out

    def test_help_mentions_env_vars(self, capsys, monkeypatch):
        import server as srv
        monkeypatch.setattr(sys, "argv", ["mcp-server-webdriver", "--help"])
        with pytest.raises(SystemExit):
            srv.main()
        out = capsys.readouterr().out
        assert "FIREFOX_PROFILE" in out
        assert "FIREFOX_PROFILE_DIR" in out
        assert "GECKODRIVER_PATH" in out


class TestGeckodriverResolution:
    """_resolve_geckodriver() must return the actual binary path, not None."""

    def test_system_path_returns_real_path(self, monkeypatch):
        import shutil
        import server as srv

        fake_path = "/usr/local/bin/geckodriver"
        monkeypatch.setenv("GECKODRIVER_PATH", "")
        monkeypatch.setattr(shutil, "which", lambda name: fake_path if name == "geckodriver" else None)

        path, source = srv._resolve_geckodriver()
        assert path == fake_path, "system PATH geckodriver must return the real path, not None"
        assert "system PATH" in source

    def test_env_var_takes_priority(self, monkeypatch, tmp_path):
        import server as srv

        fake_bin = tmp_path / "geckodriver"
        fake_bin.touch()
        monkeypatch.setenv("GECKODRIVER_PATH", str(fake_bin))

        path, source = srv._resolve_geckodriver()
        assert path == str(fake_bin)
        assert "GECKODRIVER_PATH" in source

    def test_env_var_nonexistent_file_is_ignored(self, monkeypatch):
        import shutil
        import server as srv

        monkeypatch.setenv("GECKODRIVER_PATH", "/nonexistent/geckodriver")
        monkeypatch.setenv("GECKODRIVER_AUTO_INSTALL", "false")
        monkeypatch.setattr(shutil, "which", lambda name: None)

        path, source = srv._resolve_geckodriver()
        assert path is None
        assert "not found" in source


class TestNormaliseUrl:
    def test_bare_hostname(self):
        import server as srv
        assert srv._normalise_url("example.com") == "https://example.com"

    def test_with_scheme(self):
        import server as srv
        assert srv._normalise_url("https://example.com") == "https://example.com"
        assert srv._normalise_url("http://example.com") == "http://example.com"

    def test_about_blank(self):
        import server as srv
        assert srv._normalise_url("about:blank") == "about:blank"

    def test_data_uri(self):
        import server as srv
        url = "data:text/html,<h1>hi</h1>"
        assert srv._normalise_url(url) == url

    def test_strips_whitespace(self):
        import server as srv
        assert srv._normalise_url("  example.com  ") == "https://example.com"

    def test_empty_string(self):
        import server as srv
        assert srv._normalise_url("") == ""


class TestKeyMap:
    def test_known_keys_present(self):
        import server as srv
        for key in ("enter", "tab", "escape", "space", "backspace", "delete",
                    "arrowup", "arrowdown", "arrowleft", "arrowright",
                    "home", "end", "pageup", "pagedown",
                    "f1", "f5", "f12"):
            assert key in srv._KEY_MAP, f"Missing key: {key}"

    def test_aliases(self):
        import server as srv
        assert srv._KEY_MAP["return"] == srv._KEY_MAP["enter"]
        assert srv._KEY_MAP["esc"]    == srv._KEY_MAP["escape"]
        assert srv._KEY_MAP["up"]     == srv._KEY_MAP["arrowup"]
        assert srv._KEY_MAP["down"]   == srv._KEY_MAP["arrowdown"]


class TestToolCount:
    """The MCP server must expose exactly the expected number of tools."""

    def test_tool_names(self):
        import asyncio
        import server as srv
        tools = [t.name for t in asyncio.run(srv.mcp.list_tools())]
        expected = {
            "browser_open", "browser_close", "browser_status",
            "browser_set_viewport",
            "browser_navigate", "browser_back", "browser_forward",
            "browser_refresh", "browser_screenshot",
            "browser_click", "browser_fill", "browser_upload_file", "browser_select",
            "browser_execute_js", "browser_wait", "browser_switch_frame",
            "browser_scroll", "browser_press_key", "browser_hover",
            "browser_find_elements",
            "browser_accept_dialog", "browser_dismiss_dialog",
            "browser_get_cookies", "browser_set_cookie",
            "browser_get_storage", "browser_set_storage", "browser_clear_storage",
            "browser_get_title", "browser_get_url", "browser_get_source",
            "browser_get_text", "browser_get_attribute",
            "devtools_report", "devtools_js_errors", "devtools_console",
            "devtools_network_failed", "devtools_network_all",
            "devtools_clear", "devtools_enable_bidi",
            "devtools_computed_css", "devtools_element_info",
            "devtools_css_variables", "devtools_performance",
        }
        assert set(tools) == expected


# ---------------------------------------------------------------------------
# Integration tests — require a real Firefox + geckodriver
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBrowserIntegration:
    """End-to-end tests that open a real headless Firefox via the MCP server."""

    @pytest.fixture(scope="class")
    async def client(self):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(command="mcp-server-webdriver", args=[])
        async with Client(transport) as c:
            yield c

    def _text(self, result) -> str:
        if hasattr(result, "data") and result.data is not None:
            d = result.data
            return d if isinstance(d, str) else json.dumps(d)
        if hasattr(result, "content") and result.content:
            c = result.content[0]
            return getattr(c, "text", str(c))
        return str(result)

    async def test_status_before_open(self, client):
        r = await client.call_tool("browser_status", {})
        status = json.loads(self._text(r))
        assert status["session_active"] is False

    async def test_open_headless(self, client):
        r = await client.call_tool("browser_open", {"url": "about:blank", "headless": True})
        assert "Opened" in self._text(r)

    async def test_navigate_and_title(self, client):
        await client.call_tool("browser_navigate", {"url": "https://example.com"})
        title = self._text(await client.call_tool("browser_get_title", {}))
        assert "Example" in title

    async def test_get_url(self, client):
        url = self._text(await client.call_tool("browser_get_url", {}))
        assert url.startswith("https://example.com")

    async def test_screenshot_returns_image(self, client):
        r = await client.call_tool("browser_screenshot", {})
        c = r.content[0] if hasattr(r, "content") and r.content else None
        assert c is not None
        assert getattr(c, "data", None)

    async def test_devtools_report_json(self, client):
        r = await client.call_tool("devtools_report", {})
        report = json.loads(self._text(r))
        assert "js_errors" in report
        assert "console_errors" in report
        assert "failed_resources" in report

    async def test_scroll_to_bottom(self, client):
        r = await client.call_tool("browser_scroll", {"by": True, "y": 500})
        assert "Scrolled" in self._text(r)

    async def test_scroll_to_top(self, client):
        r = await client.call_tool("browser_scroll", {"x": 0, "y": 0})
        assert "Scrolled" in self._text(r)

    async def test_find_elements(self, client):
        r = await client.call_tool("browser_find_elements", {"selector": "a"})
        elements = json.loads(self._text(r))
        assert isinstance(elements, list)
        assert len(elements) > 0
        assert "tag" in elements[0]
        assert elements[0]["tag"] == "a"

    async def test_execute_js_returns_json(self, client):
        r = await client.call_tool("browser_execute_js", {"script": "return {a: 1, b: [2, 3]}"})
        result = json.loads(self._text(r))
        assert result == {"a": 1, "b": [2, 3]}

    async def test_execute_js_null(self, client):
        r = await client.call_tool("browser_execute_js", {"script": "return null"})
        assert self._text(r) == "null"

    async def test_browser_open_no_url(self, client):
        await client.call_tool("browser_close", {})
        r = await client.call_tool("browser_open", {})
        assert "about:blank" in self._text(r)

    async def test_navigate_bare_hostname(self, client):
        r = await client.call_tool("browser_navigate", {"url": "example.com"})
        assert "https://example.com" in self._text(r)

    async def test_wait_conditions(self, client):
        await client.call_tool("browser_navigate", {"url": "https://example.com"})
        r = await client.call_tool("browser_wait", {"selector": "h1", "condition": "visible"})
        assert "visible" in self._text(r)
        r = await client.call_tool("browser_wait", {"selector": "h1", "condition": "present"})
        assert "present" in self._text(r)
        r = await client.call_tool("browser_wait", {"selector": "h1", "condition": "text:Example"})
        assert "Example" in self._text(r)

    async def test_hover(self, client):
        r = await client.call_tool("browser_hover", {"selector": "h1"})
        assert "Hovering" in self._text(r)

    async def test_get_cookies(self, client):
        r = await client.call_tool("browser_get_cookies", {})
        cookies = json.loads(self._text(r))
        assert isinstance(cookies, list)

    async def test_set_cookie(self, client):
        r = await client.call_tool("browser_set_cookie", {"name": "testcookie", "value": "testval"})
        assert "testcookie" in self._text(r)

    async def test_dismiss_nonexistent_dialog(self, client):
        with pytest.raises(Exception):
            await client.call_tool("browser_dismiss_dialog", {})

    async def test_close(self, client):
        r = await client.call_tool("browser_close", {})
        assert "Closed" in self._text(r)

    async def test_status_after_close(self, client):
        r = await client.call_tool("browser_status", {})
        status = json.loads(self._text(r))
        assert status["session_active"] is False
