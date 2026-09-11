[CmdletBinding()]
param(
    [ValidateSet("Install", "Remove", "Status", "Start", "Stop", "Run")]
    [string]$Action = "Install",
    [string]$AppDir = "",
    [string]$BundleRoot = "",
    [Alias("AppHostName", "HostName")]
    [string]$PublicAddress = "",
    [string]$Version = "unknown",
    [int]$RtspPort = 8554,
    [int]$WebRtcPort = 8889,
    [int]$WebRtcIceTcpPort = 0,
    [int]$TurnTcpPort = 3478,
    [string]$TaskName = "YBB-Inferenzserver MediaMTX"
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
    if ((Split-Path -Leaf $PSScriptRoot) -ieq "updater") {
        return (Split-Path -Parent $PSScriptRoot)
    }
    return $PSScriptRoot
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

function ConvertTo-PublicAuthority {
    param([Parameter(Mandatory = $true)][string]$Value)

    $text = $Value.Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return "127.0.0.1"
    }
    if ($text -match "^[A-Za-z][A-Za-z0-9+.-]*://") {
        $uri = [System.Uri]$text
        return $uri.Host
    }
    return ($text -split "/", 2)[0].Trim()
}

function Get-PublicHostPart {
    param([Parameter(Mandatory = $true)][string]$Value)

    $authority = ConvertTo-PublicAuthority -Value $Value
    if ($authority -match "^\[(?<Host>[^\]]+)\](?::\d+)?$") {
        return $Matches.Host
    }
    if ($authority -match "^(?<Host>\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?$") {
        return $Matches.Host
    }
    if ($authority -match "^(?<Host>[^:]+):\d+$") {
        return $Matches.Host
    }
    return $authority
}

function Resolve-PublicHost {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$RequestedPublicAddress,
        [AllowNull()]$State
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPublicAddress)) {
        return Get-PublicHostPart -Value $RequestedPublicAddress
    }
    if ($null -ne $State -and $State.PSObject.Properties.Name -contains "AppHostName") {
        $stateHost = [string]$State.AppHostName
        if (-not [string]::IsNullOrWhiteSpace($stateHost)) {
            return Get-PublicHostPart -Value $stateHost
        }
    }
    return "127.0.0.1"
}

function Test-IsLocalPublicHost {
    param([Parameter(Mandatory = $true)][string]$HostName)

    $value = $HostName.Trim().ToLowerInvariant()
    return $value -in @("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")
}

function Get-HostIpv4Addresses {
    $physicalInterfaceIndexes = @(
        Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
            Where-Object { $_.Status -eq "Up" } |
            Select-Object -ExpandProperty ifIndex -Unique
    )
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -ne "127.0.0.1" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown" -and
            $_.AddressState -eq "Preferred" -and
            (
                $physicalInterfaceIndexes.Count -eq 0 -or
                $physicalInterfaceIndexes -contains $_.InterfaceIndex
            ) -and
            (
                [string]$_.InterfaceAlias -notmatch "(?i)vEthernet|WSL|Hyper-V|Docker|VirtualBox|VMware|Loopback"
            )
        } |
        Select-Object -ExpandProperty IPAddress -Unique
}

function Get-WebRtcAdditionalHosts {
    param([Parameter(Mandatory = $true)][string]$PublicHost)

    $normalizedPublicHost = $PublicHost.Trim()
    if (
        -not [string]::IsNullOrWhiteSpace($normalizedPublicHost) -and
        -not (Test-IsLocalPublicHost -HostName $normalizedPublicHost)
    ) {
        return @($normalizedPublicHost)
    }

    $hosts = New-Object System.Collections.Generic.List[string]
    foreach ($address in @(Get-HostIpv4Addresses)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$address)) {
            $hosts.Add([string]$address)
        }
    }
    if ($hosts.Count -eq 0 -and -not [string]::IsNullOrWhiteSpace($normalizedPublicHost)) {
        $hosts.Add($normalizedPublicHost)
    }
    if ($hosts.Count -eq 0) {
        $hosts.Add("127.0.0.1")
    }
    return @($hosts | Select-Object -Unique)
}

