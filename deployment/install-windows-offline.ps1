[CmdletBinding()]
param(
    [string]$BundlePath = "",
    [string]$InstallRoot = "",
    [string]$DistroName = "",
    [Alias("AppHostName", "HostName")]
    [string]$PublicAddress = "",
    [switch]$SkipHostsEntry,
    [switch]$SkipPortProxy,
    [int]$ListenPort = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:LogFile = $null
$script:TempDir = $null
$script:LockFile = $null
$script:LockAcquired = $false

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if (-not [string]::IsNullOrWhiteSpace($script:LogFile)) {
        Add-Content -LiteralPath $script:LogFile -Value $line -Encoding utf8
    }
}

function Expand-ZipArchiveSafely {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archiveFullPath = [System.IO.Path]::GetFullPath($ArchivePath)
    $destinationFullPath = [System.IO.Path]::GetFullPath($DestinationPath)
    New-Item -ItemType Directory -Path $destinationFullPath -Force | Out-Null
    $destinationPrefix = $destinationFullPath.TrimEnd("\", "/") + "\"
    $archive = [System.IO.Compression.ZipFile]::OpenRead($archiveFullPath)
    try {
        foreach ($entry in $archive.Entries) {
            $entryPath = ([string]$entry.FullName).Replace("/", "\")
            if (
                [string]::IsNullOrWhiteSpace($entryPath) -or
                [System.IO.Path]::IsPathRooted($entryPath) -or
                $entryPath.StartsWith("\")
            ) {
                if ([string]::IsNullOrWhiteSpace($entryPath)) {
                    continue
                }
                throw "ZIP-Eintrag verwendet einen unzulässigen absoluten Pfad: $entryPath"
            }
            $targetPath = [System.IO.Path]::GetFullPath((Join-Path $destinationFullPath $entryPath))
            if (-not $targetPath.StartsWith($destinationPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "ZIP-Eintrag verlässt das Zielverzeichnis: $entryPath"
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
                continue
            }
            $targetParent = Split-Path -Parent $targetPath
            New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            $inputStream = $entry.Open()
            $outputStream = [System.IO.File]::Open(
                $targetPath,
                [System.IO.FileMode]::Create,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            try {
                $inputStream.CopyTo($outputStream)
            }
            finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
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
    if ($exitCode -notin $AllowedExitCodes) {
        throw "$Description fehlgeschlagen. Exit Code: $exitCode"
    }
    return $exitCode
}

function Resolve-BundleRoot {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return $PSScriptRoot
    }

    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) { $PathValue } else { Join-Path (Get-Location).Path $PathValue }
    $fullPath = [System.IO.Path]::GetFullPath($candidate)
    if (Test-Path -LiteralPath $fullPath -PathType Container) {
        return $fullPath
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Bundle wurde nicht gefunden: $fullPath"
    }

    $script:TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-installer-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:TempDir -Force | Out-Null
    Write-Log "Entpacke Bundle nach: $script:TempDir"
    Expand-ZipArchiveSafely -ArchivePath $fullPath -DestinationPath $script:TempDir
    return $script:TempDir
}

function Get-RequiredJsonValue {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not ($Object.PSObject.Properties.Name -contains $Name)) {
        throw "windows-bundle-manifest.json enthaelt keinen Wert fuer '$Name'."
    }
    $value = [string]$Object.$Name
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "windows-bundle-manifest.json enthaelt einen leeren Wert fuer '$Name'."
    }
    return $value
}

function Get-OptionalJsonValue {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        $DefaultValue = $null
    )

    if (-not ($Object.PSObject.Properties.Name -contains $Name)) {
        return $DefaultValue
    }
    return $Object.$Name
}

function Test-ChecksumManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RootDir,
        [Parameter(Mandatory = $true)][string[]]$ExpectedFileNames,
        [string[]]$SkipHashFileNames = @()
    )

    $manifestPath = Join-Path $RootDir "manifest.sha256"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "manifest.sha256 fehlt im Bundle."
    }

    $seenFiles = @{}
    $skipHashFiles = @{}
    foreach ($skipHashFileName in $SkipHashFileNames) {
        $skipHashFiles[$skipHashFileName.Replace("/", "\").ToLowerInvariant()] = $true
    }
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -notmatch "^(?<Hash>[a-fA-F0-9]{64})\s+(?<Path>.+)$") {
            throw "Ungueltige Zeile in manifest.sha256: $line"
        }
        $expectedHash = $Matches.Hash.ToLowerInvariant()
        $relativePath = $Matches.Path.Trim()
        if ([System.IO.Path]::IsPathRooted($relativePath) -or $relativePath -match '(^|[\\/])\.\.([\\/]|$)') {
            throw "Ungueltiger relativer Pfad in manifest.sha256: $relativePath"
        }
        $relativeKey = $relativePath.Replace("/", "\").ToLowerInvariant()
        if ($skipHashFiles.ContainsKey($relativeKey)) {
            Write-Log "Ueberspringe SHA256-Pruefung fuer PowerShell-Wrapper: $relativePath"
            $seenFiles[$relativeKey] = $true
            continue
        }
        $filePath = Join-Path $RootDir $relativePath
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "Datei aus manifest.sha256 fehlt: $relativePath"
        }
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "SHA256-Pruefsumme stimmt nicht fuer $relativePath."
        }
        $seenFiles[$relativeKey] = $true
    }

    foreach ($expectedFileName in $ExpectedFileNames) {
        $key = $expectedFileName.Replace("/", "\").ToLowerInvariant()
        if (-not $seenFiles.ContainsKey($key)) {
            throw "manifest.sha256 enthaelt keinen Eintrag fuer: $expectedFileName"
        }
    }
}

function Join-ArchiveParts {
    param(
        [Parameter(Mandatory = $true)][string]$RootDir,
        [Parameter(Mandatory = $true)][string[]]$PartNames,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }

    $buffer = New-Object byte[] (4MB)
    $outputStream = [System.IO.File]::Open($OutputPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        foreach ($partName in $PartNames) {
            if ([System.IO.Path]::IsPathRooted($partName) -or $partName -match '(^|[\\/])\.\.([\\/]|$)') {
                throw "Ungueltiger relativer Teilpfad in Manifest: $partName"
            }
            $partPath = Join-Path $RootDir $partName
            if (-not (Test-Path -LiteralPath $partPath -PathType Leaf)) {
                throw "Archiv-Teil fehlt im Bundle: $partName"
            }
            Write-Log "Fuege Archiv-Teil zusammen: $partName"
            $partStream = [System.IO.File]::Open($partPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
            try {
                while ($true) {
                    $bytesRead = $partStream.Read($buffer, 0, $buffer.Length)
                    if ($bytesRead -le 0) {
                        break
                    }
                    $outputStream.Write($buffer, 0, $bytesRead)
                }
            }
            finally {
                $partStream.Dispose()
            }
        }
    }
    finally {
        $outputStream.Dispose()
    }

    $actualSha256 = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "SHA256-Pruefsumme des zusammengesetzten Archivs stimmt nicht."
    }
}

function ConvertTo-WslPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    $fullPath = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($fullPath -notmatch "^([A-Za-z]):\\(.*)$") {
        throw "Nur lokale Laufwerkspfade koennen nach WSL uebergeben werden: $fullPath"
    }
    $drive = $Matches[1].ToLowerInvariant()
    $rest = $Matches[2].Replace("\", "/")
    return "/mnt/$drive/$rest"
}

