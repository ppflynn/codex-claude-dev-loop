<#
.SYNOPSIS
    AI Coding Collaboration CLI Orchestrator — Run Claude Code with Test Verification and Fix Loop
.DESCRIPTION
    Reads docs/PLAN.md and invokes Claude Code to implement the plan.
    After implementation, runs tests to verify correctness.
    If tests fail, enters an automated fix loop (up to MAX_ROUNDS).
    Generates structured artifacts: git status, git diff, test results, and review schema.
    All execution output is saved to docs/claude-run.log with secret redaction.
.PARAMETER MaxRounds
    Maximum number of Claude fix rounds (default: 3, range: 1-15)
.PARAMETER SkipTests
    Skip automatic test execution (useful for manual verification)
.PARAMETER AllowNoTests
    Allow the script to pass when no test commands are discovered.
    By default, no test commands results in NEEDS_MANUAL_VERIFY (exit 2).
.PARAMETER SkipCodexReview
    Allow the script to pass without a CODEX_REVIEW.json file.
    By default, missing CODEX_REVIEW.json results in NEEDS_CODEX_REVIEW (exit 8).
.PARAMETER ReviewCommand
    Optional external review command to run automatically after tests pass.
    The command must read docs/REVIEW_INPUT.md and write docs/CODEX_REVIEW.json.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 5
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -AllowNoTests
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -SkipCodexReview
    powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -ReviewCommand "powershell -File scripts/your-reviewer.ps1"
#>

param(
    [ValidateRange(1, 15)]
    [int]$MaxRounds = 3,
    [switch]$SkipTests = $false,
    [switch]$AllowNoTests = $false,
    [switch]$SkipCodexReview = $false,
    [string]$ReviewCommand = ""
)

$ErrorActionPreference = "Stop"

# ============================================================
# Helper: Write timestamped message to terminal and log
# ============================================================
$Script:LogEntries = [System.Collections.ArrayList]::new()
function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$timestamp] $Message"
    [void]$Script:LogEntries.Add($entry)
    Write-Host "  $Message" -ForegroundColor $Color
}

# ============================================================
# P1-4: Secret scanning - detect and redact secrets in text
# ============================================================
function Watch-Secrets {
    param([string]$Text, [string]$Source)
    $redacted = $Text

    # Scan for high-entropy strings (potential API keys / tokens)
    $highEntropyPatterns = @(
        @{Label='API Key (sk-)'; Pattern='\bsk-[a-zA-Z0-9_-]{20,}\b'},
        @{Label='API Key (pk-)'; Pattern='\bpk-[a-zA-Z0-9_-]{20,}\b'},
        @{Label='GitHub Token';   Pattern='\bghp_[a-zA-Z0-9]{30,}\b'},
        @{Label='JWT Token';      Pattern='\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b'},
        @{Label='Generic Token';  Pattern='\b(token|secret|key|password|api_key|apikey)\s*[:=]\s*[''"]?\S{12,}[''"]?'},
        @{Label='Private Key';    Pattern='-----BEGIN\s+(RSA|EC|DSA|OPENSSH|PGP)\s+PRIVATE\s+KEY-----'},
        @{Label='ConnectionString'; Pattern='\b(Server|Database|User\s*Id|Password)\s*=\s*[^;]{4,};'}
    )

    $foundAny = $false
    foreach ($p in $highEntropyPatterns) {
        if ($redacted -match $p.Pattern) {
            if (-not $foundAny) {
                Write-Log "SECURITY: Potential secrets detected in $Source — redacting" "Yellow"
                $foundAny = $true
            }
            Write-Log "  Found pattern: $($p.Label)" "Yellow"
            $redacted = $redacted -replace $p.Pattern, '[REDACTED]'
        }
    }

    # Always scan for .env content exposure
    if ($redacted -match '\.env[^\n]{0,50}([A-Za-z0-9_]{3,20})\s*=\s*[^\n]{4,}') {
        Write-Log "SECURITY: Possible .env content detected — redacting" "Yellow"
        $redacted = $redacted -replace '(^|\n)[ \t]*([A-Za-z0-9_]{3,30})\s*=\s*[^\n]{4,}','$1$2=[REDACTED]'
    }

    return $redacted
}

# ============================================================
# Helper: Safe file write with explicit UTF-8 (no BOM in PS7+, BOM in PS5)
# ============================================================
function Write-FileUtf8 {
    param([string]$FilePath, [string]$Content)
    # Use .NET for explicit UTF-8 without BOM when possible
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $FilePath -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path),
        $Content, $utf8NoBom)
}

