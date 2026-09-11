[CmdletBinding()]
param(
    [string]$DistroName = "YBB-Inferenzserver",
    [int]$ListenPort = 443,
    [string]$ListenAddress = "",
    [int]$ConnectPort = 443,
    [string]$FirewallRuleName = "",
    [switch]$AllHostAddresses,
    [switch]$WildcardListenAddress,
    [switch]$SkipHttpRedirectPort,
    [switch]$SkipFirewallRule,
    [switch]$NoStartServices,
    [string]$WslNetworkingMode = "",
    [string[]]$AdditionalTcpPorts = @(),
    [string[]]$AdditionalUdpPorts = @(),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function ConvertTo-TcpPortList {
    param([string[]]$Values = @())

    $ports = @(
        foreach ($value in @($Values)) {
            if ($null -eq $value) {
                continue
            }
            foreach ($part in ([string]$value -split "[,;\s]+")) {
                if ([string]::IsNullOrWhiteSpace($part)) {
                    continue
                }
                $port = 0
                if (-not [int]::TryParse($part, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                    throw "Ungueltiger zusaetzlicher TCP-Port: $part"
                }
                $port
            }
        }
    )
    return @($ports | Sort-Object -Unique)
}

function ConvertTo-UdpPortList {
    param([string[]]$Values = @())

    $ports = @(
        foreach ($value in @($Values)) {
            if ($null -eq $value) {
                continue
            }
            foreach ($part in ([string]$value -split "[,;\s]+")) {
                if ([string]::IsNullOrWhiteSpace($part)) {
                    continue
                }
                $port = 0
                if (-not [int]::TryParse($part, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                    throw "Ungueltiger zusaetzlicher UDP-Port: $part"
                }
                $port
            }
        }
    )
    return @($ports | Sort-Object -Unique)
}

$additionalTcpPortArgumentValues = @()
$additionalUdpPortArgumentValues = @($AdditionalUdpPorts)
$additionalTcpPortTokens = @($AdditionalTcpPorts + $RemainingArguments)
for ($index = 0; $index -lt $additionalTcpPortTokens.Count; $index++) {
    $token = [string]$additionalTcpPortTokens[$index]
    switch -Exact ($token) {
        "-AdditionalTcpPorts" {
            $index += 1
            if ($index -ge $additionalTcpPortTokens.Count) {
                throw "-AdditionalTcpPorts braucht einen Wert."
            }
            $additionalTcpPortArgumentValues += [string]$additionalTcpPortTokens[$index]
        }
        "-AdditionalUdpPorts" {
            $index += 1
            if ($index -ge $additionalTcpPortTokens.Count) {
                throw "-AdditionalUdpPorts braucht einen Wert."
            }
            $additionalUdpPortArgumentValues += [string]$additionalTcpPortTokens[$index]
        }
        "-ListenAddress" {
            $index += 1
            if ($index -ge $additionalTcpPortTokens.Count) {
                throw "-ListenAddress braucht einen Wert."
            }
            $ListenAddress = [string]$additionalTcpPortTokens[$index]
        }
        "-AllHostAddresses" {
            $AllHostAddresses = $true
        }
        "-WildcardListenAddress" {
            $WildcardListenAddress = $true
        }
        "-SkipHttpRedirectPort" {
            $SkipHttpRedirectPort = $true
        }
        "-SkipFirewallRule" {
            $SkipFirewallRule = $true
        }
        "-NoStartServices" {
            $NoStartServices = $true
        }
        "-WslNetworkingMode" {
            $index += 1
            if ($index -ge $additionalTcpPortTokens.Count) {
                throw "-WslNetworkingMode braucht einen Wert."
            }
            $WslNetworkingMode = [string]$additionalTcpPortTokens[$index]
        }
        default {
            $additionalTcpPortArgumentValues += $token
        }
    }
}
$AdditionalTcpPorts = $additionalTcpPortArgumentValues
$AdditionalUdpPorts = $additionalUdpPortArgumentValues

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Normalize-WslNetworkingMode {
    param([AllowEmptyString()][string]$Mode)

    $value = if ([string]::IsNullOrWhiteSpace($Mode)) { "unchanged" } else { $Mode.Trim().ToLowerInvariant() }
    switch ($value) {
        "mirrored" { return "mirrored" }
        "nat" { return "nat" }
        "default" { return "nat" }
        default { return "unchanged" }
    }
}

function Invoke-CheckedNativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Log $Description
    $previousErrorActionPreference = $ErrorActionPreference
    $hasNativeErrorPreference = Test-Path Variable:\PSNativeCommandUseErrorActionPreference
    if ($hasNativeErrorPreference) {
        $previousNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
    }
    try {
        $ErrorActionPreference = "Continue"
        if ($hasNativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $false
        }
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hasNativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
        }
    }
    foreach ($item in $output) {
        $text = $item.ToString()
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            Write-Log $text
        }
    }
    if ($exitCode -ne 0) {
        throw "$Description fehlgeschlagen. Exit Code: $exitCode"
    }
}