function Quote-Bash {
    param([Parameter(Mandatory = $true)][string]$Value)
    $single = [string][char]39
    $double = [string][char]34
    $replacement = $single + $double + $single + $double + $single
    return $single + $Value.Replace($single, $replacement) + $single
}

function Get-WslDistros {
    $output = & wsl --list --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    return @($output | ForEach-Object { $_.ToString().Trim(" `t`0") } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

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
        "unchanged" { return "unchanged" }
        default { throw "Ungueltiger WslNetworkingMode: $Mode. Erlaubt sind mirrored, nat, unchanged." }
    }
}

function Normalize-WslHyperVFirewallDefaultInboundAction {
    param([AllowEmptyString()][string]$Action)

    $value = if ([string]::IsNullOrWhiteSpace($Action)) { "Unchanged" } else { $Action.Trim() }
    switch -Regex ($value) {
        "^(?i)allow$" { return "Allow" }
        "^(?i)block$" { return "Block" }
        "^(?i)unchanged$" { return "Unchanged" }
        default { throw "Ungueltiger WslHyperVFirewallDefaultInboundAction: $Action. Erlaubt sind Allow, Block, Unchanged." }
    }
}

function ConvertTo-WslConfigBool {
    param([bool]$Value)
    if ($Value) { return "true" }
    return "false"
}

function Get-WslConfigPath {
    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        throw "USERPROFILE ist nicht gesetzt; .wslconfig kann nicht ermittelt werden."
    }
    return (Join-Path $env:USERPROFILE ".wslconfig")
}

function Get-WslConfigValue {
    param(
        [string[]]$Lines = @(),
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$Key
    )

    $currentSection = ""
    $escapedKey = [Regex]::Escape($Key)
    foreach ($line in @($Lines)) {
        if ($line -match "^\s*\[(?<Section>[^\]]+)\]\s*$") {
            $currentSection = $Matches.Section.Trim()
            continue
        }
        if ($currentSection -ieq $Section -and $line -match "^\s*$escapedKey\s*=\s*(?<Value>[^#;]*)") {
            return $Matches.Value.Trim()
        }
    }
    return ""
}

function Set-IniSectionValues {
    param(
        [string[]]$Lines = @(),
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)]$Values
    )

    $result = New-Object System.Collections.Generic.List[string]
    $remaining = [ordered]@{}
    foreach ($key in $Values.Keys) {
        $remaining[[string]$key] = [string]$Values[$key]
    }

    $currentSection = ""
    $sectionFound = $false
    $changed = $false

    foreach ($line in @($Lines)) {
        if ($line -match "^\s*\[(?<Section>[^\]]+)\]\s*$") {
            if ($currentSection -ieq $Section -and $remaining.Count -gt 0) {
                foreach ($key in @($remaining.Keys)) {
                    $result.Add("$key=$($remaining[$key])")
                }
                $remaining.Clear()
                $changed = $true
            }
            $currentSection = $Matches.Section.Trim()
            if ($currentSection -ieq $Section) {
                $sectionFound = $true
            }
            $result.Add($line)
            continue
        }

        if ($currentSection -ieq $Section -and $line -match "^\s*(?<Key>[^=;#\s]+)\s*=\s*(?<Value>.*)$") {
            $lineKey = $Matches.Key.Trim()
            $matchedKey = @($remaining.Keys | Where-Object { $_ -ieq $lineKey } | Select-Object -First 1)
            if ($matchedKey.Count -gt 0) {
                $key = [string]$matchedKey[0]
                $newLine = "$key=$($remaining[$key])"
                if ($line -ne $newLine) {
                    $changed = $true
                }
                $result.Add($newLine)
                $remaining.Remove($key)
                continue
            }
        }

        $result.Add($line)
    }

    if ($sectionFound) {
        if ($currentSection -ieq $Section -and $remaining.Count -gt 0) {
            foreach ($key in @($remaining.Keys)) {
                $result.Add("$key=$($remaining[$key])")
            }
            $changed = $true
        }
    }
    else {
        if ($result.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($result[$result.Count - 1])) {
            $result.Add("")
        }
        $result.Add("[$Section]")
        foreach ($key in @($remaining.Keys)) {
            $result.Add("$key=$($remaining[$key])")
        }
        $changed = $true
    }

    [pscustomobject]@{
        Lines = [string[]]$result.ToArray()
        Changed = $changed
    }
}

function Update-WslConfigLines {
    param(
        [string[]]$Lines = @(),
        [Parameter(Mandatory = $true)][string]$NetworkingMode,
        [Parameter(Mandatory = $true)][bool]$DnsTunneling,
        [Parameter(Mandatory = $true)][bool]$Firewall,
        [Parameter(Mandatory = $true)][bool]$AutoProxy,
        [Parameter(Mandatory = $true)][bool]$HostAddressLoopback
    )

    $wsl2Values = [ordered]@{
        networkingMode = $NetworkingMode
        dnsTunneling = ConvertTo-WslConfigBool -Value $DnsTunneling
        firewall = ConvertTo-WslConfigBool -Value $Firewall
        autoProxy = ConvertTo-WslConfigBool -Value $AutoProxy
    }
    $result = Set-IniSectionValues -Lines $Lines -Section "wsl2" -Values $wsl2Values
    $changed = [bool]$result.Changed

    $experimentalValues = [ordered]@{
        hostAddressLoopback = ConvertTo-WslConfigBool -Value $HostAddressLoopback
    }
    $result = Set-IniSectionValues -Lines $result.Lines -Section "experimental" -Values $experimentalValues
    $changed = $changed -or [bool]$result.Changed

    [pscustomobject]@{
        Lines = [string[]]$result.Lines
        Changed = $changed
    }
}

function Backup-WslConfig {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AppDir
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    $backupDir = Join-Path $AppDir "backups"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backupPath = Join-Path $backupDir (".wslconfig-{0}.bak" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    Copy-Item -LiteralPath $Path -Destination $backupPath -Force
    Write-Log "Backup der bestehenden .wslconfig angelegt: $backupPath"
    return $backupPath
}

function Get-CurrentWslNetworkingMode {
    $path = Get-WslConfigPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return "unchanged"
    }
    $lines = @(Get-Content -LiteralPath $path -ErrorAction SilentlyContinue)
    $mode = Get-WslConfigValue -Lines $lines -Section "wsl2" -Key "networkingMode"
    if ([string]::IsNullOrWhiteSpace($mode)) {
        return "unchanged"
    }
    return (Normalize-WslNetworkingMode -Mode $mode)
}