# ============================================================
# Helper: Capture native command output without treating stderr
# warnings as fatal PowerShell errors.
# ============================================================
function Invoke-NativeCapture {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $FilePath @ArgumentList 2>&1
        $exitCode = $LASTEXITCODE
        return [PSCustomObject]@{
            Output   = @($output | ForEach-Object { "$_" })
            ExitCode = $exitCode
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

# ============================================================
# Helper: Discover test commands from project
# ============================================================
function Find-TestCommands {
    $commands = @()

    # Check PLAN.md for explicit test commands
    if (Test-Path "docs/PLAN.md") {
        $planContent = Get-Content "docs/PLAN.md" -Raw -Encoding UTF8
        # Match common test command patterns
        if ($planContent -match 'pytest\s+\S+') {
            $commands += $matches[0]
        }
        if ($planContent -match 'node\s+\S+test\S*\.js') {
            $commands += $matches[0]
        }
        if ($planContent -match 'npm\s+test') {
            $commands += 'npm test'
        }
        if ($planContent -match 'npm\s+run\s+\S+test\S*') {
            $commands += $matches[0]
        }
        if ($planContent -match 'python\s+-m\s+pytest\b') {
            $commands += $matches[0]
        }
        if ($planContent -match 'py\s+-B?\s*-m\s+pytest\b') {
            $commands += $matches[0]
        }
    }

    # Auto-discover: pytest (Python)
    if (Test-Path "demo-project/test_calculator.py") {
        $cmd = "py -B -m pytest demo-project -q -p no:cacheprovider"
        if ($cmd -notin $commands) { $commands += $cmd }
    }

    # Auto-discover: package.json test script (Node.js)
    if (Test-Path "demo-project/package.json") {
        $pkg = Get-Content "demo-project/package.json" -Raw | ConvertFrom-Json
        if ($pkg.scripts -and $pkg.scripts.test) {
            $npmCmd = "npm --prefix demo-project test"
            if ($npmCmd -notin $commands) { $commands += $npmCmd }
        }
    }

    # Auto-discover: Node.js test file
    if (Test-Path "demo-project/test.js") {
        $cmd = "node demo-project/test.js"
        if ($cmd -notin $commands) { $commands += $cmd }
    }

    return $commands
}

# ============================================================
# P2-2: Quote-aware command-line splitter
# Handles double-quoted arguments that contain spaces,
# e.g.  pytest "path with spaces/test_file.py"
# ============================================================
function Split-CommandLine {
    param([string]$CommandLine)
    $parts = @()
    $current = ''
    $inQuotes = $false
    $i = 0
    while ($i -lt $CommandLine.Length) {
        $ch = $CommandLine[$i]
        if ($ch -eq '"') {
            $inQuotes = -not $inQuotes
            $i++
            continue
        }
        if ((-not $inQuotes) -and ($ch -eq ' ' -or $ch -eq "`t")) {
            if ($current.Length -gt 0) {
                $parts += $current
                $current = ''
            }
            $i++
            continue
        }
        $current += $ch
        $i++
    }
    if ($current.Length -gt 0) {
        $parts += $current
    }
    return $parts
}

# ============================================================
# P0-3: Validate CODEX_REVIEW.json against schema
# ============================================================
function Test-CodexReviewJson {
    param([string]$JsonPath)

    if (-not (Test-Path $JsonPath)) {
        Write-Log "CODEX_REVIEW.json not found at $JsonPath" "Red"
        return $false
    }

    $schemaPath = Join-Path $ProjectDir "docs/CODEX_REVIEW.schema.json"
    if (-not (Test-Path $schemaPath)) {
        Write-Log "WARNING: CODEX_REVIEW.schema.json not found — skipping schema validation" "Yellow"
    }

    try {
        $json = Get-Content $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json

        # Validate required top-level fields. Use property existence checks
        # rather than truthiness so valid empty arrays like findings: [] pass.
        $jsonProperties = @($json.PSObject.Properties.Name)
        if ('status' -notin $jsonProperties) {
            Write-Log "CODEX_REVIEW.json: missing required field 'status'" "Red"
            return $false
        }
        if ('reviewed_at' -notin $jsonProperties) {
            Write-Log "CODEX_REVIEW.json: missing required field 'reviewed_at'" "Red"
            return $false
        }
        if ($json.status -notin @('PASS','FAIL','NEEDS_FIX')) {
            Write-Log "CODEX_REVIEW.json: invalid status '$($json.status)' — expected PASS/FAIL/NEEDS_FIX" "Red"
            return $false
        }
        if ('findings' -notin $jsonProperties) {
            Write-Log "CODEX_REVIEW.json: missing required field 'findings'" "Red"
            return $false
        }
        if ($json.findings -isnot [array]) {
            Write-Log "CODEX_REVIEW.json: 'findings' must be an array" "Red"
            return $false
        }

        # Validate each finding
        $requiredFields = @('id','severity','file','description')
        foreach ($finding in $json.findings) {
            foreach ($field in $requiredFields) {
                if (-not $finding.$field) {
                    Write-Log "CODEX_REVIEW.json: finding missing required field '$field'" "Red"
                    return $false
                }
            }
            if ($finding.severity -notin @('P0','P1','P2','P3')) {
                Write-Log "CODEX_REVIEW.json: finding '$($finding.id)' has invalid severity '$($finding.severity)'" "Red"
                return $false
            }
        }

        Write-Log "CODEX_REVIEW.json: validation PASSED ($($json.findings.Count) findings, status=$($json.status))" "Green"
        return $true
    } catch {
        Write-Log "CODEX_REVIEW.json: failed to parse — $($_.Exception.Message)" "Red"
        return $false
    }
}

# ============================================================
# P0-1: Build a Codex-fix prompt from CODEX_REVIEW.json findings.
# Injects finding id, severity, file, description, and fix suggestion
# into a structured Claude prompt so Claude can address each finding.
# ============================================================
function New-CodexFixPrompt {
    param(
        [array]$Findings,
        [int]$Round,
        [int]$MaxRounds
    )

    $findingItems = ($Findings | ForEach-Object {
        $entry = "- **$($_.id)** [$($_.severity)] ``$($_.file)`` — $($_.description)"
        if ($_.fix_suggestion) {
            $entry += "`n  > Fix suggestion: $($_.fix_suggestion)"
        }
        $entry
    }) -join "`n`n"

    $changesDiff   = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("diff")).Output
    $changesStatus = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("status", "--short", "--untracked-files=all")).Output

    return @"
## Codex Review Fix Round $Round / $MaxRounds

The Codex review has identified the following issues that require fixing:

$findingItems

### Current Changes (git status)
```
$($changesStatus -join "`n")
```

### Current Diff
```
$($changesDiff -join "`n")
```

### Your Task

1. Read each affected file and fix every Codex finding listed above.
2. Apply the suggested fixes, or a better approach if you identify one.
3. Run all tests to verify your fixes do not break anything.
4. Update docs/IMPLEMENTATION_REPORT.md with:
   - Which Codex findings were fixed and how
   - The test results after your fixes
   - Any findings that could NOT be fixed and why

### Safety Rules -- YOU MUST FOLLOW ALL OF THESE

- DO NOT run: git commit, git push, git reset --hard, git clean
- DO NOT delete any existing files (modify is OK, delete is NOT)
- DO NOT read or output the contents of any .env file
- DO NOT modify the .git directory
- DO NOT develop MCP servers, web pages, databases, or background task systems
- DO NOT add multi-agent parallelism
"@
}

# ============================================================
# P0-1: Build review input bundle for Codex/external reviewer.
# The external reviewer must read docs/REVIEW_INPUT.md and write
# docs/CODEX_REVIEW.json in the schema defined by CODEX_REVIEW.schema.json.
# ============================================================
function New-ReviewInputBundle {
    param(
        [int]$Round,
        [string]$TentativeResult,
        [array]$TestResults
    )

    $changesStatus = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("status", "--short", "--untracked-files=all")).Output
    $changesDiff   = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("diff")).Output
    $headResult    = Invoke-NativeCapture -FilePath "git" -ArgumentList @("rev-parse", "HEAD")
    $headSha       = if ($headResult.ExitCode -eq 0 -and $headResult.Output.Count -gt 0) { $headResult.Output[0] } else { "UNKNOWN" }

    $recentTests = @($TestResults | Where-Object { $_.Round -eq $Round })
    if ($recentTests.Count -eq 0) {
        $recentTests = @($TestResults | Select-Object -Last 10)
    }

    $testText = if ($recentTests.Count -gt 0) {
        ($recentTests | ForEach-Object {
@"
--- Test: $($_.Command)
Round: $($_.Round)
Exit Code: $($_.ExitCode)
$($_.Output)
"@
        }) -join "`n`n"
    } else {
        "(no test output captured for this round)"
    }

    $testResultsPath = "docs/TEST_RESULTS.txt"
    @"
=== Test Results For Review ===
Timestamp: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Round: $Round
Tentative Result: $TentativeResult

$testText
=== End Test Results ===
"@ | Out-File -FilePath $testResultsPath -Encoding UTF8

    $reviewInputPath = "docs/REVIEW_INPUT.md"
    @"
# Codex Review Input

Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Round: $Round
Tentative result: $TentativeResult
HEAD: $headSha

## Required Output

Write a valid JSON file to docs/CODEX_REVIEW.json using this schema:

~~~json
{
  "status": "PASS | FAIL | NEEDS_FIX",
  "findings": [
    {
      "id": "P1-1",
      "severity": "P0 | P1 | P2 | P3",
      "file": "relative/path",
      "line": null,
      "description": "what is wrong",
      "fix_suggestion": "how to fix it"
    }
  ],
  "reviewed_at": "2026-06-11T00:00:00Z",
  "review_scope": "round $Round git status, git diff, and tests",
  "summary": "short summary"
}
~~~

Return PASS only when there are no P0/P1 issues and no blocking P2.
Do not reject PASS for P3-only suggestions.

## Git Status

~~~text
$($changesStatus -join "`n")
~~~

## Git Diff

~~~diff
$($changesDiff -join "`n")
~~~

## Test Results

~~~text
$testText
~~~
"@ | Out-File -FilePath $reviewInputPath -Encoding UTF8

    Write-Log "Review input bundle generated: $reviewInputPath, $testResultsPath" "Cyan"
    return $reviewInputPath
}

