[CmdletBinding()]
param(
    [ValidateSet("Install", "Remove", "Status", "Start", "Stop")]
    [string]$Action = "Install",
    [string]$AppDir = "",
    [string]$DistroName = "",
    [string]$TaskName = "YBB-Inferenzserver Keepalive"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Resolve-AppDir {
    param([AllowEmptyString()][string]$Path)

    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    }

    $scriptDir = $PSScriptRoot
    if ((Split-Path -Leaf $scriptDir) -ieq "updater") {
        return (Split-Path -Parent $scriptDir)
    }
    return $scriptDir
}

function Get-InstallState {
    param([Parameter(Mandatory = $true)][string]$ResolvedAppDir)

    $statePath = Join-Path $ResolvedAppDir "install-state.json"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    }
    catch {
        Write-Log "WARNUNG: install-state.json konnte nicht gelesen werden: $($_.Exception.Message)"
        return $null
    }
}

function Resolve-DistroName {
    param(
        [AllowEmptyString()][string]$RequestedDistroName,
        [AllowNull()]$State
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedDistroName)) {
        return $RequestedDistroName
    }
    if ($null -ne $State -and $State.PSObject.Properties.Name -contains "DistroName") {
        $stateDistroName = [string]$State.DistroName
        if (-not [string]::IsNullOrWhiteSpace($stateDistroName)) {
            return $stateDistroName
        }
    }
    return "YBB-Inferenzserver"
}

function Get-YbbTask {
    param([Parameter(Mandatory = $true)][string]$Name)
    return Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
}

function Resolve-WindowsAccountSid {
    param([Parameter(Mandatory = $true)][string]$AccountName)

    try {
        $account = [System.Security.Principal.NTAccount]::new($AccountName)
        $sid = $account.Translate([System.Security.Principal.SecurityIdentifier])
        return [string]$sid.Value
    }
    catch {
        return ""
    }
}

function Test-WindowsAccountEquivalent {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedUserId,
        [Parameter(Mandatory = $true)][string]$RegisteredUserId
    )

    if ([string]::Equals(
        $ExpectedUserId,
        $RegisteredUserId,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $true
    }

    $expectedSid = Resolve-WindowsAccountSid -AccountName $ExpectedUserId
    $registeredSid = Resolve-WindowsAccountSid -AccountName $RegisteredUserId
    if (
        -not [string]::IsNullOrWhiteSpace($expectedSid) -and
        -not [string]::IsNullOrWhiteSpace($registeredSid)
    ) {
        return [string]::Equals(
            $expectedSid,
            $registeredSid,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }

    # Task Scheduler can abbreviate a freshly registered local identity from
    # MACHINE\user to user. Accept that representation only when exactly one
    # side is qualified and the unqualified account names are identical.
    $expectedParts = @($ExpectedUserId -split "\\", 2)
    $registeredParts = @($RegisteredUserId -split "\\", 2)
    if ($expectedParts.Count -eq 2 -and $registeredParts.Count -eq 1) {
        return [string]::Equals(
            $expectedParts[1],
            $registeredParts[0],
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    if ($expectedParts.Count -eq 1 -and $registeredParts.Count -eq 2) {
        return [string]::Equals(
            $expectedParts[0],
            $registeredParts[1],
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    return $false
}

function Assert-WslDistroAccessible {
    param([Parameter(Mandatory = $true)][string]$Name)

    $wslPath = Join-Path $env:WINDIR "System32\wsl.exe"
    if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) {
        throw "wsl.exe wurde nicht gefunden: $wslPath"
    }
    & $wslPath -d $Name -u root -- /bin/true 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Die WSL-Distribution '$Name' ist fuer den aktuellen Windows-Benutzer " +
            "nicht erreichbar. Der Boot-Task muss unter dem Benutzer registriert werden, " +
            "dem diese benutzergebundene WSL-Distribution gehoert."
        )
    }
}

function Assert-YbbScheduledTaskDefinition {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ExpectedUserId
    )

    $task = Get-YbbTask -Name $Name
    if ($null -eq $task) {
        throw "Windows-Autostart-Aufgabe wurde nicht registriert: $Name"
    }
    if (-not (Test-WindowsAccountEquivalent `
        -ExpectedUserId $ExpectedUserId `
        -RegisteredUserId ([string]$task.Principal.UserId)
    )) {
        throw (
            "Windows-Autostart-Aufgabe laeuft unter dem falschen Benutzer. " +
            "Erwartet: $ExpectedUserId; registriert: $($task.Principal.UserId)"
        )
    }
    if ([string]$task.Principal.LogonType -ne "S4U") {
        throw "Windows-Autostart-Aufgabe verwendet nicht LogonType S4U: $($task.Principal.LogonType)"
    }
    $bootTriggers = @(
        $task.Triggers |
            Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskBootTrigger" }
    )
    if ($bootTriggers.Count -eq 0) {
        throw "Windows-Autostart-Aufgabe besitzt keinen AtStartup-/Boot-Trigger: $Name"
    }
    if (-not $task.Settings.Enabled) {
        throw "Windows-Autostart-Aufgabe ist deaktiviert: $Name"
    }
}

function Install-YbbScheduledTask {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedAppDir,
        [Parameter(Mandatory = $true)][string]$ResolvedDistroName,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $startScript = Join-Path $ResolvedAppDir "start-ybb-inferenzserver.ps1"
    if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
        throw "Start-Skript fehlt: $startScript"
    }

    Assert-WslDistroAccessible -Name $ResolvedDistroName
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $escapedStartScript = $startScript.Replace('"', '""')
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$escapedStartScript`""

    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument $arguments `
        -WorkingDirectory $ResolvedAppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity `
        -LogonType S4U `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Startet und haelt die WSL-Dienste fuer $ResolvedDistroName am Leben." `
        -Force | Out-Null

    Assert-YbbScheduledTaskDefinition -Name $Name -ExpectedUserId $identity
    Write-Log "Windows-Autostart-Aufgabe registriert: $Name"
    Write-Log "Aufgabe laeuft als Benutzer: $identity"
    Write-Log "LogonType: S4U (Start beim PC-Boot ohne interaktive Benutzeranmeldung)"
}

