$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskMirrorDirectory = Join-Path $taskProjectRoot '.tools\scrcpy'
$taskScrcpy = Join-Path $taskMirrorDirectory 'scrcpy-win64-v5.0.1\scrcpy.exe'
if (-not (Test-Path -LiteralPath $taskScrcpy)) {
    New-Item -ItemType Directory -Force -Path $taskMirrorDirectory | Out-Null
    $taskArchive = Join-Path $taskMirrorDirectory 'scrcpy-win64-v5.0.1.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip' -OutFile $taskArchive
    $taskHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskHash -ne 'b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5') {
        throw 'scrcpy checksum differs from the official release. Installation stopped.'
    }
    Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskMirrorDirectory -Force
}
& $taskScrcpy --version
Write-Output 'Start mirroring from the project directory: .\.venv\Scripts\padelbot.exe android mirror'
