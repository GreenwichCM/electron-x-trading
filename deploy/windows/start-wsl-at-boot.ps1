# Starts WSL, and with it production's services, whenever Windows starts, whether or not anyone is signed in,
# and keeps it running. Run once on the always-on machine, in PowerShell as administrator:
#
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\<Distro>\home\<you>\trading\deploy\windows\start-wsl-at-boot.ps1 -Distro <Distro>
#
# <Distro> is the name `wsl -l -v` shows. Windows asks for your own Windows password, because a task that runs
# while nobody is signed in needs it. It is stored by Windows with the task, nowhere else.
param([string]$Distro = "Ubuntu")

$name = "ElectronX trading (WSL)"
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
    -Message "Your Windows password, so the task can start WSL while nobody is signed in"
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro --exec /bin/sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName $name
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
Write-Host "WSL ($Distro) now starts with Windows. After the next restart, check with:"
Write-Host "  wsl -d $Distro -- bash -lc 'cd ~/trading && uv run python deploy/status.py'"
