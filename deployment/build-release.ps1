[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Log $Description
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description fehlgeschlagen. Exit Code: $LASTEXITCODE"
    }
}

function Resolve-ExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][ValidateSet("Leaf", "Container")][string]$PathType
    )

    $basePath = (Get-Location).Path
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $basePath $Path }
    $fullPath = [System.IO.Path]::GetFullPath($candidate)

    if (-not (Test-Path -LiteralPath $fullPath -PathType $PathType)) {
        throw "$Description wurde nicht gefunden: $fullPath"
    }

    return $fullPath
}

function Assert-RequiredVariable {
    param([Parameter(Mandatory = $true)][string]$Name)

    $variable = Get-Variable -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $variable -or [string]::IsNullOrWhiteSpace([string]$variable.Value)) {
        throw "Die Config-Datei muss `$${Name} setzen."
    }
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)

    if (-not $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Sicherheitsabbruch: Pfad liegt nicht unterhalb von ${parentFull}: $childFull"
    }
}

function New-Sha256Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseDir,
        [Parameter(Mandatory = $true)][string[]]$FileNames
    )

    $manifestPath = Join-Path $ReleaseDir "manifest.sha256"
    $lines = foreach ($fileName in $FileNames) {
        $filePath = Join-Path $ReleaseDir $fileName
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "Datei fehlt fuer SHA256-Manifest: $fileName"
        }

        $hash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $fileName"
    }

    Set-Content -LiteralPath $manifestPath -Value $lines -Encoding ascii
}

function Split-FileForBundle {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$OutputDir,
        [Parameter(Mandatory = $true)][string]$BaseName,
        [Parameter(Mandatory = $true)][int64]$PartSizeBytes
    )

    $parts = New-Object System.Collections.Generic.List[string]
    $buffer = New-Object byte[] (4MB)
    $sourceStream = [System.IO.File]::Open($SourcePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)

    try {
        $partNumber = 1
        while ($sourceStream.Position -lt $sourceStream.Length) {
            $partName = "{0}.part{1:D3}" -f $BaseName, $partNumber
            $partPath = Join-Path $OutputDir $partName
            $partStream = [System.IO.File]::Open($partPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)

            try {
                [int64]$remainingForPart = [Math]::Min($PartSizeBytes, $sourceStream.Length - $sourceStream.Position)
                while ($remainingForPart -gt 0) {
                    $readSize = [int][Math]::Min([int64]$buffer.Length, $remainingForPart)
                    $bytesRead = $sourceStream.Read($buffer, 0, $readSize)
                    if ($bytesRead -le 0) {
                        break
                    }

                    $partStream.Write($buffer, 0, $bytesRead)
                    $remainingForPart -= $bytesRead
                }
            }
            finally {
                $partStream.Dispose()
            }

            $parts.Add($partName)
            $partNumber++
        }
    }
    finally {
        $sourceStream.Dispose()
    }

    return [string[]]$parts.ToArray()
}

