param([switch]$ShowWindow)

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskSdk = Join-Path $taskRoot '.tools\android'
$taskAvdHome = Join-Path $taskRoot 'data\emulators'
$taskEmulator = Join-Path $taskSdk 'emulator\emulator.exe'
$taskAdb = Join-Path $taskSdk 'platform-tools\adb.exe'
if (!(Test-Path -LiteralPath $taskEmulator) -or !(Test-Path -LiteralPath (Join-Path $taskAvdHome 'KulPadelAuth.ini'))) {
    throw 'The local software authenticator has not been installed. See docs/software-authenticator.md.'
}
$taskDevices = & $taskAdb devices
if ($taskDevices -match '^emulator-5554\s+(device|offline|unauthorized)') {
    Write-Output 'The virtual device is already running. Close that device before switching between window and background modes.'
    exit 0
}
$env:ANDROID_SDK_ROOT = $taskSdk
$env:ANDROID_AVD_HOME = $taskAvdHome
$taskArguments = @('-avd', 'KulPadelAuth', '-port', '5554', '-no-audio', '-no-boot-anim', '-no-snapshot', '-gpu', 'swiftshader_indirect')
$taskOptions = @{
    FilePath = $taskEmulator
    ArgumentList = $taskArguments
    WorkingDirectory = $taskRoot
    RedirectStandardOutput = Join-Path $taskAvdHome 'emulator.stdout.log'
    RedirectStandardError = Join-Path $taskAvdHome 'emulator.stderr.log'
    PassThru = $true
}
if (!$ShowWindow) {
    $taskOptions.ArgumentList += '-no-window'
    $taskOptions.WindowStyle = 'Hidden'
}
$taskStarted = Start-Process @taskOptions
Set-Content -LiteralPath (Join-Path $taskAvdHome 'emulator.pid') -Value $taskStarted.Id
Write-Output "Started virtual Android (emulator-5554), process $($taskStarted.Id)."
Write-Output 'KUL account enrollment and a separate calibrated login profile are required before automatic login works.'
