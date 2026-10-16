param(
    [Parameter(Mandatory = $true)]
    [string]$ClientExe
)

$resolvedExe = (Resolve-Path -LiteralPath $ClientExe).Path
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $resolvedExe -Argument 'daemon'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'AgentSyncDaemon' -Action $action -Trigger $trigger -Principal $principal -Description 'Collect and sync Codex sessions' -Force | Out-Null
Start-ScheduledTask -TaskName 'AgentSyncDaemon'
Write-Output 'AgentSyncDaemon is registered for user logon.'
