param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiToken
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

Set-Content -Path $DefaultsPath -Encoding UTF8 -Value @"
DEFAULT_SERVER_URL = '$SafeServerUrl'
DEFAULT_API_TOKEN = '$SafeApiToken'
"@

Push-Location $ProjectRoot
try {
    & $PyInstaller --onefile --noconsole --name DriveAgent agent_client.py

    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
    Copy-Item -LiteralPath $DistExe -Destination $OutputExe -Force

    Remove-Item -LiteralPath (Join-Path $ProjectRoot "build") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $ProjectRoot "dist") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $ProjectRoot "DriveAgent.spec") -Force -ErrorAction SilentlyContinue

    Write-Host "Created $OutputExe"
    Write-Host "Embedded server URL: $ServerUrl"
}
finally {
    Pop-Location
}
