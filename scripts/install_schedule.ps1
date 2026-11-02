# Registers the daily digest pipeline (worker -> manager -> delivery) in Windows Task Scheduler (\PersonalAgent\EmailWorkerDaily).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1               # time from config/email_worker.toml
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1 -Wake         # also wake the machine from sleep
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1 -Unattended   # also when logged out (run as admin)
#   Unregister-ScheduledTask -TaskPath \PersonalAgent\ -TaskName EmailWorkerDaily        # remove
#
# Triggers: daily at [schedule].daily_at repeating hourly until 23:00, at logon, and on wake (+2 min for Wi-Fi). Windows'
# "run missed task" setting proved unreliable across sleep, so the pipeline's --scheduled guard decides instead:
# it does work only once per day, on the first trigger at/after daily_at, and exits in about a second otherwise.
param(
    [switch]$Unattended,
    [switch]$Wake  # wake the machine from sleep at the scheduled time (off by default: it's a laptop)
)

$root = Split-Path $PSScriptRoot -Parent
# Run the project's windowless Python directly: no console window to pop up or be closed mid-run
# (closing uv.exe's console killed a run with 0xC000013A). `uv sync` creates .venv.
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw; run 'uv sync' in $root first." }
$configPath = Join-Path $root "config\email_worker.toml"
if (-not (Test-Path $configPath)) { throw "Missing $configPath; copy config\email_worker.example.toml to it first." }
$config = Get-Content $configPath -Raw
$at = if ($config -match '(?m)^\s*daily_at\s*=\s*"(\d{1,2}:\d{2})"') { $Matches[1] } else { "10:00" }
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m swarm.pipelines.daily_digest --scheduled" -WorkingDirectory $root

$daily = New-ScheduledTaskTrigger -Daily -At $at
# Repeat hourly until 23:00 so an interrupted or failed run is retried the same day; once today's run has
# succeeded, these extra starts exit after the guard check.
$daily.Repetition = (New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours (23 - [int]($at.Split(":")[0])))).Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = "PT2M"
$eventClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$resume = New-CimInstance -CimClass $eventClass -ClientOnly
$resume.Enabled = $true
$resume.Delay = "PT2M"
$resume.Subscription = @"
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>
"@

# Battery flags: Windows' defaults skip tasks on battery and kill them when unplugged.
# No network condition here: the pipeline waits for the network itself instead of being silently skipped.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -WakeToRun:$Wake -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
# S4U runs without an interactive session and without storing your password (needs an elevated prompt to register).
$logonType = if ($Unattended) { "S4U" } else { "Interactive" }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $logonType -RunLevel Limited

Register-ScheduledTask -TaskPath "\PersonalAgent\" -TaskName "EmailWorkerDaily" -Action $action `
    -Trigger @($daily, $logon, $resume) -Settings $settings -Principal $principal `
    -Description "Summarize new Outlook mail and email a digest, once a day at/after $at" -Force | Out-Null

$task = Get-ScheduledTask -TaskPath "\PersonalAgent\" -TaskName "EmailWorkerDaily"
"Registered: daily at $at, at logon, on wake. Next timed run: " + (Get-ScheduledTaskInfo -InputObject $task).NextRunTime
