[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$PythonPath,

    [Parameter(Mandatory = $false)]
    [ValidateScript({ $_ -ge 1 })]
    [int]$Count = 1,

    [ValidateRange(1, 180)]
    [int]$MaxRuntimeMinutes = 170,

    [switch]$PromptForApiKeys,

    [switch]$Birdeye,

    [switch]$Force
)

# Memory-aware concurrency guard for local workstation (e.g. 16 GB RAM)
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os -and $os.TotalVisibleMemorySizeInKB) {
        $totalRamGb = [math]::Round($os.TotalVisibleMemorySizeInKB / 1024 / 1024, 1)
        if ($Count -gt 1 -and $totalRamGb -le 24.0 -and -not $Force) {
            Write-Warning "RESOURCE SAFETY GUARD: System detected with ~$totalRamGb GB RAM. Running $Count full scans simultaneously risks RAM exhaustion, swap thrashing, and provider rate-limit penalties. Clamping concurrency to 1 full scan. (Pass -Force to override)."
            $Count = 1
        }
    }
} catch {
    # Non-blocking fallback
}

$secretNames = @(
    'BIRDEYE_API_KEY',
    'BITQUERY_API_KEY',
    'BLOCKSCOUT_PRO_API_KEY',
    'COINGECKO_DEMO_API_KEY',
    'GOPLUS_ACCESS_TOKEN',
    'HELIUS_API_KEY',
    'JUPITER_API_KEY',
    'RUGCHECK_API_KEY',
    'SOLANA_RPC_URL',
    'ZEROX_API_KEY'
)
if ($PromptForApiKeys) {
    foreach ($name in $secretNames) {
        $secureValue = Read-Host "$name (input hidden; Enter keeps current value or skips)" -AsSecureString
        if ($secureValue.Length -eq 0) {
            $secureValue.Dispose()
            continue
        }
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
        try {
            $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $value = $null
            $secureValue.Dispose()
        }
    }

    foreach ($name in @('BLOCKSCOUT_CHAIN_IDS_JSON', 'ROCKET_TAKER_ADDRESS')) {
        $value = Read-Host "$name (optional; Enter keeps current value or skips)"
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
        $value = $null
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$candidates = @()
if ($PythonPath) { $candidates += $PythonPath }
if ($env:ROCKET_HUNTER_PYTHON) { $candidates += $env:ROCKET_HUNTER_PYTHON }
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) { $candidates += $pyLauncher.Source }
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand) {
    try {
        $realPy = (& python.exe -c "import sys; print(sys.executable)" 2>$null).Trim()
        if ($realPy -and (Test-Path -LiteralPath $realPy -PathType Leaf)) {
            $candidates += $realPy
        }
    } catch {}
    if ($pythonCommand.Source -notlike '*\WindowsApps\*') {
        $candidates += $pythonCommand.Source
    }
}
$candidates += 'C:\Program Files\WindowsApps\OpenAI.CodexPrimaryRuntime.v26-1105-1297-0_26.1105.1297.0_x64__3k8sg7r9htsxt\dependencies\python\python.exe'
$pythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $pythonPath) {
    throw 'Python 3 was not found. Set ROCKET_HUNTER_PYTHON or pass -PythonPath with your python.exe path.'
}
$runRoot = Join-Path $repoRoot 'rocket-runs'
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$started = @()
for ($index = 1; $index -le $Count; $index++) {
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $runName = (Get-Date -Format 'yyyyMMdd-HHmmss') + "-$index-$suffix"
    $runDir = Join-Path $runRoot $runName
    $monitorScript = Join-Path $PSScriptRoot 'watch_scan.ps1'
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $monitorScript,
        '-RunDirectory', $runDir, '-PythonPath', $pythonPath,
        '-MaxRuntimeMinutes', "$MaxRuntimeMinutes"
    )
    if ($Birdeye) { $arguments += '-Birdeye' }
    $quotedArguments = @($arguments | ForEach-Object {
        '"' + ($_ -replace '"', '\"') + '"'
    })
    if (-not $PSCmdlet.ShouldProcess($runDir, 'Start Rocket Hunter with a live progress window')) {
        continue
    }
    $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') `
        -ArgumentList ($quotedArguments -join ' ') -WorkingDirectory $repoRoot `
        -WindowStyle Normal -PassThru
    $started += [pscustomobject]@{
        Run = $runName
        MonitorProcessId = $process.Id
        Output = $runDir
    }
}

if ($started.Count -gt 0) { $started | Format-Table -AutoSize }
