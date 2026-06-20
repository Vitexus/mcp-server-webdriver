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
  echo "deb http://repo.vitexsoftware.com trixie main" \\
    | sudo tee /etc/apt/sources.list.d/vitexsoftware.list
  sudo apt update && sudo apt install gecko-driver
"""

from __future__ import annotations

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
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
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
_ENV_GECKODRIVER_PATH = "GECKODRIVER_PATH"
_ENV_AUTO_INSTALL     = "GECKODRIVER_AUTO_INSTALL"
_ENV_FIREFOX_BINARY   = "FIREFOX_BINARY"
_REPO_URL             = "http://repo.vitexsoftware.com"
_REPO_DISTRO          = "trixie"
_REPO_PKG             = "gecko-driver"

# Resources considered "slow" by default (ms)
_DEFAULT_SLOW_MS = 2000

# Resource types that matter for CSS/layout breakage
_LAYOUT_RESOURCE_TYPES = {"stylesheet", "font", "image", "script", "fetch", "xhr"}


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
        return None, f"system PATH → {system_path}"

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
            net = self.driver.network

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

            # Network events (BiDi network module)
            try:
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
    ) -> None:
        if self.driver is not None:
            return
        self.resolve()

        opts = FirefoxOptions()
        if headless:
            opts.add_argument("-headless")
        if enable_bidi:
            opts.enable_bidi = True

        binary = firefox_binary or os.environ.get(_ENV_FIREFOX_BINARY, "").strip()
        if binary:
            opts.binary_location = binary

        svc_kw: dict[str, Any] = {}
        if self.geckodriver_path:
            svc_kw["executable_path"] = self.geckodriver_path
        if geckodriver_log:
            svc_kw["log_output"] = geckodriver_log

        self.driver = webdriver.Firefox(service=FirefoxService(**svc_kw), options=opts)
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
    return ctx.state["browser"]  # type: ignore[return-value]


# ===========================================================================
# SESSION MANAGEMENT
# ===========================================================================

@mcp.tool(annotations={"readOnlyHint": False})
async def browser_open(
    url: Annotated[str, "URL to open"],
    headless: Annotated[bool, "Headless mode (no visible window)"] = True,
    enable_bidi: Annotated[
        bool,
        "Enable WebDriver BiDi for DevTools capture (JS errors, console, network). "
        "Requires Firefox + geckodriver ≥ 0.34. Default True.",
    ] = True,
    geckodriver_log: Annotated[str, "Optional file path for geckodriver log"] = "",
    firefox_binary:  Annotated[str, "Optional path to a custom Firefox binary"] = "",
    ctx: Context = None,
) -> str:
    """
    Open URL in Firefox. Starts a new browser session if none is running.

    With enable_bidi=True (default) the session automatically captures:
    • All JavaScript exceptions (file, line, column, stack trace)
    • All console.* output (log / warn / error / info / debug)
    • All network requests with status codes and durations

    geckodriver sources (priority order):
      1. GECKODRIVER_PATH env  →  2. apt install gecko-driver  →  3. webdriver-manager
    """
    state = _st(ctx)
    state.start(
        headless=headless,
        enable_bidi=enable_bidi,
        geckodriver_log=geckodriver_log or None,
        firefox_binary=firefox_binary or None,
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
    return f"Opened: {url}{bidi_note}"


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
    return info


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
    ctx: Context = None,
) -> list[dict]:
    """
    Return all captured network requests with filtering options.

    Useful for auditing which CSS/JS files are loaded, checking API response
    times, or finding resources that are unexpectedly missing from the page.
    """
    state = _st(ctx)
    if not state.bidi_enabled:
        raise RuntimeError("BiDi not active — open browser with enable_bidi=True")
    return state.network_entries(
        resource_type=resource_type, min_status=min_status, slow_ms=slow_ms, since=since
    )


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
    """Execute JavaScript and return the result as a string."""
    try:
        return str(_st(ctx).get_driver().execute_script(script))
    except WebDriverException as exc:
        raise RuntimeError(f"JS failed: {exc}") from exc


@mcp.tool(annotations={"readOnlyHint": True})
async def browser_wait(
    selector: Annotated[str, "CSS selector to wait for"],
    timeout:  Annotated[float, "Max seconds to wait"] = 10.0,
    ctx: Context = None,
) -> str:
    """Wait until an element is visible on the page."""
    driver = _st(ctx).get_driver()
    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
        )
        return f"Element {selector!r} is visible."
    except TimeoutException:
        raise RuntimeError(f"Timeout after {timeout}s waiting for {selector!r}")


@mcp.tool(annotations={"readOnlyHint": False})
async def browser_navigate(
    url: Annotated[str, "URL to navigate to"],
    ctx: Context = None,
) -> str:
    """Navigate to a URL in the existing session (keeps BiDi listeners active)."""
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


# ===========================================================================

if __name__ == "__main__":
    mcp.run()
