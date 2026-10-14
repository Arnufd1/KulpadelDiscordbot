$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskToolsDirectory = Join-Path $taskProjectRoot '.tools\android'
$taskAdb = Join-Path $taskToolsDirectory 'platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $taskAdb)) {
    New-Item -ItemType Directory -Force -Path $taskToolsDirectory | Out-Null
    $taskArchive = Join-Path $taskToolsDirectory 'platform-tools.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $taskArchive
    Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskToolsDirectory -Force
}
$taskPython = Join-Path $taskProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create the project .venv first; see README.md.'
}
Push-Location $taskProjectRoot
try {
    & $taskPython -m pip install -e '.[android]'
    if ($LASTEXITCODE -ne 0) { throw 'Android dependency installation failed.' }
    & $taskAdb devices -l
    if ($LASTEXITCODE -ne 0) { throw 'ADB failed.' }
} finally {
    Pop-Location
}
