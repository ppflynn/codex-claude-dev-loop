# Packaging

This folder holds the Windows desktop build pipeline for Codex Claude
Dev Loop.

## Layout

- `CodexClaudeDevLoop.spec` — PyInstaller spec for a Windows onedir
  build. Output is `dist\CodexClaudeDevLoop\CodexClaudeDevLoop.exe`.
- `build-exe.ps1` — Convenience wrapper that runs PyInstaller with the
  spec above. Use it for local builds.
- `installer.iss` — Inno Setup script (optional). Compile it with the
  Inno Setup Compiler to ship a `Setup.exe` that creates Start Menu and
  Desktop shortcuts and an uninstaller.

## Build the EXE

Prerequisites (only required on the build machine):

```powershell
py -3 -m pip install pyinstaller pywebview
```

Build the onedir bundle:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1
powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1 -Clean
```

The output lands in:

```
dist\CodexClaudeDevLoop\
├─ CodexClaudeDevLoop.exe
└─ _internal\
    ├─ gui\           (Python source + static web assets)
    ├─ scripts\       (run-claude.ps1, copied into target projects)
    ├─ docs\          (templates, schema)
    ├─ .claude\       (project settings template)
    └─ ...            (Python runtime + pywebview)
```

Double-click `CodexClaudeDevLoop.exe` to launch. User state, settings
and logs land in `%LOCALAPPDATA%\CodexClaudeDevLoop`.

## Build the installer (optional)

Install [Inno Setup](https://jrsoftware.org/isdl.php), then either:

1. Open `installer.iss` in the Inno Setup Compiler GUI and press
   Ctrl+Alt+C, or
2. Run `iscc.exe packaging\installer.iss` from the command line.

The `Setup.exe` is written to `packaging\Output\` by default.

## What is bundled

- Python runtime + the `gui` and `gui.orchestrator` packages.
- Static web assets (`gui/static`, including xterm.js).
- Resource templates: `docs/PLAN.template.md`,
  `docs/IMPLEMENTATION_REPORT.template.md`,
  `docs/CODEX_REVIEW.schema.json`, `.claude/settings.json`,
  `scripts/run-claude.ps1`.
- `pywebview` (Windows WebView2 / Edge Chromium runtime is provided by
  the OS on Windows 10/11).

## What is NOT bundled

Per the project safety boundary, the EXE intentionally does **not**
ship:

- Git (the user must install it separately)
- PowerShell (provided by Windows; PowerShell 7 optional)
- Claude CLI and Codex CLI (the user installs and authenticates these)
- VS Code

`desktop_app.detect_dependencies()` reports which are available each
launch and the app refuses to start only when Git or PowerShell is
missing.