function Get-WslIpv4Address {
    param([Parameter(Mandatory = $true)][string]$Name)

    $output = & wsl -d $Name -u root -- hostname -I 2>&1
    if ($LASTEXITCODE -ne 0) {
        $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "WSL-IP konnte nicht ermittelt werden: $text"
    }

    $addresses = (($output | ForEach-Object { $_.ToString() }) -join " ").Split(" `t`r`n", [System.StringSplitOptions]::RemoveEmptyEntries)
    foreach ($address in $addresses) {
        if ($address -match "^\d{1,3}(\.\d{1,3}){3}$") {
            return $address
        }
    }
    throw "Keine IPv4-Adresse fuer WSL-Distribution '$Name' gefunden."
}

function Get-HostIpv4Addresses {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -ne "127.0.0.1" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown" -and
            $_.AddressState -eq "Preferred"
        } |
        Select-Object -ExpandProperty IPAddress -Unique
}

function Wait-WslTcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        & wsl -d $Name -u root -- bash -lc "timeout 1 bash -lc '</dev/tcp/127.0.0.1/$Port' >/dev/null 2>&1"
        if ($LASTEXITCODE -eq 0) {
            Write-Log "WSL-Port ist bereit: 127.0.0.1:$Port"
            return
        }
        Start-Sleep -Seconds 1
    }

    throw "Timeout beim Warten auf WSL-Port 127.0.0.1:$Port."
}

function Start-WslKeepalive {
    param([Parameter(Mandatory = $true)][string]$Name)

    $keepaliveCommand = "if [ -x /usr/local/bin/ybb-keepalive ]; then exec /usr/local/bin/ybb-keepalive; elif [ -x /usr/local/bin/ybb-start-platform ]; then /usr/local/bin/ybb-start-platform; exec sleep infinity; else systemctl start caddy ybb-platform.target; exec sleep infinity; fi"
    $wslPath = Join-Path $env:WINDIR "System32\wsl.exe"
    $arguments = @("-d", $Name, "-u", "root", "--", "bash", "-lc", $keepaliveCommand)

    Write-Log "Starte WSL-Keepalive fuer $Name."
    Start-Process -WindowStyle Hidden -FilePath $wslPath -ArgumentList $arguments
}

function Set-PortProxyRule {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("v4tov4", "v6tov4")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$Address,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$TargetAddress,
        [Parameter(Mandatory = $true)][int]$TargetPort
    )

    Write-Log "Aktualisiere Portproxy ${Mode}: ${Address}:$Port -> ${TargetAddress}:$TargetPort"
    & netsh interface portproxy delete $Mode listenaddress=$Address listenport=$Port | Out-Null
    & netsh interface portproxy add $Mode listenaddress=$Address listenport=$Port connectaddress=$TargetAddress connectport=$TargetPort | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "netsh portproxy add $Mode ist fehlgeschlagen. Exit Code: $LASTEXITCODE"
    }
}

