# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Codex Claude Dev Loop desktop application.

Build a Windows onedir bundle that turns the Python+web app into a
double-clickable ``CodexClaudeDevLoop.exe``.

Usage::

    py -3 -m pip install pyinstaller pywebview
    py -3 -m PyInstaller packaging\\CodexClaudeDevLoop.spec --noconfirm

Output lands in ``dist\\CodexClaudeDevLoop\\``. The single executable is
``dist\\CodexClaudeDevLoop\\CodexClaudeDevLoop.exe``.

Design notes
------------
* ``runtime_hooks`` sets ``sys.frozen`` and other conventions PyInstaller
  already injects; the resource-root logic in ``gui.server`` and
  ``desktop_app`` keys off ``sys._MEIPASS`` for both onedir and onefile,
  so no extra hook is required for path resolution.
* The bundled ``gui/static`` directory is shipped verbatim so the web
  console loads without a 404. Templates under ``docs/``,
  ``.claude/settings.json`` and ``scripts/run-claude.ps1`` are bundled so
  "Initialize project" can copy them into the user's project.
* User data (task store, settings, logs) is intentionally **not**
  bundled; the desktop app writes it to
  ``%LOCALAPPDATA%\\CodexClaudeDevLoop``.
* Git, PowerShell, Claude CLI, Codex CLI and VS Code are explicitly not
  bundled; the dependency probe in ``desktop_app.detect_dependencies``
  reports their availability at startup.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


SPEC_DIR = Path(SPECPATH).resolve()  # type: ignore[name-defined]  # noqa: F821
PROJECT_ROOT = SPEC_DIR.parent

# Bundle every file under these top-level trees as data. ``gui/static`` is
# needed for the web UI, the ``docs`` templates and ``.claude/settings.json``
# are needed by "Initialize project", and ``scripts/run-claude.ps1`` is
# copied into target projects on initialization.
datas = [
    (str(PROJECT_ROOT / "gui" / "static"), "gui/static"),
    (str(PROJECT_ROOT / "gui" / "orchestrator"), "gui/orchestrator"),
    (str(PROJECT_ROOT / "docs" / "PLAN.template.md"), "docs"),
    (
        str(PROJECT_ROOT / "docs" / "IMPLEMENTATION_REPORT.template.md"),
        "docs",
    ),
    (str(PROJECT_ROOT / "docs" / "CODEX_REVIEW.schema.json"), "docs"),
    (str(PROJECT_ROOT / ".claude" / "settings.json"), ".claude"),
    (str(PROJECT_ROOT / "scripts" / "run-claude.ps1"), "scripts"),
]

# pywebview ships platform-specific runtime files (ActiveX / WebView2
# bindings). Collect them so the packaged window can actually start.
datas += collect_data_files("webview", include_py_files=False)
hiddenimports = [
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.msdhtml",
]
hiddenimports += collect_submodules("webview")


a = Analysis(
    [str(PROJECT_ROOT / "desktop_app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Source-control / IDE / Python dev tools we never want in the
        # bundle. tk is included via Tkiner for the fallback dialog path.
        "pytest",
        "py",
        "IPython",
        "jupyter",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexClaudeDevLoop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)


coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CodexClaudeDevLoop",
)
