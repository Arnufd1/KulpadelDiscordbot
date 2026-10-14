$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskExecutable = Join-Path $taskProjectRoot '.venv\Scripts\padelbot.exe'
$Host.UI.RawUI.WindowTitle = 'KUL Padel - automatische betaling'
Push-Location $taskProjectRoot
try {
    & $taskExecutable set-card
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Kaart niet opgeslagen. Start dit venster opnieuw om de gegevens te corrigeren.' -ForegroundColor Red
    }
} finally {
    Pop-Location
}
Read-Host 'Druk op Enter om dit venster te sluiten'
