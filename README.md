# mcp-server-webdriver

![mcp-server-webdriver](mcp-server-webdriver.svg)

MCP Server that lets AI agents control a real web browser via **Selenium WebDriver** (Firefox + geckodriver).  
Built with [FastMCP](https://gofastmcp.com).

---

## What it does

The server eliminates the copy-paste loop between the browser and the AI assistant.
Instead of opening DevTools, copying errors, pasting them into a chat, and repeating,
the assistant opens the browser itself, navigates, captures errors and screenshots,
and diagnoses the problem directly.

```
You:  "Why is the checkout button broken on /cart?"

AI:   browser_open → browser_navigate("/cart")
      → devtools_report          # JS errors? network failures?
      → browser_screenshot       # what does it look like?
      → devtools_computed_css("button#checkout")   # hidden? wrong z-index?
      → "The button has pointer-events: none — overridden by .disabled class
         applied when cart.js fails to load (404 on /static/cart.js)."
```

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

### Recommended — Debian package from VitexSoftware repository

```bash
sudo curl -fsSL http://repo.vitexsoftware.com/KEY.gpg -o /usr/share/keyrings/vitexsoftware-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/vitexsoftware-archive-keyring.gpg] http://repo.vitexsoftware.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
sudo apt update
sudo apt install python3-mcp-server-webdriver
```

This installs `gecko-driver`, `python3-selenium`, `python3-fastmcp`, and
`mcp-server-webdriver` in a single step.

### Alternative — system package manager

```bash
# Debian/Ubuntu (official repos — may be older geckodriver):
sudo apt install firefox-geckodriver

# macOS:
brew install geckodriver

# Rust / cargo (build from source):
cargo install geckodriver
```

Then install Python dependencies:

```bash
pip install fastmcp selenium
```

### Fallback — webdriver-manager (auto-download)

```bash
pip install fastmcp selenium webdriver-manager
# geckodriver is downloaded automatically on first browser_open call
```

---

## Usage

```
mcp-server-webdriver [OPTIONS]

OPTIONS
  -P <profile>       Start Firefox with a named profile
  --profile <path>   Start Firefox with a profile directory at <path>
  -h, --help         Show help and exit
```

The server speaks MCP over stdin/stdout and is launched automatically
by the MCP client — not by hand.

---

## Use cases

### Debug a broken page

Ask: *"Why does /dashboard show a blank screen?"*

The assistant will:

1. `browser_open` — open Firefox headlessly
2. `browser_navigate` — go to `/dashboard`
3. `devtools_report` — get JS errors, console output, and failed network resources in one call
4. `browser_screenshot` — see what the page actually looks like
5. Explain the root cause from the combined evidence

`devtools_report` is the primary diagnostic tool — equivalent to opening the Console
and Network tabs in DevTools and reading them simultaneously.

---

### Diagnose a CSS / layout problem

Ask: *"The sidebar overlaps the content area on mobile. Why?"*

1. `browser_open` — open the page
2. `browser_screenshot` — capture the broken layout
3. `devtools_computed_css(".sidebar")` — check `position`, `width`, `z-index`, `overflow`
4. `devtools_css_variables("--")` — verify design tokens loaded correctly
5. `devtools_network_failed` — check whether any stylesheet failed to load

---

### Automate a login flow

Ask: *"Log into the app at /login with user=admin, password=secret and screenshot the dashboard."*

1. `browser_open` — start the browser
2. `browser_navigate` — go to `/login`
3. `browser_fill("#username", "admin")` — type username
4. `browser_fill("#password", "secret")` — type password
5. `browser_press_key("enter")` — submit the form
6. `browser_wait("#dashboard", condition="visible")` — wait for redirect
7. `browser_screenshot` — capture the result

---

### Inject a session cookie to skip login

Ask: *"Check the admin panel using my existing session token."*

1. `browser_open` — start the browser
2. `browser_navigate` — go to the app's root so the cookie domain matches
3. `browser_set_cookie("session", "<token>")` — inject the auth cookie
4. `browser_navigate` — now navigate to the protected page
5. `browser_screenshot` — confirm access

---

### Enumerate page content

Ask: *"List all the navigation links on the homepage."*

1. `browser_open` + `browser_navigate` — open the page
2. `browser_find_elements("nav a")` — get all links with their text, href, and visibility
3. Return the structured list

---

### Interact with hover menus

Ask: *"Click the third item in the Products dropdown."*

1. `browser_hover(".nav-products")` — trigger the `:hover` state that reveals the dropdown
2. `browser_wait(".dropdown-menu", condition="visible")` — wait for animation
3. `browser_find_elements(".dropdown-menu a")` — list the items
4. `browser_click(".dropdown-menu a:nth-child(3)")` — click the right one

---

### Test a multi-step form

Ask: *"Fill out the registration form and submit it."*

1. `browser_fill("#first-name", "Alice")`
2. `browser_fill("#last-name", "Smith")`
3. `browser_fill("#email", "alice@example.com")`
4. `browser_select("#country", "Czech Republic")`
5. `browser_press_key("tab")` — move focus to next field
6. `browser_click("button[type=submit"]")`
7. `browser_wait(".success-message", condition="visible")`
8. `devtools_report` — check for any JS errors or failed API calls during submission

---

### Handle JS dialogs

Ask: *"Click the Delete button and confirm the dialog."*

1. `browser_click("#delete-btn")`
2. `browser_accept_dialog` — click OK on the `confirm("Are you sure?")`
3. `browser_wait(".deleted-notice", condition="present")`

---

### Scroll and capture a long page

Ask: *"Screenshot the footer of the page."*

1. `browser_open` + `browser_navigate`
2. `browser_scroll("footer")` — scroll the footer element into view
3. `browser_screenshot("footer")` — capture just the footer element

Or scroll by offset to trigger lazy-loaded content:

1. `browser_scroll(by=True, y=1000)` — scroll down 1000 px
2. `browser_wait(".lazy-section", condition="visible")` — wait for lazy content
3. `browser_screenshot` — capture the now-loaded content

---

### Use a real Firefox profile (stay logged in)

Configure the server with a named profile that already has your session:

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "mcp-server-webdriver",
      "args": ["-P", "work"]
    }
  }
}
```

The browser starts with your existing cookies, saved passwords, and extensions.
Ask: *"Check my GitHub notifications."* — no login step needed.

---

### Audit network performance

Ask: *"Which resources on /shop are slowest to load?"*

1. `browser_open` + `browser_navigate("/shop")`
2. `devtools_network_all(slow_ms=500, limit=20)` — requests over 500 ms, capped at 20 entries
3. Report the slowest assets with their URLs, types, and durations

---

## Available Tools

### Session management

| Tool | Description |
|---|---|
| `browser_open` | Open Firefox (URL optional, default `about:blank`); accepts `width`, `height`, `user_agent` for mobile emulation |
| `browser_close` | Quit the browser session |
| `browser_status` | Session state, geckodriver version, BiDi status, current viewport size, buffer counts |
| `browser_set_viewport` | Resize the viewport mid-session (e.g. 390×844 for iPhone 14) |

### Navigation

| Tool | Description |
|---|---|
| `browser_navigate` | Navigate to a URL (bare hostnames get `https://`) |
| `browser_back` | Go back in history |
| `browser_forward` | Go forward in history |
| `browser_refresh` | Reload the current page |