function Add-FileToZipBundle {
    param(
        [Parameter(Mandatory = $true)][System.IO.Compression.ZipArchive]$ZipArchive,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$EntryName,
        [Parameter(Mandatory = $true)][System.IO.Compression.CompressionLevel]$CompressionLevel
    )

    $zipEntryName = $EntryName.Replace("\", "/")
    $entry = $ZipArchive.CreateEntry($zipEntryName, $CompressionLevel)
    $entryStream = $entry.Open()
    $sourceStream = [System.IO.File]::Open($SourcePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)

    try {
        $sourceStream.CopyTo($entryStream, 4MB)
    }
    finally {
        $sourceStream.Dispose()
        $entryStream.Dispose()
    }
}

function New-ZipBundle {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseDir,
        [Parameter(Mandatory = $true)][string]$BundlePath,
        [Parameter(Mandatory = $true)][string[]]$FileNames
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $zipStream = [System.IO.File]::Open($BundlePath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $zipArchive = New-Object System.IO.Compression.ZipArchive -ArgumentList $zipStream, ([System.IO.Compression.ZipArchiveMode]::Create), $false

    try {
        foreach ($fileName in $FileNames) {
            $filePath = Join-Path $ReleaseDir $fileName
            if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
                throw "Datei fehlt fuer ZIP-Bundle: $fileName"
            }

            $compressionLevel = if ($fileName -like "images.tar*") {
                [System.IO.Compression.CompressionLevel]::NoCompression
            }
            else {
                [System.IO.Compression.CompressionLevel]::Optimal
            }

            Write-Log "Fuege zum ZIP hinzu: $fileName"
            Add-FileToZipBundle `
                -ZipArchive $zipArchive `
                -SourcePath $filePath `
                -EntryName $fileName `
                -CompressionLevel $compressionLevel
        }
    }
    finally {
        $zipArchive.Dispose()
        $zipStream.Dispose()
    }
}

Write-Log "Pruefe Docker CLI."
& docker version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker ist nicht verfuegbar oder laeuft nicht. Bitte 'docker version' pruefen."
}

Write-Log "Pruefe Docker Compose."
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose ist nicht ueber 'docker compose' verfuegbar."
}

$configPath = Resolve-ExistingPath -Path $Config -Description "Config-Datei" -PathType Leaf
Write-Log "Lade Config: $configPath"
. $configPath

@(
    "AppName",
    "AppVersion",
    "DockerImageName",
    "Dockerfile",
    "DockerContext",
    "DockerPlatform",
    "ComposeFile",
    "ServerBaseDir",
    "HostPort",
    "ContainerPort"
) | ForEach-Object { Assert-RequiredVariable -Name $_ }

if ($AppVersion -ieq "latest" -or $DockerImageName -imatch ":latest$") {
    throw "Dieses Deployment verwendet keine latest-Tags. Bitte eine feste AppVersion setzen."
}

if ($AppName -cne $AppName.ToLowerInvariant() -or $AppName -notmatch "^[a-z0-9][a-z0-9_-]*$") {
    throw "AppName muss fuer Docker Compose klein geschrieben sein und nur a-z, 0-9, '_' oder '-' enthalten: $AppName"
}

if ($DockerImageName -cne $DockerImageName.ToLowerInvariant()) {
    throw "DockerImageName muss klein geschrieben sein, weil Docker Image-Namen keine Grossbuchstaben erlauben: $DockerImageName"
}

$invalidFileNameChars = [System.IO.Path]::GetInvalidFileNameChars()
if ($AppName.IndexOfAny($invalidFileNameChars) -ge 0) {
    throw "AppName enthaelt ungueltige Zeichen fuer Windows-Verzeichnisse: $AppName"
}
if ($AppVersion.IndexOfAny($invalidFileNameChars) -ge 0) {
    throw "AppVersion enthaelt ungueltige Zeichen fuer Windows-Verzeichnisse: $AppVersion"
}

$dockerfilePath = Resolve-ExistingPath -Path $Dockerfile -Description "Dockerfile" -PathType Leaf
$dockerContextPath = Resolve-ExistingPath -Path $DockerContext -Description "Docker Build Context" -PathType Container
$composeFilePath = Resolve-ExistingPath -Path $ComposeFile -Description "Compose-Datei" -PathType Leaf
$imageTag = "${DockerImageName}:${AppVersion}"

if ($imageTag -imatch ":latest$") {
    throw "Dieses Deployment verwendet keine latest-Tags. Ermittelter Tag waere: $imageTag"
}

$distRoot = Join-Path (Get-Location).Path "dist"
$releaseName = "${AppName}-${AppVersion}"
$releaseDir = Join-Path $distRoot $releaseName
$bundlePath = Join-Path $distRoot "${releaseName}.bundle.zip"

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
Assert-ChildPath -Child $releaseDir -Parent $distRoot
Assert-ChildPath -Child $bundlePath -Parent $distRoot

