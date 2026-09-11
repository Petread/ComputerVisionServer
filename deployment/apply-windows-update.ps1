[CmdletBinding()]
param(
    [string]$BundlePath = "",
    [string]$AppDir = "",
    [string]$DistroName = "",
    [Alias("AppHostName", "HostName")]
    [string]$PublicAddress = "",
    [switch]$KeepBundle
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
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

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$PathValue)

    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) {
        $PathValue
    }
    else {
        Join-Path (Get-Location).Path $PathValue
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Test-IsPathUnderDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-AppDirFromScriptLocation {
    $scriptDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
    if ((Split-Path -Leaf $scriptDir) -ieq "updater") {
        return [System.IO.Path]::GetFullPath((Split-Path -Parent $scriptDir))
    }
    return ""
}

function Read-BundleManifest {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $manifestPath = Join-Path $BundleRoot "windows-bundle-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $null
    }
    return Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}

function Read-PretrainedModelsManifest {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $manifestPath = Join-Path $BundleRoot "pretrained-models-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $null
    }
    return Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}

function Expand-UpdateBundleForBootstrap {
    param([Parameter(Mandatory = $true)][string]$BundleArchivePath)

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-update-bootstrap-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    Write-Log "Entpacke Update-Bundle fuer Bootstrap nach: $tempDir"
    Expand-ZipArchiveSafely -ArchivePath $BundleArchivePath -DestinationPath $tempDir
    return $tempDir
}

function Assert-InstalledAppVersion {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedAppDir,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    $statePath = Join-Path $ResolvedAppDir "install-state.json"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw "Update meldete Erfolg, aber install-state.json fehlt: $statePath"
    }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $installedVersion = if ($state.PSObject.Properties.Name -contains "AppVersion") {
        [string]$state.AppVersion
    }
    else {
        ""
    }
    if ($installedVersion -ne $ExpectedVersion) {
        throw "Update meldete Erfolg, aber installiert ist Version '$installedVersion' statt '$ExpectedVersion'."
    }
    Write-Log "Installierte Version bestätigt: $installedVersion"
}

function Get-AppDirFromManifest {
    param([Parameter(Mandatory = $true)]$Manifest)

    if (
        -not ($Manifest.PSObject.Properties.Name -contains "ServerBaseDir") -or
        -not ($Manifest.PSObject.Properties.Name -contains "AppName")
    ) {
        return ""
    }
    $serverBaseDir = [string]$Manifest.ServerBaseDir
    $appName = [string]$Manifest.AppName
    if ([string]::IsNullOrWhiteSpace($serverBaseDir) -or [string]::IsNullOrWhiteSpace($appName)) {
        return ""
    }
    return [System.IO.Path]::GetFullPath((Join-Path $serverBaseDir $appName))
}

function Find-LatestIncomingBundle {
    param([Parameter(Mandatory = $true)][string]$ResolvedAppDir)

    $incomingDir = Join-Path $ResolvedAppDir "incoming"
    if (-not (Test-Path -LiteralPath $incomingDir -PathType Container)) {
        throw "Kein incoming-Verzeichnis gefunden: $incomingDir"
    }

    $bundles = @(
        Get-ChildItem -LiteralPath $incomingDir -Filter "*.zip" -File |
            Where-Object { $_.Name -like "*-windows-update.zip" -or $_.Name -like "*pretrained-models-update*.zip" } |
            Sort-Object LastWriteTimeUtc -Descending
    )
    if ($bundles.Count -eq 0) {
        throw "Kein Update-Bundle in $incomingDir gefunden. Kopieren Sie ein *-windows-update.zip oder *-pretrained-models-update.zip dorthin oder uebergeben Sie -BundlePath."
    }
    return [System.IO.Path]::GetFullPath($bundles[0].FullName)
}

function Resolve-UpdateScript {
    param(
        [string]$ResolvedAppDir,
        [string]$BundleRoot = ""
    )

    if (-not [string]::IsNullOrWhiteSpace($BundleRoot)) {
        $bundleScript = Join-Path $BundleRoot "update-windows-offline.ps1"
        if (Test-Path -LiteralPath $bundleScript -PathType Leaf) {
            Write-Log "Verwende Update-Skript aus dem neuen Bundle: $bundleScript"
            return [System.IO.Path]::GetFullPath($bundleScript)
        }
    }

    $localScript = Join-Path $PSScriptRoot "update-windows-offline.ps1"
    if (Test-Path -LiteralPath $localScript -PathType Leaf) {
        Write-Log "Verwende installiertes Update-Skript: $localScript"
        return [System.IO.Path]::GetFullPath($localScript)
    }

    if (-not [string]::IsNullOrWhiteSpace($ResolvedAppDir)) {
        $installedScript = Join-Path $ResolvedAppDir "updater\update-windows-offline.ps1"
        if (Test-Path -LiteralPath $installedScript -PathType Leaf) {
            Write-Log "Verwende installiertes Update-Skript: $installedScript"
            return [System.IO.Path]::GetFullPath($installedScript)
        }
    }

    throw "update-windows-offline.ps1 wurde weder im Bundle noch im installierten Updater gefunden."
}

