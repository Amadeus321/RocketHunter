[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunDirectory,

    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [ValidateRange(1, 180)]
    [int]$MaxRuntimeMinutes = 170,

    [switch]$Birdeye
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runDirectory = [System.IO.Path]::GetFullPath($RunDirectory)
$runName = Split-Path -Leaf $runDirectory
$runRoot = Join-Path $repoRoot 'rocket-runs'
$launcherRoot = Join-Path $runRoot '_launcher'
$statusPath = Join-Path $runDirectory 'scan-status.json'
$logPath = Join-Path $runDirectory 'local-run.log'
$progressPath = Join-Path $runDirectory 'checkpoints\progress.json'
$stdoutPath = Join-Path $launcherRoot "$runName-stdout.log"
$stderrPath = Join-Path $launcherRoot "$runName-stderr.log"
$metadataPath = Join-Path $launcherRoot "$runName.json"

if (Test-Path -LiteralPath $runDirectory) {
    throw "Run output folder already exists: $runDirectory"
}
New-Item -ItemType Directory -Force -Path $launcherRoot | Out-Null

$arguments = @(
    '--outdir', "rocket-runs\$runName",
    '--max-runtime-minutes', "$MaxRuntimeMinutes"
)
if ($Birdeye) { $arguments += '--birdeye' }
if ([System.IO.Path]::GetFileName($PythonPath) -ieq 'py.exe') {
    $arguments = @('-3') + $arguments
}
$arguments = @('scripts/run_local_scan.py') + $arguments
$quotedArguments = @($arguments | ForEach-Object {
    '"' + ($_ -replace '"', '\"') + '"'
})
$process = Start-Process -FilePath $PythonPath -ArgumentList ($quotedArguments -join ' ') `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
$processStarted = [DateTimeOffset]::Now
@{
    process_id = $process.Id
    process_started_local = $processStarted.ToString('o')
    run_directory = $runDirectory
} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8

Write-Host "Rocket Hunter run: $runName"
Write-Host "Python process: $($process.Id)"
Write-Host "Output folder: $runDirectory"
Write-Host "Maximum duration: $MaxRuntimeMinutes minutes"

$lastConsoleUpdate = [DateTimeOffset]::MinValue
$lastProgressLine = ''
$status = $null
while ($true) {
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        } catch {
            $status = $null
        }
    }

    $startTime = $processStarted
    if ($status -and $status.scan_started_utc) {
        try { $startTime = [DateTimeOffset]::Parse($status.scan_started_utc) } catch {}
    }
    $elapsed = [DateTimeOffset]::Now - $startTime
    $sourceCount = 0
    $lastSource = 'Preparing scan'
    if (Test-Path -LiteralPath $progressPath) {
        try {
            $progress = Get-Content -LiteralPath $progressPath -Raw | ConvertFrom-Json
            $sourceCount = [int]$progress.source_count
            if ($progress.last_source) { $lastSource = [string]$progress.last_source }
        } catch {}
    }
    $lastLogLine = ''
    if (Test-Path -LiteralPath $logPath) {
        $lastLogLine = (Get-Content -LiteralPath $logPath -Tail 1 -ErrorAction SilentlyContinue)
    }
    if (-not $lastLogLine) { $lastLogLine = $lastSource }
    $currentStatus = if ($status -and $status.status) { [string]$status.status } else { 'STARTING' }
    $process.Refresh()
    $progressText = "Elapsed $($elapsed.ToString('hh\:mm\:ss')) | $currentStatus | sources $sourceCount | $lastLogLine"
    Write-Progress -Activity "Rocket Hunter: $currentStatus" -Status $progressText `
        -CurrentOperation "Latest source: $lastSource"

    if ($status -and $status.status -ne 'IN_PROGRESS' -and $status.status) {
        break
    }
    if ($process.HasExited) {
        Start-Sleep -Seconds 2
        if (Test-Path -LiteralPath $statusPath) {
            try { $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json } catch {}
        }
        if (-not $status -or $status.status -eq 'IN_PROGRESS') {
            $finish = [DateTimeOffset]::UtcNow
            $state = @{
                status = 'INTERRUPTED'
                run_id = if ($status) { $status.run_id } else { $null }
                scan_started_utc = if ($status -and $status.scan_started_utc) { $status.scan_started_utc } else { $processStarted.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }
                scan_finished_utc = $finish.ToString("yyyy-MM-ddTHH:mm:ssZ")
                scan_duration_seconds = [math]::Round(($finish - $startTime.ToUniversalTime()).TotalSeconds, 3)
                process_id = $process.Id
                failure_reason = 'Runner process exited without finalizing scan status.'
            }
            $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statusPath -Encoding UTF8
            foreach ($name in @('rocket-hunt.json', 'forward-24h-rocket.json')) {
                $reportPath = Join-Path $runDirectory $name
                if (-not (Test-Path -LiteralPath $reportPath)) { continue }
                try {
                    $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
                    $report.scan_status = 'INTERRUPTED'
                    $report.verified_actionable_opportunities = @()
                    $report.best_immediate_entry = $null
                    $report.top_rocket_execution_ready = $false
                    $report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $reportPath -Encoding UTF8
                } catch {
                    Write-Warning "Could not invalidate partial report $reportPath`: $_"
                }
            }
            $status = [pscustomobject]$state
        }
        break
    }

    if (([DateTimeOffset]::Now - $lastConsoleUpdate).TotalSeconds -ge 15 -or $lastLogLine -ne $lastProgressLine) {
        Write-Host ("[{0}] {1} | elapsed {2} | sources {3} | {4}" -f `
            (Get-Date -Format 'HH:mm:ss'), $currentStatus, $elapsed.ToString('hh\:mm\:ss'), $sourceCount, $lastLogLine)
        $lastConsoleUpdate = [DateTimeOffset]::Now
        $lastProgressLine = $lastLogLine
    }
    Start-Sleep -Seconds 2
}

Write-Progress -Activity 'Rocket Hunter' -Completed
$finalStatus = if ($status -and $status.status) { [string]$status.status } else { 'UNKNOWN' }
Write-Host ""
Write-Host "Final scan status: $finalStatus"
if ($status -and $status.scan_duration_seconds) {
    Write-Host ("Recorded duration: {0:N1} minutes" -f ([double]$status.scan_duration_seconds / 60.0))
}
Write-Host "Output folder: $runDirectory"
Read-Host 'Press Enter to close this scan window' | Out-Null
