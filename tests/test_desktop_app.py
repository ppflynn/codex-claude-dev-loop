"""Tests for the desktop application entry point."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_app
from gui import server as gui_server


class DesktopAppPathsTests(unittest.TestCase):
    def test_user_data_dir_defaults_to_local_app_data(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Example\Local"}, clear=False):
            path = desktop_app.get_user_data_dir()
        self.assertEqual(path, Path(r"C:\Example\Local") / "CodexClaudeDevLoop")

    def test_user_data_dir_override_wins(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Default"}, clear=False):
            path = desktop_app.get_user_data_dir(r"D:\Custom\Path")
        self.assertEqual(path, Path(r"D:\Custom\Path"))

    def test_user_data_dir_env_var_supported(self):
        env = {"CCDL_USER_DATA_DIR": r"E:\FromEnv"}
        with mock.patch.dict(os.environ, env, clear=False):
            path = desktop_app.get_user_data_dir()
        self.assertEqual(path, Path(r"E:\FromEnv"))

    def test_state_and_logs_dir_under_user_data(self):
        user_data = Path(r"C:\Example\Local") / "CodexClaudeDevLoop"
        self.assertEqual(desktop_app.get_state_dir(user_data), user_data / ".gui")
        self.assertEqual(desktop_app.get_logs_dir(user_data), user_data / "logs")


class DependencyDetectionTests(unittest.TestCase):
    def test_required_dependencies_flagged(self):
        deps = desktop_app.detect_dependencies()
        self.assertTrue(deps["git"]["required"])
        self.assertTrue(deps["powershell"]["required"])
        self.assertFalse(deps["claude"]["required"])
        self.assertFalse(deps["codex"]["required"])

    def test_required_dependencies_missing_lists_only_required(self):
        fake = {
            "git": {"available": False, "label": "Git", "required": True},
            "powershell": {"available": True, "label": "PowerShell", "required": True},
            "claude": {"available": False, "label": "Claude CLI", "required": False},
            "codex": {"available": True, "label": "Codex CLI", "required": False},
        }
        missing = desktop_app.required_dependencies_missing(fake)
        self.assertEqual(missing, ["Git"])

    def test_format_dependency_report_marks_missing(self):
        fake = {
            "git": {"available": True, "path": "/usr/bin/git", "required": True, "label": "Git"},
            "powershell": {"available": False, "path": None, "required": True, "label": "PowerShell"},
            "claude": {"available": True, "path": "/usr/bin/claude", "required": False, "label": "Claude CLI"},
            "codex": {"available": False, "path": None, "required": False, "label": "Codex CLI"},
        }
        report = desktop_app.format_dependency_report(fake)
        self.assertIn("[OK]", report)
        self.assertIn("[MISSING]", report)
        self.assertIn("PowerShell", report)
        self.assertIn("Codex CLI", report)


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccdl-desktop-log-")

    def tearDown(self):
        logging = __import__("logging")
        logger = logging.getLogger("ccdl.desktop")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setup_logging_creates_log_file(self):
        logs_dir = Path(self.tmp) / "logs"
        desktop_app.setup_logging(logs_dir)
        self.assertTrue((logs_dir / "desktop.log").exists())
        # Append a test record and confirm the file receives it.
        import logging as _logging
        logger = _logging.getLogger("ccdl.desktop")
        logger.info("test message")
        for handler in logger.handlers:
            handler.flush()
        contents = (logs_dir / "desktop.log").read_text(encoding="utf-8")
        self.assertIn("test message", contents)


class BackendLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccdl-desktop-bk-")
        self.state_dir = Path(self.tmp) / ".gui"
        self.logs_dir = Path(self.tmp) / "logs"
        self.state_dir.mkdir(parents=True)
        self.logs_dir.mkdir(parents=True)
        self.logger = desktop_app.setup_logging(self.logs_dir)
        # Snapshot module-level paths so each test can restore them.
        import gui.server as srv
        self._saved_paths = {
            "STATE_DIR": srv.STATE_DIR,
            "PROJECTS_FILE": srv.PROJECTS_FILE,
            "TASKS_DIR": srv.TASKS_DIR,
            "TRASH_TASKS_DIR": srv.TRASH_TASKS_DIR,
            "SETTINGS_FILE": srv.SETTINGS_FILE,
            "AUDIT_LOG_FILE": srv.AUDIT_LOG_FILE,
            "MERGE_RECOVERY_DIR": srv.MERGE_RECOVERY_DIR,
        }
        self._saved_handler_attrs = {
            "store": srv.GuiHandler.store,
            "runs": srv.GuiHandler.runs,
            "tasks": srv.GuiHandler.tasks,
        }

    def tearDown(self):
        import gui.server as srv
        for key, value in self._saved_paths.items():
            setattr(srv, key, value)
        for key, value in self._saved_handler_attrs.items():
            setattr(srv.GuiHandler, key, value)
        # Close log file handlers so Windows lets us delete the temp dir.
        import logging as _logging
        logger = _logging.getLogger("ccdl.desktop")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_backend_binds_and_serves_actual_port(self):
        ready = threading.Event()
        result = desktop_app.start_backend(
            host="127.0.0.1",
            port=0,  # ask for OS-assigned
            state_dir=self.state_dir,
            logger=self.logger,
            ready_event=ready,
        )
        self.assertIsNotNone(result)
        server_instance, thread, bound_port = result  # type: ignore[misc]
        try:
            self.assertTrue(bound_port > 0)
            self.assertTrue(
                desktop_app.wait_for_backend("127.0.0.1", bound_port, timeout=10.0, logger=self.logger)
            )
            # Server should actually respond on /api/projects.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{bound_port}/api/projects", timeout=2.0
            ) as response:
                self.assertEqual(response.status, 200)
            # The state dir override should be reflected in the module.
            self.assertEqual(gui_server.STATE_DIR, self.state_dir.resolve())
            self.assertEqual(
                gui_server.PROJECTS_FILE, self.state_dir.resolve() / "projects.json"
            )
        finally:
            desktop_app.stop_backend(server_instance, thread, self.logger)

    def test_start_backend_falls_back_when_preferred_port_busy(self):
        # Occupy a port to force the fallback path.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        try:
            ready = threading.Event()
            result = desktop_app.start_backend(
                host="127.0.0.1",
                port=busy_port,
                state_dir=self.state_dir,
                logger=self.logger,
                ready_event=ready,
            )
            self.assertIsNotNone(result)
            server_instance, thread, bound_port = result  # type: ignore[misc]
            try:
                self.assertNotEqual(bound_port, busy_port)
                self.assertTrue(
                    desktop_app.wait_for_backend(
                        "127.0.0.1", bound_port, timeout=10.0, logger=self.logger
                    )
                )
            finally:
                desktop_app.stop_backend(server_instance, thread, self.logger)
        finally:
            blocker.close()

    def test_wait_for_backend_returns_false_when_no_server(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        free_but_unserved_port = blocker.getsockname()[1]
        blocker.close()
        ok = desktop_app.wait_for_backend(
            "127.0.0.1", free_but_unserved_port, timeout=0.6, interval=0.1
        )
        self.assertFalse(ok)

    def test_stop_backend_terminates_active_run_before_http_shutdown(self):
        """P2-1 regression: closing the desktop window must terminate any
        active backend run (RunManager's child PowerShell process), not
        just shut down the HTTP server. Otherwise project-modifying
        automation can outlive the UI that launched it."""
        runs = gui_server.GuiHandler.runs
        fake_process = mock.MagicMock(spec=subprocess.Popen)
        # poll() returns None -> process is still running.
        fake_process.poll.return_value = None
        fake_process.terminate = mock.MagicMock()
        fake_process.kill = mock.MagicMock()
        fake_process.wait = mock.MagicMock()  # returns None -> exited cleanly
        fake_process.stdout = None

        # Plant the active run directly on the singleton the way
        # ``RunManager.start`` would. Snapshot first so we can restore.
        saved_process = runs._process
        saved_current = runs.current
        saved_stopping = runs._stopping
        runs._process = fake_process
        runs.current = {
            "id": "test-run",
            "projectId": "p1",
            "projectName": "Test",
            "projectPath": "/tmp",
            "status": "running",
            "startedAt": "2026-06-23 00:00:00",
            "endedAt": None,
            "exitCode": None,
            "result": None,
            "logs": [],
            "command": ["powershell"],
        }
        runs._stopping = False
        try:
            desktop_app.stop_backend(None, None, self.logger)
            # The child process must have been asked to terminate.
            fake_process.terminate.assert_called_once()
            # _stopping flips to True so _read_process records the run
            # as "stopped" rather than "finished" once wait() returns.
            self.assertTrue(runs._stopping)
        finally:
            runs._process = saved_process
            runs.current = saved_current
            runs._stopping = saved_stopping

    def test_stop_backend_does_not_fail_when_no_active_run(self):
        """When no run is active, stop_backend must still complete the
        HTTP shutdown cleanly (idempotent run cleanup)."""
        runs = gui_server.GuiHandler.runs
        saved_process = runs._process
        saved_current = runs.current
        saved_stopping = runs._stopping
        runs._process = None
        runs.current = None
        runs._stopping = False
        try:
            # Should not raise even though there is nothing to stop.
            desktop_app.stop_backend(None, None, self.logger)
        finally:
            runs._process = saved_process
            runs.current = saved_current
            runs._stopping = saved_stopping

    def test_stop_backend_closes_job_when_parent_already_exited(self):
        """Round 5 P2-1 regression: when the PowerShell parent has
        already exited but ``self._job`` is still set (because the
        reader thread has not yet reached its natural-exit
        ``job.close()``), ``stop_backend`` must still close the Job
        handle so descendants are reaped.

        Before the fix, ``stop_if_running`` short-circuited because
        ``has_active_run`` returned False, leaving ``self._job`` open
        and any descendants (Claude/Codex CLI) running on the user's
        machine after the desktop window closed.
        """
        runs = gui_server.GuiHandler.runs
        saved_process = runs._process
        saved_current = runs.current
        saved_stopping = runs._stopping
        saved_job = runs._job

        fake_process = mock.MagicMock(spec=subprocess.Popen)
        # poll() returns 0 -> parent already exited.
        fake_process.poll.return_value = 0
        fake_process.stdout = None

        fake_job = mock.MagicMock()
        fake_job.__bool__ = lambda self: True

        runs._process = fake_process
        runs._job = fake_job
        runs.current = {
            "id": "test-run",
            "projectId": "p1",
            "projectName": "Test",
            "projectPath": "/tmp",
            "status": "running",
            "startedAt": "2026-06-23 00:00:00",
            "endedAt": None,
            "exitCode": None,
            "result": None,
            "logs": [],
            "command": ["powershell"],
        }
        runs._stopping = False
        try:
            desktop_app.stop_backend(None, None, self.logger)
            # Parent already exited — must NOT call terminate.
            fake_process.terminate.assert_not_called()
            # Job handle must still be closed so descendants are reaped.
            fake_job.close.assert_called_once()
            self.assertIsNone(runs._job)
        finally:
            runs._process = saved_process
            runs.current = saved_current
            runs._stopping = saved_stopping
            runs._job = saved_job

    def test_stop_backend_uses_shutdown_when_available(self):
        """stop_backend must prefer ``RunManager.shutdown`` over
        ``stop_if_running`` so the Job is closed unconditionally."""
        runs = gui_server.GuiHandler.runs
        # Replace the instance methods with spies.
        shutdown_calls = []
        stop_if_running_calls = []

        def fake_shutdown():
            shutdown_calls.append(True)
            return True

        def fake_stop_if_running():
            stop_if_running_calls.append(True)
            return False

        saved_shutdown = getattr(runs, "shutdown", None)
        saved_stop_if_running = getattr(runs, "stop_if_running", None)
        runs.shutdown = fake_shutdown  # type: ignore[method-assign]
        runs.stop_if_running = fake_stop_if_running  # type: ignore[method-assign]
        try:
            desktop_app.stop_backend(None, None, self.logger)
        finally:
            if saved_shutdown is not None:
                runs.shutdown = saved_shutdown  # type: ignore[method-assign]
            else:
                del runs.shutdown
            if saved_stop_if_running is not None:
                runs.stop_if_running = saved_stop_if_running  # type: ignore[method-assign]
            else:
                del runs.stop_if_running

        self.assertEqual(shutdown_calls, [True])
        # Must NOT fall back to stop_if_running when shutdown exists.
        self.assertEqual(stop_if_running_calls, [])


class ArgparseTests(unittest.TestCase):
    def test_defaults(self):
        args = desktop_app.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, desktop_app.DEFAULT_PORT)
        self.assertIsNone(args.user_data_dir)
        self.assertFalse(args.verbose)

    def test_overrides(self):
        args = desktop_app.parse_args(
            ["--host", "0.0.0.0", "--port", "9999", "--user-data-dir", "D:\\Dir", "--verbose"]
        )
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9999)
        self.assertEqual(args.user_data_dir, "D:\\Dir")
        self.assertTrue(args.verbose)


class ServerConfigurePathsTests(unittest.TestCase):
    """Verify gui.server.configure_paths updates module singletons and handler."""

    def setUp(self):
        import gui.server as srv
        self._saved_paths = {
            "STATE_DIR": srv.STATE_DIR,
            "PROJECTS_FILE": srv.PROJECTS_FILE,
            "TASKS_DIR": srv.TASKS_DIR,
            "TRASH_TASKS_DIR": srv.TRASH_TASKS_DIR,
            "SETTINGS_FILE": srv.SETTINGS_FILE,
            "AUDIT_LOG_FILE": srv.AUDIT_LOG_FILE,
            "MERGE_RECOVERY_DIR": srv.MERGE_RECOVERY_DIR,
        }
        self._saved_handler_attrs = {
            "store": srv.GuiHandler.store,
            "runs": srv.GuiHandler.runs,
            "tasks": srv.GuiHandler.tasks,
        }
        self.tmp = tempfile.mkdtemp(prefix="ccdl-cfg-")

    def tearDown(self):
        import gui.server as srv
        for key, value in self._saved_paths.items():
            setattr(srv, key, value)
        for key, value in self._saved_handler_attrs.items():
            setattr(srv.GuiHandler, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configure_paths_rebinds_module_and_handler(self):
        target = Path(self.tmp) / "state"
        returned = gui_server.configure_paths(target)
        self.assertEqual(returned, target.resolve())
        self.assertEqual(gui_server.STATE_DIR, target.resolve())
        self.assertEqual(gui_server.PROJECTS_FILE, target.resolve() / "projects.json")
        self.assertEqual(gui_server.TASKS_DIR, target.resolve() / "tasks")
        self.assertEqual(gui_server.TRASH_TASKS_DIR, target.resolve() / "trash" / "tasks")
        self.assertEqual(gui_server.SETTINGS_FILE, target.resolve() / "settings.json")
        self.assertEqual(gui_server.AUDIT_LOG_FILE, target.resolve() / "audit.log")
        self.assertEqual(gui_server.MERGE_RECOVERY_DIR, target.resolve() / "merge_recovery")
        # Handler class singletons must now point at the new state dir.
        self.assertEqual(gui_server.GuiHandler.tasks.tasks_root, target.resolve() / "tasks")
        self.assertEqual(
            gui_server.GuiHandler.tasks.trash_root, target.resolve() / "trash" / "tasks"
        )
        self.assertEqual(gui_server.GuiHandler.store.path, target.resolve() / "projects.json")

    def test_configure_paths_falls_back_to_env(self):
        env = {"CCDL_STATE_DIR": str(Path(self.tmp) / "envstate")}
        with mock.patch.dict(os.environ, env, clear=False):
            returned = gui_server.configure_paths()
        self.assertEqual(returned, (Path(self.tmp) / "envstate").resolve())


class FindAvailablePortTests(unittest.TestCase):
    def test_returns_a_bindable_port(self):
        port = gui_server.find_available_port("127.0.0.1", preferred=18790, attempts=8)
        self.assertIsInstance(port, int)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        finally:
            sock.close()

    def test_skips_busy_preferred(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        try:
            port = gui_server.find_available_port("127.0.0.1", preferred=busy_port, attempts=8)
            self.assertNotEqual(port, busy_port)
        finally:
            blocker.close()


class BrowserFallbackTests(unittest.TestCase):
    """Regression coverage for the P2-1 fix.

    Before the fix, ``run_window`` opened the default browser and returned
    immediately; ``main()`` then called ``stop_backend()``, killing the
    server the browser had just opened. These tests lock in the contract
    that the browser fallback must *block* until the user explicitly
    exits so the backend stays alive for the duration of the session.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccdl-fb-")
        self.logs_dir = Path(self.tmp) / "logs"
        self.logger = desktop_app.setup_logging(self.logs_dir)

    def tearDown(self):
        import logging as _logging
        logger = _logging.getLogger("ccdl.desktop")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_window_uses_fallback_loop_when_pywebview_missing(self):
        calls = {"fallback": None, "direct_browser": []}

        def fake_fallback(url, logger):
            calls["fallback"] = url
            return 0

        # Force `import webview` to raise ImportError inside run_window.
        with mock.patch.dict(sys.modules, {"webview": None}):
            with mock.patch.object(
                desktop_app, "_run_browser_fallback_loop", side_effect=fake_fallback
            ):
                with mock.patch.object(
                    desktop_app,
                    "_open_in_browser",
                    side_effect=lambda u, l: calls["direct_browser"].append(u) or True,
                ):
                    code = desktop_app.run_window(
                        "http://127.0.0.1:18765/",
                        logger=self.logger,
                    )

        self.assertEqual(code, 0)
        self.assertEqual(calls["fallback"], "http://127.0.0.1:18765/")
        # run_window must NOT open the browser directly anymore — that is
        # the fallback loop's job, and the loop is responsible for keeping
        # the backend alive afterwards.
        self.assertEqual(calls["direct_browser"], [])

    def test_run_window_uses_fallback_loop_when_pywebview_raises(self):
        calls = {"fallback": None}

        def fake_fallback(url, logger):
            calls["fallback"] = url
            return 0

        fake_webview = mock.MagicMock()
        fake_webview.create_window.side_effect = RuntimeError("simulated WebView2 missing")

        with mock.patch.dict(sys.modules, {"webview": fake_webview}):
            with mock.patch.object(
                desktop_app, "_run_browser_fallback_loop", side_effect=fake_fallback
            ):
                code = desktop_app.run_window(
                    "http://127.0.0.1:18766/",
                    logger=self.logger,
                )

        self.assertEqual(code, 0)
        self.assertEqual(calls["fallback"], "http://127.0.0.1:18766/")

    def test_fallback_loop_blocks_until_user_exits(self):
        """The fallback loop must block until the user signals exit so the
        backend HTTP server stays alive for the browser session."""
        import types

        exit_signal = threading.Event()
        mainloop_entered = threading.Event()

        class FakeTk:
            def title(self, _t):
                pass

            def geometry(self, _g):
                pass

            def minsize(self, _w, _h):
                pass

            def protocol(self, _name, _handler):
                pass

            def mainloop(self):
                mainloop_entered.set()
                exit_signal.wait(timeout=10.0)

            def destroy(self):
                pass

        fake_tk = types.ModuleType("tkinter")
        fake_tk.Tk = FakeTk
        fake_tk.StringVar = lambda **kw: mock.MagicMock()
        fake_ttk = types.ModuleType("tkinter.ttk")
        _widget = mock.MagicMock()
        fake_ttk.Frame = lambda *a, **k: _widget
        fake_ttk.Label = lambda *a, **k: _widget
        fake_ttk.Button = lambda *a, **k: _widget
        fake_tk.ttk = fake_ttk

        browser_opens = []

        def fake_open(url, logger):
            browser_opens.append(url)
            return True

        result_holder = {"code": None}

        def runner():
            with mock.patch.dict(
                sys.modules, {"tkinter": fake_tk, "tkinter.ttk": fake_ttk}
            ):
                with mock.patch.object(
                    desktop_app, "_open_in_browser", side_effect=fake_open
                ):
                    result_holder["code"] = desktop_app._run_browser_fallback_loop(
                        "http://127.0.0.1:18767/", self.logger
                    )

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        # The browser must open and the loop must enter mainloop (block).
        self.assertTrue(mainloop_entered.wait(timeout=2.0), "mainloop was not entered")
        self.assertEqual(browser_opens, ["http://127.0.0.1:18767/"])
        self.assertTrue(
            t.is_alive(), "fallback loop must block while the dialog is open"
        )

        # Now let the user "click Exit".
        exit_signal.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "fallback loop must return after exit")
        self.assertEqual(result_holder["code"], 0)

    def test_fallback_loop_blocks_on_stdin_when_tkinter_missing(self):
        """When tkinter is not importable, the fallback must still block
        (on stdin) so the backend stays alive."""
        block_event = threading.Event()
        input_entered = threading.Event()

        def fake_input(_prompt=""):
            input_entered.set()
            block_event.wait(timeout=10.0)
            return ""

        result_holder = {"code": None}

        def runner():
            # Setting sys.modules["tkinter"] = None makes the bare
            # `import tkinter` inside _run_browser_fallback_loop raise
            # ImportError, forcing the stdin path.
            with mock.patch.dict(sys.modules, {"tkinter": None, "tkinter.ttk": None}):
                with mock.patch.object(
                    desktop_app, "_open_in_browser", lambda u, l: True
                ):
                    with mock.patch("builtins.input", side_effect=fake_input):
                        result_holder["code"] = desktop_app._run_browser_fallback_loop(
                            "http://127.0.0.1:18768/", self.logger
                        )

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        self.assertTrue(input_entered.wait(timeout=2.0), "stdin input was not reached")
        self.assertTrue(t.is_alive(), "stdin fallback must block until user exits")
        block_event.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(result_holder["code"], 0)


