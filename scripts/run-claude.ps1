<#
.SYNOPSIS
    AI Coding Collaboration Tool — Run Claude Code
.DESCRIPTION
    Reads docs/PLAN.md and invokes Claude Code to implement the plan.
    After implementation, Claude generates docs/IMPLEMENTATION_REPORT.md.
    All execution output is saved to docs/claude-run.log.
.PARAMETER None
    All configuration is read from docs/PLAN.md
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1
#>

$ErrorActionPreference = "Stop"

# Resolve project root (parent of scripts/)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $ProjectDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " AI Coding Collaboration Tool"          -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# Step 1: Check Git repository
# ============================================================
Write-Host "[1/4] Checking Git repository..." -ForegroundColor Yellow
$gitCheck = git rev-parse --is-inside-work-tree 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Current directory is not a Git repository." -ForegroundColor Red
    Write-Host "  $ProjectDir" -ForegroundColor Red
    Write-Host "Please run this script from within a Git repository." -ForegroundColor Red
    exit 1
}
Write-Host "  OK: Git repository found." -ForegroundColor Green

# ============================================================
# Step 2: Check docs/PLAN.md exists
# ============================================================
Write-Host "[2/4] Checking docs/PLAN.md..." -ForegroundColor Yellow
if (-not (Test-Path "docs/PLAN.md")) {
    Write-Host "ERROR: docs/PLAN.md not found." -ForegroundColor Red
    Write-Host "  Expected: $ProjectDir\docs\PLAN.md" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "To fix this:" -ForegroundColor Yellow
    Write-Host "  1. Copy docs/PLAN.template.md to docs/PLAN.md" -ForegroundColor Yellow
    Write-Host "  2. Fill in your development task" -ForegroundColor Yellow
    Write-Host "  3. Run this script again" -ForegroundColor Yellow
    exit 1
}
$planSize = (Get-Item "docs/PLAN.md").Length
Write-Host "  OK: docs/PLAN.md found ($planSize bytes)." -ForegroundColor Green

# ============================================================
# Step 3: Check claude command
# ============================================================
Write-Host "[3/4] Checking Claude CLI..." -ForegroundColor Yellow
$claudeVersion = try { claude --version 2>&1 } catch { $null }
if ($LASTEXITCODE -ne 0 -or -not $claudeVersion) {
    Write-Host "ERROR: 'claude' command not available." -ForegroundColor Red
    Write-Host "Please install Claude Code CLI first." -ForegroundColor Red
    Write-Host "  https://docs.anthropic.com/en/docs/claude-code/overview" -ForegroundColor Yellow
    exit 1
}
Write-Host "  OK: Claude CLI found ($claudeVersion)." -ForegroundColor Green

# ============================================================
# Step 4: Run Claude Code
# ============================================================
Write-Host "[4/4] Running Claude Code..." -ForegroundColor Yellow
Write-Host ""

# Build the prompt that tells Claude what to do
$prompt = @"
You are implementing a development plan for the current project.

## Your Task

1. Read docs/PLAN.md to understand what needs to be done.
2. Implement all changes described in the plan by modifying code files.
3. After implementation, run any available tests to verify your changes.
4. Read docs/IMPLEMENTATION_REPORT.template.md for the expected report format.
5. Write a detailed implementation report to docs/IMPLEMENTATION_REPORT.md.
   The report MUST follow the template format and include:
   - What was changed and why
   - Complete file change list (paths, operations, descriptions)
   - Exact test commands run and their full output
   - Any issues encountered during implementation
   - Any plan items that could not be completed and why

## Safety Rules -- YOU MUST FOLLOW ALL OF THESE

- DO NOT run: git commit, git push, git reset --hard, git clean
- DO NOT delete any existing files (modify is OK, delete is NOT)
- DO NOT read or output the contents of any .env file
- DO NOT modify the .git directory
- DO NOT develop MCP servers, web pages, databases, or background task systems
- DO NOT add multi-agent parallelism
- Only create/modify files that are directly needed to implement the plan
"@

$logFile = "docs/claude-run.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Initialize log file (overwrite previous run)
@"
========================================
Claude Code Execution Log
Started: $timestamp
Project: $ProjectDir
========================================

--- Prompt ---
$prompt
--- End Prompt ---

========================================
Claude Code Output
========================================

"@ | Out-File -FilePath $logFile -Encoding UTF8

# Execute Claude in non-interactive print mode
# Capture output first, THEN read $LASTEXITCODE to avoid pipeline reset
# Set output encoding to UTF-8 so PowerShell correctly decodes Claude's UTF-8 output
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$claudeOutput = & claude -p --permission-mode bypassPermissions $prompt 2>&1
$exitCode = $LASTEXITCODE

# Display output to terminal
$claudeOutput | ForEach-Object { Write-Host $_ }

# Write captured output to log file with proper UTF8 encoding
$claudeOutput | Out-File -FilePath $logFile -Append -Encoding UTF8

# If $LASTEXITCODE is null (unlikely but defensive), check process success
if ($null -eq $exitCode) { $exitCode = 0 }

# Write log footer
$endTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
@"

========================================
Execution finished: $endTimestamp
Exit code: $exitCode
========================================
"@ | Out-File -FilePath $logFile -Append -Encoding UTF8

# ============================================================
# Result summary
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
if ($exitCode -eq 0) {
    Write-Host " Claude Code finished successfully." -ForegroundColor Green

    # Verify that IMPLEMENTATION_REPORT.md was actually generated
    if (Test-Path "docs/IMPLEMENTATION_REPORT.md") {
        $reportSize = (Get-Item "docs/IMPLEMENTATION_REPORT.md").Length
        if ($reportSize -gt 0) {
            Write-Host " IMPLEMENTATION_REPORT.md generated ($reportSize bytes)." -ForegroundColor Green
        } else {
            Write-Host " WARNING: IMPLEMENTATION_REPORT.md is empty." -ForegroundColor Red
            $exitCode = 3
        }
    } else {
        Write-Host " WARNING: IMPLEMENTATION_REPORT.md was not generated." -ForegroundColor Red
        Write-Host " Claude ran without crashing but may not have completed the task." -ForegroundColor Yellow
        $exitCode = 3
    }

    Write-Host " Please verify changes with: git status" -ForegroundColor Yellow
} else {
    Write-Host " Claude Code exited with code: $exitCode" -ForegroundColor Red
}
Write-Host " Log saved to: $logFile" -ForegroundColor Cyan
Write-Host " Report: docs/IMPLEMENTATION_REPORT.md" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

exit $exitCode