if (Test-Path -LiteralPath $releaseDir) {
    Write-Log "Entferne vorhandenes lokales Release-Verzeichnis: $releaseDir"
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

if (Test-Path -LiteralPath $bundlePath) {
    Write-Log "Entferne vorhandenes Bundle: $bundlePath"
    Remove-Item -LiteralPath $bundlePath -Force
}

Invoke-CheckedCommand `
    -Description "Baue Docker Image $imageTag fuer $DockerPlatform." `
    -FilePath "docker" `
    -Arguments @("build", "--platform", $DockerPlatform, "-t", $imageTag, "-f", $dockerfilePath, $dockerContextPath)

$imagesTarPath = Join-Path $releaseDir "images.tar"
Invoke-CheckedCommand `
    -Description "Speichere Docker Image in images.tar." `
    -FilePath "docker" `
    -Arguments @("image", "save", "-o", $imagesTarPath, $imageTag)

$imagesTarInfo = Get-Item -LiteralPath $imagesTarPath
$imagesTarHash = (Get-FileHash -LiteralPath $imagesTarPath -Algorithm SHA256).Hash.ToLowerInvariant()
$maxZipEntryBytes = 1900MB
$imageArchiveMode = "file"
$imageArchiveParts = @()
$imageArchivePartSizeBytes = 0

if ($imagesTarInfo.Length -gt $maxZipEntryBytes) {
    $imageArchiveMode = "split"
    $imageArchivePartSizeBytes = 1GB
    Write-Log ("images.tar ist {0:N2} GB gross und wird fuer das ZIP in {1:N2} GB Teile gesplittet." -f ($imagesTarInfo.Length / 1GB), ($imageArchivePartSizeBytes / 1GB))
    $imageArchiveParts = Split-FileForBundle `
        -SourcePath $imagesTarPath `
        -OutputDir $releaseDir `
        -BaseName "images.tar" `
        -PartSizeBytes $imageArchivePartSizeBytes
}

Copy-Item -LiteralPath $composeFilePath -Destination (Join-Path $releaseDir "compose.yaml") -Force

$envLines = @(
    "APP_NAME=$AppName",
    "APP_VERSION=$AppVersion",
    "DOCKER_IMAGE_NAME=$DockerImageName",
    "HOST_PORT=$HostPort",
    "CONTAINER_PORT=$ContainerPort"
)
Set-Content -LiteralPath (Join-Path $releaseDir ".env") -Value $envLines -Encoding ascii
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION") -Value $AppVersion -Encoding ascii

$deployManifest = [ordered]@{
    AppName = $AppName
    AppVersion = $AppVersion
    DockerImageName = $DockerImageName
    DockerPlatform = $DockerPlatform
    BuildTimestamp = (Get-Date).ToUniversalTime().ToString("o")
    ServerBaseDir = $ServerBaseDir
    ImageArchiveMode = $imageArchiveMode
    ImagesTarSha256 = $imagesTarHash
    ImageArchiveParts = $imageArchiveParts
    ImageArchivePartSizeBytes = $imageArchivePartSizeBytes
}
$deployManifest |
    ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $releaseDir "deploy-manifest.json") -Encoding utf8

$bundleFileNames = if ($imageArchiveMode -eq "split") {
    @($imageArchiveParts + @("compose.yaml", ".env", "VERSION", "deploy-manifest.json"))
}
else {
    @("images.tar", "compose.yaml", ".env", "VERSION", "deploy-manifest.json")
}

New-Sha256Manifest `
    -ReleaseDir $releaseDir `
    -FileNames $bundleFileNames

Write-Log "Erstelle ZIP-Bundle."
New-ZipBundle `
    -ReleaseDir $releaseDir `
    -BundlePath $bundlePath `
    -FileNames @($bundleFileNames + @("manifest.sha256"))

Write-Log "Release-Bundle fertig: $bundlePath"
Write-Output $bundlePath
