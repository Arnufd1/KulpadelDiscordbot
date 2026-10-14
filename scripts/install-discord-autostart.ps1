param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskScript = Join-Path $taskRoot 'scripts\discord-supervisor.ps1'
$taskShell = (Get-Command powershell.exe).Source
$taskName = 'KUL Padel Discord Bot'
$taskRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ($Remove) {
    $taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($taskExisting) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Remove-ItemProperty -LiteralPath $taskRunKey -Name 'KulPadelBot' -ErrorAction SilentlyContinue
    Write-Output 'Automatic startup disabled. The current bot remains running.'
    exit 0
}
$taskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskScript + '"'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskShell -Argument $taskArguments -WorkingDirectory $taskRoot
$taskTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $taskUser),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
try {
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTriggers -Principal $taskPrincipal -Settings $taskSettings -Description 'Keep the own KUL Padel bot available; the authenticator starts on demand.' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Output 'Enabled Task Scheduler startup and automatic recovery.'
} catch {
    # A standard Windows account can always use its own Run key. No admin
    # password, broader privileges, or visible console is needed.
    New-Item -Path $taskRunKey -Force | Out-Null
    New-ItemProperty -LiteralPath $taskRunKey -Name 'KulPadelBot' -PropertyType String -Value ('"' + $taskShell + '" ' + $taskArguments) -Force | Out-Null
    Start-Process -FilePath $taskShell -ArgumentList $taskArguments -WorkingDirectory $taskRoot -WindowStyle Hidden | Out-Null
    Write-Output 'Task Scheduler unavailable; enabled per-user Windows startup and bot recovery.'
}