function Set-YbbWslHyperVFirewallDefaultInboundAction {
    param(
        [Parameter(Mandatory = $true)][string]$NetworkingMode,
        [Parameter(Mandatory = $true)][string]$Action
    )

    $normalizedAction = Normalize-WslHyperVFirewallDefaultInboundAction -Action $Action
    if ($NetworkingMode -ne "mirrored" -or $normalizedAction -eq "Unchanged") {
        return
    }
    if (-not (Test-IsAdministrator)) {
        Write-Log "WARNUNG: Hyper-V-Firewall fuer WSL wurde nicht angepasst, weil PowerShell nicht als Administrator laeuft."
        Write-Log "Bei Basler/GigE-Vision im Mirrored Mode ggf. als Administrator ausfuehren: Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction $normalizedAction"
        return
    }
    if (-not (Get-Command Set-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue)) {
        Write-Log "WARNUNG: Set-NetFirewallHyperVVMSetting ist nicht verfuegbar; Hyper-V-Firewall fuer WSL wurde nicht angepasst."
        return
    }
    try {
        Set-NetFirewallHyperVVMSetting -Name "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -DefaultInboundAction $normalizedAction -ErrorAction Stop | Out-Null
        Write-Log "Hyper-V-Firewall fuer WSL gesetzt: DefaultInboundAction=$normalizedAction"
    }
    catch {
        Write-Log "WARNUNG: Hyper-V-Firewall fuer WSL konnte nicht angepasst werden: $($_.Exception.Message)"
    }
}

function Set-WslGlobalNetworkingConfig {
    param(
        [Parameter(Mandatory = $true)][string]$AppDir,
        [Parameter(Mandatory = $true)][string]$NetworkingMode,
        [Parameter(Mandatory = $true)][bool]$DnsTunneling,
        [Parameter(Mandatory = $true)][bool]$Firewall,
        [Parameter(Mandatory = $true)][bool]$AutoProxy,
        [Parameter(Mandatory = $true)][bool]$HostAddressLoopback,
        [Parameter(Mandatory = $true)][string]$HyperVFirewallDefaultInboundAction,
        [Parameter(Mandatory = $true)][bool]$RestartAllowed
    )

    $requestedMode = Normalize-WslNetworkingMode -Mode $NetworkingMode
    $currentMode = Get-CurrentWslNetworkingMode
    if ($requestedMode -eq "unchanged") {
        Write-Log "WSL-Netzwerkmodus bleibt unveraendert. Effektiv aus .wslconfig: $currentMode"
        [pscustomobject]@{ EffectiveMode = $currentMode; Changed = $false }
        return
    }

    $path = Get-WslConfigPath
    $lines = if (Test-Path -LiteralPath $path -PathType Leaf) { @(Get-Content -LiteralPath $path) } else { @() }
    $updated = Update-WslConfigLines -Lines $lines -NetworkingMode $requestedMode -DnsTunneling $DnsTunneling -Firewall $Firewall -AutoProxy $AutoProxy -HostAddressLoopback $HostAddressLoopback

    if ($updated.Changed) {
        if (-not $RestartAllowed) {
            throw "WSL-Netzwerkmodus muesste geaendert werden, aber WslRestartAllowed=false."
        }
        Backup-WslConfig -Path $path -AppDir $AppDir | Out-Null
        Set-Content -LiteralPath $path -Value $updated.Lines -Encoding ascii
        Write-Log ".wslconfig aktualisiert: networkingMode=$requestedMode, dnsTunneling=$DnsTunneling, firewall=$Firewall, autoProxy=$AutoProxy, hostAddressLoopback=$HostAddressLoopback"
        Invoke-LoggedCommand -Description "Beende WSL fuer .wslconfig-Neustart." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null
        Start-Sleep -Seconds 8
    }
    else {
        Write-Log ".wslconfig ist bereits passend fuer networkingMode=$requestedMode."
    }

    Set-YbbWslHyperVFirewallDefaultInboundAction -NetworkingMode $requestedMode -Action $HyperVFirewallDefaultInboundAction | Out-Null
    [pscustomobject]@{ EffectiveMode = $requestedMode; Changed = [bool]$updated.Changed }
}

function ConvertTo-PublicAuthority {
    param([Parameter(Mandatory = $true)][string]$Value)

    $text = $Value.Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "PublicAddress/AppHostName darf nicht leer sein."
    }

    if ($text -match "^[A-Za-z][A-Za-z0-9+.-]*://") {
        try {
            $uri = [System.Uri]$text
        }
        catch {
            throw "Ungueltige PublicAddress/AppHostName: $Value"
        }
        if ([string]::IsNullOrWhiteSpace($uri.Host)) {
            throw "Ungueltige PublicAddress/AppHostName: $Value"
        }
        if ($uri.Port -gt 0 -and -not $uri.IsDefaultPort) {
            if ($uri.Host -match ":" -and -not $uri.Host.StartsWith("[")) {
                return ("[{0}]:{1}" -f $uri.Host, $uri.Port)
            }
            return ("{0}:{1}" -f $uri.Host, $uri.Port)
        }
        return $uri.Host
    }

    $authority = ($text -split "/", 2)[0].Trim()
    if ([string]::IsNullOrWhiteSpace($authority)) {
        throw "Ungueltige PublicAddress/AppHostName: $Value"
    }
    return $authority
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

function Get-PublicPort {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [int]$DefaultPort = 80
    )

    $authority = ConvertTo-PublicAuthority -Value $Value
    if ($authority -match "^\[[^\]]+\]:(?<Port>\d+)$") {
        return [int]$Matches.Port
    }
    if ($authority -match "^[^:]+:(?<Port>\d+)$") {
        return [int]$Matches.Port
    }
    return $DefaultPort
}

function Test-IsIpOrLocalHost {
    param([Parameter(Mandatory = $true)][string]$HostName)

    if ($HostName -ieq "localhost") {
        return $true
    }
    if ($HostName -match "^\d{1,3}(?:\.\d{1,3}){3}$") {
        return $true
    }
    if ($HostName -match ":") {
        return $true
    }
    return $false
}

function Get-PortProxyListenAddress {
    param([Parameter(Mandatory = $true)][string]$PublicAddress)

    $hostPart = Get-PublicHostPart -Value $PublicAddress
    if ($hostPart -in @("127.0.0.1", "localhost", "::1")) {
        return ""
    }
    if ($hostPart -match "^\d{1,3}(?:\.\d{1,3}){3}$" -and $hostPart -ne "127.0.0.1") {
        return $hostPart
    }
    return "0.0.0.0"
}

function Get-WindowsPortProxyRules {
    param([Parameter(Mandatory = $true)][ValidateSet("v4tov4", "v4tov6", "v6tov4", "v6tov6")][string]$Mode)

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

function Remove-WindowsPortProxyRulesForPorts {
    param(
        [int[]]$Ports = @(),
        [string]$Reason = "WSL Mirrored Networking"
    )

    $portSet = @{}
    foreach ($port in @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)) {
        $portSet[[int]$port] = $true
    }
    if ($portSet.Count -eq 0) {
        return
    }

    foreach ($mode in @("v4tov4", "v4tov6", "v6tov4", "v6tov6")) {
        foreach ($rule in @(Get-WindowsPortProxyRules -Mode $mode)) {
            if (-not $portSet.ContainsKey([int]$rule.ListenPort)) {
                continue
            }
            Write-Log "Entferne Windows-Portproxy-Regel wegen ${Reason}: ${mode} $($rule.ListenAddress):$($rule.ListenPort) -> $($rule.ConnectAddress):$($rule.ConnectPort)"
            & netsh interface portproxy delete $mode listenaddress=$rule.ListenAddress listenport=$rule.ListenPort | Out-Null
        }
    }
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
    if (-not (Test-IsAdministrator)) {
        Write-Log "WARNUNG: Windows-Dienst iphlpsvc wurde nicht neu gestartet, weil PowerShell nicht als Administrator laeuft."
        return
    }

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

function Test-WindowsTcpPortsFreeForWslMirrored {
    param([int[]]$Ports = @())

    $portList = @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($portList.Count -eq 0) {
        return $true
    }
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return $true
    }
    return (@(Get-WindowsTcpListenersForPorts -Ports $portList).Count -eq 0)
}

