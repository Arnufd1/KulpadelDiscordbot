param(
    [string]$PairEndpoint = '',
    [string]$ConnectEndpoint = ''
)

$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskAdb = Join-Path $taskProjectRoot '.tools\android\platform-tools\adb.exe'
$taskCli = Join-Path $taskProjectRoot '.venv\Scripts\padelbot.exe'
if (-not (Test-Path -LiteralPath $taskAdb) -or -not (Test-Path -LiteralPath $taskCli)) {
    throw 'Run scripts/setup-android.ps1 first.'
}

function Test-PhoneEndpoint([string]$Endpoint) {
    if ($Endpoint -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}:([0-9]{1,5})$') {
        throw 'Use the phone IPv4 address and port, for example 192.168.0.20:37001.'
    }
    $taskParts = $Endpoint.Split(':')
    [void][System.Net.IPAddress]::Parse($taskParts[0])
    if ([int]$taskParts[1] -lt 1 -or [int]$taskParts[1] -gt 65535) {
        throw 'Invalid port number.'
    }
}

if (-not $PairEndpoint) {
    $PairEndpoint = Read-Host 'IP address:port shown under Pair device with pairing code'
}
Test-PhoneEndpoint $PairEndpoint
Write-Host 'Leave the pairing window open on the phone. Enter its temporary code at the ADB prompt.'
& $taskAdb pair $PairEndpoint
if ($LASTEXITCODE -ne 0) { throw 'Pairing failed. Open a new pairing code on the phone and try again.' }

if (-not $ConnectEndpoint) {
    $ConnectEndpoint = Read-Host 'Now return to Wireless debugging. Enter its IP address:port (a different port)'
}
Test-PhoneEndpoint $ConnectEndpoint
if ($PairEndpoint.Split(':')[0] -ne $ConnectEndpoint.Split(':')[0]) {
    throw 'The pairing and connection IP addresses differ. Check the phone connection.'
}
& $taskAdb connect $ConnectEndpoint
if ($LASTEXITCODE -ne 0) { throw 'Connection failed. Check the phone IP address and connection port.' }

Set-Location -LiteralPath $taskProjectRoot
$env:PADELBOT_ANDROID_SERIAL = $ConnectEndpoint
& $taskCli android mirror
if ($LASTEXITCODE -ne 0) { throw 'Mirroring could not start. Check the connection shown above.' }
