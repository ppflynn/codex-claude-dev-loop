"""Codex Claude Dev Loop - desktop application entry point.

This module is the executable entry point for the Windows desktop build.
It is responsible for:

* Resolving a writable user data directory under
  ``%LOCALAPPDATA%\\CodexClaudeDevLoop``.
* Configuring structured file logging under that directory.
* Probing the host for Git, PowerShell, Claude CLI and Codex CLI so the
  user gets a clear "missing dependency" message before the window opens.
* Redirecting :mod:`gui.server` state writes to the user data directory.
* Starting the backend HTTP server on an automatically chosen port.
* Opening a native desktop window (pywebview) that embeds the existing
  web console. When pywebview is unavailable it falls back to the user's
  default browser so source-mode runs still work.

Source-mode invocations continue to use the in-repo ``.gui`` directory
unless ``CCDL_STATE_DIR`` or ``--state-dir`` is provided.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

APP_NAME = "CodexClaudeDevLoop"
APP_TITLE = "Codex Claude Dev Loop"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900
MIN_WIDTH = 960
MIN_HEIGHT = 600
DEFAULT_PORT = 8765
STARTUP_POLL_TIMEOUT = 20.0
STARTUP_POLL_INTERVAL = 0.2


def _resource_root() -> Path:
    """Return the directory containing bundled resources.

    Source mode: repository root (parent of this file).
    Frozen mode: PyInstaller bundle root via ``sys._MEIPASS``.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_user_data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Return the writable per-user application data directory.

    Defaults to ``%LOCALAPPDATA%\\CodexClaudeDevLoop``. Source-mode users
    can override via ``CCDL_USER_DATA_DIR``; the desktop app accepts an
    explicit ``override`` to support the ``--user-data-dir`` CLI flag.
    """
    if override:
        return Path(override).expanduser().resolve()
    env_value = os.environ.get("CCDL_USER_DATA_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    # Fallback for non-Windows or stripped environments (dev/test only).
    return Path.home() / ".local" / "share" / APP_NAME


def get_state_dir(user_data_dir: Path) -> Path:
    """Return the application state directory (``<user_data>/.gui``)."""
    return user_data_dir / ".gui"


def get_logs_dir(user_data_dir: Path) -> Path:
    """Return the desktop app log directory (``<user_data>/logs``)."""
    return user_data_dir / "logs"


def setup_logging(logs_dir: Path, *, verbose: bool = False) -> logging.Logger:
    """Configure file + console logging for the desktop app.

    The file handler rotates so an app that runs many sessions does not
    grow its log unbounded. Returns the configured logger so callers can
    attach extra handlers if they need to.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ccdl.desktop")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    # Idempotent: re-running setup (e.g. tests) should not stack handlers.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    file_handler = RotatingFileHandler(
        logs_dir / "desktop.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("[desktop] %(levelname)s %(message)s"))
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console_handler)
    return logger


# ---------------------------------------------------------------------------
# Dependency detection
# ---------------------------------------------------------------------------


def _which_any(names: Iterable[str]) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def detect_dependencies() -> dict[str, dict[str, Any]]:
    """Probe the host for required and optional CLI dependencies.

    Required (the app refuses to start if missing):
      * Git
      * PowerShell (``powershell.exe`` or ``pwsh.exe``)

    Optional (the app starts but disables the matching features):
      * Claude CLI
      * Codex CLI
    """
    git_path = shutil.which("git")
    powershell_path = _which_any(("powershell", "powershell.exe", "pwsh", "pwsh.exe"))
    claude_path = shutil.which("claude")
    codex_path = shutil.which("codex")
    return {
        "git": {
            "available": bool(git_path),
            "path": git_path,
            "required": True,
            "label": "Git",
        },
        "powershell": {
            "available": bool(powershell_path),
            "path": powershell_path,
            "required": True,
            "label": "PowerShell",
        },
        "claude": {
            "available": bool(claude_path),
            "path": claude_path,
            "required": False,
            "label": "Claude CLI",
        },
        "codex": {
            "available": bool(codex_path),
            "path": codex_path,
            "required": False,
            "label": "Codex CLI",
        },
    }