function Get-MediaMtxPaths {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedAppDir,
        [Parameter(Mandatory = $true)][string]$ResolvedVersion
    )

    $root = Join-Path $ResolvedAppDir "mediamtx"
    $configDir = Join-Path $ResolvedAppDir "config"
    $secretsDir = Join-Path $ResolvedAppDir "secrets"
    [pscustomobject]@{
        Root = $root
        VersionDir = Join-Path $root $ResolvedVersion
        CurrentDir = Join-Path $root "current"
        Exe = Join-Path (Join-Path $root "current") "mediamtx.exe"
        ConfigDir = $configDir
        Config = Join-Path $configDir "mediamtx.yml"
        ClientEnv = Join-Path $configDir "mediamtx-client.env"
        SecretsDir = $secretsDir
        ApiUserFile = Join-Path $secretsDir "mediamtx_api_user.txt"
        ApiPasswordFile = Join-Path $secretsDir "mediamtx_api_password.txt"
        TurnAuthSecretFile = Join-Path $secretsDir "turn_auth_secret.txt"
        LogsDir = Join-Path $ResolvedAppDir "logs"
    }
}

function New-SecretHex {
    param([int]$Bytes = 32)

    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::Create()
    try {
        $rng.GetBytes($buffer)
    }
    finally {
        $rng.Dispose()
    }
    return (-join ($buffer | ForEach-Object { $_.ToString("x2") }))
}

function Read-TrimmedFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    return (Get-Content -LiteralPath $Path -Raw).Trim()
}

function Get-OrCreate-MediaMtxApiCredentials {
    param([Parameter(Mandatory = $true)]$Paths)

    New-Item -ItemType Directory -Path $Paths.SecretsDir -Force | Out-Null
    $user = Read-TrimmedFile -Path $Paths.ApiUserFile
    if ([string]::IsNullOrWhiteSpace($user)) {
        $user = "ybb_mediamtx_api"
        Set-Content -LiteralPath $Paths.ApiUserFile -Encoding ascii -Value $user
    }
    $password = Read-TrimmedFile -Path $Paths.ApiPasswordFile
    if ([string]::IsNullOrWhiteSpace($password)) {
        $password = New-SecretHex -Bytes 32
        Set-Content -LiteralPath $Paths.ApiPasswordFile -Encoding ascii -Value $password
    }
    $turnAuthSecret = Read-TrimmedFile -Path $Paths.TurnAuthSecretFile
    if ([string]::IsNullOrWhiteSpace($turnAuthSecret)) {
        $turnAuthSecret = New-SecretHex -Bytes 32
        Set-Content -LiteralPath $Paths.TurnAuthSecretFile -Encoding ascii -Value $turnAuthSecret
    }
    [pscustomobject]@{
        User = $user
        Password = $password
        TurnAuthSecret = $turnAuthSecret
    }
}

function Write-MediaMtxClientEnv {
    param(
        [Parameter(Mandatory = $true)]$Paths,
        [Parameter(Mandatory = $true)]$Credentials,
        [Parameter(Mandatory = $true)][string]$TurnPublicHost,
        [Parameter(Mandatory = $true)][int]$TurnTcpPort
    )

    New-Item -ItemType Directory -Path $Paths.ConfigDir -Force | Out-Null
    Set-Content -LiteralPath $Paths.ClientEnv -Encoding ascii -Value @(
        "MEDIAMTX_API_USER=$($Credentials.User)",
        "MEDIAMTX_API_PASSWORD=$($Credentials.Password)",
        "TURN_AUTH_SECRET=$($Credentials.TurnAuthSecret)",
        "TURN_PUBLIC_HOST=$TurnPublicHost",
        "TURN_TCP_PORT=$TurnTcpPort"
    )
}

