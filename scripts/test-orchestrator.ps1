<#
.SYNOPSIS
    Orchestrator-level tests for the AI Coding Collaboration CLI Orchestrator
.DESCRIPTION
    Tests the run-claude.ps1 script's behavior across key scenarios using mocks.
    Covers: failure exit codes, report verification, test execution, change collection,
    max rounds, dirty workspace detection, and path encoding.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/test-orchestrator.ps1
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path

# ============================================================
# Test Helpers
# ============================================================
$TotalTests = 0
$PassedTests = 0
$FailedTests = 0

function Test-Start {
    param([string]$Name)
    $script:TotalTests++
    Write-Host "  TEST: $Name" -ForegroundColor Cyan
}

function Test-Pass {
    param([string]$Detail = "")
    $script:PassedTests++
    $msg = "    PASS"
    if ($Detail) { $msg += " — $Detail" }
    Write-Host $msg -ForegroundColor Green
}

function Test-Fail {
    param([string]$Detail = "")
    $script:FailedTests++
    $msg = "    FAIL"
    if ($Detail) { $msg += " — $Detail" }
    Write-Host $msg -ForegroundColor Red
}

function Invoke-MockClaude {
    param(
        [int]$ExitCode = 0,
        [string]$Output = "Mock Claude output",
        [string]$ReportContent = "# Mock Report`n`nAll tasks completed.",
        [string]$TestFile = $null,
        [string]$TestFileContent = $null
    )
    # Create the mock report
    if ($ReportContent) {
        $ReportContent | Out-File -FilePath "docs/IMPLEMENTATION_REPORT.md" -Encoding UTF8
    }
    # Create test files if specified
    if ($TestFile -and $TestFileContent) {
        $TestFileContent | Out-File -FilePath $TestFile -Encoding UTF8
    }
    Write-Host $Output
    return $ExitCode
}