function Remove-PortProxyRule {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("v4tov4", "v4tov6", "v6tov4", "v6tov6")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$Address,
        [Parameter(Mandatory = $true)][int]$Port
    )

    & netsh interface portproxy delete $Mode listenaddress=$Address listenport=$Port | Out-Null
}

function Remove-PortProxyEndpointRules {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [string[]]$HostAddresses = @(),
        [AllowEmptyString()][string]$RequestedListenAddress = ""
    )

    $addresses = @(
        @("0.0.0.0", "127.0.0.1"),
        @($HostAddresses),
        @($RequestedListenAddress)
    ) | ForEach-Object { $_ } | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique

    foreach ($address in $addresses) {
        Remove-PortProxyRule -Mode "v4tov4" -Address $address -Port $Port
        Remove-PortProxyRule -Mode "v4tov6" -Address $address -Port $Port
    }
    Remove-PortProxyRule -Mode "v6tov4" -Address "::1" -Port $Port
    Remove-PortProxyRule -Mode "v6tov6" -Address "::1" -Port $Port
}

function Reset-WindowsPortProxyState {
    param([string]$Reason = "WSL Mirrored Networking")

    Write-Log "Setze Windows-Portproxy vollstaendig zurueck wegen ${Reason}; iphlpsvc haelt die Webports weiter belegt."
    & netsh interface portproxy reset | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "netsh interface portproxy reset ist fehlgeschlagen. Exit Code: $LASTEXITCODE"
    }
}

function Restart-IpHelperServiceForMirroredNetworking {
    $service = Get-Service -Name iphlpsvc -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        Write-Log "WARNUNG: Windows-Dienst iphlpsvc wurde nicht gefunden; Portproxy-Listener koennen nicht aktiv freigegeben werden."
        return
    }

    try {
        if ($service.Status -eq "Running") {
            Write-Log "Starte Windows-Dienst iphlpsvc neu, damit geloeschte Portproxy-Listener freigegeben werden."
            Restart-Service -Name iphlpsvc -Force -ErrorAction Stop | Out-Null
        }
        else {
            Write-Log "Starte Windows-Dienst iphlpsvc fuer saubere Portproxy-Freigabe."
            Start-Service -Name iphlpsvc -ErrorAction Stop | Out-Null
        }
    }
    catch {
        Write-Log "WARNUNG: Windows-Dienst iphlpsvc konnte nicht neu gestartet werden: $($_.Exception.Message)"
    }
}

function Test-IpHelperOwnsTcpPorts {
    param([int[]]$Ports = @())

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return $false
    }

    foreach ($port in @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)) {
        foreach ($connection in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
            $owningProcessId = [int]$connection.OwningProcess
            if ($owningProcessId -le 0) {
                continue
            }
            $services = @()
            try {
                $services = @(
                    Get-CimInstance Win32_Service -Filter "ProcessId=$owningProcessId" -ErrorAction SilentlyContinue |
                        Select-Object -ExpandProperty Name
                )
            }
            catch {
                $services = @()
            }
            if ($services -contains "iphlpsvc") {
                return $true
            }
        }
    }
    return $false
}

function Test-IsPrivateIpv4Address {
    param([Parameter(Mandatory = $true)][string]$Address)

    return (
        $Address -match "^10\." -or
        $Address -match "^172\.(1[6-9]|2[0-9]|3[0-1])\." -or
        $Address -match "^192\.168\."
    )
}

function Get-PortProxyRules {
    param([Parameter(Mandatory = $true)][ValidateSet("v4tov4", "v6tov4")][string]$Mode)

    $output = & netsh interface portproxy show $Mode 2>$null
    foreach ($item in $output) {
        $line = $item.ToString().Trim()
        if ($line -match "^(?<ListenAddress>\S+)\s+(?<ListenPort>\d+)\s+(?<ConnectAddress>\S+)\s+(?<ConnectPort>\d+)$") {
            [pscustomobject]@{
                ListenAddress = $Matches.ListenAddress
                ListenPort = [int]$Matches.ListenPort
                ConnectAddress = $Matches.ConnectAddress
                ConnectPort = [int]$Matches.ConnectPort
            }
        }
    }
}