function Stop-MediaMtxProcess {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    $escapedConfig = $ConfigPath.Replace("\", "\\")
    $processes = Get-CimInstance Win32_Process -Filter "name = 'mediamtx.exe'" -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -like "*$ConfigPath*" -or [string]$_.CommandLine -like "*$escapedConfig*" }
    foreach ($process in @($processes)) {
        Write-Log "Stoppe MediaMTX Prozess PID $($process.ProcessId)."
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
}

function Remove-PortProxyRule {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [string[]]$ListenAddresses = @("0.0.0.0", "127.0.0.1")
    )

    foreach ($address in @($ListenAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique)) {
        & netsh interface portproxy delete v4tov4 listenaddress=$address listenport=$Port | Out-Null
    }
    & netsh interface portproxy delete v6tov4 listenaddress="::1" listenport=$Port | Out-Null
}

function Remove-MediaMtxPortProxyRules {
    param(
        [int[]]$Ports,
        [Parameter(Mandatory = $true)][string]$PublicHost
    )

    $listenAddresses = @("0.0.0.0", "127.0.0.1")
    if ($PublicHost -match "^\d{1,3}(?:\.\d{1,3}){3}$" -and $PublicHost -ne "127.0.0.1") {
        $listenAddresses += $PublicHost
    }
    foreach ($port in @($Ports | Sort-Object -Unique)) {
        Remove-PortProxyRule -Port $port -ListenAddresses $listenAddresses
    }
    Write-Log "Alte Windows-Portproxy-Regeln fuer MediaMTX-Ports wurden entfernt."
}

function Set-MediaMtxFirewallRules {
    param(
        [Parameter(Mandatory = $true)][int[]]$TcpPorts,
        [int[]]$UdpPorts = @(),
        [Parameter(Mandatory = $true)][string]$ProgramPath
    )

    foreach ($rule in @(Get-NetFirewallRule -DisplayName "YBB-Inferenzserver MediaMTX *" -ErrorAction SilentlyContinue)) {
        Remove-NetFirewallRule -InputObject $rule
    }
    $tcpPortValues = [string[]]@($TcpPorts | Where-Object { $_ -gt 0 } | Sort-Object -Unique | ForEach-Object { [string]$_ })
    if ($tcpPortValues.Count -gt 0) {
        New-NetFirewallRule `
            -DisplayName "YBB-Inferenzserver MediaMTX TCP" `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $tcpPortValues `
            -Profile Any `
            -Enabled True `
            -EdgeTraversalPolicy Allow | Out-Null
        Write-Log "Firewall-Regel gesetzt: MediaMTX TCP $($tcpPortValues -join ',')."
    }
    $udpPortValues = [string[]]@($UdpPorts | Where-Object { $_ -gt 0 } | Sort-Object -Unique | ForEach-Object { [string]$_ })
    if ($udpPortValues.Count -gt 0) {
        New-NetFirewallRule `
            -DisplayName "YBB-Inferenzserver MediaMTX UDP" `
            -Direction Inbound `
            -Action Allow `
            -Protocol UDP `
            -LocalPort $udpPortValues `
            -Profile Any `
            -Enabled True `
            -EdgeTraversalPolicy Allow | Out-Null
        Write-Log "Firewall-Regel gesetzt: MediaMTX UDP $($udpPortValues -join ',')."
    }
    if (Test-Path -LiteralPath $ProgramPath -PathType Leaf) {
        New-NetFirewallRule `
            -DisplayName "YBB-Inferenzserver MediaMTX Program TCP" `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -Program $ProgramPath `
            -Profile Any `
            -Enabled True `
            -EdgeTraversalPolicy Allow | Out-Null
        Write-Log "Firewall-Regeln gesetzt: MediaMTX Programm TCP."
    }
}

