# Power Monitor collector — Windows Task Scheduler registration
#
# Registers a per-user background task that starts at logon (no admin,
# no console window). Uses pythonw.exe so nothing pops up on the desktop.
#
# View / manage the task: Win+R → taskschd.msc → Task Scheduler Library
#   → PowerMonitorCollector
#
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\deploy\windows\Register-CollectorTask.ps1
#
# Unregister:
#   .\deploy\windows\Unregister-CollectorTask.ps1

param(
    [string]$Python = "python",
    [string]$TaskName = "PowerMonitorCollector",
    [int]$IntervalSeconds = 10
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $env:LOCALAPPDATA "power-monitor"
$LogFile = Join-Path $DataDir "collector.log"

if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir | Out-Null
}

function Resolve-Pythonw {
    param([string]$PythonCmd)

    $pythonExe = (Get-Command $PythonCmd -ErrorAction Stop).Source
    $dir = Split-Path -Parent $pythonExe
    $pythonw = Join-Path $dir "pythonw.exe"
    if (Test-Path $pythonw) {
        return $pythonw
    }

    # python may be a launcher shim; try py -0p / sibling discovery
    $cmd = Get-Command "py" -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $listed = & py -0p 2>$null
            foreach ($line in $listed) {
                if ($line -match "(.+python\.exe)\s*$") {
                    $candidate = Join-Path (Split-Path -Parent $Matches[1].Trim()) "pythonw.exe"
                    if (Test-Path $candidate) {
                        return $candidate
                    }
                }
            }
        } catch {}
    }

    Write-Warning "pythonw.exe not found next to $pythonExe; falling back to python.exe (console may flash)."
    return $pythonExe
}

# Always use pythonw + module form so the task is headless (console .exe
# wrappers from pip would open a terminal window).
$Exe = Resolve-Pythonw -PythonCmd $Python
$Arguments = "-m power_monitor.collector --interval $IntervalSeconds --logfile `"$LogFile`""

Write-Host "Repository:  $RepoRoot"
Write-Host "Data dir:    $DataDir"
Write-Host "Log file:    $LogFile"
Write-Host "Executable:  $Exe"
Write-Host "Arguments:   $Arguments"
Write-Host ""
Write-Host "This registers a Windows Task Scheduler job (background). It is NOT"
Write-Host "the Settings → Apps → Startup list; open taskschd.msc to see it."

$Action = New-ScheduledTaskAction `
    -Execute $Exe `
    -Argument $Arguments `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Hidden

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "power-monitor collector (background): sample CPU energy via Windows EMI into SQLite" `
        -Force | Out-Null
} catch {
    Write-Error @"
Failed to register scheduled task '$TaskName': $($_.Exception.Message)

Start manually in the foreground for debugging:
  python -m power_monitor.collector
"@
    exit 1
}

# Stop any previous instance of this task, then start the new one
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
} catch {}

try {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ""
    Write-Host "Registered and started '$TaskName' (hidden / pythonw)."
    Write-Host "Last task result: $($info.LastTaskResult)  (0 = running/ok after start)"
} catch {
    Write-Warning "Task registered but could not start immediately: $($_.Exception.Message)"
    Write-Warning "It will start at next logon."
}

Write-Host ""
Write-Host "Where to find it:"
Write-Host "  Task Scheduler → taskschd.msc → $TaskName"
Write-Host "  Process: pythonw.exe (no console window)"
Write-Host ""
Write-Host "Verify with:"
Write-Host "  python -m power_monitor probe"
Write-Host "  python -m power_monitor status"
Write-Host "  Get-Content `"$LogFile`" -Tail 20"
Write-Host "DB path: $DataDir\power.db"