function Remove-ObsoletePortProxyRules {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("v4tov4", "v6tov4")][string]$Mode,
        [Parameter(Mandatory = $true)][string[]]$DesiredAddresses,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$TargetPort
    )

    $desired = @{}
    foreach ($address in $DesiredAddresses) {
        $desired[$address] = $true
    }

    foreach ($rule in @(Get-PortProxyRules -Mode $Mode)) {
        if ($rule.ListenPort -ne $Port -or $rule.ConnectPort -ne $TargetPort) {
            continue
        }
        if ($desired.ContainsKey($rule.ListenAddress)) {
            continue
        }
        if ($Mode -eq "v4tov4" -and -not (Test-IsPrivateIpv4Address -Address $rule.ConnectAddress)) {
            continue
        }
        Write-Log "Entferne veraltete Portproxy-Regel ${Mode}: $($rule.ListenAddress):$($rule.ListenPort) -> $($rule.ConnectAddress):$($rule.ConnectPort)"
        Remove-PortProxyRule -Mode $Mode -Address $rule.ListenAddress -Port $Port
    }
}

function Get-ListenAddressPlan {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$RequestedListenAddress,
        [string[]]$HostAddresses = @(),
        [Parameter(Mandatory = $true)][bool]$UseWildcardListenAddress,
        [Parameter(Mandatory = $true)][bool]$UseAllHostAddresses,
        [Parameter(Mandatory = $true)][int]$Port
    )

    if ($UseWildcardListenAddress) {
        $listenAddresses = @("0.0.0.0")
        if (-not [string]::IsNullOrWhiteSpace($RequestedListenAddress) -and $RequestedListenAddress -notin @("0.0.0.0", "127.0.0.1", "localhost", "::1")) {
            $lanListenAddresses = @($RequestedListenAddress)
        }
        else {
            $lanListenAddresses = @($HostAddresses)
        }
        Write-Log "Portproxy lauscht per Wildcard auf 0.0.0.0:$Port; Firewall/Anzeige wird auf $($lanListenAddresses -join ', ') begrenzt."
    }
    else {
        $listenAddresses = @("127.0.0.1")
        if (-not [string]::IsNullOrWhiteSpace($RequestedListenAddress)) {
            if ($RequestedListenAddress -notin @("127.0.0.1", "localhost", "::1")) {
                $listenAddresses += $RequestedListenAddress
            }
        }
        elseif ($UseAllHostAddresses) {
            $listenAddresses += $HostAddresses
        }
        $lanListenAddresses = @($listenAddresses | Where-Object { $_ -ne "127.0.0.1" })
    }

    [pscustomobject]@{
        ListenAddresses = @($listenAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
        LanListenAddresses = @($lanListenAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
    }
}

function Set-PortProxyEndpoint {
    param(
        [Parameter(Mandatory = $true)][int]$EndpointListenPort,
        [Parameter(Mandatory = $true)][int]$EndpointConnectPort,
        [Parameter(Mandatory = $true)][string]$EndpointFirewallRuleName,
        [Parameter(Mandatory = $true)][string]$TargetAddress,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$RequestedListenAddress,
        [string[]]$HostAddresses = @(),
        [Parameter(Mandatory = $true)][bool]$UseWildcardListenAddress,
        [Parameter(Mandatory = $true)][bool]$UseAllHostAddresses
    )

    $plan = Get-ListenAddressPlan `
        -RequestedListenAddress $RequestedListenAddress `
        -HostAddresses $HostAddresses `
        -UseWildcardListenAddress $UseWildcardListenAddress `
        -UseAllHostAddresses $UseAllHostAddresses `
        -Port $EndpointListenPort

    Remove-PortProxyRule -Mode "v4tov4" -Address "0.0.0.0" -Port $EndpointListenPort
    Remove-ObsoletePortProxyRules -Mode "v4tov4" -DesiredAddresses $plan.ListenAddresses -Port $EndpointListenPort -TargetPort $EndpointConnectPort
    Remove-ObsoletePortProxyRules -Mode "v6tov4" -DesiredAddresses @("::1") -Port $EndpointListenPort -TargetPort $EndpointConnectPort
    foreach ($address in $plan.ListenAddresses) {
        Set-PortProxyRule -Mode "v4tov4" -Address $address -Port $EndpointListenPort -TargetAddress $TargetAddress -TargetPort $EndpointConnectPort
    }
    Set-PortProxyRule -Mode "v6tov4" -Address "::1" -Port $EndpointListenPort -TargetAddress $TargetAddress -TargetPort $EndpointConnectPort

    $lanListenAddresses = @($plan.LanListenAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })

    if (-not $SkipFirewallRule) {
        if ($lanListenAddresses.Count -gt 0) {
            try {
                Get-NetFirewallRule -DisplayName $EndpointFirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
                $firewallParams = @{
                    DisplayName = $EndpointFirewallRuleName
                    Direction = "Inbound"
                    Action = "Allow"
                    Protocol = "TCP"
                    LocalPort = $EndpointListenPort
                    LocalAddress = $lanListenAddresses
                    Profile = "Any"
                }
                New-NetFirewallRule @firewallParams | Out-Null
                Write-Log "Firewall-Regel gesetzt: $EndpointFirewallRuleName TCP/$EndpointListenPort fuer $($lanListenAddresses -join ', ')"
            }
            catch {
                Write-Log "WARNUNG: Firewall-Regel konnte nicht gesetzt werden: $($_.Exception.Message)"
            }
        }
        else {
            Write-Log "Firewall-Regel nicht noetig: Portproxy lauscht nur lokal auf 127.0.0.1/$EndpointListenPort."
        }
    }

    $scheme = switch ($EndpointListenPort) {
        80 { "http" }
        443 { "https" }
        8554 { "rtsp" }
        8889 { "http" }
        default { "tcp" }
    }
    $localUrl = if ($EndpointListenPort -in @(80, 443)) {
        "${scheme}://127.0.0.1/"
    }
    elseif ($scheme -eq "tcp") {
        "${scheme}://127.0.0.1:$EndpointListenPort"
    }
    else {
        "${scheme}://127.0.0.1:$EndpointListenPort/"
    }
    Write-Log "Lokaler Zugriff: $localUrl"
    foreach ($address in $lanListenAddresses) {
        $lanUrl = if ($EndpointListenPort -in @(80, 443)) {
            "${scheme}://${address}/"
        }
        elseif ($scheme -eq "tcp") {
            "${scheme}://${address}:$EndpointListenPort"
        }
        else {
            "${scheme}://${address}:$EndpointListenPort/"
        }
        Write-Log "Zugriff von anderen Geraeten im Netzwerk: $lanUrl"
    }
}

function Set-UdpFirewallEndpoint {
    param(
        [Parameter(Mandatory = $true)][int]$EndpointListenPort,
        [Parameter(Mandatory = $true)][string]$EndpointFirewallRuleName,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$RequestedListenAddress,
        [string[]]$HostAddresses = @(),
        [Parameter(Mandatory = $true)][bool]$UseWildcardListenAddress,
        [Parameter(Mandatory = $true)][bool]$UseAllHostAddresses
    )

    $plan = Get-ListenAddressPlan `
        -RequestedListenAddress $RequestedListenAddress `
        -HostAddresses $HostAddresses `
        -UseWildcardListenAddress $UseWildcardListenAddress `
        -UseAllHostAddresses $UseAllHostAddresses `
        -Port $EndpointListenPort

    $lanListenAddresses = @($plan.LanListenAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })

    if (-not $SkipFirewallRule) {
        if ($lanListenAddresses.Count -gt 0) {
            try {
                Get-NetFirewallRule -DisplayName $EndpointFirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
                $firewallParams = @{
                    DisplayName = $EndpointFirewallRuleName
                    Direction = "Inbound"
                    Action = "Allow"
                    Protocol = "UDP"
                    LocalPort = $EndpointListenPort
                    LocalAddress = $lanListenAddresses
                    Profile = "Any"
                }
                New-NetFirewallRule @firewallParams | Out-Null
                Write-Log "Firewall-Regel gesetzt: $EndpointFirewallRuleName UDP/$EndpointListenPort fuer $($lanListenAddresses -join ', ')"
            }
            catch {
                Write-Log "WARNUNG: UDP-Firewall-Regel konnte nicht gesetzt werden: $($_.Exception.Message)"
            }
        }
        else {
            Write-Log "UDP-Firewall-Regel nicht noetig: Zugriff ist nur lokal geplant fuer UDP/$EndpointListenPort."
        }
    }

    Write-Log "UDP-ICE fuer WebRTC nutzt UDP/$EndpointListenPort direkt, wenn WSL-Mirrored/Direct Networking aktiv ist; netsh portproxy kann UDP nicht weiterleiten. TCP/$EndpointListenPort bleibt als ICE-Fallback ueber Portproxy aktiv."
}

