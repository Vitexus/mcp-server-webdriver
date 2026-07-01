"""
mcp-server-webdriver — AI-assisted web development tool.
Built with FastMCP (https://gofastmcp.com).

PURPOSE
-------
Eliminates the need to manually copy-paste browser DevTools output to an AI
assistant. The agent can autonomously inspect:

  • JavaScript errors (type, message, file, line, column, stack trace)
  • Console output (console.log / warn / error / info / debug)
  • Failed network resources — broken CSS, JS, images, fonts (4xx / 5xx / DNS)
  • Slow resources (configurable threshold)
  • Full-page screenshots for layout / CSS regression diagnosis
  • Computed CSS on any element
  • DOM structure of any element (outerHTML)
  • Accessibility snapshot (role / name / disabled / aria attributes)

Implementation uses W3C WebDriver BiDi (options.enable_bidi = True), which is
natively supported by Firefox + geckodriver ≥ 0.34.  No CDP, no proxy tools.

geckodriver resolution (first match wins):
  1. GECKODRIVER_PATH  env variable
  2. System PATH  ← gecko-driver .deb from repo.vitexsoftware.com  (preferred)
  3. webdriver-manager auto-download  (if GECKODRIVER_AUTO_INSTALL != false)

Install geckodriver:
  sudo curl -fsSL http://repo.vitexsoftware.com/KEY.gpg -o /usr/share/keyrings/vitexsoftware-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/vitexsoftware-archive-keyring.gpg] http://repo.vitexsoftware.com trixie main" \\
    | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
  sudo apt update && sudo apt install gecko-driver
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        NoAlertPresentException,
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
except ImportError as exc:
    raise SystemExit("selenium not installed — run: apt install python3-selenium") from exc

logger = logging.getLogger("mcp-server-webdriver")

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------
_ENV_GECKODRIVER_PATH   = "GECKODRIVER_PATH"
_ENV_AUTO_INSTALL       = "GECKODRIVER_AUTO_INSTALL"
_ENV_FIREFOX_BINARY     = "FIREFOX_BINARY"
_ENV_FIREFOX_PROFILE    = "FIREFOX_PROFILE"      # named profile (-P)
_ENV_FIREFOX_PROFILE_DIR = "FIREFOX_PROFILE_DIR" # profile path (--profile)
_REPO_URL             = "http://repo.vitexsoftware.com"
_REPO_DISTRO          = "trixie"
_REPO_PKG             = "gecko-driver"

# Resources considered "slow" by default (ms)
_DEFAULT_SLOW_MS = 2000

# Resource types that matter for CSS/layout breakage
_LAYOUT_RESOURCE_TYPES = {"stylesheet", "font", "image", "script", "fetch", "xhr"}

# Friendly key-name → Selenium Keys mapping for browser_press_key
_KEY_MAP: dict[str, str] = {
    "enter":       Keys.RETURN,
    "return":      Keys.RETURN,
    "tab":         Keys.TAB,
    "escape":      Keys.ESCAPE,
    "esc":         Keys.ESCAPE,
    "space":       Keys.SPACE,
    "backspace":   Keys.BACK_SPACE,
    "delete":      Keys.DELETE,
    "home":        Keys.HOME,
    "end":         Keys.END,
    "pageup":      Keys.PAGE_UP,
    "pagedown":    Keys.PAGE_DOWN,
    "arrowup":     Keys.ARROW_UP,
    "arrowdown":   Keys.ARROW_DOWN,
    "arrowleft":   Keys.ARROW_LEFT,
    "arrowright":  Keys.ARROW_RIGHT,
    "up":          Keys.ARROW_UP,
    "down":        Keys.ARROW_DOWN,
    "left":        Keys.ARROW_LEFT,
    "right":       Keys.ARROW_RIGHT,
    "f1":  Keys.F1,  "f2":  Keys.F2,  "f3":  Keys.F3,  "f4":  Keys.F4,
    "f5":  Keys.F5,  "f6":  Keys.F6,  "f7":  Keys.F7,  "f8":  Keys.F8,
    "f9":  Keys.F9,  "f10": Keys.F10, "f11": Keys.F11, "f12": Keys.F12,
}


def _normalise_url(url: str) -> str:
    """Prepend https:// to bare hostnames so the agent can pass 'example.com'."""
    url = url.strip()
    if url and "://" not in url and not url.startswith(("about:", "data:", "file:")):
        url = "https://" + url
    return url


# ---------------------------------------------------------------------------
# geckodriver resolution
# ---------------------------------------------------------------------------

def _geckodriver_version(executable: str) -> str:
    try:
        out = subprocess.check_output(
            [executable, "--version"], stderr=subprocess.STDOUT, timeout=5
        )
        return out.decode().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        return f"(version check failed: {exc})"


def _resolve_geckodriver() -> tuple[str | None, str]:
    env_path = os.environ.get(_ENV_GECKODRIVER_PATH, "").strip()
    if env_path:
        if os.path.isfile(env_path):
            logger.info("geckodriver: GECKODRIVER_PATH=%r", env_path)
            return env_path, f"env GECKODRIVER_PATH → {env_path}"
        logger.warning("GECKODRIVER_PATH=%r is not a file, ignoring.", env_path)

    system_path = shutil.which("geckodriver")
    if system_path:
        logger.info("geckodriver: system PATH → %r", system_path)
        return system_path, f"system PATH → {system_path}"

    auto = os.environ.get(_ENV_AUTO_INSTALL, "true").strip().lower()
    if auto not in ("0", "false", "no"):
        try:
            from webdriver_manager.firefox import GeckoDriverManager  # type: ignore
            path = GeckoDriverManager().install()
            logger.info("geckodriver: webdriver-manager → %r", path)
            return path, f"webdriver-manager → {path}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("webdriver-manager failed: %s", exc)

    logger.warning(
        "geckodriver not found. Install: apt install %s (from %s)", _REPO_PKG, _REPO_URL
    )
    return None, "not found"


# ---------------------------------------------------------------------------
# Log / network data classes
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConsoleEntry:
    ts: str
    level: str   # log | warn | error | info | debug
    text: str
    url: str = ""
    line: int = 0

    def to_dict(self) -> dict:
        return {"ts": self.ts, "level": self.level, "text": self.text,
                "url": self.url, "line": self.line}


@dataclass
class JsError:
    ts: str
    text: str
    error_type: str = ""
    url: str = ""
    line: int = 0
    column: int = 0
    stack: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts, "type": self.error_type, "text": self.text,
                "url": self.url, "line": self.line, "column": self.column,
                "stack": self.stack}


@dataclass
class NetworkEntry:
    ts: str
    method: str
    url: str
    resource_type: str   # stylesheet | script | image | font | fetch | xhr | document …
    status: int          # 0 = never completed / DNS error
    duration_ms: float   # 0 if not completed
    failed: bool         # DNS / connection / abort
    error_text: str = "" # e.g. "net::ERR_NAME_NOT_RESOLVED"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "method": self.method,
            "url": self.url,
            "type": self.resource_type,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "failed": self.failed,
            "error": self.error_text,
        }


