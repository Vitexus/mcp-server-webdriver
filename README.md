# mcp-server-webdriver

MCP Server that lets AI agents control a real web browser via **Selenium WebDriver** (Firefox + geckodriver).  
Built with [FastMCP](https://gofastmcp.com).

---

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.11 |
| [FastMCP](https://pypi.org/project/fastmcp/) | ≥ 2.10 |
| [selenium](https://pypi.org/project/selenium/) | ≥ 4.0 |
| Firefox | any recent |
| [geckodriver](https://github.com/mozilla/geckodriver) | ≥ 0.34 |

---

## Installation

### Recommended — gecko-driver .deb from VitexSoftware repository

```bash
echo "deb http://repo.vitexsoftware.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
sudo apt update
sudo apt install gecko-driver python3-selenium python3-fastmcp
```

This installs `gecko-driver` 0.36.0 (and compatible Firefox) from
[repo.vitexsoftware.com](http://repo.vitexsoftware.com).

### Alternative — system package manager

```bash
# Debian/Ubuntu (official repos — may be older version):
sudo apt install firefox-geckodriver

# macOS:
brew install geckodriver

# Rust / cargo (build from source):
cargo install geckodriver

# Manual download from GitHub releases:
# https://github.com/mozilla/geckodriver/releases
```

### Fallback — webdriver-manager (auto-download)

```bash
pip install fastmcp selenium webdriver-manager
# geckodriver will be downloaded automatically on first browser_open
```

---

## geckodriver resolution order

The server resolves geckodriver in this priority order (first match wins):

| # | Source | Configure via |
|---|---|---|
| 1 | `GECKODRIVER_PATH` env variable | Absolute path to the binary |
| 2 | **System PATH** (default) | `apt install gecko-driver` from repo.vitexsoftware.com |
| 3 | webdriver-manager auto-download | Fallback; disable with `GECKODRIVER_AUTO_INSTALL=false` |

---

## Running

```bash
# stdio transport (Claude Desktop / Claude Code)
python server.py

# HTTP transport (remote access)
fastmcp run server.py --transport streamable-http --port 8000

# Development mode (auto-reload)
fastmcp dev server.py
```

---

## Available Tools

### Session & driver management

| Tool | Description |
|---|---|
| `browser_open` | Open URL — starts Firefox+geckodriver if not running |
| `browser_close` | Quit the browser session |
| `browser_status` | Show session state, geckodriver source/version, env config |
| `geckodriver_install` | Download geckodriver via webdriver-manager (fallback) |

### Page inspection

| Tool | Description |
|---|---|
| `browser_screenshot` | Full-page PNG screenshot |
| `browser_get_title` | Current page `<title>` |
| `browser_get_url` | Current URL |
| `browser_get_source` | Raw HTML source |
| `browser_get_text` | Visible text (whole page or CSS selector) |
| `browser_get_attribute` | Value of an HTML attribute on an element |

### Interaction

| Tool | Description |
|---|---|
| `browser_click` | Click element (CSS selector) |
| `browser_fill` | Type text into an input field |
| `browser_select` | Select `<option>` in a `<select>` dropdown |
| `browser_execute_js` | Run JavaScript and return the result |

### Navigation & frames

| Tool | Description |
|---|---|
| `browser_wait` | Wait for an element to become visible |
| `browser_back` | Navigate back |
| `browser_forward` | Navigate forward |
| `browser_refresh` | Reload the page |
| `browser_switch_frame` | Switch into `<iframe>` or back to main document |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GECKODRIVER_PATH` | _(unset)_ | Absolute path to geckodriver binary (highest priority) |
| `GECKODRIVER_AUTO_INSTALL` | `true` | Set to `false` to disable webdriver-manager fallback |
| `FIREFOX_BINARY` | _(unset)_ | Path to a custom Firefox executable |

---

## Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "python3",
      "args": ["/path/to/server.py"]
    }
  }
}
```

With explicit geckodriver path:

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "python3",
      "args": ["/path/to/server.py"],
      "env": {
        "GECKODRIVER_PATH": "/usr/bin/geckodriver"
      }
    }
  }
}
```

---

## Packaging plan for Debian

To build a `python3-mcp-server-webdriver` .deb package, the following dependency
chain must be packaged first (all from PyPI → .deb via VitexSoftware build infrastructure):

**Layer 0** (no new deps): `python-dotenv`, `jsonref`, `pathable`, `python-multipart`,
`httpx-sse`, `typing-inspection`, `uncalled-for`, `pyperclip`, `cronsim`,
`burner-redis`, `python-json-logger`, `griffelib`, `taskgroup`, `joserfc`,
`opentelemetry-api`, `openapi-pydantic`

**Layer 1**: `pydantic-settings`, `rich-rst`, `griffecli`, `py-key-value-aio`

**Layer 2**: `jsonschema-path`, `sse-starlette`, `cyclopts`, `griffe`

**Layer 3**: `mcp`, `pydocket`

**Layer 4**: `prefab-ui`

**Layer 5**: `fastmcp-slim`

**Layer 6**: `fastmcp`

**Layer 7**: `mcp-server-webdriver` (depends on `gecko-driver` ✅ already in repo)

---

## License

MIT
