; Codex Claude Dev Loop - Inno Setup installer script.
;
; Build steps:
;   1. Run packaging\build-exe.ps1 to produce dist\CodexClaudeDevLoop\.
;   2. Open Inno Setup Compiler (https://jrsoftware.org/isdl.php) and
;      compile this file (Ctrl+Alt+C). The Setup.exe lands in
;      packaging\Output\ by default.
;
; This installer ships the prebuilt onedir bundle. It does NOT bundle
; Git, PowerShell, Claude CLI, Codex CLI or VS Code; the desktop app
; reports missing dependencies at startup.

#define MyAppName        "Codex Claude Dev Loop"
#define MyAppExeName     "CodexClaudeDevLoop.exe"
#define MyAppPublisher   "CodexClaudeDevLoop"
#define MyAppURL         "https://github.com/codex-claude-dev-loop/codex-claude-dev-loop-vscode"
#define MyAppVersion     "0.1.0"

[Setup]
AppId={{8D7C0F5E-2D9A-4F0F-9F2C-4D1F3F4F1A23}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=CodexClaudeDevLoopSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional shortcuts:"

[Files]
; Pull in everything produced by PyInstaller. Flags: ignoreversion skips
; version comparison, recursesubdirs includes _internal\, createallsubdirs
; ensures empty directories are preserved.
Source: "..\dist\CodexClaudeDevLoop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User data under %LOCALAPPDATA% is left in place on uninstall so users
; do not lose task state if they reinstall.
