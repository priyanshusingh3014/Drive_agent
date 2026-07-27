param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiToken,

    [string]$DrivePriority = "D",

    [int]$HeartbeatSeconds = 1,

    [int]$CountRefreshSeconds = 60,

    [int]$FileBatchSize = 1000,

    [int]$FirstFileBatchSize = 10,

    [double]$FileBatchIntervalSeconds = 0.15,

    [double]$SystemDriveDelaySeconds = 1,

    [int]$ChangeDebounceSeconds = 1,

    [bool]$LanDiscoveryEnabled = $false
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$VenvPyInstaller = Join-Path $ProjectRoot "venv\Scripts\pyinstaller.exe"
$CodexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$DefaultsPath = Join-Path $ProjectRoot "agent_build_defaults.py"
$DownloadDir = Join-Path $ProjectRoot "agent_download"
$DistExe = Join-Path $ProjectRoot "dist\DriveAgent.exe"
$OutputExe = Join-Path $DownloadDir "DriveAgent.exe"

if (-not $ServerUrl.EndsWith("/agent-heartbeat/")) {
    $ServerUrl = $ServerUrl.TrimEnd("/") + "/agent-heartbeat/"
}

function Test-PythonCommand {
    param([string[]]$CommandParts)

    $Executable = $CommandParts[0]
    $Arguments = @()

    if ($CommandParts.Length -gt 1) {
        $Arguments = $CommandParts[1..($CommandParts.Length - 1)]
    }

    try {
        & $Executable @Arguments --version *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Invoke-PyInstaller {
    if ((Test-Path -LiteralPath $VenvPyInstaller)) {
        & $VenvPyInstaller --onefile --noconsole --name DriveAgent agent_client.py

        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $DistExe)) {
            return
        }
    }

    if ((Test-Path -LiteralPath $VenvPython)) {
        & $VenvPython -m PyInstaller --onefile --noconsole --name DriveAgent agent_client.py

        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $DistExe)) {
            return
        }
    }

    if ((Test-Path -LiteralPath $CodexPython)) {
        $env:PYTHONPATH = Join-Path $ProjectRoot "venv\Lib\site-packages"
        & $CodexPython -m PyInstaller --onefile --noconsole --name DriveAgent agent_client.py

        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $DistExe)) {
            return
        }
    }

    if (Test-PythonCommand @("py")) {
        $env:PYTHONPATH = Join-Path $ProjectRoot "venv\Lib\site-packages"
        & py -m PyInstaller --onefile --noconsole --name DriveAgent agent_client.py

        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $DistExe)) {
            return
        }
    }

    if (Test-PythonCommand @("python")) {
        $env:PYTHONPATH = Join-Path $ProjectRoot "venv\Lib\site-packages"
        & python -m PyInstaller --onefile --noconsole --name DriveAgent agent_client.py

        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $DistExe)) {
            return
        }
    }

    throw "Unable to run PyInstaller. Recreate the venv or install PyInstaller for your active Python."
}

$SafeServerUrl = $ServerUrl.Replace("\", "\\").Replace("'", "\'")
$SafeApiToken = $ApiToken.Replace("\", "\\").Replace("'", "\'")
$SafeDrivePriority = $DrivePriority.Replace("\", "\\").Replace("'", "\'")
$PythonLanDiscoveryEnabled = if ($LanDiscoveryEnabled) { "True" } else { "False" }

Set-Content -Path $DefaultsPath -Encoding UTF8 -Value @"
DEFAULT_SERVER_URL = '$SafeServerUrl'
DEFAULT_API_TOKEN = '$SafeApiToken'
DEFAULT_HEARTBEAT_SECONDS = $HeartbeatSeconds
DEFAULT_COUNT_REFRESH_SECONDS = $CountRefreshSeconds
DEFAULT_FILE_BATCH_SIZE = $FileBatchSize
DEFAULT_CHANGE_DEBOUNCE_SECONDS = $ChangeDebounceSeconds
DEFAULT_LAN_DISCOVERY_ENABLED = $PythonLanDiscoveryEnabled
DEFAULT_DRIVE_PRIORITY = '$SafeDrivePriority'
DEFAULT_FIRST_FILE_BATCH_SIZE = $FirstFileBatchSize
DEFAULT_FILE_BATCH_INTERVAL_SECONDS = $FileBatchIntervalSeconds
DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS = $SystemDriveDelaySeconds
"@

Push-Location $ProjectRoot
try {
    Invoke-PyInstaller

    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
    Copy-Item -LiteralPath $DistExe -Destination $OutputExe -Force

    Remove-Item -LiteralPath (Join-Path $ProjectRoot "build") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $ProjectRoot "dist") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $ProjectRoot "DriveAgent.spec") -Force -ErrorAction SilentlyContinue

    Write-Host "Created $OutputExe"
    Write-Host "Embedded server URL: $ServerUrl"
    Write-Host "Embedded drive priority: $DrivePriority"
    Write-Host "Embedded heartbeat seconds: $HeartbeatSeconds"
    Write-Host "Embedded file batch size: $FileBatchSize"
    Write-Host "Embedded first file batch size: $FirstFileBatchSize"
    Write-Host "Embedded file batch interval seconds: $FileBatchIntervalSeconds"
    Write-Host "Embedded system drive delay seconds: $SystemDriveDelaySeconds"
    Write-Host "Embedded LAN discovery enabled: $LanDiscoveryEnabled"
}
finally {
    Pop-Location
}
