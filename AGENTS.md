# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Unit tests only (no browser required — used by Debian build):
pytest tests/ -m "not integration"

# All tests including browser integration (requires geckodriver on PATH):
pytest tests/

# Run a single test class or function:
pytest tests/test_server.py::TestNormaliseUrl
pytest tests/test_server.py::TestBrowserIntegration::test_navigate_and_title

# Install dev dependencies:
pip install -e ".[dev]"

# Run the server (normally launched by MCP client, not by hand):
mcp-server-webdriver --help
```

## Installation (VitexSoftware APT repository)

```bash
sudo curl -fsSL http://repo.vitexsoftware.com/KEY.gpg -o /usr/share/keyrings/vitexsoftware-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/vitexsoftware-archive-keyring.gpg] http://repo.vitexsoftware.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
sudo apt update && sudo apt install python3-mcp-server-webdriver
```

## Architecture

The entire server is a single file: `server.py`. There are no submodules.

### Core components

**`BrowserState` dataclass** — owns the Firefox session and all captured DevTools data. It holds:
- The `webdriver.Firefox` driver instance
- Three in-memory buffers (protected by `threading.Lock`): `_console`, `_js_errors`, `_network`
- A `_pending` dict tracking in-flight network requests by request ID, used to calculate `duration_ms` when a response arrives
- BiDi handler IDs for cleanup on re-attach

**BiDi event capture** — `_attach_bidi()` registers four async callbacks on the Selenium BiDi API (`driver.script` for console/JS errors, `driver.network` for request/response/fail events). Handlers write to the shared buffers under the lock. Network timing is derived from `(response.timestamp - request.timestamp) * 1000`. If the Selenium version doesn't support network BiDi, the network handlers are silently skipped.

**FastMCP server** — `mcp = FastMCP(...)` at module level. Tools are decorated with `@mcp.tool()`. State is passed to every tool via `Context` using the lifespan pattern: `ctx.lifespan_context["browser"]` returns the `BrowserState`. The helper `_st(ctx)` wraps this lookup.

**Lifespan** — `@asynccontextmanager async def lifespan(app)` creates one `BrowserState` per server process, resolves the geckodriver path eagerly, and calls `state.stop()` on shutdown.

**`main()`** — parses `-P <profile>` and `--profile <path>` flags by setting `FIREFOX_PROFILE` / `FIREFOX_PROFILE_DIR` env vars, then calls `mcp.run()`.

### geckodriver resolution

`_resolve_geckodriver()` returns `(path | None, description)` using priority order:
1. `GECKODRIVER_PATH` env var (must point to an existing file)
2. `shutil.which("geckodriver")` — system PATH
3. `webdriver_manager.firefox.GeckoDriverManager().install()` — auto-download (disabled by `GECKODRIVER_AUTO_INSTALL=false`)

### Tool categories (43 total)

- **Session**: `browser_open` (accepts `width`, `height`, `user_agent` for mobile emulation), `browser_close`, `browser_status`, `browser_set_viewport`
- **Navigation**: `browser_navigate`, `browser_back`, `browser_forward`, `browser_refresh`
- **Interaction**: `browser_click`, `browser_fill`, `browser_upload_file`, `browser_select`, `browser_execute_js`, `browser_wait`, `browser_scroll`, `browser_press_key`, `browser_hover`, `browser_switch_frame`
- **Inspection**: `browser_screenshot`, `browser_get_title`, `browser_get_url`, `browser_get_source`, `browser_get_text`, `browser_get_attribute`, `browser_find_elements`
- **Dialogs & cookies**: `browser_accept_dialog`, `browser_dismiss_dialog`, `browser_get_cookies`, `browser_set_cookie`
- **Web storage**: `browser_get_storage`, `browser_set_storage`, `browser_clear_storage`
- **DevTools** (require BiDi): `devtools_report`, `devtools_js_errors`, `devtools_console`, `devtools_network_failed`, `devtools_network_all`, `devtools_clear`, `devtools_enable_bidi`, `devtools_computed_css`, `devtools_element_info`, `devtools_css_variables`, `devtools_performance`

All DevTools tools guard with `if not state.bidi_enabled: raise RuntimeError(...)`. `devtools_performance` is the exception — it uses the browser's Navigation Timing API via `execute_script` and does not require BiDi.

### Tests

`tests/conftest.py` stubs `fastmcp` and `fastmcp.utilities.types` when the real package is not installed, so unit tests pass in Debian build environments where only `python3-selenium` is available. `TestToolCount` is automatically skipped when the stub is active (because `mcp.list_tools()` returns nothing meaningful from a mock).

### Debian packaging

`debian/rules` runs `pytest tests/ -m "not integration" -q` during `dh_auto_test`. The `python3-fastmcp` package is a runtime dependency (`Depends:`) but is not in `Build-Depends` — `conftest.py` stubs it so the build-time unit tests pass without it.

## Related MCP Servers by VitexSoftware

| Server | Description |
|---|---|
| [abraflexi-mcp-server](https://github.com/VitexSoftware/abraflexi-mcp-server) | AbraFlexi accounting/ERP — invoices, contacts, products, bank transactions |
| [mastodon-mcp-server](https://github.com/VitexSoftware/mastodon-mcp-server) | Mastodon — timelines, posting, account management, search |
| [semaphore-mcp-server](https://github.com/VitexSoftware/semaphore-mcp-server) | Semaphore UI — Ansible, Terraform and other automation workflows |