function Repair-MediaMtxFirewallRules {
    param(
        [Parameter(Mandatory = $true)][int[]]$TcpPorts,
        [int[]]$UdpPorts = @(),
        [Parameter(Mandatory = $true)][string]$ProgramPath
    )

    try {
        Set-MediaMtxFirewallRules -TcpPorts $TcpPorts -UdpPorts $UdpPorts -ProgramPath $ProgramPath
    }
    catch {
        # A manual non-elevated Start must still be able to start an existing
        # installation. The elevated scheduled task repairs these rules again.
        Write-Log "WARNUNG: MediaMTX Firewall-Regeln konnten nicht repariert werden: $($_.Exception.Message)"
    }
}

function Write-MediaMtxConfig {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$PublicHost,
        [Parameter(Mandatory = $true)][string]$LogFile,
        [Parameter(Mandatory = $true)]$Credentials,
        [Parameter(Mandatory = $true)][int]$TurnTcpPort
    )

    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $LogFile) -Force | Out-Null
    $normalizedLogFile = $LogFile.Replace("\", "/").Replace('"', '\"')
    $additionalHosts = @(Get-WebRtcAdditionalHosts -PublicHost $PublicHost)
    $configLines = @(
        "# Generated by YBB Windows offline updater. Streams are LAN-open by default.",
        "logLevel: info",
        "logDestinations: [stdout, file]",
        "logFile: `"$normalizedLogFile`"",
        "api: yes",
        "apiAddress: :9997",
        "metrics: yes",
        "metricsAddress: 127.0.0.1:9998",
        "rtspTransports: [tcp]",
        "rtspAddress: :$RtspPort",
        "hls: no",
        "webrtcAddress: :$WebRtcPort",
        "webrtcLocalUDPAddress: :$resolvedWebRtcIceTcpPort",
        "webrtcLocalTCPAddress: :$resolvedWebRtcIceTcpPort",
        "webrtcIPsFromInterfaces: false",
        "webrtcAdditionalHosts:"
    )
    foreach ($hostName in $additionalHosts) {
        $configLines += "  - `"$hostName`""
    }
    $turnPublicHost = [string]$additionalHosts[0]
    $configLines += @(
        "webrtcICEServers2:",
        "  - url: `"turn:${turnPublicHost}:${TurnTcpPort}?transport=tcp`"",
        "    username: AUTH_SECRET",
        "    password: `"$($Credentials.TurnAuthSecret)`"",
        "    clientOnly: true",
        "authMethod: internal",
        "authInternalUsers:",
        "  - user: any",
        "    pass:",
        "    ips: []",
        "    permissions:",
        "      - action: publish",
        "        path:",
        "      - action: read",
        "        path:",
        "      - action: playback",
        "        path:",
        "  - user: `"$($Credentials.User)`"",
        "    pass: `"$($Credentials.Password)`"",
        "    ips: []",
        "    permissions:",
        "      - action: api",
        "      - action: metrics",
        "  - user: any",
        "    pass:",
        "    ips: [`"127.0.0.1`", `"::1`"]",
        "    permissions:",
        "      - action: api",
        "      - action: metrics",
        "      - action: pprof",
        "paths:",
        "  all_others:",
        "    source: publisher"
    )
    Set-Content -LiteralPath $Path -Encoding ascii -Value $configLines
    Write-Log "MediaMTX WebRTC ICE Hosts: $($additionalHosts -join ', ')"
}