function Get-WindowsTcpListenersForPorts {
    param([int[]]$Ports = @())

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return
    }

    foreach ($port in @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)) {
        $connections = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        foreach ($connection in $connections) {
            $owningProcessId = [int]$connection.OwningProcess
            $processName = "unknown"
            if ($owningProcessId -gt 0) {
                try {
                    $processName = (Get-Process -Id $owningProcessId -ErrorAction Stop).ProcessName
                }
                catch {
                    $processName = "unknown"
                }
            }

            $serviceNames = @()
            try {
                $serviceNames = @(
                    Get-CimInstance Win32_Service -Filter "ProcessId=$owningProcessId" -ErrorAction SilentlyContinue |
                        Select-Object -ExpandProperty Name
                )
            }
            catch {
                $serviceNames = @()
            }

            [pscustomobject]@{
                LocalAddress = [string]$connection.LocalAddress
                LocalPort = [int]$connection.LocalPort
                OwningProcess = $owningProcessId
                ProcessName = $processName
                Services = ($serviceNames -join ",")
            }
        }
    }
}

function Assert-WindowsTcpPortsFreeForWslMirrored {
    param([int[]]$Ports = @())

    $portList = @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($portList.Count -eq 0) {
        return
    }

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        Write-Log "WARNUNG: Get-NetTCPConnection ist nicht verfuegbar; Windows-Portbelegung vor WSL-Start kann nicht geprueft werden."
        return
    }

    $listeners = @(Get-WindowsTcpListenersForPorts -Ports $portList)
    if ($listeners.Count -eq 0) {
        Write-Log "Windows-Webports fuer WSL Mirrored Networking sind frei: $($portList -join ', ')"
        return
    }

    foreach ($listener in $listeners) {
        $servicesText = if ([string]::IsNullOrWhiteSpace([string]$listener.Services)) { "-" } else { [string]$listener.Services }
        Write-Log "Windows-Port vor WSL-Start belegt: $($listener.LocalAddress):$($listener.LocalPort), PID=$($listener.OwningProcess), Prozess=$($listener.ProcessName), Dienste=$servicesText"
    }

    throw "WSL Mirrored Networking kann Caddy nicht starten, weil Windows-Port(s) $($portList -join ', ') bereits belegt sind. Bitte den im Log genannten Windows-Prozess/Dienst stoppen oder die Portkonfiguration anpassen."
}