# ============================================================
# P0-1: Invoke an optional external review command.
# The command is split into executable + args and run directly, not through
# cmd /c or Invoke-Expression.
# ============================================================
function Invoke-AutoCodexReview {
    param(
        [string]$CommandLine,
        [int]$Round
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return @{ Success = $true; Skipped = $true; Output = "" }
    }

    if (Test-Path "docs/CODEX_REVIEW.json") {
        $backupPath = "docs/CODEX_REVIEW.json.before-auto"
        try {
            if (Test-Path $backupPath) { Remove-Item $backupPath -Force -ErrorAction Stop }
            Move-Item -Path "docs/CODEX_REVIEW.json" -Destination $backupPath -Force -ErrorAction Stop
            Write-Log "Existing CODEX_REVIEW.json moved to $backupPath before auto review" "DarkGray"
        } catch {
            Write-Log "WARNING: Could not move existing CODEX_REVIEW.json before auto review: $($_.Exception.Message)" "Yellow"
            return @{ Success = $false; Skipped = $false; Output = "$_" }
        }
    }

    Write-Log "Running automatic review command (round $Round): $CommandLine" "Cyan"
    try {
        $cmdParts = Split-CommandLine -CommandLine $CommandLine
        if ($cmdParts.Count -eq 0) {
            return @{ Success = $false; Skipped = $false; Output = "ReviewCommand parsed to zero parts" }
        }

        $reviewExe = $cmdParts[0]
        $reviewArgs = @()
        if ($cmdParts.Count -gt 1) {
            $reviewArgs = @($cmdParts[1..($cmdParts.Count - 1)])
        }

        if ($reviewArgs.Count -gt 0) {
            $reviewOutput = & $reviewExe $reviewArgs 2>&1
        } else {
            $reviewOutput = & $reviewExe 2>&1
        }
        $reviewExit = $LASTEXITCODE
        $outputText = ($reviewOutput | ForEach-Object { "$_" }) -join "`n"

        if ($reviewExit -ne 0) {
            Write-Log "Automatic review command failed with exit $reviewExit" "Red"
            return @{ Success = $false; Skipped = $false; Output = $outputText; ExitCode = $reviewExit }
        }

        Write-Log "Automatic review command completed successfully" "Green"
        return @{ Success = $true; Skipped = $false; Output = $outputText; ExitCode = $reviewExit }
    } catch {
        Write-Log "Automatic review command failed to execute: $($_.Exception.Message)" "Red"
        return @{ Success = $false; Skipped = $false; Output = "$_" }
    }
}

# ============================================================
# P0-1: In-loop Codex review check.
# Called when tests pass inside the orchestration loop.
# - PASS status       → return Action="PASS" (success)
# - NEEDS_FIX status  → consume the file, return Action="CODEX_FIX" + Findings
# - FAIL/INVALID      → return Action with exit Result
# - NOT_PRESENT       → honour -SkipCodexReview or return NEEDS_CODEX_REVIEW
# ============================================================
function Invoke-InLoopCodexCheck {
    param(
        [int]$Round,
        [int]$MaxRounds
    )

    if (-not (Test-Path "docs/CODEX_REVIEW.json")) {
        if ($SkipCodexReview) {
            Write-Log "CODEX_REVIEW.json not found — review skipped (SkipCodexReview set)" "Yellow"
            return @{ Action = "PASS" }
        } else {
            Write-Log "CODEX_REVIEW.json not found — Codex review required before release" "Red"
            return @{ Action = "EXIT"; Result = "NEEDS_CODEX_REVIEW" }
        }
    }

    $valid = Test-CodexReviewJson -JsonPath "docs/CODEX_REVIEW.json"
    if (-not $valid) {
        Write-Log "CODEX_REVIEW.json is invalid — cannot PASS" "Red"
        return @{ Action = "EXIT"; Result = "FAIL_CODEX_REVIEW_INVALID" }
    }

    try {
        $json = Get-Content "docs/CODEX_REVIEW.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Log "CODEX_REVIEW.json: failed to parse — $($_.Exception.Message)" "Red"
        return @{ Action = "EXIT"; Result = "FAIL_CODEX_REVIEW_INVALID" }
    }

    Write-Log "CODEX_REVIEW.json status: $($json.status) ($($json.findings.Count) findings)" "Cyan"

    switch ($json.status) {
        "PASS" {
            Write-Log "Codex review PASSED — all clear." "Green"
            return @{ Action = "PASS" }
        }
        "FAIL" {
            Write-Log "Codex review FAILED — review rejected." "Red"
            return @{ Action = "EXIT"; Result = "FAIL_CODEX_REVIEW" }
        }
        "NEEDS_FIX" {
            if ($Round -lt $MaxRounds) {
                # Consume the file so we don't re-read the same findings.
                # The user must provide a new CODEX_REVIEW.json after the fix round.
                $previousPath = "docs/CODEX_REVIEW.json.previous"
                try {
                    if (Test-Path $previousPath) { Remove-Item $previousPath -Force -ErrorAction Stop }
                    Move-Item -Path "docs/CODEX_REVIEW.json" -Destination "docs/CODEX_REVIEW.json.previous" -Force -ErrorAction Stop
                } catch {
                    Write-Log "WARNING: Could not consume CODEX_REVIEW.json: $($_.Exception.Message)" "Yellow"
                    # Continue anyway — the post-loop check will catch a stale NEEDS_FIX
                }
                Write-Log "CODEX_REVIEW.json consumed (moved to .previous) — injecting $($json.findings.Count) findings into fix round." "Yellow"
                return @{
                    Action   = "CODEX_FIX"
                    Findings = @($json.findings)
                }
            } else {
                Write-Log "CODEX_REVIEW.json is NEEDS_FIX but max rounds ($MaxRounds) reached." "Red"
                return @{ Action = "EXIT"; Result = "NEEDS_FIX_CODEX_REVIEW" }
            }
        }
        default {
            Write-Log "CODEX_REVIEW.json: unknown status '$($json.status)'" "Red"
            return @{ Action = "EXIT"; Result = "FAIL_CODEX_REVIEW_INVALID" }
        }
    }
}