if (-not (Test-IsAdministrator)) {
    throw "Bitte PowerShell als Administrator starten. Portproxy und Firewall-Regel brauchen Administratorrechte."
}

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw "wsl.exe wurde nicht gefunden."
}

$hostIps = @(Get-HostIpv4Addresses)
$requestedListenAddress = $ListenAddress.Trim()
$useWildcardListenAddress = $WildcardListenAddress -or $requestedListenAddress -eq "0.0.0.0"
$normalizedWslNetworkingMode = Normalize-WslNetworkingMode -Mode $WslNetworkingMode
$additionalTcpPortsToProxy = @(
    ConvertTo-TcpPortList -Values $AdditionalTcpPorts |
        Where-Object { $_ -notin @($ListenPort, $ConnectPort, 80) } |
        Sort-Object -Unique
)

if ($normalizedWslNetworkingMode -eq "mirrored") {
    Write-Log "WSL Mirrored Networking ist aktiv; netsh portproxy wird fuer WSL-Webports uebersprungen."
    Write-Log "Direkter Zugriff nutzt die WSL-Mirrored-Netzwerkschnittstellen; bestehende Portproxy-Regeln fuer betroffene Ports werden entfernt."
    $portsToRelease = @($ListenPort)
    Remove-PortProxyEndpointRules -Port $ListenPort -HostAddresses $hostIps -RequestedListenAddress $requestedListenAddress
    if (-not $SkipHttpRedirectPort -and $ListenPort -eq 443 -and $ConnectPort -eq 443) {
        $portsToRelease += 80
        Remove-PortProxyEndpointRules -Port 80 -HostAddresses $hostIps -RequestedListenAddress $requestedListenAddress
    }
    foreach ($additionalPort in $additionalTcpPortsToProxy) {
        $portsToRelease += $additionalPort
        Remove-PortProxyEndpointRules -Port $additionalPort -HostAddresses $hostIps -RequestedListenAddress $requestedListenAddress
    }
    Restart-IpHelperServiceForMirroredNetworking
    Start-Sleep -Seconds 2
    if (Test-IpHelperOwnsTcpPorts -Ports $portsToRelease) {
        Reset-WindowsPortProxyState -Reason "WSL Mirrored Networking"
        Restart-IpHelperServiceForMirroredNetworking
    }
    Write-Log "Lokaler Zugriff im Mirrored Mode: https://127.0.0.1/"
    foreach ($address in @($hostIps)) {
        Write-Log "Zugriff von anderen Geraeten im Netzwerk im Mirrored Mode: https://${address}/"
    }
    Write-Log "Falls LAN-Zugriff blockiert ist, die Hyper-V-Firewall fuer WSL pruefen."
    return
}