class StartBackendPowerShellPlumbingTests(unittest.TestCase):
    """P3-1 regression: ``start_backend`` must forward the resolved
    PowerShell executable into ``gui.server`` so a host whose only shell
    is ``pwsh.exe`` does not pass detection then fail at run time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccdl-ps-")
        self.state_dir = Path(self.tmp) / ".gui"
        self.logs_dir = Path(self.tmp) / "logs"
        self.state_dir.mkdir(parents=True)
        self.logs_dir.mkdir(parents=True)
        self.logger = desktop_app.setup_logging(self.logs_dir)
        self._saved_powershell = gui_server.POWERSHELL_EXECUTABLE
        import gui.server as srv
        self._saved_paths = {
            "STATE_DIR": srv.STATE_DIR,
            "PROJECTS_FILE": srv.PROJECTS_FILE,
            "TASKS_DIR": srv.TASKS_DIR,
            "TRASH_TASKS_DIR": srv.TRASH_TASKS_DIR,
            "SETTINGS_FILE": srv.SETTINGS_FILE,
            "AUDIT_LOG_FILE": srv.AUDIT_LOG_FILE,
            "MERGE_RECOVERY_DIR": srv.MERGE_RECOVERY_DIR,
        }
        self._saved_handler_attrs = {
            "store": srv.GuiHandler.store,
            "runs": srv.GuiHandler.runs,
            "tasks": srv.GuiHandler.tasks,
        }

    def tearDown(self):
        gui_server.POWERSHELL_EXECUTABLE = self._saved_powershell
        import gui.server as srv
        for key, value in self._saved_paths.items():
            setattr(srv, key, value)
        for key, value in self._saved_handler_attrs.items():
            setattr(srv.GuiHandler, key, value)
        import logging as _logging
        logger = _logging.getLogger("ccdl.desktop")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_backend_forwards_resolved_powershell_path(self):
        """A non-default PowerShell path is plumbed into the backend so
        ``build_run_command`` uses it instead of the bare ``"powershell"``
        name."""
        ready = threading.Event()
        custom = r"C:\Program Files\PowerShell\7\pwsh.exe"
        result = desktop_app.start_backend(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            logger=self.logger,
            ready_event=ready,
            powershell_path=custom,
        )
        self.assertIsNotNone(result)
        server_instance, thread, _ = result  # type: ignore[misc]
        try:
            self.assertEqual(gui_server.POWERSHELL_EXECUTABLE, custom)
        finally:
            desktop_app.stop_backend(server_instance, thread, self.logger)

    def test_start_backend_resets_to_default_when_path_none(self):
        """Passing ``None`` must reset to the default so legacy callers
        get the bare ``"powershell"`` name."""
        # Seed with a non-default value to prove the reset actually happens.
        gui_server.POWERSHELL_EXECUTABLE = "/some/other/path"
        ready = threading.Event()
        result = desktop_app.start_backend(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            logger=self.logger,
            ready_event=ready,
            powershell_path=None,
        )
        self.assertIsNotNone(result)
        server_instance, thread, _ = result  # type: ignore[misc]
        try:
            self.assertEqual(gui_server.POWERSHELL_EXECUTABLE, "powershell")
        finally:
            desktop_app.stop_backend(server_instance, thread, self.logger)

    def test_set_powershell_executable_normalises_and_validates(self):
        gui_server.set_powershell_executable("   ")
        self.assertEqual(gui_server.POWERSHELL_EXECUTABLE, "powershell")
        gui_server.set_powershell_executable(None)
        self.assertEqual(gui_server.POWERSHELL_EXECUTABLE, "powershell")
        gui_server.set_powershell_executable(r"C:\bin\pwsh.exe")
        self.assertEqual(gui_server.POWERSHELL_EXECUTABLE, r"C:\bin\pwsh.exe")


class MainUserDataDirBootstrapTests(unittest.TestCase):
    """P2-2 regression: directory creation failures must surface to the
    user even when logging cannot be configured. Before the fix
    ``setup_logging`` ran first and would itself raise when the logs
    directory was unwritable, swallowing the error in the windowed EXE."""

    def setUp(self):
        self._saved_argv = sys.argv
        sys.argv = ["desktop_app"]

    def tearDown(self):
        sys.argv = self._saved_argv

    def test_main_returns_2_when_user_data_dir_unwritable(self):
        # Point the override at a path whose parent we will make
        # un-creatable by anchoring it under a regular file.
        tmp = tempfile.mkdtemp(prefix="ccdl-bt-")
        try:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            target = blocker / "CodexClaudeDevLoop" / ".gui"

            dialog_calls: list[tuple[str, str]] = []

            def fake_dialog(title, message, *, style="error", logger=None):
                dialog_calls.append((title, message))

            with mock.patch.object(desktop_app, "_show_blocking_message", side_effect=fake_dialog):
                with mock.patch.object(desktop_app, "setup_logging") as fake_log:
                    with mock.patch.object(desktop_app, "detect_dependencies") as fake_deps:
                        code = desktop_app.main(
                            ["--user-data-dir", str(target)]
                        )

            # Must have failed early without configuring logging,
            # probing deps, or otherwise progressing.
            self.assertEqual(code, 2)
            fake_log.assert_not_called()
            fake_deps.assert_not_called()
            # The blocking dialog must have fired so the user sees the
            # failure even with no logger.
            self.assertEqual(len(dialog_calls), 1)
            self.assertEqual(dialog_calls[0][0], desktop_app.APP_TITLE)
            self.assertIn("Cannot create user data directory", dialog_calls[0][1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