# ============================================================
# Resolve project root
# ============================================================
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $ProjectDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " AI Coding Collaboration CLI Orchestrator" -ForegroundColor Cyan
Write-Host " Max Rounds: $MaxRounds"                 -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# Safeguard: backup old IMPLEMENTATION_REPORT.md (P2-2)
# ============================================================
$oldReportBackup = $null
if (Test-Path "docs/IMPLEMENTATION_REPORT.md") {
    $backupPath = "docs/IMPLEMENTATION_REPORT.md.bak"
    Copy-Item "docs/IMPLEMENTATION_REPORT.md" $backupPath -Force
    $oldReportBackup = $backupPath
    Write-Log "Backed up previous report to $backupPath" "DarkGray"
}

# ============================================================
# Step 1: Check Git repository
# ============================================================
Write-Host "[1/5] Checking Git repository..." -ForegroundColor Yellow
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
Write-Host "[2/5] Checking docs/PLAN.md..." -ForegroundColor Yellow
if (-not (Test-Path "docs/PLAN.md")) {
    Write-Host "ERROR: docs/PLAN.md not found." -ForegroundColor Red
    Write-Host "  Expected: $ProjectDir\docs\PLAN.md" -ForegroundColor Red
    Write-Host ""
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
Write-Host "[3/5] Checking Claude CLI..." -ForegroundColor Yellow
$claudeVersion = try { claude --version 2>&1 } catch { $null }
if ($LASTEXITCODE -ne 0 -or -not $claudeVersion) {
    Write-Host "ERROR: 'claude' command not available." -ForegroundColor Red
    Write-Host "Please install Claude Code CLI first." -ForegroundColor Red
    Write-Host "  https://docs.anthropic.com/en/docs/claude-code/overview" -ForegroundColor Yellow
    exit 1
}
Write-Host "  OK: Claude CLI found ($claudeVersion)." -ForegroundColor Green

# ============================================================
# P1-2: Record pre-run baseline
# ============================================================
Write-Host "[4/5] Recording pre-run baseline..." -ForegroundColor Yellow
$baselineStatusResult = Invoke-NativeCapture -FilePath "git" -ArgumentList @("status", "--porcelain")
$baselineDiffResult   = Invoke-NativeCapture -FilePath "git" -ArgumentList @("diff")
$baselineUntrackedResult = Invoke-NativeCapture -FilePath "git" -ArgumentList @("ls-files", "--others", "--exclude-standard")
$baselineStatus = $baselineStatusResult.Output
$baselineDiff   = $baselineDiffResult.Output
$baselineUntracked = $baselineUntrackedResult.Output

@"
=== Pre-run Baseline ===
Timestamp: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

--- git status --porcelain ---
$baselineStatus

--- git diff ---
$($baselineDiff -join "`n")

--- Untracked files ---
$($baselineUntracked -join "`n")
=== End Baseline ===
"@ | Out-File -FilePath "docs/BASELINE_STATUS.txt" -Encoding UTF8

$hadPreexistingChanges = ($baselineStatus -join "`n").Trim().Length -gt 0
if ($hadPreexistingChanges) {
    Write-Host "  NOTE: Workspace has pre-existing changes. This round's diff will isolate new changes." -ForegroundColor Yellow
    Write-Host "  Baseline saved to docs/BASELINE_STATUS.txt" -ForegroundColor Yellow
}

# P2-1: Parse baseline to identify pre-existing changed files for isolation
$baselineModifiedFiles = @{}
$baselineUntrackedFiles = @{}
if ($hadPreexistingChanges) {
    foreach ($line in ($baselineStatus -split "`n")) {
        $line = $line.Trim()
        if ($line -match '^\s*[MADRCU]\s+(.+)$') {
            $file = $matches[1].Trim()
            $baselineModifiedFiles[$file] = $true
        } elseif ($line -match '^\?\?\s+(.+)$') {
            $file = $matches[1].Trim()
            $baselineUntrackedFiles[$file] = $true
        }
    }
}
Write-Host "  OK: Baseline recorded." -ForegroundColor Green

# ============================================================
# Step 5: Run Claude Code (in orchestration loop)
# ============================================================
Write-Host "[5/5] Running orchestration loop..." -ForegroundColor Yellow
Write-Host ""

# Discover test commands
$TestCommands = @()
if (-not $SkipTests) {
    $TestCommands = Find-TestCommands
    if ($TestCommands.Count -gt 0) {
        Write-Log "Discovered test commands: $($TestCommands -join ', ')" "Cyan"
    } else {
        Write-Log "No test commands discovered. Tests will be skipped." "Yellow"
    }
}

# Initialize log file
$logFile = "docs/claude-run.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$logContent = @"
========================================
Claude Code Orchestration Log
Started: $timestamp
Project: $ProjectDir
Max Rounds: $MaxRounds
========================================

"@

# ============================================================
# P0-1+P0-2: Main orchestration loop with test verification
# ============================================================
$finalResult = "UNKNOWN"
$roundResultFiles = @()
$testResults = @()
$tentativeResult = $null
$roundAllTestsPassed = $false
# P0-1: Codex-fix round tracking — set when CODEX_REVIEW.json says NEEDS_FIX
# so the next round's prompt injects the findings instead of using the
# test-failure template.
$codexFixRound = $false
$codexFindings = $null

# P2-2: Ctrl+C handler — track subprocess for clean termination
$script:ClaudeProcess = $null
$script:CtrlCPressed = $false
$ctrlCHandler = {
    param($ctrlSender, $ctrlEventArgs)
    Write-Host "`n  Ctrl+C detected — cleaning up..." -ForegroundColor Yellow
    $ctrlEventArgs.Cancel = $true  # Prevent immediate script termination
    $script:CtrlCPressed = $true
    if ($script:ClaudeProcess -and !$script:ClaudeProcess.HasExited) {
        try {
            # Kill the process tree (claude may have child processes)
            Stop-Process -Id $script:ClaudeProcess.Id -Force -ErrorAction SilentlyContinue
            Write-Host "  Terminated Claude process (PID $($script:ClaudeProcess.Id))." -ForegroundColor Yellow
        } catch {
            Write-Host "  Could not terminate Claude process: $_" -ForegroundColor Red
        }
    }
}
try {
    # P2-2: Register Ctrl+C handler (safe: wrapped in try-catch for non-interactive sessions)
    try { [Console]::CancelKeyPress += $ctrlCHandler } catch {
        Write-Log "Note: Ctrl+C handler not registered (non-interactive session)" "DarkGray"
    }

    for ($round = 1; $round -le $MaxRounds; $round++) {
        Write-Host ""
        Write-Host "========================================" -ForegroundColor Magenta
        Write-Host " ROUND $round / $MaxRounds" -ForegroundColor Magenta
        Write-Host "========================================" -ForegroundColor Magenta

        # Build prompt based on round
        if ($round -eq 1) {
            # Initial implementation prompt
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
        } elseif ($codexFixRound) {
            # P0-1: Codex-fix round — inject Codex review findings into prompt.
            # Consume the flag so subsequent rounds revert to test-failure prompts
            # unless a new Codex review triggers another Codex-fix round.
            $findingCount = if ($codexFindings) { $codexFindings.Count } else { 0 }
            Write-Log "Round $round : Codex-fix prompt with $findingCount findings from CODEX_REVIEW.json" "Cyan"
            $prompt = New-CodexFixPrompt -Findings $codexFindings -Round $round -MaxRounds $MaxRounds
            $codexFixRound = $false
            $codexFindings = $null
        } else {
            # Fix round: feed test failures back to Claude
            $lastTestOutput = if ($testResults.Count -gt 0) { $testResults[-1].Output } else { "No test output available" }
            $lastTestExitCode = if ($testResults.Count -gt 0) { $testResults[-1].ExitCode } else { -1 }
            $changesDiff = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("diff")).Output
            $changesStatus = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("status", "--short", "--untracked-files=all")).Output

            $prompt = @"
## Fix Round $round / $MaxRounds

Your previous implementation has FAILING TESTS (exit code: $lastTestExitCode).

### Test Failure Output
```
$lastTestOutput
```

### Current Changes (git status)
```
$($changesStatus -join "`n")
```

### Current Diff
```
$($changesDiff -join "`n")
```

## Your Task

1. Analyze the test failures above and identify the root cause.
2. Fix the code to make all tests pass.
3. Run the tests again to verify your fixes.
4. Update docs/IMPLEMENTATION_REPORT.md with:
   - What was fixed and why
   - The test results after your fixes
   - Any remaining issues

## Safety Rules -- YOU MUST FOLLOW ALL OF THESE

- DO NOT run: git commit, git push, git reset --hard, git clean
- DO NOT delete any existing files (modify is OK, delete is NOT)
- DO NOT read or output the contents of any .env file
- DO NOT modify the .git directory
- DO NOT develop MCP servers, web pages, databases, or background task systems
- DO NOT add multi-agent parallelism
"@
        }

        # Remove old report (backup was already made)
        if (Test-Path "docs/IMPLEMENTATION_REPORT.md") {
            Remove-Item "docs/IMPLEMENTATION_REPORT.md" -Force
        }

        # Log prompt (redacted for secrets P1-4)
        $safePrompt = Watch-Secrets -Text $prompt -Source "prompt (round $round)"
        $logContent += @"

========================================
--- Round $round Prompt ---
$safePrompt
--- End Prompt ---

"@

        # Execute Claude in non-interactive print mode.
        # Safety is enforced by .claude/settings.json deny rules (tool layer)
        # and prompt-level safety rules (prompt layer).
        # P2-2: Snapshot existing claude processes before invocation so Ctrl+C
        # handler can identify and kill only the Claude process we spawned.
        Write-Host "  Invoking Claude Code..." -ForegroundColor Yellow
        [Console]::OutputEncoding = [Text.Encoding]::UTF8

        $beforeClaudePids = @(Get-Process -Name "claude" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $claudeOutput = & claude -p --permission-mode bypassPermissions $prompt 2>&1
        $exitCode = $LASTEXITCODE

        # Track any newly-spawned claude process for Ctrl+C cleanup
        $afterClaudePids = @(Get-Process -Name "claude" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $newClaudePid = $afterClaudePids | Where-Object { $_ -notin $beforeClaudePids }
        if ($newClaudePid) {
            $script:ClaudeProcess = Get-Process -Id $newClaudePid[0] -ErrorAction SilentlyContinue
        }

        # Check for Ctrl+C during Claude execution
        if ($script:CtrlCPressed) {
            Write-Log "Claude execution was interrupted by Ctrl+C" "Yellow"
            $finalResult = "INTERRUPTED"
            break
        }

        # Display output
        $claudeOutput | ForEach-Object { Write-Host $_ }

        # Redact secrets in output before logging (P1-4)
        $safeClaudeOutput = Watch-Secrets -Text ($claudeOutput -join "`n") -Source "Claude output (round $round)"
        $logContent += @"

========================================
--- Claude Output (Round $round) ---
$safeClaudeOutput
--- End Output ---

"@

        if ($null -eq $exitCode) { $exitCode = 0 }

        # Verify Claude exit code
        if ($exitCode -ne 0) {
            Write-Host "  Claude exited with code: $exitCode" -ForegroundColor Red
            $finalResult = "FAIL_CLAUDE_CRASH"
            break
        }

        # Verify IMPLEMENTATION_REPORT.md was generated
        if (-not (Test-Path "docs/IMPLEMENTATION_REPORT.md") -or (Get-Item "docs/IMPLEMENTATION_REPORT.md").Length -eq 0) {
            Write-Host "  WARNING: IMPLEMENTATION_REPORT.md was not generated or is empty." -ForegroundColor Red
            $finalResult = "FAIL_NO_REPORT"
            break
        }

        Write-Host "  IMPLEMENTATION_REPORT.md generated." -ForegroundColor Green

        # ============================================================
        # P1-1: Auto-collect git status and diff after Claude runs
        # ============================================================
        try {
        Write-Host "  Collecting change artifacts..." -ForegroundColor Yellow
        $roundTag = if ($round -gt 1) { "_R${round}" } else { "" }

        $changesStatus = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("status", "--short", "--untracked-files=all")).Output
        $changesDiff   = (Invoke-NativeCapture -FilePath "git" -ArgumentList @("diff")).Output

        $statusFile = "docs/CHANGES_STATUS${roundTag}.txt"
        $diffFile   = "docs/CHANGES_DIFF${roundTag}.txt"

        # Safely convert to string (handle $null or empty results)
        $statusText = if ($changesStatus) { $changesStatus -join "`n" } else { "(no changes)" }
        $diffText   = if ($changesDiff) { $changesDiff -join "`n" } else { "(no diff)" }

        @"
=== Changes Status (Round $round) ===
Timestamp: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
$statusText
=== End Status ===
"@ | Out-File -FilePath $statusFile -Encoding UTF8

        @"
=== Changes Diff (Round $round) ===
Timestamp: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
$diffText
=== End Diff ===
"@ | Out-File -FilePath $diffFile -Encoding UTF8

        $roundResultFiles += $statusFile
        $roundResultFiles += $diffFile
        Write-Log "Change artifacts saved: $statusFile, $diffFile" "Cyan"

        # P2-1: Generate this-round-only diff (isolated from pre-existing changes)
        if ($hadPreexistingChanges) {
            $thisRoundOnlyFile = "docs/CHANGES_THIS_ROUND${roundTag}.txt"
            $thisRoundLines = [System.Collections.ArrayList]::new()
            [void]$thisRoundLines.Add("=== This-Round Changes (Round $round) ===")
            [void]$thisRoundLines.Add("Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
            [void]$thisRoundLines.Add("")

            # Parse current git status and classify each file
            $statusLines = if ($changesStatus) { @($changesStatus) } else { @() }
            $thisRoundFiles = @()
            foreach ($line in $statusLines) {
                $lineTrimmed = "$line".Trim()
                if ($lineTrimmed -match '^\s*[MADRCU]\s+(.+)$') {
                    $file = $matches[1].Trim()
                    if ($baselineModifiedFiles.ContainsKey($file)) {
                        [void]$thisRoundLines.Add("  [PRE-EXISTING] $lineTrimmed")
                    } else {
                        [void]$thisRoundLines.Add("  [THIS ROUND]   $lineTrimmed")
                        $thisRoundFiles += $file
                    }
                } elseif ($lineTrimmed -match '^\?\?\s+(.+)$') {
                    $file = $matches[1].Trim()
                    if ($baselineUntrackedFiles.ContainsKey($file)) {
                        [void]$thisRoundLines.Add("  [PRE-EXISTING] $lineTrimmed")
                    } else {
                        [void]$thisRoundLines.Add("  [THIS ROUND]   $lineTrimmed")
                        $thisRoundFiles += $file
                    }
                } elseif ($lineTrimmed.Length -gt 0) {
                    [void]$thisRoundLines.Add("  [?]            $lineTrimmed")
                }
            }

            [void]$thisRoundLines.Add("")
            [void]$thisRoundLines.Add("=== Summary ===")
            [void]$thisRoundLines.Add("Pre-existing files from baseline: $($baselineModifiedFiles.Count + $baselineUntrackedFiles.Count)")
            [void]$thisRoundLines.Add("New files this round: $($thisRoundFiles.Count)")
            [void]$thisRoundLines.Add("=== End This-Round Changes ===")

            $thisRoundLines -join "`n" | Out-File -FilePath $thisRoundOnlyFile -Encoding UTF8
            $roundResultFiles += $thisRoundOnlyFile
            Write-Log "This-round isolation saved: $thisRoundOnlyFile ($($thisRoundFiles.Count) new files)" "Cyan"
        }
        } catch {
            Write-Log "WARNING: Error collecting change artifacts: $($_.Exception.Message)" "Yellow"
            # Don't crash the orchestrator for artifact collection failures
        }

        # ============================================================
        # P0-2: Actually run tests and capture exit codes
        # ============================================================
        if ($SkipTests) {
            Write-Log "Skipping tests (SkipTests flag set) — manual verification required" "Yellow"
            $tentativeResult = "SKIPPED"
        } elseif ($TestCommands.Count -eq 0) {
            if ($AllowNoTests) {
                Write-Log "No test commands configured. Allowing PASS (AllowNoTests set)." "Yellow"
                $tentativeResult = "PASS_NO_TESTS"
            } else {
                Write-Log "No test commands configured. Manual verification required." "Yellow"
                Write-Log "Use -AllowNoTests to permit PASS without tests, or add test commands to PLAN.md." "Yellow"
                $finalResult = "NEEDS_MANUAL_VERIFY"
                break
            }
        } else {
            Write-Host "  Running tests..." -ForegroundColor Yellow
            $roundAllTestsPassed = $true

            foreach ($testCmd in $TestCommands) {
                Write-Log "Running: $testCmd" "Cyan"
                # P1-1: Safe command execution — parse into exe + args array
                # to avoid shell injection via cmd /c string interpolation.
                try {
                    $cmdParts = Split-CommandLine -CommandLine $testCmd
                    $testExe = $cmdParts[0]
                    $testArgs = $cmdParts[1..($cmdParts.Count - 1)]
                    if ($testArgs.Count -gt 0) {
                        $testOutput = & $testExe $testArgs 2>&1
                    } else {
                        $testOutput = & $testExe 2>&1
                    }
                    $testExit = $LASTEXITCODE
                } catch {
                    # If the command itself can't be found (CommandNotFoundException),
                    # treat as test failure rather than crashing the orchestrator.
                    $testOutput = @("ERROR: Failed to execute test command: $_")
                    $testExit = -1
                    Write-Log "Test command failed to execute: $testCmd — $($_.Exception.Message)" "Red"
                }

                $testResults += [PSCustomObject]@{
                    Round    = $round
                    Command  = $testCmd
                    ExitCode = $testExit
                    Output   = $testOutput -join "`n"
                }

                $logContent += @"

========================================
--- Test (Round $round): $testCmd ---
Exit Code: $testExit
$($testOutput -join "`n")
--- End Test ---

"@

                Write-Host "    Command: $testCmd" -ForegroundColor DarkGray
                if ($testExit -eq 0) {
                    Write-Host "    Result: PASS (exit $testExit)" -ForegroundColor Green
                } else {
                    Write-Host "    Result: FAIL (exit $testExit)" -ForegroundColor Red
                    $roundAllTestsPassed = $false
                }

                # Show test output for failures
                if ($testExit -ne 0) {
                    Write-Host "    --- Test Output ---" -ForegroundColor Red
                    $testOutput | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
                    Write-Host "    --- End Output ---" -ForegroundColor Red
                }
            }

            if ($roundAllTestsPassed) {
                $tentativeResult = "PASS"
            }
        }

        # ============================================================
        # Common Codex check for any tentative success (tests passed,
        # tests skipped, or no tests with AllowNoTests).
        # P0-1: NEEDS_FIX findings are injected into a Codex-fix round
        # instead of exiting.
        # ============================================================
        if ($tentativeResult) {
            Write-Host ""
            Write-Host "  Tests result: $tentativeResult in round $round!" -ForegroundColor $(if ($tentativeResult -eq "PASS") { "Green" } else { "Yellow" })

            if (-not $SkipCodexReview) {
                $reviewInputPath = New-ReviewInputBundle -Round $round -TentativeResult $tentativeResult -TestResults $testResults
                if (-not [string]::IsNullOrWhiteSpace($ReviewCommand)) {
                    $autoReview = Invoke-AutoCodexReview -CommandLine $ReviewCommand -Round $round
                    $safeReviewOutput = Watch-Secrets -Text "$($autoReview.Output)" -Source "automatic review output (round $round)"
                    $logContent += @"

========================================
--- Automatic Review (Round $round) ---
Command: $ReviewCommand
Input: $reviewInputPath
Success: $($autoReview.Success)
$safeReviewOutput
--- End Automatic Review ---

"@
                    if (-not $autoReview.Success) {
                        $finalResult = "FAIL_CODEX_REVIEW_INVALID"
                    }
                }
            }

            # P0-1: Check CODEX_REVIEW.json before declaring success
            if ($finalResult -eq "UNKNOWN") {
                $inLoopCodexCheck = Invoke-InLoopCodexCheck -Round $round -MaxRounds $MaxRounds
                switch ($inLoopCodexCheck.Action) {
                    "PASS" {
                        # Codex review passed (or skipped) — success
                        $finalResult = $tentativeResult
                    }
                    "CODEX_FIX" {
                        # Codex says NEEDS_FIX — consume the file, inject findings
                        # into the next Claude round's prompt.
                        Write-Host "  Codex review returned NEEDS_FIX — injecting findings into fix round $($round + 1)..." -ForegroundColor Yellow
                        $codexFixRound = $true
                        $codexFindings = $inLoopCodexCheck.Findings
                        $tentativeResult = $null
                        # Fall through — continue to next loop iteration
                    }
                    default {
                        # Validation failure or missing file — exit with appropriate code
                        $finalResult = $inLoopCodexCheck.Result
                    }
                }
            }
        }

        # Reset per-round state for next iteration
        $roundAllTestsPassed = $false

        # Determine whether to break or continue the loop
        if ($finalResult -ne "UNKNOWN") {
            # A final result was set (PASS, FAIL_*, NEEDS_*, etc.)
            break
        }
        if (-not $tentativeResult) {
            # No tentative success — this is a test failure or a Codex-fix
            # round that consumed the tentative result.
            if ($codexFixRound) {
                # Codex-fix round: continue to next iteration
                Write-Host "  Continuing to Codex-fix round..." -ForegroundColor Yellow
            } else {
                # Pure test failure (no Codex involvement)
                Write-Host ""
                if ($round -lt $MaxRounds) {
                    Write-Host "  Tests FAILED in round $round — entering fix round $($round + 1)..." -ForegroundColor Yellow
                } else {
                    Write-Host "  Tests FAILED in round $round — MAX_ROUNDS ($MaxRounds) reached." -ForegroundColor Red
                    $finalResult = "FAIL_MAX_ROUNDS"
                    break
                }
            }
        }
        $tentativeResult = $null
    }

    # ============================================================
    # Post-loop: handle case where loop completed without breaking
    # ============================================================
    if ($finalResult -eq "UNKNOWN") {
        $finalResult = "FAIL_UNKNOWN"
    }

    # ============================================================
    # P0-2: Post-loop Codex validation safety net.
    # The in-loop check already handles PASS + NEEDS_FIX decisions.
    # This post-loop check validates the JSON itself and catches cases
    # where the loop exited for non-Codex reasons (crash, max rounds).
    # ============================================================
    $codexReviewValid = $true
    $codexReviewStatus = "NOT_PRESENT"
    if (Test-Path "docs/CODEX_REVIEW.json") {
        $codexReviewValid = Test-CodexReviewJson -JsonPath "docs/CODEX_REVIEW.json"
        if ($codexReviewValid) {
            try {
                $codexJson = Get-Content "docs/CODEX_REVIEW.json" -Raw -Encoding UTF8 | ConvertFrom-Json
                $codexReviewStatus = $codexJson.status
                Write-Log "CODEX_REVIEW.json status: $codexReviewStatus" "Cyan"
            } catch {
                $codexReviewValid = $false
                $codexReviewStatus = "PARSE_ERROR"
            }
        } else {
            $codexReviewStatus = "INVALID"
        }
    }

    # Integrate Codex review status into final result.
    # Note: NEEDS_FIX with PASS is handled in-loop (injects findings into
    # a fix round). This post-loop check is a safety net for non-PASS results
    # and validation errors.
    if (-not $codexReviewValid) {
        # Invalid CODEX_REVIEW.json should prevent PASS
        Write-Log "CODEX_REVIEW.json is invalid or missing required fields — cannot PASS" "Red"
        if ($finalResult -eq "PASS") {
            $finalResult = "FAIL_CODEX_REVIEW_INVALID"
        }
    } elseif ($codexReviewStatus -eq "FAIL") {
        Write-Log "CODEX_REVIEW.json status is FAIL — review rejected" "Red"
        if ($finalResult -eq "PASS" -or $finalResult -eq "PASS_NO_TESTS" -or $finalResult -eq "SKIPPED") {
            $finalResult = "FAIL_CODEX_REVIEW"
        }
    } elseif ($codexReviewStatus -eq "NEEDS_FIX" -and $finalResult -eq "PASS") {
        # Safety net: if PASS somehow reached without in-loop Codex check,
        # still prevent silent PASS on NEEDS_FIX.
        Write-Log "CODEX_REVIEW.json status is NEEDS_FIX — additional fix round needed" "Yellow"
        $finalResult = "NEEDS_FIX_CODEX_REVIEW"
    } elseif ($codexReviewStatus -eq "NOT_PRESENT") {
        if ($SkipCodexReview) {
            Write-Log "CODEX_REVIEW.json not found — review skipped (SkipCodexReview set)" "Yellow"
            if ($finalResult -eq "PASS" -or $finalResult -eq "PASS_NO_TESTS" -or $finalResult -eq "SKIPPED") {
                $finalResult = "SKIPPED_CODEX_REVIEW"
            }
        } else {
            Write-Log "CODEX_REVIEW.json not found — Codex review required before release" "Red"
            Write-Log "Run Codex review and save results to docs/CODEX_REVIEW.json, or use -SkipCodexReview to bypass." "Red"
            if ($finalResult -eq "PASS" -or $finalResult -eq "PASS_NO_TESTS" -or $finalResult -eq "SKIPPED") {
                $finalResult = "NEEDS_CODEX_REVIEW"
            }
        }
    }

} finally {
    # ============================================================
    # P2-2: finally block — always execute cleanup
    # ============================================================
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " Finalizing..." -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan

    # Restore old report if Claude didn't produce one
    if ($oldReportBackup -and -not (Test-Path "docs/IMPLEMENTATION_REPORT.md")) {
        Copy-Item $oldReportBackup "docs/IMPLEMENTATION_REPORT.md" -Force
        Write-Log "Restored previous report from backup" "DarkGray"
    }

    # Clean up backup
    if ($oldReportBackup -and (Test-Path $oldReportBackup)) {
        Remove-Item $oldReportBackup -Force
    }

    # P2-2: Remove Ctrl+C handler and clean up tracked Claude process
    try { [Console]::CancelKeyPress -= $ctrlCHandler } catch {}
    if ($script:ClaudeProcess -and !$script:ClaudeProcess.HasExited) {
        try {
            Stop-Process -Id $script:ClaudeProcess.Id -Force -ErrorAction SilentlyContinue
            Write-Log "Cleaned up lingering Claude process (PID $($script:ClaudeProcess.Id))" "DarkGray"
        } catch {}
    }
    $script:ClaudeProcess = $null

    # Write interrupt status (P2-2: detect if script was interrupted)
    if ($finalResult -eq "UNKNOWN") {
        $logContent += @"

========================================
SCRIPT INTERRUPTED — finalization running
========================================

"@
        $finalResult = "INTERRUPTED"
    }

    # Final log footer
    $endTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logContent += @"

========================================
Execution finished: $endTimestamp
Final result: $finalResult
========================================
"@

    # P1-4: Final secret scan of entire log before writing
    $logContent = Watch-Secrets -Text $logContent -Source "final log"
    [System.IO.File]::WriteAllText(
        (Join-Path $ProjectDir $logFile),
        $logContent,
        (New-Object System.Text.UTF8Encoding $false)
    )

    # ============================================================
    # Result summary
    # ============================================================
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    switch ($finalResult) {
        "PASS" {
            Write-Host " RESULT: PASS — All tests passed." -ForegroundColor Green
            Write-Host " Rounds used: $round / $MaxRounds" -ForegroundColor Green
            $scriptExitCode = 0
        }
        "PASS_NO_TESTS" {
            Write-Host " RESULT: PASS — No tests configured (AllowNoTests was set)." -ForegroundColor Yellow
            $scriptExitCode = 0
        }
        "SKIPPED" {
            Write-Host " RESULT: SKIPPED — Tests skipped by user request." -ForegroundColor Yellow
            $scriptExitCode = 0
        }
        "NEEDS_MANUAL_VERIFY" {
            Write-Host " RESULT: NEEDS_MANUAL_VERIFY — No test commands discovered." -ForegroundColor Yellow
            Write-Host " Add test commands to docs/PLAN.md or use -AllowNoTests to skip verification." -ForegroundColor Yellow
            $scriptExitCode = 2
        }
        "FAIL_MAX_ROUNDS" {
            Write-Host " RESULT: FAIL — $MaxRounds rounds exhausted with test failures." -ForegroundColor Red
            Write-Host " Review test output above and in docs/claude-run.log" -ForegroundColor Red
            $scriptExitCode = 2
        }
        "FAIL_CLAUDE_CRASH" {
            Write-Host " RESULT: FAIL — Claude exited with non-zero code." -ForegroundColor Red
            $scriptExitCode = 3
        }
        "FAIL_NO_REPORT" {
            Write-Host " RESULT: FAIL — Claude did not generate IMPLEMENTATION_REPORT.md." -ForegroundColor Red
            $scriptExitCode = 3
        }
        "INTERRUPTED" {
            Write-Host " RESULT: INTERRUPTED — Script was stopped before completion." -ForegroundColor Yellow
            $scriptExitCode = 4
        }
        "FAIL_CODEX_REVIEW_INVALID" {
            Write-Host " RESULT: FAIL — CODEX_REVIEW.json is invalid or missing required fields." -ForegroundColor Red
            Write-Host " Fix the CODEX_REVIEW.json file and re-run the review step." -ForegroundColor Red
            $scriptExitCode = 6
        }
        "FAIL_CODEX_REVIEW" {
            Write-Host " RESULT: FAIL — Codex review returned FAIL status." -ForegroundColor Red
            Write-Host " Review the findings in docs/CODEX_REVIEW.json and fix issues." -ForegroundColor Red
            $scriptExitCode = 6
        }
        "NEEDS_FIX_CODEX_REVIEW" {
            Write-Host " RESULT: NEEDS_FIX — Codex review requires additional fixes." -ForegroundColor Yellow
            Write-Host " Address Codex findings and re-run the orchestrator." -ForegroundColor Yellow
            $scriptExitCode = 7
        }
        "NEEDS_CODEX_REVIEW" {
            Write-Host " RESULT: NEEDS_CODEX_REVIEW — CODEX_REVIEW.json not found." -ForegroundColor Red
            Write-Host " Run Codex review and save CODEX_REVIEW.json, or use -SkipCodexReview to bypass." -ForegroundColor Red
            $scriptExitCode = 8
        }
        "SKIPPED_CODEX_REVIEW" {
            Write-Host " RESULT: SKIPPED_CODEX_REVIEW — All tests passed, Codex review skipped by user request." -ForegroundColor Yellow
            $scriptExitCode = 0
        }
        default {
            Write-Host " RESULT: $finalResult" -ForegroundColor Red
            $scriptExitCode = 5
        }
    }

    Write-Host ""
    Write-Host " Verify changes with:" -ForegroundColor Yellow
    Write-Host "   git status --short --untracked-files=all" -ForegroundColor Yellow
    Write-Host "   git diff" -ForegroundColor Yellow
    Write-Host ""
    Write-Host " Artifacts:" -ForegroundColor Cyan
    Write-Host "   Log:     docs/claude-run.log" -ForegroundColor Cyan
    Write-Host "   Report:  docs/IMPLEMENTATION_REPORT.md" -ForegroundColor Cyan
    Write-Host "   Status:  docs/CHANGES_STATUS.txt" -ForegroundColor Cyan
    Write-Host "   Diff:    docs/CHANGES_DIFF.txt" -ForegroundColor Cyan
    Write-Host "   Baseline: docs/BASELINE_STATUS.txt" -ForegroundColor Cyan
    if (Test-Path "docs/CODEX_REVIEW.json") {
        Write-Host "   Review:  docs/CODEX_REVIEW.json" -ForegroundColor Cyan
    }
    Write-Host "========================================" -ForegroundColor Cyan

    # Clean up per-round artifacts: keep primary copies, remove round-tagged files
    # Round 1 files are CHANGES_STATUS.txt / CHANGES_DIFF.txt (already the primary copy)
    # Rounds 2+ produce CHANGES_STATUS_R{N}.txt — keep the latest round as primary
    if ($round -gt 1) {
        $lastStatus = "docs/CHANGES_STATUS_R${round}.txt"
        $lastDiff   = "docs/CHANGES_DIFF_R${round}.txt"
        if (Test-Path $lastStatus) {
            Copy-Item $lastStatus "docs/CHANGES_STATUS.txt" -Force
        }
        if (Test-Path $lastDiff) {
            Copy-Item $lastDiff "docs/CHANGES_DIFF.txt" -Force
        }
    }
    # Remove all per-round tagged files (they are redundant with the primary copy)
    Remove-Item "docs/CHANGES_STATUS_R*.txt" -Force -ErrorAction SilentlyContinue
    Remove-Item "docs/CHANGES_DIFF_R*.txt" -Force -ErrorAction SilentlyContinue

    exit $scriptExitCode
}
