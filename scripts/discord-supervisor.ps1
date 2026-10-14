$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskData = Join-Path $taskRoot 'data'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskEntry = Join-Path $taskRoot 'scripts\run-discord.py'
$taskPidFile = Join-Path $taskData 'discord.pid'
if (!(Test-Path -LiteralPath $taskPython) -or !(Test-Path -LiteralPath $taskEntry) -or !(Test-Path -LiteralPath (Join-Path $taskRoot '.env'))) {
    throw 'Install and configure this project before enabling autostart.'
}
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
$taskPythonResolved = (& $taskPython -c 'import sys; print(sys.executable)').Trim()
$taskPythonBase = (& $taskPython -c 'import sys; print(sys._base_executable)').Trim()
$taskHash = [System.Security.Cryptography.SHA256]::Create()
$taskRootHash = [BitConverter]::ToString($taskHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($taskRoot.ToLowerInvariant()))).Replace('-', '').Substring(0, 24)
$taskHash.Dispose()
$taskMutex = New-Object System.Threading.Mutex($false, "Local\KulPadelSupervisor-$taskRootHash")
$taskAcquired = $false
try {
    try { $taskAcquired = $taskMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $taskAcquired = $true }
    if (!$taskAcquired) { exit 0 }
    Set-Content -LiteralPath (Join-Path $taskData 'discord.supervisor.pid') -Value $PID
    $taskBackoff = 5
    while ($true) {
        $taskExisting = $null
        if (Test-Path -LiteralPath $taskPidFile) {
            $taskCandidatePid = 0
            if ([int]::TryParse((Get-Content -LiteralPath $taskPidFile -Raw).Trim(), [ref]$taskCandidatePid)) {
                $taskCandidate = Get-CimInstance Win32_Process -Filter "ProcessId=$taskCandidatePid"
                if ($taskCandidate -and $taskCandidate.ExecutablePath -in @($taskPython, $taskPythonResolved, $taskPythonBase) -and $taskCandidate.CommandLine -match [regex]::Escape($taskEntry)) {
                    $taskExisting = $taskCandidate
                }
            }
        }
        if ($taskExisting) {
            Set-Content -LiteralPath (Join-Path $taskData 'discord.supervisor.status') -Value "healthy $($taskExisting.ProcessId) $([DateTimeOffset]::Now.ToString('o'))"
            $taskBackoff = 5
            Start-Sleep -Seconds 5
            continue
        }
        $taskStamp = [DateTimeOffset]::Now.ToString('yyyyMMdd-HHmmss-fff')
        foreach ($taskLogName in @('discord.stdout.log', 'discord.stderr.log')) {
            $taskLogPath = Join-Path $taskData $taskLogName
            if (Test-Path -LiteralPath $taskLogPath) {
                # Exact file moves within data; retain the previous crash evidence.
                Move-Item -LiteralPath $taskLogPath -Destination (Join-Path $taskData "$taskLogName.$taskStamp")
            }
        }
        $taskStarted = Start-Process -FilePath $taskPython -ArgumentList ('"' + $taskEntry + '"') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskData 'discord.stdout.log') -RedirectStandardError (Join-Path $taskData 'discord.stderr.log') -PassThru
        Add-Content -LiteralPath (Join-Path $taskData 'discord.supervisor.log') -Value "$taskStamp started launcher=$($taskStarted.Id)"
        # The Python singleton writes its own worker PID. Never replace it with
        # the venv launcher PID, and never kill a process using a stale PID file.
        Start-Sleep -Seconds $taskBackoff
        if ($taskStarted.HasExited) { $taskBackoff = [Math]::Min(60, $taskBackoff * 2) }
    }
} finally {
    if ($taskAcquired) { $taskMutex.ReleaseMutex() }
    $taskMutex.Dispose()
}