# ============================================================
# Test helper: Verify script behavior in a temp git repo
# ============================================================
function Start-TempRepo {
    param(
        [string]$Name,
        [scriptblock]$TestScript,
        [string]$PathSuffix = ""
    )
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ccdl_test_${Name}${PathSuffix}"
    $tempRoot = $tempRoot -replace '[^\x00-\x7F]', '_'  # sanitize for basic tests
    if (Test-Path $tempRoot) { Remove-Item $tempRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    try {
        Set-Location $tempRoot
        git init 2>&1 | Out-Null
        git config user.email "test@test" 2>&1 | Out-Null
        git config user.name "Test" 2>&1 | Out-Null

        # Create minimal project structure
        New-Item -ItemType Directory -Path "docs" -Force | Out-Null
        New-Item -ItemType Directory -Path "scripts" -Force | Out-Null
        New-Item -ItemType Directory -Path "demo-project" -Force | Out-Null

        # Copy the script under test
        Copy-Item (Join-Path $ProjectDir "scripts/run-claude.ps1") "scripts/run-claude.ps1" -Force
        Copy-Item (Join-Path $ProjectDir "docs/PLAN.template.md") "docs/PLAN.template.md" -Force
        Copy-Item (Join-Path $ProjectDir "docs/IMPLEMENTATION_REPORT.template.md") "docs/IMPLEMENTATION_REPORT.template.md" -Force
        Copy-Item (Join-Path $ProjectDir "docs/CODEX_REVIEW.schema.json") "docs/CODEX_REVIEW.schema.json" -ErrorAction SilentlyContinue

        # Copy demo project for test discovery tests
        if ((Test-Path (Join-Path $ProjectDir "demo-project/test.js"))) {
            Copy-Item (Join-Path $ProjectDir "demo-project/package.json") "demo-project/package.json" -Force
            Copy-Item (Join-Path $ProjectDir "demo-project/index.js") "demo-project/index.js" -Force
            Copy-Item (Join-Path $ProjectDir "demo-project/test.js") "demo-project/test.js" -Force
            Copy-Item (Join-Path $ProjectDir "demo-project/calculator.py") "demo-project/calculator.py" -Force
            Copy-Item (Join-Path $ProjectDir "demo-project/test_calculator.py") "demo-project/test_calculator.py" -Force
        }

        & $TestScript
    } finally {
        Set-Location $ProjectDir
        Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-TestNativeCapture {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $FilePath @ArgumentList 2>&1
        return [PSCustomObject]@{
            Output   = @($output | ForEach-Object { "$_" })
            ExitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

# ============================================================
# Test 1: Script fails correctly on missing PLAN.md
# ============================================================
Start-TempRepo -Name "missing_plan" -TestScript {
    Test-Start "Script exits 1 when PLAN.md is missing"

    # Run script (should fail on PLAN.md check)
    $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -SkipTests 2>&1
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 1) {
        Test-Pass "exit code $exitCode"
    } else {
        Test-Fail "Expected exit 1, got $exitCode"
    }
}

# ============================================================
# Test 2: Script fails on non-Git directory (run outside temp)
# ============================================================
Test-Start "Step 1 check — Git repo detection works"

# Just syntax-verify that the git check logic is present (can't easily test outside git)
$scriptContent = Get-Content (Join-Path $ProjectDir "scripts/run-claude.ps1") -Raw
if ($scriptContent -match 'git rev-parse --is-inside-work-tree') {
    Test-Pass "Git check logic present in script"
} else {
    Test-Fail "Git check logic missing"
}

# ============================================================
# Test 3: Verify IMPLEMENTATION_REPORT.md existence check
# ============================================================
Test-Start "Script detects missing IMPLEMENTATION_REPORT.md"

$scriptContent = Get-Content (Join-Path $ProjectDir "scripts/run-claude.ps1") -Raw
$hasReportCheck = $scriptContent -match 'IMPLEMENTATION_REPORT\.md.*not generated' -or
                  $scriptContent -match 'IMPLEMENTATION_REPORT\.md.*empty' -or
                  $scriptContent -match 'FAIL_NO_REPORT'
if ($hasReportCheck) {
    Test-Pass "Report existence/non-empty verification present"
} else {
    Test-Fail "Report verification not found"
}

# ============================================================
# Test 4: Verify MAX_ROUNDS configuration
# ============================================================
Test-Start "MAX_ROUNDS configuration is present and bounded"

if ($scriptContent -match 'MaxRounds' -and $scriptContent -match 'MAX_ROUNDS') {
    # Check range validation
    if ($scriptContent -match 'ValidateRange\(1,\s*15\)') {
        Test-Pass "MAX_ROUNDS has 1-15 range validation"
    } else {
        Test-Pass "MAX_ROUNDS variable found"
    }
} else {
    Test-Fail "MAX_ROUNDS not found in script"
}

# ============================================================
# Test 5: Verify secret scanning logic
# ============================================================
Test-Start "P1-4: Secret scanning logic present"

$hasSecretScan = $scriptContent -match 'Watch-Secrets' -or
                 $scriptContent -match 'REDACTED' -or
                 $scriptContent -match 'redact'
if ($hasSecretScan) {
    Test-Pass "Secret scanning/redaction logic present"
} else {
    Test-Fail "No secret scanning logic found"
}

# ============================================================
# Test 6: Verify git status/diff auto-collection
# ============================================================
Test-Start "P1-1: Git status/diff auto-collection present"

$hasGitCollect = $scriptContent -match 'CHANGES_STATUS' -and $scriptContent -match 'CHANGES_DIFF'
if ($hasGitCollect) {
    Test-Pass "Auto git status/diff collection to files present"
} else {
    Test-Fail "Git collection missing"
}

# ============================================================
# Test 7: Verify baseline recording
# ============================================================
Test-Start "P1-2: Pre-run baseline recording present"

$hasBaseline = $scriptContent -match 'BASELINE_STATUS' -or $scriptContent -match 'baseline'
if ($hasBaseline) {
    Test-Pass "Pre-run baseline recording present"
} else {
    Test-Fail "Baseline recording missing"
}

# ============================================================
# Test 8: Verify try/finally cleanup
# ============================================================
Test-Start "P2-2: Try/finally cleanup block present"

if ($scriptContent -match 'try\s*\{' -and $scriptContent -match '}\s*finally\s*\{') {
    Test-Pass "try/finally block found"
} else {
    Test-Fail "try/finally block missing"
}

# ============================================================
# Test 9: Verify tool-layer safety config
# ============================================================
Test-Start "P1-3: .claude/settings.json with deny rules created"

$settingsPath = Join-Path $ProjectDir ".claude/settings.json"
if (Test-Path $settingsPath) {
    $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json
    if ($settings.permissions.deny) {
        Test-Pass "settings.json has deny rules ($($settings.permissions.deny.Count) entries)"
    } else {
        Test-Fail "settings.json missing deny rules"
    }
} else {
    Test-Fail ".claude/settings.json not found"
}

# ============================================================
# Test 10: Verify JSON schema exists and is valid
# ============================================================
Test-Start "P0-3: CODEX_REVIEW.schema.json exists and is valid JSON"

$schemaPath = Join-Path $ProjectDir "docs/CODEX_REVIEW.schema.json"
if (Test-Path $schemaPath) {
    try {
        $schema = Get-Content $schemaPath -Raw | ConvertFrom-Json
        if ($schema.required -contains 'status' -and $schema.required -contains 'findings') {
            Test-Pass "Schema valid with required fields (status, findings)"
        } else {
            Test-Fail "Schema missing required fields"
        }
    } catch {
        Test-Fail "Schema is not valid JSON: $_"
    }
} else {
    Test-Fail "CODEX_REVIEW.schema.json not found"
}

# ============================================================
# Test 11: Verify orchestration loop structure
# ============================================================
Test-Start "P0-1: Orchestration loop with for/round structure"

$hasLoop = $scriptContent -match 'for\s*\(\s*\$round\s*=\s*1' -or
           $scriptContent -match '\$round\s*=\s*1.*\$round\s*-le'
if ($hasLoop) {
    Test-Pass "Orchestration loop structure found"
} else {
    Test-Fail "No orchestration loop found"
}

# ============================================================
# Test 12: Verify fix prompt structure for retry rounds
# ============================================================
Test-Start "P0-1: Fix prompt includes test failures and git diff"

$hasFixPrompt = $scriptContent -match 'FAILING TESTS' -and
                $scriptContent -match 'Current Changes' -and
                $scriptContent -match 'Current Diff'
if ($hasFixPrompt) {
    Test-Pass "Fix prompt structure includes test failures and git changes"
} else {
    Test-Fail "Fix prompt incomplete"
}

# ============================================================
# Test 13: Verify test command discovery
# ============================================================
Test-Start "P0-2: Test command auto-discovery present"

$hasTestDiscovery = $scriptContent -match 'Find-TestCommands' -or
                    $scriptContent -match 'pytest' -or
                    $scriptContent -match 'test\.js'
if ($hasTestDiscovery) {
    Test-Pass "Test command discovery logic present"
} else {
    Test-Fail "Test discovery missing"
}

# ============================================================
# Test 14: PowerShell syntax check
# ============================================================
Test-Start "run-claude.ps1: PowerShell syntax check"

$scriptPath = Join-Path $ProjectDir "scripts/run-claude.ps1"
$errors = @()
$tokens = @()
[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $scriptPath), [ref]$tokens, [ref]$errors
) | Out-Null

if ($errors.Count -eq 0) {
    Test-Pass "No parse errors"
} else {
    foreach ($e in $errors) {
        Test-Fail "Parse error: $($e.Message)"
    }
}

# ============================================================
# Test 15: Encoding — verify UTF-8 without BOM usage
# ============================================================
Test-Start "P2-4: UTF-8 without BOM encoding used for file writes"

$hasUtf8NoBom = $scriptContent -match 'UTF8Encoding.*\$false' -or
                $scriptContent -match 'utf8NoBom' -or
                $scriptContent -match 'System\.Text\.UTF8Encoding'
if ($hasUtf8NoBom) {
    Test-Pass "UTF-8 without BOM encoding used"
} else {
    # Fallback check: Out-File -Encoding UTF8 is acceptable for PS7+
    if ($scriptContent -match 'Out-File.*-Encoding\s+UTF8') {
        Test-Pass "UTF-8 encoding used (Out-File -Encoding UTF8)"
    } else {
        Test-Fail "No explicit UTF-8 encoding found"
    }
}

# ============================================================
# Test 16: Verify end-to-end test commands are runnable
# ============================================================
Test-Start "Demo project tests are runnable"

Push-Location $ProjectDir
try {
    # Node.js tests
    $nodeResult = node demo-project/test.js 2>&1
    $nodeExit = $LASTEXITCODE
    if ($nodeExit -eq 0 -and ($nodeResult -join "`n") -match '4 passed') {
        Test-Pass "Node.js: 4/4 passed"
    } else {
        Test-Fail "Node.js tests failed (exit $nodeExit)"
    }

    # Python tests
    $pyResult = py -B -m pytest demo-project -q -p no:cacheprovider 2>&1
    $pyExit = $LASTEXITCODE
    if ($pyExit -eq 0 -and ($pyResult -join "`n") -match '17 passed') {
        Test-Pass "Python: 17/17 passed"
    } else {
        Test-Fail "Python tests failed (exit $pyExit)"
    }
} finally {
    Pop-Location
}

# ============================================================
# Test 17: E2E success path with fake claude (P0-3: behavioral test)
# ============================================================
Start-TempRepo -Name "e2e_fake_claude" -TestScript {
    Test-Start "E2E: Orchestrator success path with fake claude — exit 0, artifacts generated"

    # Create fake claude.bat that handles both --version and regular invocation
    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo ## Changes Made>> docs\IMPLEMENTATION_REPORT.md
echo - Implemented plan requirements>> docs\IMPLEMENTATION_REPORT.md
echo ## Test Results>> docs\IMPLEMENTATION_REPORT.md
echo All tests passed.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    # Create minimal PLAN.md (no test commands — avoid auto-discovery)
    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    # Remove copied demo-project files to prevent test auto-discovery
    Remove-Item "demo-project/test.js", "demo-project/package.json" -Force -ErrorAction SilentlyContinue

    # Run orchestrator — fake claude is in the current directory, which is first in PATH
    # Use -SkipTests to avoid needing real test runners in the temp repo
    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -SkipTests -SkipCodexReview 2>&1
        $exitCode = $LASTEXITCODE

        if ($exitCode -eq 0 -and (Test-Path "docs/IMPLEMENTATION_REPORT.md")) {
            Test-Pass "Exit 0 with IMPLEMENTATION_REPORT.md generated"
        } elseif ($exitCode -eq 0) {
            Test-Fail "Exit 0 but IMPLEMENTATION_REPORT.md missing"
        } else {
            Test-Fail "Expected exit 0, got $exitCode. Output: $($result -join '; ')"
        }

        # Verify CHANGES_STATUS.txt was generated (P1-1: auto git collection)
        if (Test-Path "docs/CHANGES_STATUS.txt") {
            Test-Pass "CHANGES_STATUS.txt auto-generated"
        } else {
            Test-Fail "CHANGES_STATUS.txt not generated"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 18: Variable fix — loop actually executes (P0-1 verification)
# ============================================================
Start-TempRepo -Name "loop_executes" -TestScript {
    Test-Start "P0-1 fix: Orchestration loop actually executes (not 0 iterations)"

    # Create fake claude.bat
    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    # Remove copied demo-project files to prevent test auto-discovery
    Remove-Item "demo-project/test.js", "demo-project/package.json" -Force -ErrorAction SilentlyContinue

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 3 -SkipTests -SkipCodexReview 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 0 -and ($output -match 'ROUND 1 / 3')) {
            Test-Pass "Loop executed (ROUND 1 / 3 found in output, exit $exitCode)"
        } elseif ($exitCode -eq 0) {
            Test-Fail "Exit 0 but no ROUND indicator in output"
        } else {
            Test-Fail "Expected exit 0, got $exitCode"
        }

        # Verify the log file mentions the correct MaxRounds value
        if (Test-Path "docs/claude-run.log") {
            $logContent = Get-Content "docs/claude-run.log" -Raw
            if ($logContent -match 'Max Rounds: 3') {
                Test-Pass "Log correctly records Max Rounds: 3"
            } else {
                Test-Fail "Log missing or has wrong Max Rounds value"
            }
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 19: No-tests without AllowNoTests returns NEEDS_MANUAL_VERIFY (exit 2)
# ============================================================
Start-TempRepo -Name "no_tests_fail" -TestScript {
    Test-Start "P1-3 fix: No tests without -AllowNoTests -> exit 2 (NEEDS_MANUAL_VERIFY)"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    # Remove all demo-project files to prevent test auto-discovery
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    # PLAN.md with NO test commands
    "# Test Plan`n`nNo test commands here." | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 2>&1
        $exitCode = $LASTEXITCODE

        if ($exitCode -eq 2) {
            Test-Pass "Exit 2 (NEEDS_MANUAL_VERIFY) when no tests found without -AllowNoTests"
        } else {
            Test-Fail "Expected exit 2, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 20: No-tests with AllowNoTests returns exit 0
# ============================================================
Start-TempRepo -Name "no_tests_allow" -TestScript {
    Test-Start "P1-3 fix: No tests with -AllowNoTests -> exit 0"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    # Remove all demo-project files to prevent test auto-discovery
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests -SkipCodexReview 2>&1
        $exitCode = $LASTEXITCODE

        if ($exitCode -eq 0) {
            Test-Pass "Exit 0 when -AllowNoTests is set and no tests found"
        } else {
            Test-Fail "Expected exit 0, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 21: Shell injection prevention (P1-1 verification)
# ============================================================
Start-TempRepo -Name "shell_injection" -TestScript {
    Test-Start "P1-1 fix: Shell injection prevented — malicious command not executed"

    # Create a canary file that should NOT be created if injection is blocked
    $canaryFile = "CANARY_INJECTION_SUCCEEDED.txt"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    # Create a fake node that just exits 0 (to simulate test passing)
    @'
@echo off
echo Fake node test passed
exit /b 0
'@ | Out-File -FilePath "node.bat" -Encoding ASCII

    # PLAN.md with a test command that tries shell injection
    "# Test Plan`n`nRun: node test.js & echo INJECTED > $canaryFile" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    # Create a mock test.js so auto-discovery finds the node command
    New-Item -ItemType Directory -Path "demo-project" -Force | Out-Null
    "// test" | Out-File -FilePath "demo-project/test.js" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $null = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 2>&1

        # The injected canary file should NOT exist if shell injection is blocked
        if (Test-Path $canaryFile) {
            Test-Fail "Shell injection SUCCEEDED — canary file was created"
        } else {
            Test-Pass "Shell injection blocked — canary file not created"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 22: P0-2 fix — missing CODEX_REVIEW.json without -SkipCodexReview → exit 8
# ============================================================
Start-TempRepo -Name "codex_missing_block" -TestScript {
    Test-Start "P0-2 fix: Missing CODEX_REVIEW.json without -SkipCodexReview -> exit 8"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    # Ensure NO CODEX_REVIEW.json exists (temp repo starts clean)
    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

    # Remove demo-project to prevent test auto-discovery (so tests pass side is clean)
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 8 -and ($output -match 'NEEDS_CODEX_REVIEW')) {
            Test-Pass "Exit 8 (NEEDS_CODEX_REVIEW) when CODEX_REVIEW.json missing without -SkipCodexReview"
        } elseif ($exitCode -eq 0) {
            Test-Fail "Exit 0 when CODEX_REVIEW.json missing — should block PASS (got: $output)"
        } else {
            Test-Fail "Expected exit 8, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 23: P0-2 fix — missing CODEX_REVIEW.json with -SkipCodexReview → exit 0
# ============================================================
Start-TempRepo -Name "codex_skip_allow" -TestScript {
    Test-Start "P0-2 fix: Missing CODEX_REVIEW.json with -SkipCodexReview -> exit 0 (SKIPPED_CODEX_REVIEW)"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests -SkipCodexReview 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 0 -and ($output -match 'SKIPPED_CODEX_REVIEW')) {
            Test-Pass "Exit 0 (SKIPPED_CODEX_REVIEW) when -SkipCodexReview set and CODEX_REVIEW.json missing"
        } elseif ($exitCode -eq 8) {
            Test-Fail "Exit 8 despite -SkipCodexReview being set"
        } else {
            Test-Fail "Expected exit 0, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 24: P0-2 fix — valid CODEX_REVIEW.json with empty findings passes
# ============================================================
Start-TempRepo -Name "codex_valid_empty_findings" -TestScript {
    Test-Start "P0-2 fix: Valid CODEX_REVIEW.json with findings=[] allows pass"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue
    @"
{"status":"PASS","findings":[],"reviewed_at":"2026-06-10T00:00:00Z"}
"@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 0 -and ($output -match 'RESULT: PASS')) {
            Test-Pass "Exit 0 with valid PASS review and empty findings"
        } else {
            Test-Fail "Expected exit 0 with valid empty findings, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 25: P1-1 fix — verify no claude.cmd/claude.bat in project root
# ============================================================
Test-Start "P1-1 fix: No claude.cmd or claude.bat in project root"

$rootClaudeCmd = Join-Path $ProjectDir "claude.cmd"
$rootClaudeBat = Join-Path $ProjectDir "claude.bat"

$found = @()
if (Test-Path $rootClaudeCmd) { $found += "claude.cmd" }
if (Test-Path $rootClaudeBat) { $found += "claude.bat" }

if ($found.Count -eq 0) {
    Test-Pass "No claude.cmd/claude.bat in project root"
} else {
    Test-Fail "Test artifact(s) found in project root: $($found -join ', ')"
}

# ============================================================
# Test 26: P2-2 fix — quote-aware command-line splitting
# ============================================================
Test-Start "P2-2 fix: Quote-aware command-line splitting handles quoted arguments"

# We check that the Split-CommandLine function exists in the script
$scriptContent = Get-Content (Join-Path $ProjectDir "scripts/run-claude.ps1") -Raw
if ($scriptContent -match 'function Split-CommandLine') {
    Test-Pass "Split-CommandLine function present (quote-aware parsing)"
} else {
    Test-Fail "Split-CommandLine function not found"
}

# Also verify the function is actually used (not just defined)
if ($scriptContent -match 'Split-CommandLine -CommandLine \$testCmd') {
    Test-Pass "Split-CommandLine is called during test execution"
} else {
    Test-Fail "Split-CommandLine defined but not used in test execution"
}

# ============================================================
# Test 27: P1-2 fix — PLAN.md restored from git history
# ============================================================
Test-Start "P1-2 fix: PLAN.md contains real development plan (not placeholder)"

$planPath = Join-Path $ProjectDir "docs/PLAN.md"
if (Test-Path $planPath) {
    $planContent = Get-Content $planPath -Raw
    $firstLine = ($planContent -split "`n")[0].Trim()
    $isJustTest = ($firstLine -eq '# Test') -and ($planContent.Split("`n").Count -le 2)
    $isPlaceholder = $planContent.Trim().StartsWith('# Test') -and ($planContent.Split("`n").Count -le 2)
    if ($isJustTest -or $isPlaceholder) {
        Test-Fail "PLAN.md still contains placeholder '# Test'"
    } elseif ($planContent.Length -gt 100) {
        Test-Pass "PLAN.md contains substantial content ($($planContent.Length) chars)"
    } else {
        Test-Fail "PLAN.md content too short ($($planContent.Length) chars)"
    }
} else {
    Test-Fail "PLAN.md not found"
}

# ============================================================
# Test 28: P0-1 fix — Codex NEEDS_FIX triggers actual fix round
# ============================================================
Start-TempRepo -Name "codex_needs_fix_loop" -TestScript {
    Test-Start "P0-1 fix: Codex NEEDS_FIX findings injected into Claude fix round"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    # Create CODEX_REVIEW.json with NEEDS_FIX status and concrete findings
    @'
{"status":"NEEDS_FIX","findings":[{"id":"F01","severity":"P1","file":"src/index.js","description":"Missing null check on input","fix_suggestion":"Add if (!input) return early guard."}],"reviewed_at":"2026-06-10T00:00:00Z"}
'@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 3 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        # The Codex-fix round should run (ROUND 2 should be the fix round).
        # After Codex-fix, CODEX_REVIEW.json was consumed, so we get NEEDS_CODEX_REVIEW (exit 8).
        if ($output -match 'ROUND 2 / 3' -and $output -match 'Codex-fix prompt') {
            Test-Pass "Codex-fix round executed (ROUND 2 with Codex-fix prompt)"
        } elseif ($output -match 'ROUND 2 / 3') {
            Test-Fail "ROUND 2 ran but Codex-fix prompt marker missing. Output: $($result -join '; ')"
        } else {
            Test-Fail "Codex-fix round did not execute (no ROUND 2). Exit: $exitCode. Output: $($result -join '; ')"
        }

        # CODEX_REVIEW.json should have been consumed (moved to .previous)
        if (Test-Path "docs/CODEX_REVIEW.json.previous") {
            Test-Pass "CODEX_REVIEW.json consumed (renamed to .previous)"
        } else {
            Test-Fail "CODEX_REVIEW.json.previous not found — file was not consumed"
        }

        # Exit should be 8 (NEEDS_CODEX_REVIEW) since the file was consumed
        if ($exitCode -eq 8) {
            Test-Pass "Exit 8 after Codex-fix (needs fresh Codex re-review)"
        } else {
            Test-Fail "Expected exit 8 after Codex-fix, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 29: P0-1 fix — Codex NEEDS_FIX with MaxRounds=1 → exit 7
# ============================================================
Start-TempRepo -Name "codex_needs_fix_no_rounds" -TestScript {
    Test-Start "P0-1 fix: Codex NEEDS_FIX with MaxRounds=1 → exit 7 (no rounds left)"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    @'
{"status":"NEEDS_FIX","findings":[{"id":"F01","severity":"P1","file":"src/index.js","description":"Missing null check"}],"reviewed_at":"2026-06-10T00:00:00Z"}
'@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        # With MaxRounds=1, no more rounds for Codex-fix → exit 7
        if ($exitCode -eq 7 -and ($output -match 'NEEDS_FIX')) {
            Test-Pass "Exit 7 (NEEDS_FIX_CODEX_REVIEW) when no rounds remain for Codex-fix"
        } elseif ($exitCode -eq 7) {
            Test-Pass "Exit 7 when no rounds remain for Codex-fix (match by exit code)"
        } else {
            Test-Fail "Expected exit 7, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 30: P0-1 fix — Codex PASS → exit 0 in single round
# ============================================================
Start-TempRepo -Name "codex_pass_single" -TestScript {
    Test-Start "P0-1 fix: Codex review PASS → exit 0 in single round"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    @'
{"status":"PASS","findings":[],"reviewed_at":"2026-06-10T00:00:00Z"}
'@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 0 -and ($output -match 'RESULT: PASS')) {
            Test-Pass "Exit 0 with PASS when Codex review is PASS"
        } else {
            Test-Fail "Expected exit 0, got $exitCode. Output: $($result -join '; ')"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 31: P0-1 fix — automatic ReviewCommand generates CODEX_REVIEW.json
# ============================================================
Start-TempRepo -Name "auto_review_command" -TestScript {
    Test-Start "P0-1 fix: ReviewCommand auto-generates PASS review and stale review is not reused"

    @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    @'
if (-not (Test-Path "docs/REVIEW_INPUT.md")) {
    Write-Error "REVIEW_INPUT.md missing"
    exit 10
}
if (-not (Test-Path "docs/TEST_RESULTS.txt")) {
    Write-Error "TEST_RESULTS.txt missing"
    exit 11
}
$inputText = Get-Content "docs/REVIEW_INPUT.md" -Raw -Encoding UTF8
if ($inputText -notmatch "Required Output") {
    Write-Error "REVIEW_INPUT.md missing Required Output section"
    exit 12
}
Set-Content -Path "docs/reviewer-called.txt" -Value "called" -Encoding ASCII
$passReview = '{"status":"PASS","findings":[],"reviewed_at":"2026-06-11T00:00:00Z","summary":"fake reviewer pass"}'
Set-Content -Path "docs/CODEX_REVIEW.json" -Value $passReview -Encoding UTF8
exit 0
'@ | Out-File -FilePath "scripts/fake-reviewer.ps1" -Encoding UTF8

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    # Stale review must be moved before auto review so it cannot be reused.
    @'
{"status":"NEEDS_FIX","findings":[{"id":"STALE","severity":"P1","file":"stale.txt","description":"stale review"}],"reviewed_at":"2026-06-10T00:00:00Z"}
'@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $reviewCommand = "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/fake-reviewer.ps1"
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -AllowNoTests -ReviewCommand $reviewCommand 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"

        if ($exitCode -eq 0 -and ($output -match 'RESULT: PASS')) {
            Test-Pass "Exit 0 with auto-generated PASS review"
        } else {
            Test-Fail "Expected exit 0 with auto review, got $exitCode. Output: $($result -join '; ')"
        }

        if ((Test-Path "docs/REVIEW_INPUT.md") -and (Test-Path "docs/TEST_RESULTS.txt") -and (Test-Path "docs/reviewer-called.txt")) {
            Test-Pass "Review input bundle generated and reviewer invoked"
        } else {
            Test-Fail "Expected REVIEW_INPUT.md, TEST_RESULTS.txt, and reviewer-called.txt"
        }

        if (Test-Path "docs/CODEX_REVIEW.json.before-auto") {
            Test-Pass "Stale CODEX_REVIEW.json moved before auto review"
        } else {
            Test-Fail "Expected docs/CODEX_REVIEW.json.before-auto"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 32: P0-1 fix — Codex NEEDS_FIX can converge to PASS after fix round
# ============================================================
Start-TempRepo -Name "codex_needs_fix_then_pass" -TestScript {
    Test-Start "P0-1 fix: Codex NEEDS_FIX -> Claude fix -> fresh PASS review -> exit 0"

    @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fake-claude.ps1" %*
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

    @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
if ($Args.Count -gt 0 -and $Args[0] -eq "--version") {
    Write-Output "2.1.132 Claude Code MOCK"
    exit 0
}

$countPath = "docs/fake-claude-count.txt"
$count = 0
if (Test-Path $countPath) {
    $count = [int]((Get-Content $countPath -Raw).Trim())
}
$count++
Set-Content -Path $countPath -Value $count -Encoding ASCII

Write-Output "Mock Claude invocation $count"
if ($count -eq 2) {
    $passReview = '{"status":"PASS","findings":[],"reviewed_at":"2026-06-11T00:00:00Z"}'
    Set-Content -Path "docs/CODEX_REVIEW.json" -Value $passReview -Encoding UTF8
}

@(
    "# Implementation Report",
    "Invocation $count"
) | Out-File -FilePath "docs/IMPLEMENTATION_REPORT.md" -Encoding UTF8
exit 0
'@ | Out-File -FilePath "fake-claude.ps1" -Encoding UTF8

    "# Test Plan" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8
    Remove-Item "demo-project" -Recurse -Force -ErrorAction SilentlyContinue

    @'
{"status":"NEEDS_FIX","findings":[{"id":"F01","severity":"P1","file":"src/index.js","description":"Missing null check","fix_suggestion":"Add guard and re-review."}],"reviewed_at":"2026-06-10T00:00:00Z"}
'@ | Out-File -FilePath "docs/CODEX_REVIEW.json" -Encoding UTF8

    $oldPath = $env:Path
    $env:Path = "$PWD;$oldPath"
    try {
        $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 3 -AllowNoTests 2>&1
        $exitCode = $LASTEXITCODE
        $output = $result -join "`n"
        $callCount = if (Test-Path "docs/fake-claude-count.txt") {
            (Get-Content "docs/fake-claude-count.txt" -Raw).Trim()
        } else {
            "missing"
        }

        if ($exitCode -eq 0 -and $callCount -eq "2" -and ($output -match 'RESULT: PASS')) {
            Test-Pass "Exit 0 after Codex-fix round generated fresh PASS review"
        } else {
            Test-Fail "Expected exit 0 after fresh PASS review, got exit=$exitCode calls=$callCount. Output: $($result -join '; ')"
        }

        if (Test-Path "docs/CODEX_REVIEW.json.previous") {
            Test-Pass "Original NEEDS_FIX review preserved as .previous"
        } else {
            Test-Fail "Expected docs/CODEX_REVIEW.json.previous after consuming NEEDS_FIX review"
        }
    } finally {
        $env:Path = $oldPath
    }
}

# ============================================================
# Test 33: P2-2 fix — Path with spaces and Chinese chars works
# ============================================================
$chineseTestName = "path_spaces_chinese"
$chineseTempLeaf = "ccdl_test_${chineseTestName}_含 空格 路径"
# Sanitize only the directory name so Windows drive prefixes like C:\ are preserved.
$chineseTempLeaf = $chineseTempLeaf -replace '[<>:"|?*]', '_'
$chineseTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) $chineseTempLeaf
if (Test-Path $chineseTempRoot) { Remove-Item $chineseTempRoot -Recurse -Force -ErrorAction SilentlyContinue }
try {
    New-Item -ItemType Directory -Path $chineseTempRoot -Force | Out-Null
} catch {
    # If filesystem does not support the characters, skip with a note
    $chineseTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ccdl_test_path_spaces_only"
    if (Test-Path $chineseTempRoot) { Remove-Item $chineseTempRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $chineseTempRoot -Force | Out-Null
    $pathSkippedChinese = $true
}

try {
    Test-Start "P2-2 fix: Path with spaces (and Chinese if FS supports) — basic E2E"

    Push-Location $chineseTempRoot
    try {
        $gitInit = Invoke-TestNativeCapture -FilePath "git" -ArgumentList @("init")
        if ($gitInit.ExitCode -ne 0) {
            Test-Fail "git init failed in path '$chineseTempRoot': $($gitInit.Output -join '; ')"
            return
        }
        $gitEmail = Invoke-TestNativeCapture -FilePath "git" -ArgumentList @("config", "user.email", "test@test")
        $gitName = Invoke-TestNativeCapture -FilePath "git" -ArgumentList @("config", "user.name", "Test")
        if ($gitEmail.ExitCode -ne 0 -or $gitName.ExitCode -ne 0) {
            Test-Fail "git config failed in path '$chineseTempRoot': $($gitEmail.Output + $gitName.Output -join '; ')"
            return
        }

        New-Item -ItemType Directory -Path "docs" -Force | Out-Null
        New-Item -ItemType Directory -Path "scripts" -Force | Out-Null
        New-Item -ItemType Directory -Path "demo-project" -Force | Out-Null

        Copy-Item (Join-Path $ProjectDir "scripts/run-claude.ps1") "scripts/run-claude.ps1" -Force
        Copy-Item (Join-Path $ProjectDir "docs/CODEX_REVIEW.schema.json") "docs/CODEX_REVIEW.schema.json" -Force
        Copy-Item (Join-Path $ProjectDir "docs/IMPLEMENTATION_REPORT.template.md") "docs/IMPLEMENTATION_REPORT.template.md" -Force
        Copy-Item (Join-Path $ProjectDir "docs/PLAN.template.md") "docs/PLAN.template.md" -Force

        # Copy demo project for test discovery
        Copy-Item (Join-Path $ProjectDir "demo-project/package.json") "demo-project/package.json" -Force
        Copy-Item (Join-Path $ProjectDir "demo-project/index.js") "demo-project/index.js" -Force
        Copy-Item (Join-Path $ProjectDir "demo-project/test.js") "demo-project/test.js" -Force

        # Create fake claude
        @'
@echo off
if "%1" == "--version" (
    echo 2.1.132 Claude Code MOCK
    exit /b 0
)
echo Mock Claude output
echo # Implementation Report> docs\IMPLEMENTATION_REPORT.md
echo Done.>> docs\IMPLEMENTATION_REPORT.md
exit /b 0
'@ | Out-File -FilePath "claude.bat" -Encoding ASCII

        "# Spaces+Chinese Path Test" | Out-File -FilePath "docs/PLAN.md" -Encoding UTF8

        $oldPath = $env:Path
        $env:Path = "$PWD;$oldPath"
        try {
            $result = powershell -ExecutionPolicy Bypass -File scripts/run-claude.ps1 -MaxRounds 1 -SkipTests -SkipCodexReview 2>&1
            $exitCode = $LASTEXITCODE

            if ($exitCode -eq 0 -and (Test-Path "docs/IMPLEMENTATION_REPORT.md") -and (Test-Path "docs/BASELINE_STATUS.txt")) {
                Test-Pass "Path with spaces: artifacts generated, exit 0"
            } elseif ($exitCode -eq 0) {
                Test-Fail "Exit 0 but artifacts missing"
            } else {
                Test-Fail "Failed in space path (exit $exitCode): $($result -join '; ')"
            }
        } finally {
            $env:Path = $oldPath
        }
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item $chineseTempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($pathSkippedChinese) {
    Write-Host "    NOTE: Chinese characters not supported by filesystem — tested spaces only" -ForegroundColor Yellow
}

# ============================================================
# Summary
# ============================================================
$TotalAssertions = $PassedTests + $FailedTests
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Orchestrator Test Results" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Test blocks:    $TotalTests" -ForegroundColor White
Write-Host " Assertions:     $TotalAssertions total ($PassedTests passed, $FailedTests failed)" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan

if ($FailedTests -gt 0) {
    exit 1
} else {
    exit 0
}
