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
#
# Do NOT replace this with "... | Select-Object -Last N". See AGENTS-python.md.

param(
    [string]$Pattern = "",
    [int]$Timeout = 300
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root "exports\test-runs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $logDir "test-$stamp.log"

$python = "python"
$args = @("-u", "-m", "unittest", "discover", "-s", "tests")
if ($Pattern) { $args += @("-k", $Pattern) }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $python
$psi.Arguments = ($args -join " ")
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.WorkingDirectory = $root

$proc = [System.Diagnostics.Process]::Start($psi)
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()

$finished = $proc.WaitForExit($Timeout * 1000)
if (-not $finished) {
    try { $proc.Kill() } catch { }
    $out = $stdoutTask.Result
    $err = $stderrTask.Result
    ($out + "`n" + $err) | Out-File -FilePath $log -Encoding utf8
    Write-Host "TIMED OUT after $Timeout s. Full output: $log" -ForegroundColor Red
    exit 124
}

$out = $stdoutTask.Result
$err = $stderrTask.Result
($out + "`n" + $err) | Out-File -FilePath $log -Encoding utf8
$code = $proc.ExitCode

Write-Host "Full output: $log"
Write-Host "Exit code: $code"
if ($code -ne 0) {
    Write-Host "--- tail ---"
    Get-Content $log -Tail 40
}
exit $code