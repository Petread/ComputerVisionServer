param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [Parameter(Mandatory = $true)][string]$InterfaceAlias,
    [Parameter(Mandatory = $true)][string]$Address,
    [ValidateRange(8, 32)][int]$PrefixLength = 24,
    [Parameter(Mandatory = $true)][string]$Gateway,
    [string[]]$DnsServers = @(),
    [string]$DnsServersCsv = "",
    [Parameter(Mandatory = $true)][string]$ServerIp,
    [string]$StateRoot = "C:\ProgramData\YBB\Edge",
    [string]$ReleasePublicKey = ""
)
$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run elevated." }
if ((Get-CimInstance Win32_OperatingSystem).Caption -notmatch "Windows 11") { throw "Windows 11 is required." }
$effectiveDnsServers = if ($DnsServersCsv) {
    @($DnsServersCsv.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} else {
    @($DnsServers)
}
if ($effectiveDnsServers.Count -eq 0) { throw "At least one DNS server is required." }
foreach ($candidate in @($Address, $Gateway, $ServerIp) + $effectiveDnsServers) {
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($candidate, [ref]$parsed) -or $parsed.AddressFamily -ne "InterNetwork") { throw "Invalid IPv4: $candidate" }
}
if (-not (Get-NetAdapter -Name $InterfaceAlias -ErrorAction SilentlyContinue)) { throw "Network adapter not found: $InterfaceAlias" }

Rename-Computer -NewName $Hostname -Force
Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
Get-NetRoute -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object DestinationPrefix -eq "0.0.0.0/0" | Remove-NetRoute -Confirm:$false
New-NetIPAddress -InterfaceAlias $InterfaceAlias -IPAddress $Address -PrefixLength $PrefixLength -DefaultGateway $Gateway | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ServerAddresses $effectiveDnsServers
Set-TimeZone -Id "W. Europe Standard Time"
w32tm.exe /config /syncfromflags:DOMHIER /update | Out-Null
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
if ($ReleasePublicKey) {
    if (-not (Test-Path -LiteralPath $ReleasePublicKey -PathType Leaf)) { throw "Release public key not found" }
    $trustRoot = "C:\ProgramData\YBB\Trust"
    New-Item -ItemType Directory -Force -Path $trustRoot | Out-Null
    Copy-Item -LiteralPath $ReleasePublicKey -Destination (Join-Path $trustRoot "release-signing-public.pem") -Force
}
$highPerformance = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
powercfg.exe /setactive $highPerformance | Out-Null
Write-Host "Windows base provisioning complete. Restart, then apply the offline release bundle."