### Page inspection

| Tool | Description |
|---|---|
| `browser_screenshot` | Full-page or element PNG screenshot |
| `browser_get_title` | Current page `<title>` |
| `browser_get_url` | Current URL |
| `browser_get_source` | Raw HTML source |
| `browser_get_text` | Visible text (whole page or CSS selector) |
| `browser_get_attribute` | Value of an HTML attribute on an element |
| `browser_find_elements` | List all elements matching a CSS selector |

### Interaction

| Tool | Description |
|---|---|
| `browser_click` | Click element (CSS selector) |
| `browser_fill` | Type text into an input field (clears first by default) |
| `browser_select` | Select `<option>` in a `<select>` dropdown |
| `browser_execute_js` | Run JavaScript — returns JSON |
| `browser_wait` | Wait: `visible` / `clickable` / `present` / `text:<str>` |
| `browser_scroll` | Scroll to coords, by offset, or element into view |
| `browser_press_key` | Send `enter` / `tab` / `escape` / arrow / F-keys |
| `browser_hover` | Hover mouse over element (`:hover` states, tooltips, dropdowns) |
| `browser_switch_frame` | Switch into `<iframe>` or back to main document |

### Dialogs & cookies

| Tool | Description |
|---|---|
| `browser_accept_dialog` | Accept a JS `alert()` / `confirm()` / `prompt()` |
| `browser_dismiss_dialog` | Dismiss a JS `confirm()` / `prompt()` |
| `browser_get_cookies` | Read all cookies for the current page |
| `browser_set_cookie` | Inject a cookie (auth tokens, session IDs) |

