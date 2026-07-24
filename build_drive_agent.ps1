param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiToken,

    [string]$DrivePriority = "D,C",

    [int]$HeartbeatSeconds = 1,

    [int]$CountRefreshSeconds = 60,

    [int]$FileBatchSize = 250,

    [int]$FirstFileBatchSize = 10,

    [double]$FileBatchIntervalSeconds = 1,

    [int]$ChangeDebounceSeconds = 1,

    [bool]$LanDiscoveryEnabled = $false
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$PyInstaller = Join-Path $ProjectRoot "venv\Scripts\pyinstaller.exe"
$DefaultsPath = Join-Path $ProjectRoot "agent_build_defaults.py"
$DownloadDir = Join-Path $ProjectRoot "agent_download"
$DistExe = Join-Path $ProjectRoot "dist\DriveAgent.exe"
$OutputExe = Join-Path $DownloadDir "DriveAgent.exe"

if (-not $ServerUrl.EndsWith("/agent-heartbeat/")) {
    $ServerUrl = $ServerUrl.TrimEnd("/") + "/agent-heartbeat/"
}

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

if (-not (Test-Path -LiteralPath $PyInstaller)) {
    & $Python -m pip install pyinstaller
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
"@

Push-Location $ProjectRoot
try {
    & $PyInstaller --onefile --name DriveAgent agent_client.py

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
    Write-Host "Embedded LAN discovery enabled: $LanDiscoveryEnabled"
}
finally {
    Pop-Location
}
