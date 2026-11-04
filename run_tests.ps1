# run_tests.ps1 - canonical, safe test runner for the Hotel-Management project.
#
# Why this exists:
#   Piping a test run through a truncating filter (Select-Object, tail, head, grep,
#   Out-GridView) as the FINAL stage does two bad things:
#     1. It buffers stdout, so a hung process never flushes the traceback that
#        would identify the hang (we hit this: an un-stubbed wizard helper looping
#        on scripted input, traceback invisible, hours lost).
#     2. It makes the pipe the last stage, so $LASTEXITCODE reports the filter's
#        success, not the runner's - a hung or failing suite can exit 0.
#
# This wrapper instead:
#   - runs Python UNBUFFERED (python -u) so output flushes as it is produced
#   - captures the FULL stream to a dated log file (source of truth)
#   - enforces a hard timeout on the process
#   - prints only the log path and the real exit code
#   - inspects the tail ONLY after the process has exited
#
# Usage:
#   .\run_tests.ps1                 # run the whole suite
#   .\run_tests.ps1 -Pattern foo    # run tests matching 'foo'
#   .\run_tests.ps1 -Timeout 120    # override the timeout (seconds)
#   .\run_tests.ps1 -Db             # also run the DB/schema checks (opt-in)
#
# -Db runs check_schema_sync.py, check_migration_sql.py,
# check_applied_migrations.py and verify_e2e.py after the suite. Opt-in because
# verify_e2e.py creates and drops its own database and needs a login with
# CREATE DATABASE authority. check_applied_migrations.py and verify_e2e.py also
# need a live SQL Server reachable through config.ini.
#
# Do NOT replace this with "... | Select-Object -Last N". See AGENTS-python.md.

param(
    [string]$Pattern = "",
    [int]$Timeout = 300,
    [switch]$Db
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root "exports\test-runs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $logDir "test-$stamp.log"

$python = "python"

function Invoke-PythonStage {
    param(
        [string[]]$Args,
        [int]$StageTimeout = $Timeout
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = ($Args -join " ")
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.UseShellExecute = $false
    $psi.WorkingDirectory = $root
    $proc = [System.Diagnostics.Process]::Start($psi)
    try { $proc.StandardInput.Close() } catch { }
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $finished = $proc.WaitForExit($StageTimeout * 1000)
    if (-not $finished) {
        try { $proc.Kill() } catch { }
        $out = $stdoutTask.Result
        $err = $stderrTask.Result
        ($out + "`n" + $err) | Out-File -FilePath $log -Append -Encoding utf8
        Write-Host "TIMED OUT after $StageTimeout s. Full output: $log" -ForegroundColor Red
        return 124
    }
    $out = $stdoutTask.Result
    $err = $stderrTask.Result
    ($out + "`n" + $err) | Out-File -FilePath $log -Append -Encoding utf8
    return $proc.ExitCode
}

$args = @("-u", "-m", "unittest", "discover", "-s", "tests")
if ($Pattern) { $args += @("-k", $Pattern) }

"=== unit tests ===" | Out-File -FilePath $log -Encoding utf8
$code = Invoke-PythonStage -Args $args
Write-Host "Full output: $log"
Write-Host "Unit tests exit code: $code"
if ($code -ne 0) {
    Write-Host "--- tail ---"
    Get-Content $log -Tail 40
    exit $code
}

if ($Db) {
    $dbChecks = @(
        @("-u", "tests/check_schema_sync.py"),
        @("-u", "tests/check_migration_sql.py"),
        @("-u", "tests/check_applied_migrations.py"),
        @("-u", "tests/verify_e2e.py")
    )
    foreach ($check in $dbChecks) {
        "`n=== $($check -join ' ') ===" | Out-File -FilePath $log -Append -Encoding utf8
        Write-Host "Running $check ..."
        $c = Invoke-PythonStage -Args $check -StageTimeout 600
        Write-Host "$check exit code: $c"
        if ($c -ne 0) {
            Write-Host "--- tail ---"
            Get-Content $log -Tail 40
            exit $c
        }
    }
}

exit 0