function Test-ScriptSupportsParameter {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ParameterName
    )

    try {
        $text = Get-Content -LiteralPath $ScriptPath -Raw
        return $text -match ("`$" + [Regex]::Escape($ParameterName) + "\b")
    }
    catch {
        return $false
    }
}

function Move-ProcessedIncomingBundle {
    param(
        [Parameter(Mandatory = $true)][string]$BundleArchivePath,
        [Parameter(Mandatory = $true)][string]$ResolvedAppDir,
        [Parameter(Mandatory = $true)][ValidateSet("deployed", "failed")][string]$TargetName
    )

    if ($KeepBundle) {
        return
    }
    if (-not (Test-Path -LiteralPath $BundleArchivePath -PathType Leaf)) {
        return
    }

    $incomingDir = Join-Path $ResolvedAppDir "incoming"
    if (-not (Test-IsPathUnderDirectory -Child $BundleArchivePath -Parent $incomingDir)) {
        return
    }

    $targetDir = Join-Path $ResolvedAppDir $TargetName
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    $destination = Join-Path $targetDir (Split-Path -Leaf $BundleArchivePath)
    if (Test-Path -LiteralPath $destination) {
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($destination)
        $extension = [System.IO.Path]::GetExtension($destination)
        $destination = Join-Path $targetDir ("{0}-{1}{2}" -f $stem, (Get-Date -Format "yyyyMMdd-HHmmss"), $extension)
    }
    Move-Item -LiteralPath $BundleArchivePath -Destination $destination
    Write-Log "Bundle wurde nach $TargetName verschoben: $destination"
}

$resolvedAppDir = if ([string]::IsNullOrWhiteSpace($AppDir)) {
    Get-AppDirFromScriptLocation
}
else {
    Resolve-FullPath -PathValue $AppDir
}

$bundleArgument = ""
$bundleArchivePath = ""
$bundleRoot = ""
$bundleManifest = $null
$pretrainedModelsManifest = $null
$bootstrapBundleRoot = ""

if ([string]::IsNullOrWhiteSpace($BundlePath)) {
    $localManifest = Read-BundleManifest -BundleRoot $PSScriptRoot
    if ($null -ne $localManifest) {
        $bundleRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
        $bundleArgument = $bundleRoot
        $bundleManifest = $localManifest
        $pretrainedModelsManifest = Read-PretrainedModelsManifest -BundleRoot $bundleRoot
        if ([string]::IsNullOrWhiteSpace($resolvedAppDir)) {
            $resolvedAppDir = Get-AppDirFromManifest -Manifest $bundleManifest
        }
    }
    else {
        if ([string]::IsNullOrWhiteSpace($resolvedAppDir)) {
            throw "AppDir konnte nicht ermittelt werden. Fuehren Sie dieses Skript aus dem installierten updater-Verzeichnis aus oder uebergeben Sie -AppDir."
        }
        $bundleArchivePath = Find-LatestIncomingBundle -ResolvedAppDir $resolvedAppDir
        $bootstrapBundleRoot = Expand-UpdateBundleForBootstrap -BundleArchivePath $bundleArchivePath
        $bundleArgument = $bootstrapBundleRoot
        $bundleManifest = Read-BundleManifest -BundleRoot $bootstrapBundleRoot
        $pretrainedModelsManifest = Read-PretrainedModelsManifest -BundleRoot $bootstrapBundleRoot
        if ($null -ne $bundleManifest -and [string]::IsNullOrWhiteSpace($resolvedAppDir)) {
            $resolvedAppDir = Get-AppDirFromManifest -Manifest $bundleManifest
        }
    }
}
else {
    $resolvedBundlePath = Resolve-FullPath -PathValue $BundlePath
    if (Test-Path -LiteralPath $resolvedBundlePath -PathType Container) {
        $bundleRoot = $resolvedBundlePath
        $bundleArgument = $bundleRoot
        $bundleManifest = Read-BundleManifest -BundleRoot $bundleRoot
        $pretrainedModelsManifest = Read-PretrainedModelsManifest -BundleRoot $bundleRoot
        if ($null -ne $bundleManifest -and [string]::IsNullOrWhiteSpace($resolvedAppDir)) {
            $resolvedAppDir = Get-AppDirFromManifest -Manifest $bundleManifest
        }
    }
    elseif (Test-Path -LiteralPath $resolvedBundlePath -PathType Leaf) {
        $bundleArchivePath = $resolvedBundlePath
        $bootstrapBundleRoot = Expand-UpdateBundleForBootstrap -BundleArchivePath $bundleArchivePath
        $bundleArgument = $bootstrapBundleRoot
        $bundleManifest = Read-BundleManifest -BundleRoot $bootstrapBundleRoot
        $pretrainedModelsManifest = Read-PretrainedModelsManifest -BundleRoot $bootstrapBundleRoot
        if ([string]::IsNullOrWhiteSpace($resolvedAppDir)) {
            if ($null -ne $bundleManifest) {
                $resolvedAppDir = Get-AppDirFromManifest -Manifest $bundleManifest
            }
            $parentDir = Split-Path -Parent $bundleArchivePath
            if ([string]::IsNullOrWhiteSpace($resolvedAppDir) -and (Split-Path -Leaf $parentDir) -ieq "incoming") {
                $resolvedAppDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $parentDir))
            }
        }
    }
    else {
        throw "BundlePath wurde nicht gefunden: $resolvedBundlePath"
    }
}