def format_dependency_report(deps: dict[str, dict[str, Any]]) -> str:
    """Return a human-readable summary suitable for a startup dialog."""
    lines: list[str] = []
    for key in ("git", "powershell", "claude", "codex"):
        info = deps[key]
        marker = "OK" if info["available"] else "MISSING"
        required = " (required)" if info["required"] else " (optional)"
        path = info.get("path") or "<not on PATH>"
        lines.append(f"  [{marker}] {info['label']}{required}: {path}")
    return "\n".join(lines)


def required_dependencies_missing(deps: dict[str, dict[str, Any]]) -> list[str]:
    return [
        deps[key]["label"]
        for key in ("git", "powershell")
        if not deps[key]["available"]
    ]


# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------


def wait_for_backend(
    host: str,
    port: int,
    *,
    timeout: float = STARTUP_POLL_TIMEOUT,
    interval: float = STARTUP_POLL_INTERVAL,
    logger: logging.Logger | None = None,
) -> bool:
    """Poll ``http://<host>:<port>/`` until the server responds.

    Returns ``True`` if the backend answered within ``timeout`` seconds.
    Any non-network error from the server still counts as "ready" — we
    only care that *something* is listening.
    """
    url = f"http://{host}:{port}/"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                # Any HTTP response (even 404) means the server is up.
                response.read(1)
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, ConnectionError, OSError, socket.timeout) as exc:
            last_error = exc
            time.sleep(interval)
    if logger and last_error:
        logger.warning("Backend readiness probe failed: %s", last_error)
    return False


def start_backend(
    *,
    host: str,
    port: int,
    state_dir: Path,
    logger: logging.Logger,
    ready_event: threading.Event,
    on_error: Callable[[str], None] | None = None,
    powershell_path: str | None = None,
) -> tuple[Any, threading.Thread, int] | None:
    """Configure :mod:`gui.server` paths and start its HTTP server thread.

    Returns ``(server_instance, thread, bound_port)`` so the caller can
    ``shutdown`` the server, ``join`` the thread at exit and know which
    port the server actually bound. If configuration or bind fails,
    ``on_error`` is invoked with a description and the function returns
    ``None``.

    ``powershell_path`` is the resolved executable (``powershell.exe`` or
    ``pwsh.exe``) the desktop dependency probe found. Plumbed into the
    backend's :func:`gui.server.set_powershell_executable` so the run loop
    uses the same shell detection passed startup — without this, a host
    whose only PowerShell is ``pwsh.exe`` would pass dependency detection
    but fail the run loop because ``build_run_command`` defaults to the
    bare ``"powershell"`` name.
    """
    try:
        from gui import server as gui_server
    except Exception:
        tb = traceback.format_exc()
        logger.error("Failed to import gui.server:\n%s", tb)
        if on_error:
            on_error(f"Could not load backend module:\n{tb}")
        return None

    try:
        gui_server.configure_paths(state_dir)
        gui_server.set_powershell_executable(powershell_path)
        state_dir.mkdir(parents=True, exist_ok=True)
        # Merge-recovery sweep must run on the same thread that owns the
        # TaskStore singletons, which is the case here because we just
        # reconfigured them.
        gui_server._recover_pending_merges_at_startup()
        server_instance, bound_port = gui_server.create_server_on_free_port(
            host=host,
            preferred_port=port,
        )
    except Exception:
        tb = traceback.format_exc()
        logger.error("Backend startup failed:\n%s", tb)
        if on_error:
            on_error(f"Backend startup failed:\n{tb}")
        return None

    if bound_port != port:
        logger.info("Preferred port %s busy; using %s instead", port, bound_port)

    def _serve() -> None:
        logger.info(
            "Backend HTTP server listening on http://%s:%s (state_dir=%s)",
            host,
            bound_port,
            state_dir,
        )
        try:
            server_instance.serve_forever()
        except Exception:
            tb = traceback.format_exc()
            logger.error("Backend server thread crashed:\n%s", tb)
        finally:
            logger.info("Backend HTTP server thread exited")

    thread = threading.Thread(
        target=_serve,
        name="ccdl-backend",
        daemon=True,
    )
    thread.start()
    ready_event.set()
    return server_instance, thread, bound_port