### DevTools (require BiDi — Firefox + geckodriver ≥ 0.34)

| Tool | Description |
|---|---|
| `devtools_report` | **Main diagnostic tool** — JS errors + console + failed/slow network |
| `devtools_js_errors` | JavaScript exceptions only |
| `devtools_console` | Console output (log / warn / error / info / debug) |
| `devtools_network_failed` | Failed resources (4xx, 5xx, DNS errors) |
| `devtools_network_all` | All network requests (supports `limit=` and filters) |
| `devtools_clear` | Clear buffered DevTools data (use before navigating) |
| `devtools_enable_bidi` | Attach BiDi listeners to a running session |
| `devtools_computed_css` | Computed CSS properties of an element |
| `devtools_element_info` | Bounding box, visibility, attributes, aria, outerHTML |
| `devtools_css_variables` | CSS custom properties (`--var`) in scope |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GECKODRIVER_PATH` | _(unset)_ | Absolute path to geckodriver binary (highest priority) |
| `GECKODRIVER_AUTO_INSTALL` | `true` | Set to `false` to disable webdriver-manager fallback |
| `FIREFOX_BINARY` | _(unset)_ | Path to a custom Firefox executable |
| `FIREFOX_PROFILE` | _(unset)_ | Named Firefox profile — same as `-P` |
| `FIREFOX_PROFILE_DIR` | _(unset)_ | Profile directory path — same as `--profile` |

---

## geckodriver resolution order

| # | Source | Configure via |
|---|---|---|
| 1 | `GECKODRIVER_PATH` env variable | Absolute path to the binary |
| 2 | **System PATH** (default) | `apt install gecko-driver` from repo.vitexsoftware.com |
| 3 | webdriver-manager auto-download | Fallback; disable with `GECKODRIVER_AUTO_INSTALL=false` |

---

## MCP client configuration

Minimal config:

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "mcp-server-webdriver"
    }
  }
}
```

With a named Firefox profile (stays logged in, uses saved passwords):

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "mcp-server-webdriver",
      "args": ["-P", "work"]
    }
  }
}
```

With a profile directory and explicit geckodriver path:

```json
{
  "mcpServers": {
    "webdriver": {
      "command": "mcp-server-webdriver",
      "args": ["--profile", "/home/user/.mozilla/firefox/abc123.dev"],
      "env": {
        "GECKODRIVER_PATH": "/usr/bin/geckodriver"
      }
    }
  }
}
```

---

## Running tests

```bash
# Unit tests only (no browser required):
pytest tests/ -m "not integration"

# All tests including browser integration:
pytest tests/
```

---

## Related MCP Servers by VitexSoftware

| Server | Description |
|---|---|
| [abraflexi-mcp-server](https://github.com/VitexSoftware/abraflexi-mcp-server) | AbraFlexi accounting/ERP integration — invoices, contacts, products, bank transactions |
| [mastodon-mcp-server](https://github.com/VitexSoftware/mastodon-mcp-server) | Mastodon integration — timelines, posting, account management, search |
| [semaphore-mcp-server](https://github.com/VitexSoftware/semaphore-mcp-server) | Semaphore UI integration — manage Ansible, Terraform and other automation workflows |

---

## License

MIT