if (-not $NoStartServices) {
    Start-WslKeepalive -Name $DistroName
}

Wait-WslTcpPort -Name $DistroName -Port $ConnectPort
Wait-WslTcpPort -Name $DistroName -Port 8080
if (-not $SkipHttpRedirectPort -and $ListenPort -eq 443 -and $ConnectPort -eq 443) {
    Wait-WslTcpPort -Name $DistroName -Port 80
}

$wslIp = Get-WslIpv4Address -Name $DistroName
Write-Log "Aktuelle WSL-IP: $wslIp"

$ipHelper = Get-Service -Name iphlpsvc -ErrorAction SilentlyContinue
if ($null -ne $ipHelper -and $ipHelper.Status -ne "Running") {
    Write-Log "Starte Windows-Dienst iphlpsvc fuer netsh portproxy."
    Start-Service -Name iphlpsvc
}

$mainFirewallRuleName = if ([string]::IsNullOrWhiteSpace($FirewallRuleName)) { "YBB-Inferenzserver TCP $ListenPort" } else { $FirewallRuleName }
Set-PortProxyEndpoint `
    -EndpointListenPort $ListenPort `
    -EndpointConnectPort $ConnectPort `
    -EndpointFirewallRuleName $mainFirewallRuleName `
    -TargetAddress $wslIp `
    -RequestedListenAddress $requestedListenAddress `
    -HostAddresses $hostIps `
    -UseWildcardListenAddress $useWildcardListenAddress `
    -UseAllHostAddresses ([bool]$AllHostAddresses)