$scriptBundleRoot = if (-not [string]::IsNullOrWhiteSpace($bootstrapBundleRoot)) { $bootstrapBundleRoot } else { $bundleRoot }
$updateScript = Resolve-UpdateScript -ResolvedAppDir $resolvedAppDir -BundleRoot $scriptBundleRoot
$powerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $powerShellExe -PathType Leaf)) {
    throw "Windows PowerShell wurde nicht gefunden: $powerShellExe"
}
$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $updateScript,
    "-BundlePath",
    $bundleArgument
)
if (-not [string]::IsNullOrWhiteSpace($DistroName)) {
    $arguments += "-DistroName"
    $arguments += $DistroName
}
if (
    -not [string]::IsNullOrWhiteSpace($resolvedAppDir) -and
    (Test-ScriptSupportsParameter -ScriptPath $updateScript -ParameterName "AppDir")
) {
    $arguments += "-AppDir"
    $arguments += $resolvedAppDir
}
if (-not [string]::IsNullOrWhiteSpace($PublicAddress)) {
    $arguments += "-PublicAddress"
    $arguments += $PublicAddress
}

try {
    Write-Log "Starte Update mit Bundle: $bundleArgument"
    & $powerShellExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Update-Skript fehlgeschlagen. Exit Code: $LASTEXITCODE"
    }
    if (
        $null -ne $bundleManifest -and
        $bundleManifest.PSObject.Properties.Name -contains "AppVersion" -and
        -not [string]::IsNullOrWhiteSpace($resolvedAppDir)
    ) {
        Assert-InstalledAppVersion `
            -ResolvedAppDir $resolvedAppDir `
            -ExpectedVersion ([string]$bundleManifest.AppVersion)
    }
    if (-not [string]::IsNullOrWhiteSpace($bundleArchivePath) -and -not [string]::IsNullOrWhiteSpace($resolvedAppDir)) {
        Move-ProcessedIncomingBundle -BundleArchivePath $bundleArchivePath -ResolvedAppDir $resolvedAppDir -TargetName "deployed"
    }
    Write-Log "Update erfolgreich abgeschlossen."
}
catch {
    if (-not [string]::IsNullOrWhiteSpace($bundleArchivePath) -and -not [string]::IsNullOrWhiteSpace($resolvedAppDir)) {
        Move-ProcessedIncomingBundle -BundleArchivePath $bundleArchivePath -ResolvedAppDir $resolvedAppDir -TargetName "failed"
    }
    Write-Log ("FEHLER: {0}" -f $_.Exception.Message)
    exit 1
}
finally {
    if (-not [string]::IsNullOrWhiteSpace($bootstrapBundleRoot) -and (Test-Path -LiteralPath $bootstrapBundleRoot -PathType Container)) {
        Remove-Item -LiteralPath $bootstrapBundleRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