def stop_backend(
    server_instance: Any | None,
    thread: threading.Thread | None,
    logger: logging.Logger,
    *,
    timeout: float = 5.0,
) -> None:
    # Stop any active backend run (e.g. a Claude/Codex PowerShell child
    # started via POST /api/run/start) BEFORE shutting down the HTTP
    # server. ``RunManager`` owns the child ``subprocess.Popen`` and
    # would otherwise leave it running after the desktop window closes,
    # silently continuing project-modifying automation without the UI
    # that launched it.
    #
    # We prefer ``RunManager.shutdown`` over ``stop_if_running`` because
    # ``shutdown`` closes the Win32 Job Object handle even when the
    # direct PowerShell parent has already exited but a descendant
    # (Claude / Codex CLI) is still running inside the Job, or when the
    # reader thread has not yet reached its natural-exit ``job.close()``.
    # ``stop_if_running`` short-circuits in those cases and would leave
    # ``self._job`` open, so descendants would outlive the desktop
    # window. Falling back to ``stop_if_running`` keeps the function
    # working against older ``gui.server`` builds that predate
    # ``shutdown``.
    try:
        from gui import server as gui_server

        runs = getattr(gui_server.GuiHandler, "runs", None)
        cleaned_up = False
        if runs is not None:
            shutdown = getattr(runs, "shutdown", None)
            if shutdown is not None:
                cleaned_up = bool(shutdown())
            else:
                stop_if_running = getattr(runs, "stop_if_running", None)
                if stop_if_running is not None:
                    cleaned_up = bool(stop_if_running())
        if cleaned_up:
            logger.info(
                "Stopped active backend run before HTTP server shutdown"
            )
        else:
            logger.debug("No active backend run to stop")
    except Exception:
        logger.debug(
            "RunManager shutdown raised during backend stop",
            exc_info=True,
        )

    if server_instance is not None:
        try:
            server_instance.shutdown()
        except Exception:
            logger.debug("server.shutdown() raised", exc_info=True)
        try:
            server_instance.server_close()
        except Exception:
            logger.debug("server.server_close() raised", exc_info=True)
    if thread is not None:
        thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Window / front-end
# ---------------------------------------------------------------------------


def _open_in_browser(url: str, logger: logging.Logger) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception:
        logger.exception("Default browser failed to open")
        return False