if (-not $SkipHttpRedirectPort -and $ListenPort -eq 443 -and $ConnectPort -eq 443) {
    Set-PortProxyEndpoint `
        -EndpointListenPort 80 `
        -EndpointConnectPort 80 `
        -EndpointFirewallRuleName "YBB-Inferenzserver TCP 80" `
        -TargetAddress $wslIp `
        -RequestedListenAddress $requestedListenAddress `
        -HostAddresses $hostIps `
        -UseWildcardListenAddress $useWildcardListenAddress `
        -UseAllHostAddresses ([bool]$AllHostAddresses)
}

foreach ($additionalPort in $additionalTcpPortsToProxy) {
    try {
        Wait-WslTcpPort -Name $DistroName -Port $additionalPort -TimeoutSeconds 30
        Set-PortProxyEndpoint `
            -EndpointListenPort $additionalPort `
            -EndpointConnectPort $additionalPort `
            -EndpointFirewallRuleName "YBB-Inferenzserver TCP $additionalPort" `
            -TargetAddress $wslIp `
            -RequestedListenAddress $requestedListenAddress `
            -HostAddresses $hostIps `
            -UseWildcardListenAddress $useWildcardListenAddress `
            -UseAllHostAddresses ([bool]$AllHostAddresses)
    }
    catch {
        Write-Log "WARNUNG: Portproxy fuer zusaetzlichen TCP-Port $additionalPort wurde nicht gesetzt: $($_.Exception.Message)"
    }
}

$additionalUdpPortsToOpen = @(
    ConvertTo-UdpPortList -Values $AdditionalUdpPorts |
        Where-Object { $_ -notin @($ListenPort, $ConnectPort, 80) } |
        Sort-Object -Unique
)
foreach ($additionalPort in $additionalUdpPortsToOpen) {
    try {
        Set-UdpFirewallEndpoint `
            -EndpointListenPort $additionalPort `
            -EndpointFirewallRuleName "YBB-Inferenzserver UDP $additionalPort" `
            -RequestedListenAddress $requestedListenAddress `
            -HostAddresses $hostIps `
            -UseWildcardListenAddress $useWildcardListenAddress `
            -UseAllHostAddresses ([bool]$AllHostAddresses)
    }
    catch {
        Write-Log "WARNUNG: Firewall-Freigabe fuer zusaetzlichen UDP-Port $additionalPort wurde nicht gesetzt: $($_.Exception.Message)"
    }
}

Write-Log "Portproxy-Status:"
& netsh interface portproxy show v4tov4 | Out-Host
& netsh interface portproxy show v6tov4 | Out-Host