function Reset-WslMirroredWebPortBindings {
    param(
        [int[]]$Ports = @(),
        [string]$Reason = "WSL Mirrored Networking",
        [bool]$RestartAllowed = $true
    )

    $portList = @($Ports | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($portList.Count -eq 0) {
        return
    }

    Remove-WindowsPortProxyRulesForPorts -Ports $portList -Reason $Reason

    if (-not $RestartAllowed) {
        throw "WSL Mirrored Networking benoetigt einen WSL-Neustart zur sicheren Webport-Freigabe, aber WslRestartAllowed=false."
    }

    Invoke-LoggedCommand -Description "Beende WSL fuer Mirrored-Webport-Freigabe." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null
    Start-Sleep -Seconds 5
    Restart-IpHelperServiceForMirroredNetworking
    Start-Sleep -Seconds 2
    if (-not (Test-WindowsTcpPortsFreeForWslMirrored -Ports $portList)) {
        Reset-WindowsPortProxyState -Reason $Reason
        Restart-IpHelperServiceForMirroredNetworking
        Start-Sleep -Seconds 2
    }
    Assert-WindowsTcpPortsFreeForWslMirrored -Ports $portList
}

function Add-HostsEntry {
    param([Parameter(Mandatory = $true)][string]$HostName)

    if ([string]::IsNullOrWhiteSpace($HostName)) {
        return
    }
    $hostPart = Get-PublicHostPart -Value $HostName
    if ([string]::IsNullOrWhiteSpace($hostPart) -or (Test-IsIpOrLocalHost -HostName $hostPart)) {
        Write-Log "Hosts-Eintrag nicht noetig fuer IP/localhost: $HostName"
        return
    }
    if (-not (Test-IsAdministrator)) {
        Write-Log "Hosts-Datei wird nicht geaendert, weil PowerShell nicht als Administrator laeuft. Bitte manuell eintragen: 127.0.0.1 $hostPart"
        return
    }

    $hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
    $content = Get-Content -LiteralPath $hostsPath -ErrorAction SilentlyContinue
    if ($content -match "(^|\s)$([Regex]::Escape($hostPart))(\s|$)") {
        Write-Log "Hosts-Eintrag ist bereits vorhanden: $hostPart"
        return
    }
    Add-Content -LiteralPath $hostsPath -Value "127.0.0.1 $hostPart" -Encoding ascii
    Write-Log "Hosts-Eintrag hinzugefuegt: 127.0.0.1 $hostPart"
}

function Install-TargetUpdaterFiles {
    param(
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [Parameter(Mandatory = $true)][string]$AppDir,
        [Parameter(Mandatory = $true)][string]$AppName,
        [Parameter(Mandatory = $true)][string]$AppVersion,
        [Parameter(Mandatory = $true)][string]$DistroName,
        [Parameter(Mandatory = $true)][string]$AppHostName,
        [string]$WslNetworkingMode = ""
    )

    $updaterDir = Join-Path $AppDir "updater"
    $incomingDir = Join-Path $AppDir "incoming"
    foreach ($dir in @($updaterDir, $incomingDir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    foreach ($fileName in @("update-windows-offline.ps1", "apply-windows-update.ps1", "configure-windows-portproxy.ps1", "install-windows-autostart.ps1", "install-windows-mediamtx.ps1", "export-pretrained-models-update.ps1")) {
        $source = Join-Path $BundleRoot $fileName
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $updaterDir $fileName) -Force
            if ($fileName -in @("install-windows-autostart.ps1", "install-windows-mediamtx.ps1")) {
                Copy-Item -LiteralPath $source -Destination (Join-Path $AppDir $fileName) -Force
            }
        }
    }
    foreach ($fileName in @("YBB-Deployment-UI.exe", "windows-deployment-ui.ps1")) {
        $source = Join-Path $BundleRoot $fileName
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $updaterDir $fileName) -Force
            Copy-Item -LiteralPath $source -Destination (Join-Path $AppDir $fileName) -Force
        }
    }

    $updateCmdPath = Join-Path $AppDir "update-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $updateCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0updater\apply-windows-update.ps1" %*'
    )

    $startPs1Path = Join-Path $AppDir "start-ybb-inferenzserver.ps1"
    $keepaliveCommand = "if [ -x /usr/local/bin/ybb-keepalive ]; then exec /usr/local/bin/ybb-keepalive; elif [ -x /usr/local/bin/ybb-start-platform ]; then /usr/local/bin/ybb-start-platform; exec sleep infinity; else systemctl start caddy ybb-platform.target; exec sleep infinity; fi"
    $quotedKeepaliveCommand = $keepaliveCommand.Replace("'", "''")
    $quotedDistroName = $DistroName.Replace("'", "''")
    Set-Content -LiteralPath $startPs1Path -Encoding ascii -Value @(
        "`$ErrorActionPreference = `"Stop`"",
        "`$distroName = '$quotedDistroName'",
        "`$keepaliveCommand = '$quotedKeepaliveCommand'",
        "`$logDir = Join-Path `$PSScriptRoot `"logs`"",
        "New-Item -ItemType Directory -Path `$logDir -Force | Out-Null",
        "`$logPath = Join-Path `$logDir (`"keepalive-{0:yyyyMMdd}.log`" -f (Get-Date))",
        "function Write-KeepaliveLog { param([string]`$Message) Add-Content -LiteralPath `$logPath -Value (`"[{0}] {1}`" -f (Get-Date -Format `"yyyy-MM-dd HH:mm:ss`"), `$Message) -Encoding utf8 }",
        "`$wslPath = Join-Path `$env:WINDIR `"System32\wsl.exe`"",
        "`$arguments = @(`"-d`", `$distroName, `"-u`", `"root`", `"--`", `"bash`", `"-lc`", `$keepaliveCommand)",
        "Write-KeepaliveLog `"Starte WSL-Keepalive fuer `$distroName.`"",
        "try {",
        "    & `$wslPath @arguments 2>&1 | ForEach-Object { `$text = `$_.ToString(); if (-not [string]::IsNullOrWhiteSpace(`$text)) { Write-KeepaliveLog `$text } }",
        "    `$exitCode = `$LASTEXITCODE",
        "    Write-KeepaliveLog `"WSL-Keepalive beendet. Exit Code: `$exitCode`"",
        "    exit `$exitCode",
        "}",
        "catch {",
        "    Write-KeepaliveLog `"FEHLER: `$(`$_.Exception.Message)`"",
        "    exit 1",
        "}"
    )

    $startCmdPath = Join-Path $AppDir "start-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $startCmdPath -Encoding ascii -Value @(
        "@echo off",
        'start "YBB Inferenzserver" /min "%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ybb-inferenzserver.ps1"'
    )

    $stopCmdPath = Join-Path $AppDir "stop-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $stopCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-mediamtx.ps1" -Action Stop -AppDir "%~dp0"',
        ('"%WINDIR%\System32\wsl.exe" --terminate {0}' -f $DistroName)
    )

    $statusCmdPath = Join-Path $AppDir "status-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $statusCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-mediamtx.ps1" -Action Status -AppDir "%~dp0"',
        ('"%WINDIR%\System32\wsl.exe" -d {0} -u root -- bash -lc "if pgrep -f ybb-keepalive >/dev/null 2>&1; then echo YBB-Keepalive: aktiv; else echo YBB-Keepalive: nicht aktiv; fi; systemctl --no-pager list-units ''ybb-*'' caddy --type=service --state=running,failed,activating"' -f $DistroName)
    )

    $rtspProbeCmdPath = Join-Path $AppDir "rtsp-probe-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $rtspProbeCmdPath -Encoding ascii -Value @(
        "@echo off",
        ('"%WINDIR%\System32\wsl.exe" -d {0} -u root -- /usr/local/bin/ybb-rtsp-probe %*' -f $DistroName)
    )

    $autostartScriptPath = Join-Path $AppDir "install-windows-autostart.ps1"
    $installAutostartCmdPath = Join-Path $AppDir "install-autostart-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $installAutostartCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-autostart.ps1" -Action Install %*'
    )
    $removeAutostartCmdPath = Join-Path $AppDir "remove-autostart-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $removeAutostartCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-autostart.ps1" -Action Remove %*'
    )
    $statusAutostartCmdPath = Join-Path $AppDir "status-autostart-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $statusAutostartCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-autostart.ps1" -Action Status %*'
    )
    $startAutostartCmdPath = Join-Path $AppDir "start-autostart-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $startAutostartCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-autostart.ps1" -Action Start %*'
    )
    $stopAutostartCmdPath = Join-Path $AppDir "stop-autostart-ybb-inferenzserver.cmd"
    Set-Content -LiteralPath $stopAutostartCmdPath -Encoding ascii -Value @(
        "@echo off",
        '"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows-autostart.ps1" -Action Stop %*'
    )

    $statePath = Join-Path $AppDir "install-state.json"
    $state = [ordered]@{
        AppName = $AppName
        AppVersion = $AppVersion
        DistroName = $DistroName
        AppHostName = $AppHostName
        WslNetworkingMode = $WslNetworkingMode
        InstalledAt = (Get-Date).ToUniversalTime().ToString("o")
        IncomingDir = $incomingDir
        UpdaterDir = $updaterDir
        UpdateCommand = $updateCmdPath
        RtspProbeCommand = $rtspProbeCmdPath
        AutostartTaskName = "YBB-Inferenzserver Keepalive"
        AutostartScript = $autostartScriptPath
        AutostartInstallCommand = $installAutostartCmdPath
        AutostartStatusCommand = $statusAutostartCmdPath
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
    Write-Log "Updater wurde installiert: $updaterDir"
    Write-Log "Update-Bundles koennen nach $incomingDir kopiert und mit $updateCmdPath eingespielt werden."
}

function Install-TargetAutostart {
    param([Parameter(Mandatory = $true)][string]$AppDir)

    $autostartScript = Join-Path $AppDir "install-windows-autostart.ps1"
    if (-not (Test-Path -LiteralPath $autostartScript -PathType Leaf)) {
        throw "Autostart-Skript fehlt: $autostartScript"
    }

    try {
        Invoke-LoggedCommand `
            -Description "Registriere Windows-Autostart-Aufgabe." `
            -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
            -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $autostartScript, "-Action", "Install", "-AppDir", $AppDir)
    }
    catch {
        throw (
            "Windows-Autostart ist eine Pflichtkomponente und konnte nicht " +
            "registriert werden: $($_.Exception.Message)"
        )
    }
}

function Start-TargetApplication {
    param([Parameter(Mandatory = $true)][string]$AppDir)

    $startScript = Join-Path $AppDir "start-ybb-inferenzserver.ps1"
    if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
        Write-Log "WARNUNG: Start-Skript fehlt: $startScript"
        return
    }

    $autostartScript = Join-Path $AppDir "install-windows-autostart.ps1"
    if (-not (Test-Path -LiteralPath $autostartScript -PathType Leaf)) {
        throw "Autostart-Skript fehlt: $autostartScript"
    }

    try {
        Invoke-LoggedCommand `
            -Description "Starte und validiere Windows-Autostart-Aufgabe." `
            -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
            -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $autostartScript, "-Action", "Start", "-AppDir", $AppDir)
    }
    catch {
        throw (
            "Der login-unabhaengige WSL-Autostart konnte nicht gestartet " +
            "oder validiert werden: $($_.Exception.Message)"
        )
    }
}

function Install-WindowsMediaMtx {
    param(
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [Parameter(Mandatory = $true)][string]$AppDir,
        [Parameter(Mandatory = $true)][string]$PublicAddress,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][int]$RtspPort,
        [Parameter(Mandatory = $true)][int]$WebRtcPort,
        [Parameter(Mandatory = $true)][int]$WebRtcIceTcpPort,
        [Parameter(Mandatory = $true)][int]$TurnTcpPort,
        [Parameter(Mandatory = $true)][bool]$Skipped
    )

    $scriptPath = Join-Path $BundleRoot "install-windows-mediamtx.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        $scriptPath = Join-Path $AppDir "install-windows-mediamtx.ps1"
    }
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "MediaMTX Windows Installationsskript fehlt: $scriptPath"
    }

    if ($Skipped) {
        Write-Log "MediaMTX wurde fuer dieses Bundle uebersprungen; starte vorhandenen Windows-Host-Fanout."
        Invoke-LoggedCommand `
            -Description "Starte vorhandenes MediaMTX direkt auf Windows." `
            -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
            -Arguments @(
                "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", $scriptPath,
                "-Action", "Start",
                "-AppDir", $AppDir,
                "-PublicAddress", $PublicAddress,
                "-RtspPort", [string]$RtspPort,
                "-WebRtcPort", [string]$WebRtcPort,
                "-WebRtcIceTcpPort", [string]$WebRtcIceTcpPort,
                "-TurnTcpPort", [string]$TurnTcpPort
            )
        return
    }
    Invoke-LoggedCommand `
        -Description "Installiere/starte MediaMTX direkt auf Windows." `
        -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", $scriptPath,
            "-Action", "Install",
            "-AppDir", $AppDir,
            "-BundleRoot", $BundleRoot,
            "-PublicAddress", $PublicAddress,
            "-Version", $Version,
            "-RtspPort", [string]$RtspPort,
            "-WebRtcPort", [string]$WebRtcPort,
            "-WebRtcIceTcpPort", [string]$WebRtcIceTcpPort,
            "-TurnTcpPort", [string]$TurnTcpPort
        )
}

function Get-MediaMtxClientCredentials {
    param([Parameter(Mandatory = $true)][string]$AppDir)

    $envPath = Join-Path (Join-Path $AppDir "config") "mediamtx-client.env"
    $values = @{}
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $envPath) {
            if ($line -match "^(?<Name>[^=]+)=(?<Value>.*)$") {
                $values[$Matches.Name.Trim()] = $Matches.Value.Trim()
            }
        }
    }
    [pscustomobject]@{
        User = [string]$values["MEDIAMTX_API_USER"]
        Password = [string]$values["MEDIAMTX_API_PASSWORD"]
        TurnAuthSecret = [string]$values["TURN_AUTH_SECRET"]
    }
}

function Invoke-PortProxySetup {
    param(
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [Parameter(Mandatory = $true)][string]$DistroName,
        [Parameter(Mandatory = $true)][int]$ListenPort,
        [Parameter(Mandatory = $true)][string]$PublicAddress,
        [string]$WslNetworkingMode = "",
        [int[]]$AdditionalTcpPorts = @(),
        [int[]]$AdditionalUdpPorts = @()
    )

    $additionalTcpPortsList = @($AdditionalTcpPorts | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    $additionalTcpPortsArgument = $additionalTcpPortsList -join ","
    $additionalUdpPortsList = @($AdditionalUdpPorts | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    $additionalUdpPortsArgument = $additionalUdpPortsList -join ","
    $scriptPath = Join-Path $BundleRoot "configure-windows-portproxy.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        Write-Log "WARNUNG: Portproxy-Skript fehlt im Bundle: $scriptPath"
        return
    }
    if (-not (Test-IsAdministrator)) {
        Write-Log "Portproxy wurde nicht automatisch eingerichtet, weil PowerShell nicht als Administrator laeuft."
        $additionalHint = if ($additionalTcpPortsList.Count -gt 0) { " -AdditionalTcpPorts $additionalTcpPortsArgument" } else { "" }
        if ($additionalUdpPortsList.Count -gt 0) { $additionalHint += " -AdditionalUdpPorts $additionalUdpPortsArgument" }
        Write-Log "Fuer lokalen Zugriff spaeter als Administrator ausfuehren: $scriptPath -DistroName `"$DistroName`" -ListenPort $ListenPort$additionalHint"
        return
    }

    try {
        $connectPort = if ($ListenPort -eq 80) { 80 } else { 443 }
        $proxyArguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", $scriptPath,
            "-DistroName", $DistroName,
            "-ListenPort", [string]$ListenPort,
            "-ConnectPort", [string]$connectPort
        )
        if (-not [string]::IsNullOrWhiteSpace($WslNetworkingMode)) {
            $proxyArguments += @("-WslNetworkingMode", $WslNetworkingMode)
        }
        if ($additionalTcpPortsList.Count -gt 0) {
            $proxyArguments += @("-AdditionalTcpPorts", $additionalTcpPortsArgument)
        }
        if ($additionalUdpPortsList.Count -gt 0) {
            $proxyArguments += @("-AdditionalUdpPorts", $additionalUdpPortsArgument)
        }
        $listenAddress = Get-PortProxyListenAddress -PublicAddress $PublicAddress
        if (-not [string]::IsNullOrWhiteSpace($listenAddress)) {
            $proxyArguments += @("-ListenAddress", $listenAddress)
            $proxyArguments += "-WildcardListenAddress"
        }

        Invoke-LoggedCommand `
            -Description "Konfiguriere Windows-Portproxy fuer TCP/$ListenPort." `
            -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
            -Arguments $proxyArguments
    }
    catch {
        Write-Log "WARNUNG: Portproxy konnte nicht automatisch eingerichtet werden: $($_.Exception.Message)"
        $additionalHint = if ($additionalTcpPortsList.Count -gt 0) { " -AdditionalTcpPorts $additionalTcpPortsArgument" } else { "" }
        if ($additionalUdpPortsList.Count -gt 0) { $additionalHint += " -AdditionalUdpPorts $additionalUdpPortsArgument" }
        Write-Log "Fuer lokalen Zugriff spaeter als Administrator ausfuehren: $scriptPath -DistroName `"$DistroName`" -ListenPort $ListenPort$additionalHint"
    }
}

function Acquire-Lock {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $writer = New-Object System.IO.StreamWriter($stream)
        $writer.WriteLine("CreatedAt={0}" -f (Get-Date).ToString("o"))
        $writer.WriteLine("ProcessId={0}" -f $PID)
        $writer.Dispose()
        $stream.Dispose()
        $script:LockAcquired = $true
    }
    catch [System.IO.IOException] {
        throw "Installation abgebrochen: Lock-Datei existiert bereits: $Path"
    }
}

function Invoke-WslInstall {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$AppName,
        [Parameter(Mandatory = $true)][string]$HostName,
        [string]$WslNetworkingMode = "",
        [string]$MediaMtxApiUser = "",
        [string]$MediaMtxApiPassword = "",
        [string]$TurnAuthSecret = ""
    )

    $bundleWslPath = ConvertTo-WslPath -WindowsPath $BundleRoot
    $scriptPath = "$bundleWslPath/wsl/install-offline.sh"
    $command = "bash $(Quote-Bash $scriptPath) --bundle $(Quote-Bash $bundleWslPath) --version $(Quote-Bash $Version) --app-name $(Quote-Bash $AppName) --host-name $(Quote-Bash $HostName)"
    if (-not [string]::IsNullOrWhiteSpace($WslNetworkingMode)) {
        $command = "YBB_WSL_NETWORKING_MODE=$(Quote-Bash $WslNetworkingMode) $command"
    }
    if (-not [string]::IsNullOrWhiteSpace($MediaMtxApiUser) -and -not [string]::IsNullOrWhiteSpace($MediaMtxApiPassword)) {
        $command += " --mediamtx-api-user $(Quote-Bash $MediaMtxApiUser) --mediamtx-api-password $(Quote-Bash $MediaMtxApiPassword)"
    }
    if (-not [string]::IsNullOrWhiteSpace($TurnAuthSecret)) {
        $command += " --turn-auth-secret $(Quote-Bash $TurnAuthSecret)"
    }
    return Invoke-LoggedCommand `
        -Description "Fuehre WSL-Installationsskript aus." `
        -FilePath "wsl" `
        -Arguments @("-d", $Distro, "-u", "root", "--", "bash", "-lc", $command) `
        -AllowedExitCodes @(0, 42)
}

try {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        throw "wsl.exe wurde nicht gefunden. Aktivieren Sie WSL2 auf dem Zielgeraet, danach erneut ausfuehren."
    }

    $bundleRoot = Resolve-BundleRoot -PathValue $BundlePath
    $manifestPath = Join-Path $bundleRoot "windows-bundle-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "windows-bundle-manifest.json fehlt im Bundle."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ((Get-RequiredJsonValue -Object $manifest -Name "BundleType") -ne "installer") {
        throw "Dieses Bundle ist kein Installer-Bundle."
    }

    $appName = Get-RequiredJsonValue -Object $manifest -Name "AppName"
    $appVersion = Get-RequiredJsonValue -Object $manifest -Name "AppVersion"
    $manifestDistroName = Get-RequiredJsonValue -Object $manifest -Name "DistroName"
    $serverBaseDir = Get-RequiredJsonValue -Object $manifest -Name "ServerBaseDir"
    $appHostName = Get-RequiredJsonValue -Object $manifest -Name "AppHostName"
    if (-not [string]::IsNullOrWhiteSpace($PublicAddress)) {
        $appHostName = $PublicAddress
    }
    $appHostName = ConvertTo-PublicAuthority -Value $appHostName
    $portProxyListenPort = if ($ListenPort -gt 0) { $ListenPort } else { Get-PublicPort -Value $appHostName -DefaultPort 443 }
    $mediaMtxSkipped = [bool](Get-OptionalJsonValue -Object $manifest -Name "MediaMtxSkipped" -DefaultValue $false)
    $streamingRtspPort = [int](Get-OptionalJsonValue -Object $manifest -Name "StreamingRtspPort" -DefaultValue 8554)
    $streamingWebRtcPort = [int](Get-OptionalJsonValue -Object $manifest -Name "StreamingWebRtcPort" -DefaultValue 8889)
    $streamingWebRtcIceTcpPort = [int](Get-OptionalJsonValue -Object $manifest -Name "StreamingWebRtcIceTcpPort" -DefaultValue 8189)
    $streamingTurnTcpPort = [int](Get-OptionalJsonValue -Object $manifest -Name "StreamingTurnTcpPort" -DefaultValue 3478)
    $mediaMtxVersion = [string](Get-OptionalJsonValue -Object $manifest -Name "MediaMtxVersion" -DefaultValue "unknown")
    $wslNetworkingMode = [string](Get-OptionalJsonValue -Object $manifest -Name "WslNetworkingMode" -DefaultValue "unchanged")
    $wslDnsTunneling = [bool](Get-OptionalJsonValue -Object $manifest -Name "WslDnsTunneling" -DefaultValue $true)
    $wslFirewall = [bool](Get-OptionalJsonValue -Object $manifest -Name "WslFirewall" -DefaultValue $true)
    $wslAutoProxy = [bool](Get-OptionalJsonValue -Object $manifest -Name "WslAutoProxy" -DefaultValue $false)
    $wslHostAddressLoopback = [bool](Get-OptionalJsonValue -Object $manifest -Name "WslHostAddressLoopback" -DefaultValue $true)
    $wslHyperVFirewallDefaultInboundAction = [string](Get-OptionalJsonValue -Object $manifest -Name "WslHyperVFirewallDefaultInboundAction" -DefaultValue "Unchanged")
    $wslRestartAllowed = [bool](Get-OptionalJsonValue -Object $manifest -Name "WslRestartAllowed" -DefaultValue $true)
    $streamingTcpPorts = @($streamingTurnTcpPort)
    $streamingUdpPorts = @()
    $runtimeArchiveMode = [string](Get-OptionalJsonValue -Object $manifest -Name "RuntimeArchiveMode" -DefaultValue "file")
    $runtimeArchiveSha256 = Get-RequiredJsonValue -Object $manifest -Name "RuntimeArchiveSha256"
    $runtimeArchiveParts = @(
        Get-OptionalJsonValue -Object $manifest -Name "RuntimeArchiveParts" -DefaultValue @()
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | ForEach-Object { [string]$_ }

    if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
        $serverBaseDir = $InstallRoot
    }
    if (-not [string]::IsNullOrWhiteSpace($DistroName)) {
        $manifestDistroName = $DistroName
    }
    if (-not [System.IO.Path]::IsPathRooted($serverBaseDir)) {
        throw "InstallRoot/ServerBaseDir muss ein absoluter Windows-Pfad sein: $serverBaseDir"
    }

    $appDir = Join-Path $serverBaseDir $appName
    $logsDir = Join-Path $appDir "logs"
    $incomingDir = Join-Path $appDir "incoming"
    $deployedDir = Join-Path $appDir "deployed"
    $failedDir = Join-Path $appDir "failed"
    $updaterDir = Join-Path $appDir "updater"
    $wslDir = Join-Path $appDir "wsl"
    foreach ($dir in @($appDir, $logsDir, $incomingDir, $deployedDir, $failedDir, $updaterDir, $wslDir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $script:LogFile = Join-Path $logsDir ("install-{0}.log" -f (Get-Date -Format "yyyy-MM-dd-HHmmss"))
    New-Item -ItemType File -Path $script:LogFile -Force | Out-Null
    Write-Log "Starte Offline-Installation fuer $appName $appVersion."

    $script:LockFile = Join-Path $appDir "install.lock"
    Acquire-Lock -Path $script:LockFile

    $commonFiles = @("app.tar.gz", "install-windows-offline.ps1", "update-windows-offline.ps1", "apply-windows-update.ps1", "configure-windows-portproxy.ps1", "install-windows-autostart.ps1", "install-windows-mediamtx.ps1", "YBB-Deployment-UI.exe", "windows-deployment-ui.ps1", "wsl/install-offline.sh", "wsl/apply-release.sh", "windows-bundle-manifest.json", "VERSION")
    if (Test-Path -LiteralPath (Join-Path $bundleRoot "export-pretrained-models-update.ps1") -PathType Leaf) {
        $commonFiles += "export-pretrained-models-update.ps1"
    }
    $checksummedFiles = if ($runtimeArchiveMode -eq "split") { @($commonFiles + $runtimeArchiveParts) } else { @($commonFiles + @("runtime.tar")) }
    if (Test-Path -LiteralPath (Join-Path $bundleRoot "runtime\mediamtx.zip") -PathType Leaf) {
        $checksummedFiles += "runtime/mediamtx.zip"
    }
    if (Test-Path -LiteralPath (Join-Path $bundleRoot "runtime\gstreamer-debs.tar.gz") -PathType Leaf) {
        $checksummedFiles += "runtime/gstreamer-debs.tar.gz"
    }
    if (Test-Path -LiteralPath (Join-Path $bundleRoot "runtime\python-wheels.tar.gz") -PathType Leaf) {
        $checksummedFiles += "runtime/python-wheels.tar.gz"
    }
    Write-Log "Pruefe SHA256-Pruefsummen."
    Test-ChecksumManifest -RootDir $bundleRoot -ExpectedFileNames $checksummedFiles -SkipHashFileNames @("install-windows-offline.ps1")

    $runtimeTarPath = Join-Path $bundleRoot "runtime.tar"
    if ($runtimeArchiveMode -eq "split") {
        if ($runtimeArchiveParts.Count -eq 0) {
            throw "Installer-Bundle meldet geteiltes Runtime-Archiv, enthaelt aber keine Teile."
        }
        $runtimeTarPath = Join-Path $appDir "runtime-import.tar"
        Join-ArchiveParts -RootDir $bundleRoot -PartNames $runtimeArchiveParts -OutputPath $runtimeTarPath -ExpectedSha256 $runtimeArchiveSha256
    }
    else {
        $actualRuntimeSha256 = (Get-FileHash -LiteralPath $runtimeTarPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualRuntimeSha256 -ne $runtimeArchiveSha256.ToLowerInvariant()) {
            throw "SHA256-Pruefsumme von runtime.tar stimmt nicht."
        }
    }

    $distros = Get-WslDistros
    if ($distros -contains $manifestDistroName) {
        throw "Die WSL-Distribution '$manifestDistroName' existiert bereits. Fuer vorhandene Installationen bitte update-windows-offline.ps1 verwenden."
    }

    $wslNetworking = Set-WslGlobalNetworkingConfig `
        -AppDir $appDir `
        -NetworkingMode $wslNetworkingMode `
        -DnsTunneling $wslDnsTunneling `
        -Firewall $wslFirewall `
        -AutoProxy $wslAutoProxy `
        -HostAddressLoopback $wslHostAddressLoopback `
        -HyperVFirewallDefaultInboundAction $wslHyperVFirewallDefaultInboundAction `
        -RestartAllowed $wslRestartAllowed
    $effectiveWslNetworkingMode = [string]$wslNetworking.EffectiveMode
    if ($effectiveWslNetworkingMode -eq "mirrored") {
        $portsToRelease = @($portProxyListenPort, $streamingTurnTcpPort)
        if ($portProxyListenPort -eq 443) {
            $portsToRelease += 80
        }
        Reset-WslMirroredWebPortBindings -Ports $portsToRelease -Reason "WSL Mirrored Networking vor WSL-Start" -RestartAllowed $wslRestartAllowed
    }

    Invoke-LoggedCommand -Description "Pruefe WSL-Status." -FilePath "wsl" -Arguments @("--status") -AllowedExitCodes @(0)
    Invoke-LoggedCommand `
        -Description "Importiere vorbereitete WSL2-Runtime." `
        -FilePath "wsl" `
        -Arguments @("--import", $manifestDistroName, $wslDir, $runtimeTarPath, "--version", "2")

    Install-WindowsMediaMtx -BundleRoot $bundleRoot -AppDir $appDir -PublicAddress $appHostName -Version $mediaMtxVersion -RtspPort $streamingRtspPort -WebRtcPort $streamingWebRtcPort -WebRtcIceTcpPort $streamingWebRtcIceTcpPort -TurnTcpPort $streamingTurnTcpPort -Skipped $mediaMtxSkipped
    $mediaMtxCredentials = Get-MediaMtxClientCredentials -AppDir $appDir
    if ([string]::IsNullOrWhiteSpace($mediaMtxCredentials.TurnAuthSecret)) {
        throw "MediaMTX hat kein TURN-Shared-Secret erzeugt."
    }

    $exitCode = Invoke-WslInstall -Distro $manifestDistroName -BundleRoot $bundleRoot -Version $appVersion -AppName $appName -HostName $appHostName -WslNetworkingMode $effectiveWslNetworkingMode -MediaMtxApiUser $mediaMtxCredentials.User -MediaMtxApiPassword $mediaMtxCredentials.Password -TurnAuthSecret $mediaMtxCredentials.TurnAuthSecret
    if ($exitCode -eq 42) {
        Write-Log "WSL systemd wurde aktiviert; Distribution wird neu gestartet."
        Invoke-LoggedCommand -Description "Beende WSL-Distribution fuer systemd-Neustart." -FilePath "wsl" -Arguments @("--terminate", $manifestDistroName) -AllowedExitCodes @(0)
        Start-Sleep -Seconds 3
        $exitCode = Invoke-WslInstall -Distro $manifestDistroName -BundleRoot $bundleRoot -Version $appVersion -AppName $appName -HostName $appHostName -WslNetworkingMode $effectiveWslNetworkingMode -MediaMtxApiUser $mediaMtxCredentials.User -MediaMtxApiPassword $mediaMtxCredentials.Password -TurnAuthSecret $mediaMtxCredentials.TurnAuthSecret
        if ($exitCode -ne 0) {
            throw "WSL-Installationsskript konnte nach systemd-Neustart nicht abgeschlossen werden."
        }
    }

    if (-not $SkipHostsEntry) {
        Add-HostsEntry -HostName $appHostName
    }
    Install-TargetUpdaterFiles -BundleRoot $bundleRoot -AppDir $appDir -AppName $appName -AppVersion $appVersion -DistroName $manifestDistroName -AppHostName $appHostName -WslNetworkingMode $effectiveWslNetworkingMode
    Install-TargetAutostart -AppDir $appDir
    Start-TargetApplication -AppDir $appDir
    if (-not $SkipPortProxy) {
        Invoke-PortProxySetup -BundleRoot $bundleRoot -DistroName $manifestDistroName -ListenPort $portProxyListenPort -PublicAddress $appHostName -WslNetworkingMode $effectiveWslNetworkingMode -AdditionalTcpPorts $streamingTcpPorts -AdditionalUdpPorts $streamingUdpPorts
    }

    Write-Log "Installation erfolgreich abgeschlossen."
    Write-Log "Weboberflaeche: https://$appHostName"
    Write-Log "Initiales Admin-Passwort liegt im WSL-State unter /srv/cv-platform/secrets/bootstrap_admin_initial_password.txt und wird bei Erstinstallation erzeugt."
}
catch {
    Write-Log ("FEHLER: {0}" -f $_.Exception.Message)
    exit 1
}
finally {
    if ($script:LockAcquired -and -not [string]::IsNullOrWhiteSpace($script:LockFile) -and (Test-Path -LiteralPath $script:LockFile)) {
        Remove-Item -LiteralPath $script:LockFile -Force
    }
    if (-not [string]::IsNullOrWhiteSpace($script:TempDir) -and (Test-Path -LiteralPath $script:TempDir)) {
        Remove-Item -LiteralPath $script:TempDir -Recurse -Force
    }
}