function Install-MediaMtxBinary {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedBundleRoot,
        [Parameter(Mandatory = $true)]$Paths
    )

    $archive = Join-Path $ResolvedBundleRoot "runtime\mediamtx.zip"
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        if (Test-Path -LiteralPath $Paths.Exe -PathType Leaf) {
            Write-Log "MediaMTX Windows Archiv fehlt im Bundle; vorhandene Installation wird weiterverwendet."
            return
        }
        throw "MediaMTX Windows Archiv fehlt im Bundle: $archive"
    }

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-mediamtx-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $archive -DestinationPath $tempDir -Force
        $binary = Get-ChildItem -LiteralPath $tempDir -Recurse -Filter "mediamtx.exe" -File | Select-Object -First 1
        if ($null -eq $binary) {
            throw "MediaMTX Windows Archiv enthaelt keine mediamtx.exe."
        }
        if (Test-Path -LiteralPath $Paths.VersionDir) {
            Remove-Item -LiteralPath $Paths.VersionDir -Recurse -Force
        }
        New-Item -ItemType Directory -Path $Paths.VersionDir -Force | Out-Null
        Copy-Item -LiteralPath $binary.FullName -Destination (Join-Path $Paths.VersionDir "mediamtx.exe") -Force
        if (Test-Path -LiteralPath $Paths.CurrentDir) {
            Remove-Item -LiteralPath $Paths.CurrentDir -Recurse -Force
        }
        New-Item -ItemType Directory -Path $Paths.CurrentDir -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $Paths.VersionDir "mediamtx.exe") -Destination $Paths.Exe -Force
        Write-Log "MediaMTX Windows Binary installiert: $($Paths.Exe)"
    }
    finally {
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force
        }
    }
}

function Register-MediaMtxTask {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedAppDir,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $scriptPath = Join-Path $ResolvedAppDir "install-windows-mediamtx.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        if (-not [string]::IsNullOrWhiteSpace($PSCommandPath) -and (Test-Path -LiteralPath $PSCommandPath -PathType Leaf)) {
            Copy-Item -LiteralPath $PSCommandPath -Destination $scriptPath -Force
            Write-Log "MediaMTX Start-Skript fuer Autostart installiert: $scriptPath"
        }
        else {
            $scriptPath = $PSCommandPath
        }
    }
    $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $escapedScript = $scriptPath.Replace('"', '""')
    $escapedAppDir = $ResolvedAppDir.Replace('"', '""')
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$escapedScript`" " +
        "-Action Run -AppDir `"$escapedAppDir`" " +
        "-RtspPort $RtspPort -WebRtcPort $WebRtcPort " +
        "-WebRtcIceTcpPort $resolvedWebRtcIceTcpPort " +
        "-TurnTcpPort $resolvedTurnTcpPort"
    )
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $ResolvedAppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # MediaMTX is infrastructure, not an interactive user process. Running the
    # boot supervisor as LocalSystem avoids domain-password changes and the
    # "Log on as a batch job" policy that can invalidate an S4U task after a
    # reboot. All files and listeners used here are local to this host.
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Startet MediaMTX direkt auf dem Windows Host." -Force | Out-Null
    Write-Log "Windows-Autostart-Aufgabe registriert: $Name (LocalSystem/ServiceAccount)"
}