def _block_on_stdin_fallback(url: str, logger: logging.Logger) -> int:
    """Last-resort block used when the tkinter dialog cannot be created.

    Keeps the backend alive until the user presses Enter (or stdin closes).
    Never raises.
    """
    logger.info(
        "tkinter unavailable; blocking on stdin so the backend stays alive. "
        "Press Enter to stop the backend and exit."
    )
    try:
        input(
            f"\n[{APP_TITLE}] Browser fallback active at {url}\n"
            "Press Enter to stop the backend and exit... "
        )
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def _run_browser_fallback_loop(url: str, logger: logging.Logger) -> int:
    """Open the browser and block until the user explicitly exits.

    This is the fallback when pywebview is unavailable or fails. The
    backend HTTP server stays alive for the duration of this call so the
    browser session keeps working. Returns 0 once the user has clicked
    Exit (or signalled exit via stdin when tkinter is unavailable).
    """
    browser_opened = _open_in_browser(url, logger)
    if not browser_opened:
        logger.warning(
            "Default browser did not open automatically. The console is "
            "still reachable by opening the URL manually."
        )

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        logger.warning("tkinter unavailable; falling back to stdin blocking")
        return _block_on_stdin_fallback(url, logger)

    try:
        root = tk.Tk()
        root.title(f"{APP_TITLE} - Browser Mode")
        root.geometry("560x260")
        root.minsize(480, 220)

        content = ttk.Frame(root, padding=20)
        content.pack(fill="both", expand=True)

        status = (
            "The web console has been opened in your default browser."
            if browser_opened
            else "The default browser could not be opened automatically."
        )
        ttk.Label(content, text=status, wraplength=500, justify="left").pack(
            anchor="w", pady=(0, 8)
        )

        url_var = tk.StringVar(value=f"URL: {url}")
        ttk.Label(
            content,
            textvariable=url_var,
            foreground="#0066cc",
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(
            content,
            text=(
                "Keep this window open while you work. Click Exit to stop "
                "the backend service and quit the application."
            ),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(0, 16))

        def reopen() -> None:
            if _open_in_browser(url, logger):
                url_var.set(f"URL: {url}")
            else:
                url_var.set(f"URL: {url} (could not reopen)")

        def quit_app() -> None:
            logger.info("Exit requested from browser-fallback dialog")
            root.destroy()

        button_row = ttk.Frame(content)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Open browser again", command=reopen).pack(side="left")
        ttk.Button(button_row, text="Exit", command=quit_app).pack(side="right")

        # Treat the window-close (X) button as Exit too so closing the
        # companion window still stops the backend cleanly.
        root.protocol("WM_DELETE_WINDOW", quit_app)

        logger.info("Entering browser-fallback blocking loop at %s", url)
        root.mainloop()
        return 0
    except Exception:
        tb = traceback.format_exc()
        logger.error("Browser-fallback dialog crashed:\n%s", tb)
        return _block_on_stdin_fallback(url, logger)


def run_window(
    url: str,
    *,
    logger: logging.Logger,
    on_error: Callable[[str], str | None] | None = None,
) -> int:
    """Open the desktop window.

    Uses pywebview when available so the user gets a real native window
    without a browser tab. Falls back to ``_run_browser_fallback_loop``
    when pywebview is missing or fails: the browser opens and a small
    companion window stays open with an Exit button so the backend
    service is not torn down before the user is done.
    """
    try:
        import webview  # type: ignore[import-not-found]
    except Exception:
        logger.warning(
            "pywebview not installed; opening the console in the default browser"
        )
        if on_error:
            on_error(
                "pywebview is not installed in this environment. The console "
                "will open in your default browser and a companion window will "
                "stay open to keep the backend alive. Install pywebview "
                "(pip install pywebview) for the native desktop window."
            )
        return _run_browser_fallback_loop(url, logger)

    try:
        webview.create_window(
            APP_TITLE,
            url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            text_select=True,
        )
        webview.start()
        return 0
    except Exception:
        tb = traceback.format_exc()
        logger.error("pywebview failed to start:\n%s", tb)
        if on_error:
            on_error(
                f"The desktop window could not open:\n{tb}\n"
                "Falling back to your default browser."
            )
        return _run_browser_fallback_loop(url, logger)


# ---------------------------------------------------------------------------
# User-facing dialog helpers (kept tiny so they are easy to swap in tests)
# ---------------------------------------------------------------------------


def _show_blocking_message(
    title: str,
    message: str,
    *,
    style: str = "error",
    logger: logging.Logger | None = None,
) -> None:
    """Best-effort blocking message box.

    Uses tkinter if available (Python ships with it on Windows), then
    falls back to console output. Never raises.
    """
    if logger:
        (logger.error if style == "error" else logger.info)(
            "Dialog [%s]: %s\n%s", title, message, message
        )
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        print(f"[{title}]\n{message}", file=sys.stderr)
        return

    try:
        root = tk.Tk()
        root.withdraw()
        if style == "error":
            messagebox.showerror(title, message)
        elif style == "warning":
            messagebox.showwarning(title, message)
        else:
            messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        print(f"[{title}]\n{message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Codex Claude Dev Loop desktop application.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface the backend HTTP server binds to (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Preferred backend port. The app falls back to the next free port.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Override the user data directory (defaults to %%LOCALAPPDATA%%\\CodexClaudeDevLoop).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    user_data_dir = get_user_data_dir(args.user_data_dir)
    state_dir = get_state_dir(user_data_dir)
    logs_dir = get_logs_dir(user_data_dir)

    # P2-2 fix: create and validate the user data / state / logs
    # directories BEFORE configuring logging. ``setup_logging`` itself
    # creates the logs directory, so an invalid or unwritable
    # ``--user-data-dir`` / ``LOCALAPPDATA`` path would otherwise raise
    # before any message box or desktop log is available. In the
    # windowed EXE that fails silently instead of showing the clear
    # startup error the desktop contract requires. We surface the
    # failure via a best-effort dialog (and stderr) here, then bail.
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _show_blocking_message(
            APP_TITLE,
            f"Cannot create user data directory:\n{user_data_dir}\n\nError: {exc}",
        )
        print(
            f"[{APP_TITLE}] Cannot create user data directory: {user_data_dir}\n{exc}",
            file=sys.stderr,
        )
        return 2

    logger = setup_logging(logs_dir, verbose=args.verbose)

    logger.info("=== %s starting ===", APP_TITLE)
    logger.info("User data dir : %s", user_data_dir)
    logger.info("State dir     : %s", state_dir)
    logger.info("Logs dir       : %s", logs_dir)
    logger.info("Resource root : %s", _resource_root())
    logger.info("Frozen        : %s", getattr(sys, "frozen", False))
    logger.info("Host          : %s", args.host)
    logger.info("Preferred port: %s", args.port)

    deps = detect_dependencies()
    logger.info("Dependency probe:\n%s", format_dependency_report(deps))

    missing_required = required_dependencies_missing(deps)
    if missing_required:
        message = (
            "Required tools are missing:\n\n"
            + "\n".join(f"  - {name}" for name in missing_required)
            + "\n\nInstall them and relaunch. Git is required to inspect and merge "
            "project changes. PowerShell is required to drive the Claude/Codex CLI "
            "windows. The application will close now."
        )
        _show_blocking_message(APP_TITLE, message, logger=logger)
        logger.error("Missing required dependencies: %s", missing_required)
        return 3

    missing_optional = [
        deps[key]["label"]
        for key in ("claude", "codex")
        if not deps[key]["available"]
    ]
    if missing_optional:
        optional_message = (
            "Optional CLIs are not on PATH:\n\n"
            + "\n".join(f"  - {name}" for name in missing_optional)
            + "\n\nThe workbench will still open, but the matching "
            "Launch CLI button will report the tool as missing. Install "
            "them to enable the full Claude/Codex collaboration loop."
        )
        logger.warning("Optional CLIs missing: %s", missing_optional)
        _show_blocking_message(
            APP_TITLE,
            optional_message,
            style="warning",
            logger=logger,
        )

    # Plumb the resolved PowerShell path into the backend so the run loop
    # uses the same executable detection passed at startup. Without this
    # a host whose only PowerShell is pwsh.exe passes detection but the run
    # command still falls back to the bare ``"powershell"`` name and fails.
    powershell_path = deps["powershell"].get("path") or "powershell"
    logger.info("PowerShell resolved: %s", powershell_path)

    ready_event = threading.Event()
    server_holder: dict[str, Any] = {"server": None, "thread": None, "port": None}
    startup_error: dict[str, str | None] = {"message": None}

    def on_backend_error(message: str) -> None:
        startup_error["message"] = message

    result = start_backend(
        host=args.host,
        port=args.port,
        state_dir=state_dir,
        logger=logger,
        ready_event=ready_event,
        on_error=on_backend_error,
        powershell_path=powershell_path,
    )
    if result is None:
        message = startup_error["message"] or "Unknown backend startup failure."
        _show_blocking_message(APP_TITLE, message, logger=logger)
        return 4

    server_instance, thread, bound_port = result
    server_holder.update(server=server_instance, thread=thread, port=bound_port)

    if not wait_for_backend(args.host, bound_port, logger=logger):
        message = (
            f"The backend service did not become ready at "
            f"http://{args.host}:{bound_port}/ within "
            f"{int(STARTUP_POLL_TIMEOUT)} seconds. Check the log at "
            f"{logs_dir / 'desktop.log'} for details."
        )
        _show_blocking_message(APP_TITLE, message, logger=logger)
        stop_backend(server_instance, thread, logger)
        return 5

    url = f"http://{args.host}:{bound_port}/"
    logger.info("Opening desktop window at %s", url)

    exit_code = run_window(url, logger=logger)

    logger.info("Window closed; shutting down backend")
    stop_backend(server_instance, thread, logger)
    logger.info("=== %s exiting (code=%s) ===", APP_TITLE, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
