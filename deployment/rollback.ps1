[CmdletBinding(DefaultParameterSetName = "Rollback")]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppName,

    [Parameter(Mandatory = $true, ParameterSetName = "Rollback")]
    [string]$Version,

    [Parameter(Mandatory = $true, ParameterSetName = "List")]
    [switch]$List,

    [string]$ServerBaseDir = "C:\DockerApps"
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

function Reset-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AllowedParent
    )

    Assert-ChildPath -Child $Path -Parent $AllowedParent
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$DestinationDir
    )

    Get-ChildItem -LiteralPath $SourceDir -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $DestinationDir -Recurse -Force
    }
}

try {
    if (-not [System.IO.Path]::IsPathRooted($ServerBaseDir)) {
        throw "ServerBaseDir muss ein absoluter Windows-Pfad sein: $ServerBaseDir"
    }

    $appDir = Join-Path $ServerBaseDir $AppName
    $releasesDir = Join-Path $appDir "releases"
    $currentDir = Join-Path $appDir "current"

    if ($List) {
        if (-not (Test-Path -LiteralPath $releasesDir -PathType Container)) {
            Write-Log "Keine Releases gefunden: $releasesDir"
            exit 0
        }

        Write-Log "Verfuegbare Releases fuer ${AppName}:"
        Get-ChildItem -LiteralPath $releasesDir -Directory |
            Sort-Object Name |
            ForEach-Object { Write-Host $_.Name }
        exit 0
    }

    $releaseDir = Join-Path $releasesDir $Version
    if (-not (Test-Path -LiteralPath $releaseDir -PathType Container)) {
        throw "Release existiert nicht: $releaseDir"
    }

    Invoke-CheckedCommand -Description "Pruefe Docker CLI." -FilePath "docker" -Arguments @("version")
    Invoke-CheckedCommand -Description "Pruefe Docker Compose." -FilePath "docker" -Arguments @("compose", "version")

    Write-Log "Kopiere Release $Version nach current."
    Reset-Directory -Path $currentDir -AllowedParent $appDir
    Copy-DirectoryContents -SourceDir $releaseDir -DestinationDir $currentDir

    $imagesTar = Join-Path $currentDir "images.tar"
    if (Test-Path -LiteralPath $imagesTar -PathType Leaf) {
        Invoke-CheckedCommand `
            -Description "Lade Docker Images aus images.tar." `
            -FilePath "docker" `
            -Arguments @("image", "load", "-i", $imagesTar)
    }

    Push-Location $currentDir
    try {
        Invoke-CheckedCommand `
            -Description "Starte Rollback-Version mit Docker Compose." `
            -FilePath "docker" `
            -Arguments @("compose", "--env-file", ".env", "-f", "compose.yaml", "up", "-d", "--remove-orphans")

        Invoke-CheckedCommand `
            -Description "Zeige Docker Compose Status." `
            -FilePath "docker" `
            -Arguments @("compose", "--env-file", ".env", "-f", "compose.yaml", "ps")
    }
    finally {
        Pop-Location
    }

    Write-Log "Rollback erfolgreich abgeschlossen: $AppName $Version"
}
catch {
    Write-Error ("Rollback fehlgeschlagen: {0}" -f $_.Exception.Message)
    exit 1
}