function Start-MediaMtxProcess {
    param([Parameter(Mandatory = $true)]$Paths)

    if (-not (Test-Path -LiteralPath $Paths.Exe -PathType Leaf)) {
        throw "MediaMTX Binary fehlt: $($Paths.Exe)"
    }
    if (-not (Test-Path -LiteralPath $Paths.Config -PathType Leaf)) {
        throw "MediaMTX Konfiguration fehlt: $($Paths.Config)"
    }

    $managedExePath = [System.IO.Path]::GetFullPath([string]$Paths.Exe)
    $escapedConfig = ([string]$Paths.Config).Replace("\", "\\")
    $managedProcesses = @(
        Get-CimInstance Win32_Process -Filter "name = 'mediamtx.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_.ExecutablePath) -and
                [System.IO.Path]::GetFullPath([string]$_.ExecutablePath) -ieq $managedExePath
            }
    )
    $configuredProcesses = @(
        $managedProcesses |
            Where-Object {
                [string]$_.CommandLine -like "*$($Paths.Config)*" -or
                [string]$_.CommandLine -like "*$escapedConfig*"
            }
    )
    if ($configuredProcesses.Count -gt 0) {
        return
    }

    # A double-clicked mediamtx.exe starts with its built-in/default config.
    # It can own all listener ports while rejecting every dynamic YBB channel
    # with "path ... is not configured". Replace only instances of the
    # YBB-managed binary; unrelated MediaMTX installations remain untouched.
    foreach ($process in $managedProcesses) {
        Write-Log "Stoppe MediaMTX PID $($process.ProcessId), da die YBB-Konfiguration nicht geladen wurde."
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
        Wait-Process -Id ([int]$process.ProcessId) -Timeout 10 -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Path $Paths.LogsDir -Force | Out-Null
    Start-Process -WindowStyle Hidden -FilePath $Paths.Exe -ArgumentList @($Paths.Config) -WorkingDirectory $Paths.CurrentDir
    Write-Log "MediaMTX Windows Prozess gestartet."
}

function Run-MediaMtxKeepalive {
    param(
        [Parameter(Mandatory = $true)]$Paths,
        [Parameter(Mandatory = $true)][int]$WebRtcPort,
        [Parameter(Mandatory = $true)][int]$WebRtcIceTcpPort
    )

    $consecutiveHealthFailures = 0
    while ($true) {
        try {
            Start-MediaMtxProcess -Paths $Paths
            if (Test-MediaMtxHealth -WebRtcPort $WebRtcPort -WebRtcIceTcpPort $WebRtcIceTcpPort) {
                if ($consecutiveHealthFailures -gt 0) {
                    Write-Log "MediaMTX ist wieder gesund."
                }
                $consecutiveHealthFailures = 0
            }
            else {
                $consecutiveHealthFailures += 1
                Write-Log "WARNUNG: MediaMTX Healthcheck fehlgeschlagen ($consecutiveHealthFailures/3)."
                if ($consecutiveHealthFailures -ge 3) {
                    Write-Log "MediaMTX Prozess ist vorhanden, aber nicht gesund; starte ihn neu."
                    Stop-MediaMtxProcess -ConfigPath $Paths.Config
                    Start-Sleep -Seconds 1
                    Start-MediaMtxProcess -Paths $Paths
                    $consecutiveHealthFailures = 0
                }
            }
        }
        catch {
            Write-Log "WARNUNG: MediaMTX konnte nicht gestartet werden: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 10
    }
}

function Start-MediaMtxTask {
    param([Parameter(Mandatory = $true)][string]$Name)

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        if ($task.State -ne "Running") {
            Start-ScheduledTask -TaskName $Name
        }
        Write-Log "MediaMTX Scheduled Task ist gestartet: $Name"
        return
    }
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 1000
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($connect)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Test-MediaMtxHealth {
    param(
        [Parameter(Mandatory = $true)][int]$WebRtcPort,
        [Parameter(Mandatory = $true)][int]$WebRtcIceTcpPort
    )

    if (-not (Test-TcpPort -HostName "127.0.0.1" -Port 9997)) {
        return $false
    }
    if (-not (Test-TcpPort -HostName "127.0.0.1" -Port $WebRtcPort)) {
        return $false
    }
    if (-not (Test-TcpPort -HostName "127.0.0.1" -Port $WebRtcIceTcpPort)) {
        return $false
    }
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:9997/v3/paths/list" `
            -TimeoutSec 3 `
            -ErrorAction Stop
        return [int]$response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-MediaMtxReady {
    param(
        [Parameter(Mandatory = $true)][int]$WebRtcPort,
        [Parameter(Mandatory = $true)][int]$WebRtcIceTcpPort,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $apiReady = $false
    $whepReady = $false
    $iceTcpReady = $false
    while ((Get-Date) -lt $deadline) {
        $apiReady = Test-TcpPort -HostName "127.0.0.1" -Port 9997
        $whepReady = Test-TcpPort -HostName "127.0.0.1" -Port $WebRtcPort
        $iceTcpReady = Test-TcpPort -HostName "127.0.0.1" -Port $WebRtcIceTcpPort
        if ($apiReady -and $whepReady -and $iceTcpReady) {
            Write-Log "MediaMTX ist bereit: API 127.0.0.1:9997, WHEP 127.0.0.1:$WebRtcPort, ICE TCP/$WebRtcIceTcpPort."
            return
        }
        Start-Sleep -Milliseconds 500
    }

    $logPath = Join-Path $paths.LogsDir "mediamtx.log"
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        Write-Log "Letzte MediaMTX-Logzeilen:"
        Get-Content -LiteralPath $logPath -Tail 40 | ForEach-Object { Write-Log "[mediamtx] $($_)" }
    }
    $processes = @(Get-CimInstance Win32_Process -Filter "name = 'mediamtx.exe'" -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -like "*$($paths.Config)*" })
    if ($processes.Count -eq 0) {
        throw "MediaMTX wurde gestartet, aber es laeuft kein mediamtx.exe Prozess fuer $($paths.Config)."
    }
    throw "MediaMTX ist nicht erreichbar: API=$apiReady auf 127.0.0.1:9997, WHEP=$whepReady auf 127.0.0.1:$WebRtcPort, ICE-TCP=$iceTcpReady auf TCP/$WebRtcIceTcpPort."
}

$resolvedAppDir = Resolve-AppDir -Path $AppDir
$state = Get-InstallState -ResolvedAppDir $resolvedAppDir
$publicHost = Resolve-PublicHost -RequestedPublicAddress $PublicAddress -State $state
$resolvedVersion = if ([string]::IsNullOrWhiteSpace($Version) -or $Version -eq "unknown") { "current" } else { $Version }
$paths = Get-MediaMtxPaths -ResolvedAppDir $resolvedAppDir -ResolvedVersion $resolvedVersion
$resolvedWebRtcIceTcpPort = if ($WebRtcIceTcpPort -gt 0) { $WebRtcIceTcpPort } else { 8189 }
$resolvedTurnTcpPort = if ($TurnTcpPort -gt 0) { $TurnTcpPort } else { 3478 }

switch ($Action) {
    "Install" {
        $resolvedBundleRoot = if ([string]::IsNullOrWhiteSpace($BundleRoot)) { $PSScriptRoot } else { (Resolve-Path -LiteralPath $BundleRoot -ErrorAction Stop).Path }
        Stop-MediaMtxProcess -ConfigPath $paths.Config
        Install-MediaMtxBinary -ResolvedBundleRoot $resolvedBundleRoot -Paths $paths
        $credentials = Get-OrCreate-MediaMtxApiCredentials -Paths $paths
        Write-MediaMtxClientEnv -Paths $paths -Credentials $credentials -TurnPublicHost $publicHost -TurnTcpPort $resolvedTurnTcpPort
        Write-MediaMtxConfig -Path $paths.Config -PublicHost $publicHost -LogFile (Join-Path $paths.LogsDir "mediamtx.log") -Credentials $credentials -TurnTcpPort $resolvedTurnTcpPort
        Remove-MediaMtxPortProxyRules -Ports @($RtspPort, $WebRtcPort, $resolvedWebRtcIceTcpPort) -PublicHost $publicHost
        Set-MediaMtxFirewallRules -TcpPorts @($RtspPort, $WebRtcPort, $resolvedWebRtcIceTcpPort, $resolvedTurnTcpPort, 9997) -UdpPorts @($resolvedWebRtcIceTcpPort) -ProgramPath $paths.Exe
        Register-MediaMtxTask -ResolvedAppDir $resolvedAppDir -Name $TaskName
        Start-MediaMtxTask -Name $TaskName
        Start-MediaMtxProcess -Paths $paths
        Wait-MediaMtxReady -WebRtcPort $WebRtcPort -WebRtcIceTcpPort $resolvedWebRtcIceTcpPort
    }
    "Start" {
        $credentials = Get-OrCreate-MediaMtxApiCredentials -Paths $paths
        Write-MediaMtxClientEnv -Paths $paths -Credentials $credentials -TurnPublicHost $publicHost -TurnTcpPort $resolvedTurnTcpPort
        Write-MediaMtxConfig -Path $paths.Config -PublicHost $publicHost -LogFile (Join-Path $paths.LogsDir "mediamtx.log") -Credentials $credentials -TurnTcpPort $resolvedTurnTcpPort
        Repair-MediaMtxFirewallRules -TcpPorts @($RtspPort, $WebRtcPort, $resolvedWebRtcIceTcpPort, $resolvedTurnTcpPort, 9997) -UdpPorts @($resolvedWebRtcIceTcpPort) -ProgramPath $paths.Exe
        # Start is also the repair path used when an update reuses an existing
        # MediaMTX binary. Re-register the task so principal/settings changes
        # are applied even without running the full Install action.
        Register-MediaMtxTask -ResolvedAppDir $resolvedAppDir -Name $TaskName
        Start-MediaMtxTask -Name $TaskName
        Start-MediaMtxProcess -Paths $paths
        Wait-MediaMtxReady -WebRtcPort $WebRtcPort -WebRtcIceTcpPort $resolvedWebRtcIceTcpPort
    }
    "Run" {
        Repair-MediaMtxFirewallRules -TcpPorts @($RtspPort, $WebRtcPort, $resolvedWebRtcIceTcpPort, $resolvedTurnTcpPort, 9997) -UdpPorts @($resolvedWebRtcIceTcpPort) -ProgramPath $paths.Exe
        Run-MediaMtxKeepalive -Paths $paths -WebRtcPort $WebRtcPort -WebRtcIceTcpPort $resolvedWebRtcIceTcpPort
    }
    "Stop" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $task -and $task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $TaskName
        }
        Stop-MediaMtxProcess -ConfigPath $paths.Config
    }
    "Status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Log "MediaMTX Scheduled Task ist nicht vorhanden."
        }
        else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Log "Task: $TaskName / State: $($task.State) / LastTaskResult: $($info.LastTaskResult)"
        }
        Get-CimInstance Win32_Process -Filter "name = 'mediamtx.exe'" -ErrorAction SilentlyContinue |
            Where-Object { [string]$_.CommandLine -like "*$($paths.Config)*" } |
            ForEach-Object { Write-Log "MediaMTX Prozess: PID $($_.ProcessId) $($_.CommandLine)" }
        Write-Log "TCP API 127.0.0.1:9997 erreichbar: $(Test-TcpPort -HostName "127.0.0.1" -Port 9997)"
        Write-Log "TCP WHEP 127.0.0.1:$WebRtcPort erreichbar: $(Test-TcpPort -HostName "127.0.0.1" -Port $WebRtcPort)"
        Write-Log "TCP ICE Listener TCP/$resolvedWebRtcIceTcpPort aktiv: $(Test-TcpPort -HostName "127.0.0.1" -Port $resolvedWebRtcIceTcpPort)"
        Write-Log "TURN-Transport TCP/$resolvedTurnTcpPort erreichbar: $(Test-TcpPort -HostName "127.0.0.1" -Port $resolvedTurnTcpPort)"
        Write-Log "MediaMTX HTTP/API/ICE Healthcheck: $(Test-MediaMtxHealth -WebRtcPort $WebRtcPort -WebRtcIceTcpPort $resolvedWebRtcIceTcpPort)"
        $logPath = Join-Path $paths.LogsDir "mediamtx.log"
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            Write-Log "Letzte MediaMTX-Logzeilen:"
            Get-Content -LiteralPath $logPath -Tail 30 | ForEach-Object { Write-Log "[mediamtx] $($_)" }
        }
    }
    "Remove" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Stop-MediaMtxProcess -ConfigPath $paths.Config
    }
}
