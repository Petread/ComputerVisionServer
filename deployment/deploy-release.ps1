[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BundlePath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:LogFile = $null
$script:LockFile = $null
$script:LockAcquired = $false
$script:TempDir = $null
$script:BundleFullPath = $null
$script:FailedDir = $null

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if (-not [string]::IsNullOrWhiteSpace($script:LogFile)) {
        Add-Content -LiteralPath $script:LogFile -Value $line -Encoding utf8
    }
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Log $Description
    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE

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

function Get-RequiredJsonValue {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not ($Object.PSObject.Properties.Name -contains $Name)) {
        throw "deploy-manifest.json enthaelt keinen Wert fuer '$Name'."
    }

    $value = [string]$Object.$Name
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "deploy-manifest.json enthaelt einen leeren Wert fuer '$Name'."
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

function Join-ImageArchiveParts {
    param(
        [Parameter(Mandatory = $true)][string]$RootDir,
        [Parameter(Mandatory = $true)][string[]]$PartNames,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [string]$ExpectedSha256
    )

    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }

    $buffer = New-Object byte[] (4MB)
    $outputStream = [System.IO.File]::Open($OutputPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)

    try {
        foreach ($partName in $PartNames) {
            if ([System.IO.Path]::IsPathRooted($partName) -or $partName -match '(^|[\\/])\.\.([\\/]|$)') {
                throw "Ungueltiger relativer Image-Teilpfad in deploy-manifest.json: $partName"
            }

            $partPath = Join-Path $RootDir $partName
            if (-not (Test-Path -LiteralPath $partPath -PathType Leaf)) {
                throw "Image-Teil fehlt im Bundle: $partName"
            }

            Write-Log "Fuege Image-Teil zusammen: $partName"
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

    if (-not [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
        $actualSha256 = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
            throw "SHA256-Pruefsumme der zusammengesetzten images.tar stimmt nicht."
        }
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

function Test-ChecksumManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RootDir,
        [Parameter(Mandatory = $true)][string[]]$ExpectedFileNames
    )

    $manifestPath = Join-Path $RootDir "manifest.sha256"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "manifest.sha256 fehlt im Bundle."
    }

    $seenFiles = @{}
    $lines = Get-Content -LiteralPath $manifestPath
    foreach ($line in $lines) {
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

        $filePath = Join-Path $RootDir $relativePath
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "Datei aus manifest.sha256 fehlt: $relativePath"
        }

        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "SHA256-Pruefsumme stimmt nicht fuer $relativePath."
        }

        $seenFiles[$relativePath.Replace("/", "\").ToLowerInvariant()] = $true
    }

    foreach ($expectedFileName in $ExpectedFileNames) {
        $key = $expectedFileName.Replace("/", "\").ToLowerInvariant()
        if (-not $seenFiles.ContainsKey($key)) {
            throw "manifest.sha256 enthaelt keinen Eintrag fuer: $expectedFileName"
        }
    }
}

function Acquire-DeployLock {
    param([Parameter(Mandatory = $true)][string]$LockFile)

    $stream = $null
    try {
        $stream = [System.IO.File]::Open($LockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $writer = New-Object System.IO.StreamWriter($stream)
        $writer.WriteLine("CreatedAt={0}" -f (Get-Date).ToString("o"))
        $writer.WriteLine("ProcessId={0}" -f $PID)
        $writer.Flush()
        $writer.Dispose()
        $script:LockAcquired = $true
        Write-Log "Deployment-Lock gesetzt: $LockFile"
    }
    catch [System.IO.IOException] {
        throw "Deployment abgebrochen: Lock-Datei existiert bereits: $LockFile"
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Move-BundleToDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$TargetDir
    )

    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        Write-Log "Bundle kann nicht verschoben werden, weil es nicht mehr existiert: $SourcePath"
        return
    }

    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    $fileName = Split-Path -Leaf $SourcePath
    $destinationPath = Join-Path $TargetDir $fileName

    if (Test-Path -LiteralPath $destinationPath) {
        $baseName = [System.IO.Path]::GetFileNameWithoutExtension($fileName)
        $extension = [System.IO.Path]::GetExtension($fileName)
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $destinationPath = Join-Path $TargetDir "${baseName}-${stamp}${extension}"
    }

    Move-Item -LiteralPath $SourcePath -Destination $destinationPath -Force
    Write-Log "Bundle verschoben nach: $destinationPath"
}

try {
    $candidateBundle = if ([System.IO.Path]::IsPathRooted($BundlePath)) { $BundlePath } else { Join-Path (Get-Location).Path $BundlePath }
    $script:BundleFullPath = [System.IO.Path]::GetFullPath($candidateBundle)

    if (-not (Test-Path -LiteralPath $script:BundleFullPath -PathType Leaf)) {
        throw "Bundle wurde nicht gefunden: $script:BundleFullPath"
    }

    $script:TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("offline-deploy-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:TempDir -Force | Out-Null

    Write-Log "Entpacke Bundle in temporaeres Verzeichnis: $script:TempDir"
    Expand-Archive -LiteralPath $script:BundleFullPath -DestinationPath $script:TempDir -Force

    $deployManifestPath = Join-Path $script:TempDir "deploy-manifest.json"
    if (-not (Test-Path -LiteralPath $deployManifestPath -PathType Leaf)) {
        throw "deploy-manifest.json fehlt im Bundle."
    }

    $deployManifest = Get-Content -LiteralPath $deployManifestPath -Raw | ConvertFrom-Json
    $appName = Get-RequiredJsonValue -Object $deployManifest -Name "AppName"
    $appVersion = Get-RequiredJsonValue -Object $deployManifest -Name "AppVersion"
    $serverBaseDir = Get-RequiredJsonValue -Object $deployManifest -Name "ServerBaseDir"
    $imageArchiveMode = [string](Get-OptionalJsonValue -Object $deployManifest -Name "ImageArchiveMode" -DefaultValue "file")
    $imagesTarSha256 = [string](Get-OptionalJsonValue -Object $deployManifest -Name "ImagesTarSha256" -DefaultValue "")
    $imageArchiveParts = @(
        Get-OptionalJsonValue -Object $deployManifest -Name "ImageArchiveParts" -DefaultValue @()
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | ForEach-Object { [string]$_ }

    if ($imageArchiveMode -notin @("file", "split")) {
        throw "Ungueltiger ImageArchiveMode in deploy-manifest.json: $imageArchiveMode"
    }

    if (-not [System.IO.Path]::IsPathRooted($serverBaseDir)) {
        throw "ServerBaseDir muss ein absoluter Windows-Pfad sein: $serverBaseDir"
    }
    if ($appName.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "AppName enthaelt ungueltige Zeichen fuer Windows-Verzeichnisse: $appName"
    }
    if ($appVersion.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "AppVersion enthaelt ungueltige Zeichen fuer Windows-Verzeichnisse: $appVersion"
    }

    $appDir = Join-Path $serverBaseDir $appName
    $incomingDir = Join-Path $appDir "incoming"
    $releasesDir = Join-Path $appDir "releases"
    $currentDir = Join-Path $appDir "current"
    $deployedDir = Join-Path $appDir "deployed"
    $script:FailedDir = Join-Path $appDir "failed"
    $logsDir = Join-Path $appDir "logs"

    foreach ($dir in @($incomingDir, $releasesDir, $currentDir, $deployedDir, $script:FailedDir, $logsDir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $script:LogFile = Join-Path $logsDir ("deploy-{0}.log" -f (Get-Date -Format "yyyy-MM-dd-HHmmss"))
    New-Item -ItemType File -Path $script:LogFile -Force | Out-Null
    Write-Log "Starte Deployment fuer $appName $appVersion."

    $script:LockFile = Join-Path $appDir "deploy.lock"
    Acquire-DeployLock -LockFile $script:LockFile

    Invoke-LoggedCommand -Description "Pruefe Docker CLI." -FilePath "docker" -Arguments @("version")
    Invoke-LoggedCommand -Description "Pruefe Docker Compose." -FilePath "docker" -Arguments @("compose", "version")

    $commonChecksummedFiles = @("compose.yaml", ".env", "VERSION", "deploy-manifest.json")
    $checksummedFiles = if ($imageArchiveMode -eq "split") {
        if ($imageArchiveParts.Count -eq 0) {
            throw "deploy-manifest.json meldet ImageArchiveMode 'split', aber keine ImageArchiveParts."
        }
        @($imageArchiveParts + $commonChecksummedFiles)
    }
    else {
        @("images.tar") + $commonChecksummedFiles
    }

    foreach ($requiredFile in @($checksummedFiles + @("manifest.sha256"))) {
        $path = Join-Path $script:TempDir $requiredFile
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Bundle ist unvollstaendig. Datei fehlt: $requiredFile"
        }
    }

    Write-Log "Pruefe SHA256-Pruefsummen."
    Test-ChecksumManifest -RootDir $script:TempDir -ExpectedFileNames $checksummedFiles

    if ($imageArchiveMode -eq "split") {
        Write-Log "Setze gesplittete images.tar wieder zusammen."
        Join-ImageArchiveParts `
            -RootDir $script:TempDir `
            -PartNames $imageArchiveParts `
            -OutputPath (Join-Path $script:TempDir "images.tar") `
            -ExpectedSha256 $imagesTarSha256

        foreach ($partName in $imageArchiveParts) {
            Remove-Item -LiteralPath (Join-Path $script:TempDir $partName) -Force
        }

        New-Sha256Manifest `
            -ReleaseDir $script:TempDir `
            -FileNames @("images.tar", "compose.yaml", ".env", "VERSION", "deploy-manifest.json")
    }
    elseif (-not [string]::IsNullOrWhiteSpace($imagesTarSha256)) {
        $actualImagesTarSha256 = (Get-FileHash -LiteralPath (Join-Path $script:TempDir "images.tar") -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualImagesTarSha256 -ne $imagesTarSha256.ToLowerInvariant()) {
            throw "SHA256-Pruefsumme von images.tar stimmt nicht."
        }
    }

    $releaseDir = Join-Path $releasesDir $appVersion
    Write-Log "Schreibe Release nach: $releaseDir"
    Reset-Directory -Path $releaseDir -AllowedParent $releasesDir
    Copy-DirectoryContents -SourceDir $script:TempDir -DestinationDir $releaseDir

    Invoke-LoggedCommand `
        -Description "Lade Docker Images aus images.tar." `
        -FilePath "docker" `
        -Arguments @("image", "load", "-i", (Join-Path $releaseDir "images.tar"))

    Write-Log "Aktualisiere current-Verzeichnis."
    Reset-Directory -Path $currentDir -AllowedParent $appDir
    Copy-DirectoryContents -SourceDir $releaseDir -DestinationDir $currentDir

    Push-Location $currentDir
    try {
        Invoke-LoggedCommand `
            -Description "Starte Anwendung mit Docker Compose." `
            -FilePath "docker" `
            -Arguments @("compose", "--env-file", ".env", "-f", "compose.yaml", "up", "-d", "--remove-orphans")

        Invoke-LoggedCommand `
            -Description "Zeige Docker Compose Status." `
            -FilePath "docker" `
            -Arguments @("compose", "--env-file", ".env", "-f", "compose.yaml", "ps")
    }
    finally {
        Pop-Location
    }

    Move-BundleToDirectory -SourcePath $script:BundleFullPath -TargetDir $deployedDir
    Write-Log "Deployment erfolgreich abgeschlossen: $appName $appVersion"
}
catch {
    Write-Log ("FEHLER: {0}" -f $_.Exception.Message)

    if (-not [string]::IsNullOrWhiteSpace($script:FailedDir) -and -not [string]::IsNullOrWhiteSpace($script:BundleFullPath)) {
        try {
            Move-BundleToDirectory -SourcePath $script:BundleFullPath -TargetDir $script:FailedDir
        }
        catch {
            Write-Log ("Bundle konnte nicht nach failed verschoben werden: {0}" -f $_.Exception.Message)
        }
    }

    exit 1
}
finally {
    if ($script:LockAcquired -and -not [string]::IsNullOrWhiteSpace($script:LockFile) -and (Test-Path -LiteralPath $script:LockFile)) {
        Remove-Item -LiteralPath $script:LockFile -Force
        Write-Log "Deployment-Lock entfernt: $script:LockFile"
    }

    if (-not [string]::IsNullOrWhiteSpace($script:TempDir) -and (Test-Path -LiteralPath $script:TempDir)) {
        Remove-Item -LiteralPath $script:TempDir -Recurse -Force
    }
}