function Remove-YbbScheduledTask {
    param([Parameter(Mandatory = $true)][string]$Name)

    $task = Get-YbbTask -Name $Name
    if ($null -eq $task) {
        Write-Log "Windows-Autostart-Aufgabe ist nicht vorhanden: $Name"
        return
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Log "Windows-Autostart-Aufgabe entfernt: $Name"
}

function Show-YbbScheduledTaskStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ResolvedDistroName
    )

    $task = Get-YbbTask -Name $Name
    if ($null -eq $task) {
        Write-Log "Windows-Autostart-Aufgabe ist nicht vorhanden: $Name"
        exit 2
    }

    $info = Get-ScheduledTaskInfo -TaskName $Name
    Write-Log "Task: $Name"
    Write-Log "State: $($task.State)"
    Write-Log "UserId: $($task.Principal.UserId)"
    Write-Log "LogonType: $($task.Principal.LogonType)"
    Write-Log "LastRunTime: $($info.LastRunTime)"
    Write-Log "LastTaskResult: $($info.LastTaskResult)"
    Write-Log "NextRunTime: $($info.NextRunTime)"

    $wslPath = Join-Path $env:WINDIR "System32\wsl.exe"
    if (Test-Path -LiteralPath $wslPath -PathType Leaf) {
        & $wslPath -d $ResolvedDistroName -u root -- bash -lc "if pgrep -f ybb-keepalive >/dev/null 2>&1; then echo 'YBB-Keepalive: aktiv'; else echo 'YBB-Keepalive: nicht aktiv'; fi" 2>$null
    }
}

function Start-YbbScheduledTask {
    param([Parameter(Mandatory = $true)][string]$Name)

    $task = Get-YbbTask -Name $Name
    if ($null -eq $task) {
        throw "Windows-Autostart-Aufgabe ist nicht vorhanden: $Name"
    }
    if ($task.State -eq "Running") {
        Write-Log "Windows-Autostart-Aufgabe laeuft bereits: $Name"
        return
    }
    Start-ScheduledTask -TaskName $Name
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $task = Get-YbbTask -Name $Name
        if ($null -ne $task -and $task.State -eq "Running") {
            Write-Log "Windows-Autostart-Aufgabe gestartet und laeuft dauerhaft: $Name"
            return
        }
    } while ((Get-Date) -lt $deadline)

    $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction SilentlyContinue
    $lastResult = if ($null -eq $info) { "<unbekannt>" } else { [string]$info.LastTaskResult }
    $state = if ($null -eq $task) { "<nicht vorhanden>" } else { [string]$task.State }
    throw (
        "Windows-Autostart-Aufgabe blieb nach dem Start nicht aktiv. " +
        "Task: $Name; State: $state; LastTaskResult: $lastResult. " +
        "Pruefe insbesondere die Richtlinie 'Anmelden als Stapelverarbeitungsauftrag'."
    )
}

function Stop-YbbScheduledTask {
    param([Parameter(Mandatory = $true)][string]$Name)

    $task = Get-YbbTask -Name $Name
    if ($null -eq $task) {
        Write-Log "Windows-Autostart-Aufgabe ist nicht vorhanden: $Name"
        return
    }
    if ($task.State -ne "Running") {
        Write-Log "Windows-Autostart-Aufgabe laeuft nicht: $Name"
        return
    }
    Stop-ScheduledTask -TaskName $Name
    Write-Log "Windows-Autostart-Aufgabe gestoppt: $Name"
}

$resolvedAppDir = Resolve-AppDir -Path $AppDir
$state = Get-InstallState -ResolvedAppDir $resolvedAppDir
$resolvedDistroName = Resolve-DistroName -RequestedDistroName $DistroName -State $state

switch ($Action) {
    "Install" {
        Install-YbbScheduledTask -ResolvedAppDir $resolvedAppDir -ResolvedDistroName $resolvedDistroName -Name $TaskName
    }
    "Remove" {
        Remove-YbbScheduledTask -Name $TaskName
    }
    "Status" {
        Show-YbbScheduledTaskStatus -Name $TaskName -ResolvedDistroName $resolvedDistroName
    }
    "Start" {
        Start-YbbScheduledTask -Name $TaskName
    }
    "Stop" {
        Stop-YbbScheduledTask -Name $TaskName
    }
}