# ---------------------------------------------------------------------------
# BrowserState
# ---------------------------------------------------------------------------

@dataclass
class BrowserState:
    driver: webdriver.Firefox | None = None
    headless: bool = True
    bidi_enabled: bool = False
    geckodriver_path: str | None = None
    geckodriver_source: str = "not resolved yet"

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _console: list[ConsoleEntry] = field(default_factory=list)
    _js_errors: list[JsError] = field(default_factory=list)
    _network: list[NetworkEntry] = field(default_factory=list)
    # pending requests: request_id → (ts, method, url, type, start_time_ms)
    _pending: dict[str, tuple] = field(default_factory=dict)

    _console_hid: Any = None
    _error_hid: Any = None
    _net_sent_hid: Any = None
    _net_done_hid: Any = None
    _net_fail_hid: Any = None

    # ------------------------------------------------------------------
    def resolve(self) -> None:
        if self.geckodriver_source == "not resolved yet":
            self.geckodriver_path, self.geckodriver_source = _resolve_geckodriver()

    def get_driver(self) -> webdriver.Firefox:
        if self.driver is None:
            raise RuntimeError("No browser session open — call browser_open first.")
        return self.driver

    # ------------------------------------------------------------------
    # BiDi handler factories
    # ------------------------------------------------------------------

    def _console_handler(self):
        def h(e):
            try:
                src = getattr(e, "source", None)
                with self._lock:
                    self._console.append(ConsoleEntry(
                        ts=_now(),
                        level=str(getattr(e, "method", "log")),
                        text=str(getattr(e, "text", "")),
                        url=str(getattr(src, "url", "") or ""),
                        line=int(getattr(src, "line_number", 0) or 0),
                    ))
            except Exception:  # noqa: BLE001
                pass
        return h

    def _js_error_handler(self):
        def h(e):
            try:
                det  = getattr(e, "exception_details", None)
                exc_ = getattr(det, "exception", None) if det else None
                st   = getattr(det, "stacktrace", None) if det else None
                frames = getattr(st, "call_frames", []) or [] if st else []
                stack_str = "\n".join(
                    f"  at {getattr(f,'function_name','?')} "
                    f"({getattr(f,'url','?')}:{getattr(f,'line_number',0)}:{getattr(f,'column_number',0)})"
                    for f in frames
                )
                with self._lock:
                    self._js_errors.append(JsError(
                        ts=_now(),
                        text=str(getattr(e, "text", "") or ""),
                        error_type=str(getattr(exc_, "type", "") or ""),
                        url=str(getattr(det, "url", "") or ""),
                        line=int(getattr(det, "line_number", 0) or 0),
                        column=int(getattr(det, "column_number", 0) or 0),
                        stack=stack_str,
                    ))
            except Exception:  # noqa: BLE001
                pass
        return h

    def _net_sent_handler(self):
        """Record request start time for duration calculation."""
        def h(e):
            try:
                req = getattr(e, "request", None)
                if req is None:
                    return
                rid   = str(getattr(req, "request_id", "") or "")
                url   = str(getattr(req, "url", "") or "")
                meth  = str(getattr(req, "method", "GET") or "GET")
                rtype = str(getattr(e, "initiator", None) and
                            getattr(getattr(e, "initiator", None), "type", "") or "")
                # BiDi uses resource_type on the event level
                rtype = str(getattr(e, "resource_type", rtype) or rtype)
                ts_ms = float(getattr(req, "timestamp", 0) or 0)
                if rid:
                    with self._lock:
                        self._pending[rid] = (_now(), meth, url, rtype, ts_ms)
            except Exception:  # noqa: BLE001
                pass
        return h

    def _net_done_handler(self):
        def h(e):
            try:
                req  = getattr(e, "request", None)
                resp = getattr(e, "response", None)
                if req is None or resp is None:
                    return
                rid    = str(getattr(req, "request_id", "") or "")
                status = int(getattr(resp, "status", 0) or 0)
                url    = str(getattr(resp, "url", "") or
                             getattr(req, "url", "") or "")
                ts_end = float(getattr(resp, "timestamp", 0) or 0)

                with self._lock:
                    pending = self._pending.pop(rid, None)
                    if pending is None:
                        return
                    ts_start_iso, meth, req_url, rtype, ts_start_ms = pending
                    dur = max(0.0, (ts_end - ts_start_ms) * 1000) if ts_end and ts_start_ms else 0.0
                    self._network.append(NetworkEntry(
                        ts=ts_start_iso,
                        method=meth,
                        url=url or req_url,
                        resource_type=rtype,
                        status=status,
                        duration_ms=dur,
                        failed=False,
                    ))
            except Exception:  # noqa: BLE001
                pass
        return h

    def _net_fail_handler(self):
        def h(e):
            try:
                req = getattr(e, "request", None)
                if req is None:
                    return
                rid        = str(getattr(req, "request_id", "") or "")
                error_text = str(getattr(e, "error_text", "") or "network error")
                with self._lock:
                    pending = self._pending.pop(rid, None)
                    if pending is None:
                        return
                    ts_iso, meth, url, rtype, _ = pending
                    self._network.append(NetworkEntry(
                        ts=ts_iso,
                        method=meth,
                        url=url,
                        resource_type=rtype,
                        status=0,
                        duration_ms=0.0,
                        failed=True,
                        error_text=error_text,
                    ))
            except Exception:  # noqa: BLE001
                pass
        return h

    # ------------------------------------------------------------------
    def _attach_bidi(self) -> bool:
        if self.driver is None:
            return False
        try:
            scr = self.driver.script

            # Remove stale handlers
            for attr, remover in (
                ("_console_hid",  scr.remove_console_message_handler),
                ("_error_hid",    scr.remove_javascript_error_handler),
            ):
                hid = getattr(self, attr)
                if hid is not None:
                    try:
                        remover(hid)
                    except Exception:  # noqa: BLE001
                        pass

            self._console_hid = scr.add_console_message_handler(self._console_handler())
            self._error_hid   = scr.add_javascript_error_handler(self._js_error_handler())

            # Network events (BiDi network module — requires Selenium with network BiDi support)
            try:
                net = self.driver.network
                self._net_sent_hid = net.add_request_handler(
                    callback=self._net_sent_handler()
                )
                self._net_done_hid = net.add_response_completed_handler(
                    callback=self._net_done_handler()
                )
                self._net_fail_hid = net.add_fetch_error_handler(
                    callback=self._net_fail_handler()
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("Network BiDi handlers not available (%s) — skipping", exc)

            self.bidi_enabled = True
            logger.info("WebDriver BiDi listeners attached (console + JS errors + network)")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("BiDi attach failed: %s", exc)
            self.bidi_enabled = False
            return False

    # ------------------------------------------------------------------
    def start(
        self,
        headless: bool = True,
        enable_bidi: bool = True,
        geckodriver_log: str | None = None,
        firefox_binary: str | None = None,
        width: int = 0,
        height: int = 0,
        user_agent: str = "",
    ) -> None:
        if self.driver is not None:
            return
        self.resolve()

        opts = FirefoxOptions()
        if headless:
            opts.add_argument("-headless")
        if enable_bidi:
            opts.enable_bidi = True
        if user_agent:
            opts.set_preference("general.useragent.override", user_agent)

        binary = firefox_binary or os.environ.get(_ENV_FIREFOX_BINARY, "").strip()
        if binary:
            opts.binary_location = binary

        profile_name = os.environ.get(_ENV_FIREFOX_PROFILE, "").strip()
        profile_dir  = os.environ.get(_ENV_FIREFOX_PROFILE_DIR, "").strip()
        if profile_dir:
            opts.profile = profile_dir
            logger.info("Firefox profile dir: %s", profile_dir)
        elif profile_name:
            opts.add_argument("-P")
            opts.add_argument(profile_name)
            logger.info("Firefox profile name: %s", profile_name)

        svc_kw: dict[str, Any] = {}
        if self.geckodriver_path:
            svc_kw["executable_path"] = self.geckodriver_path
        if geckodriver_log:
            svc_kw["log_output"] = geckodriver_log

        self.driver = webdriver.Firefox(service=FirefoxService(**svc_kw), options=opts)
        if width and height:
            self.driver.set_window_size(width, height)
        self.headless = headless
        if enable_bidi:
            self._attach_bidi()
        logger.info("Firefox started (headless=%s bidi=%s gd=%s)",
                    headless, self.bidi_enabled, self.geckodriver_source)

    def stop(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None
            self.bidi_enabled = False
        logger.info("Firefox stopped")

    # ------------------------------------------------------------------
    # Buffer accessors
    # ------------------------------------------------------------------

    def console_entries(self, level: str | None = None, since: str = "") -> list[dict]:
        with self._lock:
            rows = list(self._console)
        if level:
            rows = [r for r in rows if r.level == level]
        if since:
            rows = [r for r in rows if r.ts > since]
        return [r.to_dict() for r in rows]

    def js_errors(self, since: str = "") -> list[dict]:
        with self._lock:
            rows = list(self._js_errors)
        if since:
            rows = [r for r in rows if r.ts > since]
        return [r.to_dict() for r in rows]

    def network_entries(
        self,
        failed_only: bool = False,
        min_status: int = 0,
        resource_type: str = "",
        slow_ms: float = 0,
        since: str = "",
    ) -> list[dict]:
        with self._lock:
            rows = list(self._network)
        if since:
            rows = [r for r in rows if r.ts > since]
        if failed_only:
            rows = [r for r in rows if r.failed or r.status >= 400]
        if min_status:
            rows = [r for r in rows if r.status >= min_status or r.failed]
        if resource_type:
            rows = [r for r in rows if r.resource_type == resource_type]
        if slow_ms:
            rows = [r for r in rows if r.duration_ms >= slow_ms]
        return [r.to_dict() for r in rows]

    def clear(self) -> tuple[int, int, int]:
        with self._lock:
            nc, ne, nn = len(self._console), len(self._js_errors), len(self._network)
            self._console.clear()
            self._js_errors.clear()
            self._network.clear()
            self._pending.clear()
        return nc, ne, nn


# ---------------------------------------------------------------------------
# Lifespan + server
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastMCP):
    state = BrowserState()
    state.resolve()
    yield {"browser": state}
    state.stop()


mcp = FastMCP(
    name="mcp-server-webdriver",
    instructions=(
        "Web development assistant. Opens a Firefox browser and continuously captures "
        "JavaScript errors, console output, and failed network resources (broken CSS, "
        "JS, fonts, images) via WebDriver BiDi — no manual copy-pasting needed. "
        "Key workflow: browser_open → interact → devtools_report for full diagnosis, "
        "or use focused tools: devtools_js_errors, devtools_console, devtools_network_failed."
    ),
    lifespan=lifespan,
)


def _st(ctx: Context) -> BrowserState:
    return ctx.lifespan_context["browser"]  # type: ignore[return-value]


# ===========================================================================
# SESSION MANAGEMENT
# ===========================================================================

@mcp.tool(annotations={"readOnlyHint": False})
async def browser_open(
    url: Annotated[str, "URL to open (default: about:blank)"] = "about:blank",
    headless: Annotated[bool, "Headless mode (no visible window)"] = True,
    enable_bidi: Annotated[
        bool,
        "Enable WebDriver BiDi for DevTools capture (JS errors, console, network). "
        "Requires Firefox + geckodriver ≥ 0.34. Default True.",
    ] = True,
    geckodriver_log: Annotated[str, "Optional file path for geckodriver log"] = "",
    firefox_binary:  Annotated[str, "Optional path to a custom Firefox binary"] = "",
    width:  Annotated[int, "Viewport width in pixels. 0 = browser default. E.g. 390 for iPhone 14."] = 0,
    height: Annotated[int, "Viewport height in pixels. 0 = browser default. E.g. 844 for iPhone 14."] = 0,
    user_agent: Annotated[
        str,
        "Override the browser User-Agent string. Useful for mobile emulation so "
        "sites that sniff the UA serve their mobile layout. "
        "E.g. 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'",
    ] = "",
    ctx: Context = None,
) -> str:
    """
    Open URL in Firefox. Starts a new browser session if none is running.

    With enable_bidi=True (default) the session automatically captures:
    • All JavaScript exceptions (file, line, column, stack trace)
    • All console.* output (log / warn / error / info / debug)
    • All network requests with status codes and durations

    To test responsive / mobile layouts pass width + height (and optionally user_agent):
      width=390  height=844   — iPhone 14
      width=375  height=667   — iPhone SE
      width=360  height=800   — Samsung Galaxy S21
      width=768  height=1024  — iPad
      width=1280 height=800   — laptop

    geckodriver sources (priority order):
      1. GECKODRIVER_PATH env  →  2. apt install gecko-driver  →  3. webdriver-manager
    """
    url = _normalise_url(url)
    state = _st(ctx)
    state.start(
        headless=headless,
        enable_bidi=enable_bidi,
        geckodriver_log=geckodriver_log or None,
        firefox_binary=firefox_binary or None,
        width=width,
        height=height,
        user_agent=user_agent,
    )
    try:
        state.get_driver().get(url)
    except WebDriverException as exc:
        raise RuntimeError(f"Navigation failed: {exc}") from exc

    bidi_note = (
        " | BiDi active: JS errors + console + network are being captured"
        if state.bidi_enabled else
        " | BiDi unavailable — DevTools capture disabled"
    )
    viewport_note = f" | viewport: {width}×{height}" if width and height else ""
    return f"Opened: {url}{viewport_note}{bidi_note}"


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_close(ctx: Context = None) -> str:
    """Close the browser session. All buffered DevTools data is discarded."""
    state = _st(ctx)
    nc, ne, nn = state.clear()
    state.stop()
    return f"Closed. Discarded {nc} console, {ne} JS errors, {nn} network entries."


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def browser_status(ctx: Context = None) -> dict:
    """Session state, geckodriver info, BiDi status and buffer sizes."""
    state = _st(ctx)
    state.resolve()
    gd_bin = state.geckodriver_path or shutil.which("geckodriver") or "geckodriver"
    with state._lock:
        nc, ne, nn = len(state._console), len(state._js_errors), len(state._network)
    info: dict[str, Any] = {
        "session_active": state.driver is not None,
        "headless": state.headless,
        "bidi_enabled": state.bidi_enabled,
        "buffered": {"console": nc, "js_errors": ne, "network": nn},
        "geckodriver_source": state.geckodriver_source,
        "geckodriver_version": _geckodriver_version(gd_bin),
        "selenium_version": __import__("selenium").__version__,
    }
    if state.driver is not None:
        info["current_url"]   = state.driver.current_url
        info["current_title"] = state.driver.title
        size = state.driver.get_window_size()
        info["viewport"] = {"width": size["width"], "height": size["height"]}
    return info


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_set_viewport(
    width:  Annotated[int, "Viewport width in pixels"],
    height: Annotated[int, "Viewport height in pixels"],
    ctx: Context = None,
) -> str:
    """
    Resize the browser viewport to test responsive / mobile layouts.

    Call this at any point during a session to switch between breakpoints.

    Common presets:
      390×844   — iPhone 14
      375×667   — iPhone SE
      360×800   — Samsung Galaxy S21
      768×1024  — iPad
      1280×800  — laptop
      1920×1080 — desktop full-HD
    """
    _st(ctx).get_driver().set_window_size(width, height)
    return f"Viewport set to {width}×{height}."


# ===========================================================================
# DEVTOOLS — primary diagnostic tools
# ===========================================================================

@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_report(
    since: Annotated[str, "ISO 8601 timestamp — only entries after this. Empty = all."] = "",
    ctx: Context = None,
) -> dict:
    """
    MAIN DIAGNOSTIC TOOL — returns a complete DevTools report in one call:

    • js_errors   — all JavaScript exceptions with file, line, column, stack
    • console_errors — console.error() and console.warn() output
    • failed_resources — CSS / JS / images / fonts that returned 4xx/5xx or failed to load
    • slow_resources — requests that took longer than 2 s

    Use this after navigating to a page or after triggering a UI action.
    Equivalent to opening DevTools and checking the Console + Network tabs.
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")

    return {
        "js_errors": state.js_errors(since=since),
        "console_errors": state.console_entries(level="error", since=since) +
                          state.console_entries(level="warn",  since=since),
        "failed_resources": state.network_entries(failed_only=True, since=since),
        "slow_resources": state.network_entries(slow_ms=_DEFAULT_SLOW_MS, since=since),
    }


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_js_errors(
    since: Annotated[str, "ISO 8601 timestamp filter. Empty = all."] = "",
    ctx: Context = None,
) -> list[dict]:
    """
    Return all JavaScript exceptions captured since browser_open.

    Each entry contains:
      ts         — ISO 8601 timestamp
      type       — error type (e.g. 'TypeError', 'ReferenceError')
      text       — error message
      url        — source file URL
      line       — line number in source file
      column     — column number
      stack      — full stack trace

    This is the primary tool for finding which JS file and line causes a bug.
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")
    return state.js_errors(since=since)


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_console(
    level: Annotated[
        str,
        "Filter by level: 'log', 'info', 'warn', 'error', 'debug'. Empty = all.",
    ] = "",
    since: Annotated[str, "ISO 8601 timestamp filter. Empty = all."] = "",
    ctx: Context = None,
) -> list[dict]:
    """
    Return buffered browser console messages.

    Each entry: ts, level, text, url (source file), line.
    Covers console.log / warn / error / info / debug.
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")
    return state.console_entries(level=level or None, since=since)


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_network_failed(
    resource_type: Annotated[
        str,
        "Filter by type: 'stylesheet', 'script', 'image', 'font', 'fetch', 'xhr', "
        "or '' for all.",
    ] = "",
    since: Annotated[str, "ISO 8601 timestamp filter. Empty = all."] = "",
    ctx: Context = None,
) -> list[dict]:
    """
    Return network requests that FAILED — 4xx / 5xx status or DNS / connection errors.

    This is how the agent detects broken CSS files, missing JS bundles,
    unavailable fonts, or failed API calls that cause layout or functionality issues.

    Each entry: ts, method, url, type, status (0 = connection failed), duration_ms,
                failed (bool), error (error description).
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")
    return state.network_entries(failed_only=True, resource_type=resource_type, since=since)


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_network_all(
    resource_type: Annotated[str, "Type filter: stylesheet/script/image/font/fetch/xhr/''"] = "",
    min_status:    Annotated[int, "Minimum HTTP status to include (e.g. 400). 0 = all."] = 0,
    slow_ms:       Annotated[float, "Include only requests slower than this many ms. 0 = all."] = 0,
    since:         Annotated[str, "ISO 8601 timestamp filter. Empty = all."] = "",
    limit:         Annotated[int, "Max entries to return (most recent first). 0 = all."] = 0,
    ctx: Context = None,
) -> list[dict]:
    """
    Return all captured network requests with filtering options.

    Useful for auditing which CSS/JS files are loaded, checking API response
    times, or finding resources that are unexpectedly missing from the page.
    Use limit= to avoid overwhelming the context window on busy pages.
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")
    entries = state.network_entries(
        resource_type=resource_type, min_status=min_status, slow_ms=slow_ms, since=since
    )
    if limit > 0:
        entries = entries[-limit:]
    return entries


@mcp.tool(annotations={"readOnlyHint": False})
async def devtools_clear(ctx: Context = None) -> str:
    """
    Clear all buffered console, JS error, and network entries.

    Call this before navigating to a new page to get a clean baseline,
    so subsequent devtools_* calls only reflect the new page's activity.
    """
    nc, ne, nn = _st(ctx).clear()
    return f"Cleared: {nc} console entries, {ne} JS errors, {nn} network entries."


@mcp.tool(annotations={"readOnlyHint": False})
async def devtools_enable_bidi(ctx: Context = None) -> str:
    """
    Attach (or re-attach) WebDriver BiDi listeners to the running session.

    Use if the session was opened with enable_bidi=False, or if listeners
    were lost after a page crash.
    """
    state = _st(ctx)
    if state.driver is None:
        raise RuntimeError("No browser session open — call browser_open first.")
    ok = state._attach_bidi()
    if not ok:
        raise RuntimeError(
            "BiDi attachment failed. Ensure geckodriver ≥ 0.34 and Firefox release channel."
        )
    return "BiDi active: JavaScript errors, console, and network events are now captured."


# ===========================================================================
# VISUAL DIAGNOSIS — layout / CSS
# ===========================================================================

@mcp.tool(annotations={"readOnlyHint": True})
async def browser_screenshot(
    selector: Annotated[
        str,
        "CSS selector of element to capture. Empty = full page screenshot.",
    ] = "",
    ctx: Context = None,
) -> Image:
    """
    Take a PNG screenshot for visual / layout / CSS diagnosis.

    Capture the full page to spot broken layout, or pass a CSS selector
    to isolate a specific component (header, nav, modal…).
    The agent can use this to identify misaligned elements, invisible text,
    broken flex/grid layouts, or unstyled components.
    """
    driver = _st(ctx).get_driver()
    if selector:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            return Image(data=el.screenshot_as_png, format="png")
        except NoSuchElementException:
            raise RuntimeError(f"Element not found: {selector!r}")
    return Image(data=driver.get_screenshot_as_png(), format="png")


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_computed_css(
    selector:   Annotated[str, "CSS selector of the element to inspect"],
    properties: Annotated[
        str,
        "Comma-separated CSS property names to read, e.g. "
        "'display,visibility,color,font-family,width,height,margin,padding'. "
        "Empty = a useful default set.",
    ] = "",
    ctx: Context = None,
) -> dict:
    """
    Return computed (final applied) CSS properties of an element.

    Use this to understand why an element looks wrong:
    • Is it display:none or visibility:hidden?
    • What color / font is actually applied?
    • Is a CSS variable resolving correctly?
    • Are grid/flex dimensions what you expect?

    Returns a dict of {property: computed_value}.
    """
    driver = _st(ctx).get_driver()
    try:
        driver.find_element(By.CSS_SELECTOR, selector)
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")

    if properties:
        prop_list = [p.strip() for p in properties.split(",") if p.strip()]
    else:
        prop_list = [
            "display", "visibility", "opacity", "position",
            "width", "height", "max-width", "max-height",
            "margin", "padding", "box-sizing",
            "color", "background-color", "background-image",
            "font-family", "font-size", "font-weight", "line-height",
            "flex", "flex-direction", "grid-template-columns",
            "overflow", "z-index", "border",
        ]

    js = """
const el = document.querySelector(arguments[0]);
if (!el) return null;
const cs = window.getComputedStyle(el);
const result = {};
for (const p of arguments[1]) {
    result[p] = cs.getPropertyValue(p) || '';
}
return result;
"""
    result = driver.execute_script(js, selector, prop_list)
    if result is None:
        raise RuntimeError(f"Element not found in DOM: {selector!r}")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_element_info(
    selector: Annotated[str, "CSS selector of the element to inspect"],
    ctx: Context = None,
) -> dict:
    """
    Return detailed info about a DOM element for debugging:

    • outerHTML    — the element's markup (first 4000 chars)
    • bounding_box — position and size on screen (x, y, width, height)
    • visible      — whether the element is actually visible
    • in_viewport  — whether it's within the visible scroll area
    • attributes   — all HTML attributes
    • aria          — ARIA role, name, label, disabled, hidden
    • child_count  — number of child elements
    • text_content — visible text (first 500 chars)

    Use this to diagnose elements that should be visible but aren't,
    or to understand the structure around a broken component.
    """
    driver = _st(ctx).get_driver()
    js = """
const el = document.querySelector(arguments[0]);
if (!el) return null;
const r   = el.getBoundingClientRect();
const cs  = window.getComputedStyle(el);
const vis = cs.display !== 'none'
         && cs.visibility !== 'hidden'
         && parseFloat(cs.opacity) > 0
         && r.width > 0 && r.height > 0;
const attrs = {};
for (const a of el.attributes) { attrs[a.name] = a.value; }
return {
  outerHTML:    el.outerHTML.slice(0, 4000),
  bounding_box: {x: Math.round(r.x), y: Math.round(r.y),
                  width: Math.round(r.width), height: Math.round(r.height)},
  visible:      vis,
  in_viewport:  r.top >= 0 && r.left >= 0
                && r.bottom <= window.innerHeight
                && r.right  <= window.innerWidth,
  attributes:   attrs,
  aria: {
    role:     el.getAttribute('role') || el.tagName.toLowerCase(),
    label:    el.getAttribute('aria-label') || '',
    disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
    hidden:   el.getAttribute('aria-hidden') === 'true',
  },
  child_count:   el.children.length,
  text_content:  el.textContent.trim().slice(0, 500),
};
"""
    result = driver.execute_script(js, selector)
    if result is None:
        raise RuntimeError(f"Element not found: {selector!r}")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_css_variables(
    prefix: Annotated[
        str,
        "Only return variables whose name starts with this prefix, e.g. '--color' or '--'. "
        "Empty = all custom properties on :root.",
    ] = "--",
    ctx: Context = None,
) -> dict:
    """
    Return CSS custom properties (variables) defined on :root.

    Helps diagnose theming issues: missing tokens, wrong colour values,
    variables that weren't loaded because a CSS file failed to fetch.
    """
    js = """
const styles = window.getComputedStyle(document.documentElement);
const result = {};
const prefix = arguments[0] || '--';
for (const sheet of document.styleSheets) {
  try {
    for (const rule of sheet.cssRules || []) {
      if (rule.style) {
        for (let i = 0; i < rule.style.length; i++) {
          const name = rule.style[i];
          if (name.startsWith(prefix)) {
            result[name] = styles.getPropertyValue(name).trim();
          }
        }
      }
    }
  } catch(e) { /* cross-origin sheet, skip */ }
}
return result;
"""
    return _st(ctx).get_driver().execute_script(js, prefix) or {}


@mcp.tool(annotations={"readOnlyHint": True})
async def devtools_performance(
    include_resources: Annotated[
        bool,
        "Include per-resource timing entries (stylesheets, scripts, images…). "
        "Can be large on busy pages.",
    ] = False,
    ctx: Context = None,
) -> dict:
    """
    Return page performance timing from the browser Navigation Timing API.

    navigation — key milestones for the current page load:
      ttfb_ms        — Time to First Byte (server latency)
      dom_loaded_ms  — DOMContentLoaded (page parsed, defer scripts done)
      load_ms        — full load event (all resources fetched)
      dns_ms         — DNS lookup duration
      connect_ms     — TCP/TLS connection duration
      request_ms     — time from request sent to last response byte
      transfer_size  — response body size in bytes

    resources (when include_resources=True) — per-asset breakdown:
      name, type, duration_ms, transfer_size, start_time

    Use this to identify slow pages, expensive assets, or server latency.
    Complements devtools_network_all which captures requests via BiDi listeners.
    """
    js = """
const t = performance.getEntriesByType('navigation')[0] || {};
const nav = {
  ttfb_ms:       Math.round((t.responseStart  - t.requestStart)       || 0),
  dom_loaded_ms: Math.round((t.domContentLoadedEventEnd - t.startTime) || 0),
  load_ms:       Math.round((t.loadEventEnd   - t.startTime)           || 0),
  dns_ms:        Math.round((t.domainLookupEnd - t.domainLookupStart)  || 0),
  connect_ms:    Math.round((t.connectEnd      - t.connectStart)       || 0),
  request_ms:    Math.round((t.responseEnd     - t.requestStart)       || 0),
  transfer_size: t.transferSize || 0,
  url: t.name || location.href,
};
const result = { navigation: nav };
if (arguments[0]) {
  result.resources = performance.getEntriesByType('resource').map(r => ({
    name:          r.name,
    type:          r.initiatorType,
    duration_ms:   Math.round(r.duration),
    transfer_size: r.transferSize || 0,
    start_ms:      Math.round(r.startTime),
  }));
}
return result;
"""
    return _st(ctx).get_driver().execute_script(js, include_resources)


# ===========================================================================
# STANDARD BROWSER INTERACTION
# ===========================================================================

@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_title(ctx: Context = None) -> str:
    """Return the <title> of the current page."""
    return _st(ctx).get_driver().title


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_url(ctx: Context = None) -> str:
    """Return the current URL."""
    return _st(ctx).get_driver().current_url


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_source(ctx: Context = None) -> str:
    """Return the full HTML source of the current page."""
    return _st(ctx).get_driver().page_source


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_text(
    selector: Annotated[str, "CSS selector (empty = whole <body>)"] = "",
    ctx: Context = None,
) -> str:
    """Get visible text content of the page or a specific element."""
    driver = _st(ctx).get_driver()
    if selector:
        try:
            return driver.find_element(By.CSS_SELECTOR, selector).text
        except NoSuchElementException:
            raise RuntimeError(f"Element not found: {selector!r}")
    return driver.find_element(By.TAG_NAME, "body").text


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_attribute(
    selector:  Annotated[str, "CSS selector"],
    attribute: Annotated[str, "Attribute name (e.g. 'href', 'src', 'value')"],
    ctx: Context = None,
) -> str:
    """Return the value of an HTML attribute on an element."""
    driver = _st(ctx).get_driver()
    try:
        val = driver.find_element(By.CSS_SELECTOR, selector).get_attribute(attribute)
        return val if val is not None else ""
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_click(
    selector: Annotated[str, "CSS selector of element to click"],
    ctx: Context = None,
) -> str:
    """Click an element."""
    driver = _st(ctx).get_driver()
    try:
        driver.find_element(By.CSS_SELECTOR, selector).click()
        return f"Clicked: {selector!r}"
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")
    except WebDriverException as exc:
        raise RuntimeError(f"Click failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_fill(
    selector:    Annotated[str, "CSS selector of input field"],
    value:       Annotated[str, "Text to type"],
    clear_first: Annotated[bool, "Clear field before typing"] = True,
    ctx: Context = None,
) -> str:
    """Type text into an input field."""
    driver = _st(ctx).get_driver()
    try:
        el = driver.find_element(By.CSS_SELECTOR, selector)
        if clear_first:
            el.clear()
        el.send_keys(value)
        return f"Filled {selector!r}"
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")
    except WebDriverException as exc:
        raise RuntimeError(f"Fill failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_upload_file(
    selector: Annotated[str, "CSS selector of the <input type='file'> element"],
    path:     Annotated[str, "Absolute path to the local file to upload"],
    ctx: Context = None,
) -> str:
    """
    Upload a local file through a <input type="file"> element.

    Works even when the file input is visually hidden (the common pattern of
    hiding the native input and styling a custom button over it).
    The file must exist on the machine running the MCP server.
    """
    driver = _st(ctx).get_driver()
    if not os.path.isfile(path):
        raise RuntimeError(f"File not found: {path!r}")
    try:
        el = driver.find_element(By.CSS_SELECTOR, selector)
        driver.execute_script(
            "arguments[0].style.display='block'; arguments[0].style.visibility='visible';", el
        )
        el.send_keys(path)
        return f"Uploaded {path!r} via {selector!r}."
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")
    except WebDriverException as exc:
        raise RuntimeError(f"Upload failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_select(
    selector: Annotated[str, "CSS selector of <select> element"],
    value:    Annotated[str, "Option: visible text → value attribute → index"],
    ctx: Context = None,
) -> str:
    """Select an <option> in a <select> dropdown."""
    driver = _st(ctx).get_driver()
    try:
        sel = Select(driver.find_element(By.CSS_SELECTOR, selector))
        try:
            sel.select_by_visible_text(value)
        except Exception:  # noqa: BLE001
            try:
                sel.select_by_value(value)
            except Exception:  # noqa: BLE001
                sel.select_by_index(int(value))
        return f"Selected {value!r} in {selector!r}"
    except NoSuchElementException:
        raise RuntimeError(f"<select> not found: {selector!r}")
    except WebDriverException as exc:
        raise RuntimeError(f"Select failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def browser_execute_js(
    script: Annotated[str, "JavaScript to run in the page context"],
    ctx: Context = None,
) -> str:
    """Execute JavaScript and return the result as JSON (falls back to str for non-serialisable values)."""
    try:
        result = _st(ctx).get_driver().execute_script(script)
        try:
            return json.dumps(result)
        except (TypeError, ValueError):
            return str(result)
    except WebDriverException as exc:
        raise RuntimeError(f"JS failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_wait(
    selector:  Annotated[str, "CSS selector to wait for"],
    timeout:   Annotated[float, "Max seconds to wait"] = 10.0,
    condition: Annotated[
        str,
        "What to wait for: 'visible' (default), 'clickable', 'present', or 'text:<string>'",
    ] = "visible",
    ctx: Context = None,
) -> str:
    """Wait until an element satisfies a condition: visible, clickable, present in DOM, or contains text."""
    driver = _st(ctx).get_driver()
    loc = (By.CSS_SELECTOR, selector)
    try:
        if condition == "clickable":
            WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(loc))
            return f"Element {selector!r} is clickable."
        elif condition == "present":
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located(loc))
            return f"Element {selector!r} is present in DOM."
        elif condition.startswith("text:"):
            text = condition[5:]
            WebDriverWait(driver, timeout).until(EC.text_to_be_present_in_element(loc, text))
            return f"Element {selector!r} contains text {text!r}."
        else:  # "visible" (default)
            WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(loc))
            return f"Element {selector!r} is visible."
    except TimeoutException:
        raise RuntimeError(f"Timeout after {timeout}s waiting for {selector!r} ({condition})")


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_navigate(
    url: Annotated[str, "URL to navigate to (bare hostnames get https:// prepended)"],
    ctx: Context = None,
) -> str:
    """Navigate to a URL in the existing session (keeps BiDi listeners active)."""
    url = _normalise_url(url)
    try:
        _st(ctx).get_driver().get(url)
        return f"Navigated to: {url}"
    except WebDriverException as exc:
        raise RuntimeError(f"Navigation failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_back(ctx: Context = None) -> str:
    """Navigate back."""
    _st(ctx).get_driver().back()
    return "Navigated back."


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_forward(ctx: Context = None) -> str:
    """Navigate forward."""
    _st(ctx).get_driver().forward()
    return "Navigated forward."


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_refresh(ctx: Context = None) -> str:
    """Reload the current page."""
    _st(ctx).get_driver().refresh()
    return "Page refreshed."


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_switch_frame(
    selector: Annotated[str, "CSS selector of <iframe>, or '' for main document"] = "",
    ctx: Context = None,
) -> str:
    """Switch into an <iframe> or back to the main document."""
    driver = _st(ctx).get_driver()
    if not selector:
        driver.switch_to.default_content()
        return "Switched to main document."
    try:
        driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, selector))
        return f"Switched into frame: {selector!r}"
    except NoSuchElementException:
        raise RuntimeError(f"Frame not found: {selector!r}")


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_scroll(
    selector: Annotated[str, "CSS selector — scroll this element into view. Empty = scroll the page."] = "",
    x:        Annotated[int, "Horizontal scroll position in px (page scroll, ignored when selector given)"] = 0,
    y:        Annotated[int, "Vertical scroll position in px (page scroll, ignored when selector given)"] = 0,
    by:       Annotated[bool, "If true, scroll BY (x, y) relative to current position instead of TO (x, y)"] = False,
    ctx: Context = None,
) -> str:
    """
    Scroll the page or scroll an element into view.

    • Pass a CSS selector to scroll that element into view (smoothly).
    • Pass x/y to jump the page to absolute scroll coordinates.
    • Pass by=true with x/y to scroll relative to the current position (e.g. y=500 scrolls down 500 px).
    • No arguments scrolls to the top of the page.
    """
    driver = _st(ctx).get_driver()
    if selector:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth', block:'center'});", el)
            return f"Scrolled {selector!r} into view."
        except NoSuchElementException:
            raise RuntimeError(f"Element not found: {selector!r}")
    elif by:
        driver.execute_script("window.scrollBy(arguments[0], arguments[1]);", x, y)
        return f"Scrolled by ({x}, {y})."
    else:
        driver.execute_script("window.scrollTo(arguments[0], arguments[1]);", x, y)
        return f"Scrolled to ({x}, {y})."


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_press_key(
    key:      Annotated[str, "Key name: enter, tab, escape, space, backspace, delete, "
                             "home, end, pageup, pagedown, arrowup/down/left/right, f1-f12"],
    selector: Annotated[str, "CSS selector of element to send the key to. Empty = active element."] = "",
    ctx: Context = None,
) -> str:
    """
    Send a keyboard key press to an element or the currently focused element.

    Useful for submitting forms (enter), moving focus (tab), closing modals (escape),
    or triggering keyboard-driven UI components.
    """
    driver = _st(ctx).get_driver()
    key_value = _KEY_MAP.get(key.lower())
    if key_value is None:
        raise RuntimeError(
            f"Unknown key {key!r}. Supported: {', '.join(sorted(_KEY_MAP))}"
        )
    if selector:
        try:
            driver.find_element(By.CSS_SELECTOR, selector).send_keys(key_value)
        except NoSuchElementException:
            raise RuntimeError(f"Element not found: {selector!r}")
    else:
        driver.switch_to.active_element.send_keys(key_value)
    return f"Pressed {key!r}" + (f" on {selector!r}" if selector else " on active element.")


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_hover(
    selector: Annotated[str, "CSS selector of element to hover over"],
    ctx: Context = None,
) -> str:
    """
    Move the mouse over an element (hover).

    Triggers CSS :hover states and any mouseenter/mouseover event listeners —
    essential for dropdown menus, tooltips, and hover-activated controls.
    """
    driver = _st(ctx).get_driver()
    try:
        el = driver.find_element(By.CSS_SELECTOR, selector)
        ActionChains(driver).move_to_element(el).perform()
        return f"Hovering over {selector!r}."
    except NoSuchElementException:
        raise RuntimeError(f"Element not found: {selector!r}")
    except WebDriverException as exc:
        raise RuntimeError(f"Hover failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_find_elements(
    selector: Annotated[str, "CSS selector"],
    limit:    Annotated[int, "Max elements to return"] = 50,
    ctx: Context = None,
) -> list[dict]:
    """
    Return a list of all elements matching a CSS selector.

    Each entry contains: index, tag, text (first 200 chars), id, class,
    href, src, value, type, name, visible, and aria-label.

    Useful for enumerating links, buttons, inputs, or any repeated component.
    """
    driver = _st(ctx).get_driver()
    elements = driver.find_elements(By.CSS_SELECTOR, selector)[:limit]
    result = []
    for i, el in enumerate(elements):
        try:
            visible = el.is_displayed()
        except WebDriverException:
            visible = False
        result.append({
            "index":      i,
            "tag":        el.tag_name,
            "text":       (el.text or "")[:200],
            "id":         el.get_attribute("id") or "",
            "class":      el.get_attribute("class") or "",
            "href":       el.get_attribute("href") or "",
            "src":        el.get_attribute("src") or "",
            "value":      el.get_attribute("value") or "",
            "type":       el.get_attribute("type") or "",
            "name":       el.get_attribute("name") or "",
            "aria_label": el.get_attribute("aria-label") or "",
            "visible":    visible,
        })
    return result


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_accept_dialog(
    ctx: Context = None,
) -> str:
    """
    Accept (click OK on) a JavaScript dialog: alert(), confirm(), or prompt().

    Call this when a browser action triggers a dialog that blocks further interaction.
    """
    driver = _st(ctx).get_driver()
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return f"Accepted dialog: {text!r}"
    except NoAlertPresentException:
        raise RuntimeError("No dialog is currently open.")


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_dismiss_dialog(
    ctx: Context = None,
) -> str:
    """
    Dismiss (click Cancel on) a JavaScript confirm() or prompt() dialog,
    or close an alert().
    """
    driver = _st(ctx).get_driver()
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.dismiss()
        return f"Dismissed dialog: {text!r}"
    except NoAlertPresentException:
        raise RuntimeError("No dialog is currently open.")


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_cookies(
    ctx: Context = None,
) -> list[dict]:
    """
    Return all cookies for the current page as a list of dicts.

    Each entry: name, value, domain, path, secure, httpOnly, expiry.
    Useful for inspecting authentication state or session tokens.
    """
    return _st(ctx).get_driver().get_cookies()


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_set_cookie(
    name:   Annotated[str, "Cookie name"],
    value:  Annotated[str, "Cookie value"],
    domain: Annotated[str, "Cookie domain (default: current page domain)"] = "",
    path:   Annotated[str, "Cookie path"] = "/",
    secure: Annotated[bool, "Secure flag"] = False,
    ctx: Context = None,
) -> str:
    """
    Set a cookie on the current page.

    Useful for injecting auth tokens or session cookies without going through
    a login flow. The browser must already be on a page in the target domain.
    """
    driver = _st(ctx).get_driver()
    cookie: dict[str, Any] = {"name": name, "value": value, "path": path, "secure": secure}
    if domain:
        cookie["domain"] = domain
    driver.add_cookie(cookie)
    return f"Set cookie {name!r}."


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_get_storage(
    storage: Annotated[str, "'local' for localStorage (default) or 'session' for sessionStorage"] = "local",
    key:     Annotated[str, "Specific key to read. Empty = return all entries as a dict."] = "",
    ctx: Context = None,
) -> dict:
    """
    Read from localStorage or sessionStorage.

    Returns all key→value pairs when no key is given, or {key: value}
    for a single key (value is null if the key does not exist).

    Useful for inspecting auth tokens, cached API responses, feature flags,
    or any client-side state stored in web storage rather than cookies.
    """
    driver = _st(ctx).get_driver()
    store = "sessionStorage" if storage.lower().startswith("s") else "localStorage"
    if key:
        val = driver.execute_script(f"return {store}.getItem(arguments[0]);", key)
        return {key: val}
    js = f"""
const s = {store}, out = {{}};
for (let i = 0; i < s.length; i++) {{ const k = s.key(i); out[k] = s.getItem(k); }}
return out;
"""
    return driver.execute_script(js) or {}


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_set_storage(
    key:     Annotated[str, "Storage key"],
    value:   Annotated[str, "Value to store"],
    storage: Annotated[str, "'local' for localStorage (default) or 'session' for sessionStorage"] = "local",
    ctx: Context = None,
) -> str:
    """
    Write a key→value pair to localStorage or sessionStorage.

    Useful for injecting auth tokens, feature flags, or test fixtures
    directly into web storage without going through a login or setup flow.
    """
    driver = _st(ctx).get_driver()
    store = "sessionStorage" if storage.lower().startswith("s") else "localStorage"
    driver.execute_script(f"{store}.setItem(arguments[0], arguments[1]);", key, value)
    return f"Set {store}[{key!r}]."


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_clear_storage(
    storage: Annotated[str, "'local' for localStorage (default) or 'session' for sessionStorage"] = "local",
    key:     Annotated[str, "Specific key to remove. Empty = clear all entries."] = "",
    ctx: Context = None,
) -> str:
    """
    Remove one key or clear all entries from localStorage or sessionStorage.
    """
    driver = _st(ctx).get_driver()
    store = "sessionStorage" if storage.lower().startswith("s") else "localStorage"
    if key:
        driver.execute_script(f"{store}.removeItem(arguments[0]);", key)
        return f"Removed {store}[{key!r}]."
    driver.execute_script(f"{store}.clear();")
    return f"Cleared {store}."


# ===========================================================================

_HELP = """\
mcp-server-webdriver — MCP server for AI-assisted browser automation

USAGE
  mcp-server-webdriver [OPTIONS]   Start the MCP server (stdio transport)
  mcp-server-webdriver --help      Show this message

OPTIONS
  -P <profile>       Start Firefox with a named profile
  --profile <path>   Start Firefox with a profile directory at <path>

DESCRIPTION
  Exposes Firefox browser automation as MCP tools so an AI assistant can
  inspect JavaScript errors, console output, failed network resources,
  take screenshots, read DOM/CSS, and interact with web pages — without
  the user manually copy-pasting DevTools output.

  Communication is over stdin/stdout using the MCP protocol (JSON-RPC).
  Add it to your MCP client config and it will be launched automatically.

TOOLS (43)
  Session management:
    browser_open            Open Firefox (URL optional, default about:blank)
    browser_close           Close the browser session
    browser_status          Show session state and geckodriver info
    browser_set_viewport    Resize viewport (width×height) for responsive testing

  Navigation & interaction:
    browser_navigate        Navigate to a URL (bare hostnames get https://)
    browser_back / forward  History navigation
    browser_refresh         Reload the current page
    browser_screenshot      Capture a full-page or element screenshot
    browser_click           Click an element (CSS selector)
    browser_fill            Type text into an input (clears first by default)
    browser_upload_file     Upload a local file via <input type="file">
    browser_select          Choose a <select> option
    browser_execute_js      Run JavaScript — returns JSON
    browser_wait            Wait for visible/clickable/present/text:<str>
    browser_scroll          Scroll page to coords, by offset, or element into view
    browser_press_key       Send enter/tab/escape/arrow/f-keys to an element
    browser_hover           Hover the mouse over an element (:hover / tooltips)
    browser_switch_frame    Switch into an <iframe>

  Page inspection:
    browser_get_title       Page title
    browser_get_url         Current URL
    browser_get_source      Full page HTML source
    browser_get_text        Text content of an element
    browser_get_attribute   Attribute value of an element
    browser_find_elements   List all elements matching a CSS selector

  Dialogs & cookies:
    browser_accept_dialog   Accept a JS alert() / confirm() / prompt()
    browser_dismiss_dialog  Dismiss a JS confirm() / prompt()
    browser_get_cookies     Read all cookies for the current page
    browser_set_cookie      Inject a cookie (auth, session tokens)

  Web storage:
    browser_get_storage     Read localStorage / sessionStorage (all or one key)
    browser_set_storage     Write a key→value to localStorage / sessionStorage
    browser_clear_storage   Remove one key or clear all localStorage / sessionStorage

  DevTools (require BiDi — Firefox + geckodriver ≥ 0.34):
    devtools_report         Full diagnostics: JS errors + console + network
    devtools_js_errors      JavaScript exceptions only
    devtools_console        Console output (log/warn/error/info/debug)
    devtools_network_failed Failed/slow resources (4xx, 5xx, DNS errors)
    devtools_network_all    All captured network requests (supports limit=)
    devtools_clear          Clear buffered DevTools data
    devtools_enable_bidi    Attach BiDi listeners to a running session
    devtools_computed_css   Computed CSS properties of an element
    devtools_element_info   Outer HTML of an element
    devtools_css_variables  CSS custom properties (--var) in scope
    devtools_performance    Navigation timing + optional per-resource breakdown

ENVIRONMENT VARIABLES
  GECKODRIVER_PATH          Absolute path to geckodriver binary
  GECKODRIVER_AUTO_INSTALL  Set to "false" to disable webdriver-manager fallback
  FIREFOX_BINARY            Path to a custom Firefox binary
  FIREFOX_PROFILE           Named Firefox profile (same as -P)
  FIREFOX_PROFILE_DIR       Profile directory path (same as --profile)

GECKODRIVER RESOLUTION (first match wins)
  1. GECKODRIVER_PATH env variable
  2. System PATH  (apt install gecko-driver from {repo})
  3. webdriver-manager auto-download (if GECKODRIVER_AUTO_INSTALL != false)

INSTALL GECKODRIVER (Debian/Ubuntu)
  sudo curl -fsSL {repo}/KEY.gpg -o /usr/share/keyrings/vitexsoftware-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/vitexsoftware-archive-keyring.gpg] {repo} trixie main" | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
  sudo apt update && sudo apt install gecko-driver

MCP CLIENT CONFIG EXAMPLE (claude_desktop_config.json)
  {{
    "mcpServers": {{
      "webdriver": {{
        "command": "mcp-server-webdriver",
        "args": ["-P", "myprofile"]
      }}
    }}
  }}
""".format(repo=_REPO_URL)


def main() -> None:
    import sys
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(_HELP, end="")
        raise SystemExit(0)

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

    mcp.run()


if __name__ == "__main__":
    main()
