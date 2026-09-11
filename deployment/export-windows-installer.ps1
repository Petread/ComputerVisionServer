[CmdletBinding()]
param(
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
trap {
    Write-Host ("FEHLER: {0}" -f $_.Exception.Message)
    exit 1
}

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = (Get-Location).Path,
        [switch]$ForwardOutputAsHost
    )

    Write-Log $Description
    Push-Location $WorkingDirectory
    try {
        if ($ForwardOutputAsHost) {
            $stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-export-{0}.out" -f ([guid]::NewGuid().ToString("N")))
            $stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-export-{0}.err" -f ([guid]::NewGuid().ToString("N")))
            try {
                $process = Start-Process `
                    -FilePath $FilePath `
                    -ArgumentList $Arguments `
                    -WorkingDirectory $WorkingDirectory `
                    -NoNewWindow `
                    -Wait `
                    -PassThru `
                    -RedirectStandardOutput $stdoutPath `
                    -RedirectStandardError $stderrPath
                if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
                    Get-Content -LiteralPath $stdoutPath | ForEach-Object { Write-Host ([string]$_) }
                }
                if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
                    Get-Content -LiteralPath $stderrPath | ForEach-Object { Write-Host ([string]$_) }
                }
                if ($process.ExitCode -ne 0) {
                    throw "$Description fehlgeschlagen. Exit Code: $($process.ExitCode)"
                }
            }
            finally {
                Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
            }
        }
        else {
            & $FilePath @Arguments
            if ($LASTEXITCODE -ne 0) {
                throw "$Description fehlgeschlagen. Exit Code: $LASTEXITCODE"
            }
        }
    }
    finally {
        Pop-Location
    }
}

function Resolve-ExecutableCommand {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            $source = if ($command.Source) { $command.Source } else { $command.Path }
            if (-not [string]::IsNullOrWhiteSpace([string]$source) -and $source -notlike "*.ps1") {
                return [string]$source
            }
        }
    }
    return ""
}

function Get-PnpmPackageSpec {
    $packageJsonPath = Join-Path (Get-Location).Path "package.json"
    if (Test-Path -LiteralPath $packageJsonPath -PathType Leaf) {
        $packageJson = Get-Content -LiteralPath $packageJsonPath -Raw | ConvertFrom-Json
        if ($packageJson.PSObject.Properties.Name -contains "packageManager" -and [string]$packageJson.packageManager -match "^pnpm@") {
            return [string]$packageJson.packageManager
        }
    }
    return "pnpm@10.10.0"
}

function Invoke-FrontendBuild {
    $pnpmPath = Resolve-ExecutableCommand -Names @("pnpm.cmd", "pnpm.exe", "pnpm")
    if (-not [string]::IsNullOrWhiteSpace($pnpmPath)) {
        Invoke-CheckedCommand -Description "Baue Admin-Frontend." -FilePath $pnpmPath -Arguments @("build:admin-web") -WorkingDirectory (Get-Location).Path
        return
    }

    $npmPath = Resolve-ExecutableCommand -Names @("npm.cmd", "npm.exe", "npm")
    if (-not [string]::IsNullOrWhiteSpace($npmPath)) {
        $pnpmPackageSpec = Get-PnpmPackageSpec
        Invoke-CheckedCommand -Description "Baue Admin-Frontend ueber npm exec $pnpmPackageSpec." -FilePath $npmPath -Arguments @("exec", "--yes", "--package", $pnpmPackageSpec, "--", "pnpm", "build:admin-web") -WorkingDirectory (Get-Location).Path
        return
    }

    $corepackPath = Resolve-ExecutableCommand -Names @("corepack.cmd", "corepack.exe", "corepack")
    if (-not [string]::IsNullOrWhiteSpace($corepackPath)) {
        Invoke-CheckedCommand -Description "Baue Admin-Frontend ueber Corepack/pnpm." -FilePath $corepackPath -Arguments @("pnpm", "build:admin-web") -WorkingDirectory (Get-Location).Path
        return
    }

    throw "Weder pnpm, npm noch corepack wurde gefunden. Bitte Node.js installieren oder pnpm verfuegbar machen."
}

function Resolve-PathForExport {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("Leaf", "Container")][string]$PathType,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path (Get-Location).Path $Path }
    $fullPath = [System.IO.Path]::GetFullPath($candidate)
    if (-not (Test-Path -LiteralPath $fullPath -PathType $PathType)) {
        throw "$Description wurde nicht gefunden: $fullPath"
    }
    return $fullPath
}

function New-SafeFileName {
    param([Parameter(Mandatory = $true)][string]$Value)

    return ($Value -replace '[^A-Za-z0-9_.-]', '_')
}

function Resolve-OutputPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDir
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $BaseDir $Path }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Get-MediaMtxDownloadUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [AllowEmptyString()][string]$DownloadUrl
    )

    if (-not [string]::IsNullOrWhiteSpace($DownloadUrl)) {
        return $DownloadUrl
    }
    $tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
    return "https://github.com/bluenviron/mediamtx/releases/download/$tag/mediamtx_${tag}_windows_amd64.zip"
}

function New-MediaMtxArchiveFromRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [AllowEmptyString()][string]$DownloadUrl
    )

    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
    $tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
    $archivePath = Join-Path $CacheDir ("mediamtx-{0}-windows-amd64.zip" -f (New-SafeFileName -Value $tag))
    if ((Test-Path -LiteralPath $archivePath -PathType Leaf) -and ((Get-Item -LiteralPath $archivePath).Length -gt 0)) {
        Write-Log "Verwende gecachtes MediaMTX Archiv: $archivePath"
        return $archivePath
    }

    $url = Get-MediaMtxDownloadUrl -Version $Version -DownloadUrl $DownloadUrl
    $tmpArchivePath = "$archivePath.tmp.zip"
    $tmpExtractDir = Join-Path $CacheDir ("mediamtx-verify-" + [System.Guid]::NewGuid().ToString("N"))
    Remove-Item -LiteralPath $tmpArchivePath -Force -ErrorAction SilentlyContinue
    Write-Log "Lade MediaMTX Archiv herunter: $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmpArchivePath -UseBasicParsing
        New-Item -ItemType Directory -Path $tmpExtractDir -Force | Out-Null
        Expand-Archive -LiteralPath $tmpArchivePath -DestinationPath $tmpExtractDir -Force
        $binary = Get-ChildItem -LiteralPath $tmpExtractDir -Recurse -Filter "mediamtx.exe" -File | Select-Object -First 1
        if ($null -eq $binary) {
            throw "MediaMTX Archiv enthaelt keine Windows-Binary 'mediamtx.exe'."
        }
        Move-Item -LiteralPath $tmpArchivePath -Destination $archivePath -Force
    }
    catch {
        Remove-Item -LiteralPath $tmpArchivePath -Force -ErrorAction SilentlyContinue
        throw
    }
    finally {
        Remove-Item -LiteralPath $tmpExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Log "MediaMTX Archiv erstellt: $archivePath"
    return $archivePath
}

function Resolve-MediaMtxArchiveForExport {
    param(
        [AllowEmptyString()][string]$ArchivePath,
        [bool]$SkipMediaMtxCheck,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [AllowEmptyString()][string]$DownloadUrl
    )

    if (-not [string]::IsNullOrWhiteSpace($ArchivePath)) {
        return Resolve-PathForExport -Path $ArchivePath -PathType Leaf -Description "MediaMtxArchivePath"
    }
    if ($SkipMediaMtxCheck) {
        return ""
    }
    return New-MediaMtxArchiveFromRelease -Version $Version -CacheDir $CacheDir -DownloadUrl $DownloadUrl
}

function New-GStreamerDebBundleForExport {
    param(
        [Parameter(Mandatory = $true)][string]$DistroName,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [string[]]$PackageNames = @()
    )

    if ([string]::IsNullOrWhiteSpace($DistroName)) {
        throw "GStreamerDebBundleDistroName/RuntimeDistroName wurde nicht gesetzt; Debian-Paketbundle kann nicht erzeugt werden."
    }
    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
    $archivePath = Join-Path $CacheDir ("gstreamer-debs-v4-{0}.tar.gz" -f (New-SafeFileName -Value $DistroName))
    if ((Test-Path -LiteralPath $archivePath -PathType Leaf) -and ((Get-Item -LiteralPath $archivePath).Length -gt 0)) {
        Write-Log "Verwende gecachtes GStreamer Debian-Paketbundle: $archivePath"
        return $archivePath
    }

    $generatorPath = Join-Path (Get-Location).Path "deployment\create-gstreamer-deb-bundle.ps1"
    if (-not (Test-Path -LiteralPath $generatorPath -PathType Leaf)) {
        throw "GStreamer Bundle-Generator fehlt: $generatorPath"
    }
    $packageListPath = Join-Path $CacheDir ("gstreamer-packages-{0}.txt" -f (New-SafeFileName -Value $DistroName))
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines(
        $packageListPath,
        [string[]]@($PackageNames | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique),
        $utf8NoBom
    )
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $generatorPath,
        "-DistroName",
        $DistroName,
        "-OutputPath",
        $archivePath,
        "-PackageListPath",
        $packageListPath
    )
    $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $powerShell -PathType Leaf)) {
        throw "Windows PowerShell wurde nicht gefunden: $powerShell"
    }
    Invoke-CheckedCommand -Description "Erzeuge GStreamer Debian-Paketbundle." -FilePath $powerShell -Arguments $arguments -ForwardOutputAsHost
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "GStreamer Debian-Paketbundle wurde nicht erzeugt: $archivePath"
    }
    return $archivePath
}

function Resolve-GStreamerDebBundleForExport {
    param(
        [AllowEmptyString()][string]$BundlePath,
        [bool]$SkipGStreamerCheck,
        [Parameter(Mandatory = $true)][string]$DistroName,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [string[]]$PackageNames = @()
    )

    if (-not [string]::IsNullOrWhiteSpace($BundlePath)) {
        return Resolve-PathForExport -Path $BundlePath -PathType Leaf -Description "GStreamerDebBundlePath"
    }
    if ($SkipGStreamerCheck) {
        return ""
    }
    return New-GStreamerDebBundleForExport -DistroName $DistroName -CacheDir $CacheDir -PackageNames $PackageNames
}

function Resolve-PythonForWheelDownload {
    $pyLauncher = Resolve-ExecutableCommand -Names @("py.exe", "py")
    if (-not [string]::IsNullOrWhiteSpace($pyLauncher)) {
        return [pscustomobject]@{
            FilePath = $pyLauncher
            Arguments = [string[]]@()
        }
    }
    $python = Resolve-ExecutableCommand -Names @("python.exe", "python", "python3")
    if (-not [string]::IsNullOrWhiteSpace($python)) {
        return [pscustomobject]@{
            FilePath = $python
            Arguments = [string[]]@()
        }
    }
    throw "Python wurde nicht gefunden. Installieren Sie Python mit pip auf dem Entwicklungsrechner oder setzen Sie PythonWheelBundlePath auf ein vorbereitetes Wheel-Bundle."
}

function New-PythonWheelBundleForExport {
    param(
        [Parameter(Mandatory = $true)][string]$CacheDir
    )

    $tarPath = Resolve-ExecutableCommand -Names @("tar.exe", "tar")
    if ([string]::IsNullOrWhiteSpace($tarPath)) {
        throw "tar wurde nicht gefunden. Setzen Sie PythonWheelBundlePath manuell oder stellen Sie Windows tar.exe bereit."
    }
    $resolvedCacheDir = Resolve-OutputPath -Path $CacheDir -BaseDir (Get-Location).Path
    New-Item -ItemType Directory -Path $resolvedCacheDir -Force | Out-Null
    $archivePath = Join-Path $resolvedCacheDir "python-wheels.tar.gz"
    if ((Test-Path -LiteralPath $archivePath -PathType Leaf) -and ((Get-Item -LiteralPath $archivePath).Length -gt 0)) {
        if (
            (Test-PythonWheelBundleContainsSnap7 -ArchivePath $archivePath) -and
            (Test-PythonWheelBundleContainsPypylon -ArchivePath $archivePath) -and
            (Test-PythonWheelBundleContainsDeploymentRuntime -ArchivePath $archivePath)
        ) {
            Write-Log "Verwende gecachtes Python-Wheel-Bundle: $archivePath"
            return $archivePath
        }
        Write-Log "Ignoriere gecachtes Python-Wheel-Bundle ohne pypylon oder vollstaendige Offline-Runtime-Wheels: $archivePath"
        Remove-Item -LiteralPath $archivePath -Force
    }

    $stagingDir = Join-Path $resolvedCacheDir "python-wheels-staging"
    if (Test-Path -LiteralPath $stagingDir -PathType Container) {
        Assert-ChildPath -Child $stagingDir -Parent $resolvedCacheDir
        Remove-Item -LiteralPath $stagingDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
    try {
        $serviceRequirements = [string[]]@(
            "python-snap7>=3,<4",
            "pypylon>=3.0.0,<27",
            "numpy>=1.26,<2",
            "opencv-python-headless>=4.10.0,<5"
        )
        $deploymentRequirements = [string[]]@(
            "mmdeploy==1.3.1",
            "tensorrt-cu12-bindings==10.3.0",
            "onnx==1.17.0",
            "onnxruntime==1.19.0",
            "numpy<2",
            "setuptools<81"
        )
        $requirements = [string[]]@($serviceRequirements + $deploymentRequirements | Select-Object -Unique)
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines((Join-Path $stagingDir "requirements.txt"), $serviceRequirements, $utf8NoBom)
        [System.IO.File]::WriteAllLines((Join-Path $stagingDir "deployment-requirements.txt"), $deploymentRequirements, $utf8NoBom)
        $python = Resolve-PythonForWheelDownload
        $downloadArgs = [string[]]@($python.Arguments) + [string[]]@(
            "-m",
            "pip",
            "download",
            "--dest",
            $stagingDir,
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_x86_64",
            "--platform",
            "manylinux_2_17_x86_64",
            "--platform",
            "manylinux_2_27_x86_64",
            "--platform",
            "manylinux_2_28_x86_64",
            "--platform",
            "manylinux_2_31_x86_64",
            "--implementation",
            "cp",
            "--python-version",
            "311",
            "--abi",
            "cp311"
        ) + $requirements
        Invoke-CheckedCommand `
            -Description "Lade Python-Wheels fuer Linux-Service-Runtime herunter." `
            -FilePath $python.FilePath `
            -Arguments $downloadArgs `
            -ForwardOutputAsHost
        if (-not (Get-ChildItem -LiteralPath $stagingDir -Filter "*.whl" -File)) {
            throw "Python-Wheel-Bundle enthaelt keine Wheels."
        }
        Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
        Invoke-CheckedCommand `
            -Description "Packe Python-Wheel-Bundle fuer Offline-Installer." `
            -FilePath $tarPath `
            -Arguments @("-C", $stagingDir, "-czf", $archivePath, ".") `
            -ForwardOutputAsHost
    }
    finally {
        if (Test-Path -LiteralPath $stagingDir -PathType Container) {
            Assert-ChildPath -Child $stagingDir -Parent $resolvedCacheDir
            Remove-Item -LiteralPath $stagingDir -Recurse -Force
        }
    }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "Python-Wheel-Bundle wurde nicht erzeugt: $archivePath"
    }
    return $archivePath
}

function Test-PythonWheelBundleContainsSnap7 {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $tarPath = Resolve-ExecutableCommand -Names @("tar.exe", "tar")
    if ([string]::IsNullOrWhiteSpace($tarPath)) {
        return $false
    }
    $entries = & $tarPath -tzf $ArchivePath 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])python[-_]snap7.*\.whl$' } | Select-Object -First 1)
}

function Test-PythonWheelBundleContainsPypylon {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $tarPath = Resolve-ExecutableCommand -Names @("tar.exe", "tar")
    if ([string]::IsNullOrWhiteSpace($tarPath)) {
        return $false
    }
    $entries = & $tarPath -tzf $ArchivePath 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])pypylon-.*\.whl$' } | Select-Object -First 1)
}

function Test-PythonWheelBundleContainsDeploymentRuntime {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $tarPath = Resolve-ExecutableCommand -Names @("tar.exe", "tar")
    if ([string]::IsNullOrWhiteSpace($tarPath)) {
        return $false
    }
    $entries = & $tarPath -tzf $ArchivePath 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    $hasDeploymentRequirements = [bool]($entries | Where-Object { $_ -match '(^|[/\\])deployment-requirements\.txt$' } | Select-Object -First 1)
    $hasMmdeploy = [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])mmdeploy-.*\.whl$' } | Select-Object -First 1)
    $hasTensorrtBindings = [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])tensorrt_cu12_bindings-.*\.whl$' } | Select-Object -First 1)
    $hasNumpy = [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])numpy-.*\.whl$' } | Select-Object -First 1)
    $hasOpenCv = [bool]($entries | Where-Object { $_ -match '(?i)(^|[/\\])opencv_python_headless-.*\.whl$' } | Select-Object -First 1)
    return $hasDeploymentRequirements -and $hasMmdeploy -and $hasTensorrtBindings -and $hasNumpy -and $hasOpenCv
}

function Resolve-PythonWheelBundleForExport {
    param(
        [AllowEmptyString()][string]$BundlePath,
        [Parameter(Mandatory = $true)][string]$CacheDir
    )

    if (-not [string]::IsNullOrWhiteSpace($BundlePath)) {
        $resolvedBundle = Resolve-PathForExport -Path $BundlePath -PathType Leaf -Description "PythonWheelBundlePath"
        if (-not (Test-PythonWheelBundleContainsSnap7 -ArchivePath $resolvedBundle)) {
            throw "PythonWheelBundlePath enthaelt kein python-snap7 Wheel: $resolvedBundle"
        }
        if (-not (Test-PythonWheelBundleContainsPypylon -ArchivePath $resolvedBundle)) {
            throw "PythonWheelBundlePath enthaelt kein pypylon Wheel fuer Basler Pylon Quellen: $resolvedBundle"
        }
        if (-not (Test-PythonWheelBundleContainsDeploymentRuntime -ArchivePath $resolvedBundle)) {
            throw "PythonWheelBundlePath enthaelt keine Deployment-Runtime-Wheels wie mmdeploy oder keine deployment-requirements.txt: $resolvedBundle"
        }
        return $resolvedBundle
    }
    $defaultArchive = Join-Path (Resolve-OutputPath -Path $CacheDir -BaseDir (Get-Location).Path) "python-wheels.tar.gz"
    if (Test-Path -LiteralPath $defaultArchive -PathType Leaf) {
        if (
            (Test-PythonWheelBundleContainsSnap7 -ArchivePath $defaultArchive) -and
            (Test-PythonWheelBundleContainsPypylon -ArchivePath $defaultArchive) -and
            (Test-PythonWheelBundleContainsDeploymentRuntime -ArchivePath $defaultArchive)
        ) {
            return $defaultArchive
        }
        Write-Log "Ignoriere gecachtes Python-Wheel-Bundle ohne pypylon oder vollstaendige Offline-Runtime-Wheels: $defaultArchive"
        Remove-Item -LiteralPath $defaultArchive -Force
    }
    return New-PythonWheelBundleForExport -CacheDir $CacheDir
}

function Assert-RequiredVariable {
    param([Parameter(Mandatory = $true)][string]$Name)

    $variable = Get-Variable -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $variable -or [string]::IsNullOrWhiteSpace([string]$variable.Value)) {
        throw "Die Config-Datei muss `$${Name} setzen."
    }
}

function Get-ConfigValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowNull()]$DefaultValue
    )

    $variable = Get-Variable -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $variable -or $null -eq $variable.Value) {
        return $DefaultValue
    }
    if ($variable.Value -is [string] -and [string]::IsNullOrWhiteSpace($variable.Value)) {
        return $DefaultValue
    }
    return $variable.Value
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

function New-Sha256Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseDir,
        [Parameter(Mandatory = $true)][string[]]$FileNames
    )

    $lines = foreach ($fileName in $FileNames) {
        $filePath = Join-Path $ReleaseDir $fileName
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "Datei fehlt fuer SHA256-Manifest: $fileName"
        }
        $hash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $fileName"
    }
    Set-Content -LiteralPath (Join-Path $ReleaseDir "manifest.sha256") -Value $lines -Encoding ascii
}

function Add-FileToZipBundle {
    param(
        [Parameter(Mandatory = $true)][System.IO.Compression.ZipArchive]$ZipArchive,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$EntryName,
        [Parameter(Mandatory = $true)][System.IO.Compression.CompressionLevel]$CompressionLevel
    )

    $entry = $ZipArchive.CreateEntry($EntryName.Replace("\", "/"), $CompressionLevel)
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
            $compressionLevel = if ($fileName -like "*.tar" -or $fileName -like "*.tar.gz" -or $fileName -like "*.part*") {
                [System.IO.Compression.CompressionLevel]::NoCompression
            }
            else {
                [System.IO.Compression.CompressionLevel]::Optimal
            }
            Write-Log "Fuege zum ZIP hinzu: $fileName"
            Add-FileToZipBundle -ZipArchive $zipArchive -SourcePath $filePath -EntryName $fileName -CompressionLevel $compressionLevel
        }
    }
    finally {
        $zipArchive.Dispose()
        $zipStream.Dispose()
    }
}

function Ensure-DeploymentUiExe {
    $deploymentDir = Join-Path (Get-Location).Path "deployment"
    $uiScript = Join-Path $deploymentDir "windows-deployment-ui.ps1"
    $uiBuilder = Join-Path $deploymentDir "build-windows-ui-exe.ps1"
    $uiExe = Join-Path $deploymentDir "YBB-Deployment-UI.exe"
    foreach ($path in @($uiScript, $uiBuilder)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Windows-UI-Datei fehlt: $path"
        }
    }
    $needsBuild = -not (Test-Path -LiteralPath $uiExe -PathType Leaf)
    if (-not $needsBuild) {
        $exeInfo = Get-Item -LiteralPath $uiExe
        foreach ($sourceInfo in @((Get-Item -LiteralPath $uiScript), (Get-Item -LiteralPath $uiBuilder))) {
            if ($sourceInfo.LastWriteTimeUtc -gt $exeInfo.LastWriteTimeUtc) {
                $needsBuild = $true
            }
        }
    }
    if ($needsBuild) {
        Write-Log "Erstelle Windows-UI-EXE."
        $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        Invoke-CheckedCommand -Description "Baue YBB-Deployment-UI.exe." -FilePath $powerShell -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $uiBuilder)
    }
    if (-not (Test-Path -LiteralPath $uiExe -PathType Leaf)) {
        throw "Windows-UI-EXE konnte nicht erstellt werden: $uiExe"
    }
}

function Set-FrontendBuildEnv {
    $script:PreviousFrontendEnv = @{}
    $map = @{
        VITE_ADMIN_API_URL = $FrontendAdminApiUrl
        VITE_RUNTIME_ADMIN_API_URL = $FrontendRuntimeAdminApiUrl
        VITE_RESULT_SERVICE_URL = $FrontendResultServiceUrl
        VITE_TRIGGER_EXECUTOR_URL = $FrontendTriggerExecutorUrl
        VITE_INFERENCE_GATEWAY_RT_URL = $FrontendInferenceGatewayRtUrl
        VITE_INFERENCE_GATEWAY_BATCH_URL = $FrontendInferenceGatewayBatchUrl
        VITE_ADMIN_WS_URL = $FrontendAdminWsUrl
        VITE_FRONTEND_BASE_PATH = $FrontendBasePath
    }
    foreach ($key in $map.Keys) {
        $script:PreviousFrontendEnv[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$map[$key], "Process")
    }
}

function Restore-FrontendBuildEnv {
    if ($null -eq $script:PreviousFrontendEnv) {
        return
    }
    foreach ($key in $script:PreviousFrontendEnv.Keys) {
        [Environment]::SetEnvironmentVariable($key, $script:PreviousFrontendEnv[$key], "Process")
    }
}

function New-AppArchive {
    param(
        [Parameter(Mandatory = $true)][string]$OutputPath
    )

    $rootExcludeNames = @(
        ".git",
        ".venv",
        "node_modules",
        "dist",
        ".runtime",
        ".local",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".test-tmp",
        ".tmp",
        "runs",
        "mlruns"
    )
    $rootExcludeSet = @{}
    foreach ($name in $rootExcludeNames) {
        $rootExcludeSet[$name.ToLowerInvariant()] = $true
    }

    $includeArgs = @(
        Get-ChildItem -LiteralPath (Get-Location).Path -Force |
            Where-Object { -not $rootExcludeSet.ContainsKey($_.Name.ToLowerInvariant()) } |
            ForEach-Object { "./$($_.Name)" }
    )
    if ($includeArgs.Count -eq 0) {
        throw "Es wurden keine Dateien fuer app.tar.gz gefunden."
    }

    $excludeArgs = @(
        "--exclude=node_modules",
        "--exclude=dist-demo",
        "--exclude=__pycache__",
        "--exclude=*.pyc",
        "--exclude=.venv",
        "--exclude=.runtime",
        "--exclude=.local",
        "--exclude=.pytest_cache",
        "--exclude=.ruff_cache",
        "--exclude=.mypy_cache",
        "--exclude=.test-tmp",
        "--exclude=.tmp",
        "--exclude=runs",
        "--exclude=mlruns",
        "--exclude=*.pt",
        "--exclude=*.pth",
        "--exclude=*.onnx",
        "--exclude=*.engine",
        "--exclude=*.log",
        "--exclude=*.db",
        "--exclude=*.sqlite",
        "--exclude=*.sqlite3"
    )

    $tarArgs = @("-czf", $OutputPath) + $excludeArgs + $includeArgs
    Invoke-CheckedCommand -Description "Packe Anwendungscode in app.tar.gz." -FilePath "tar" -Arguments $tarArgs -WorkingDirectory (Get-Location).Path
}

function Assert-FrontendBuildOutput {
    . (Join-Path $PSScriptRoot 'assert-frontend-workspace.ps1')
    Assert-ProductionWorkspaceBuild -DistPath (Join-Path (Get-Location).Path 'frontend\apps\admin-web\dist')
    $indexPath = Join-Path (Get-Location).Path "frontend\apps\admin-web\dist\index.html"
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        throw "Admin-Frontend wurde nicht gefunden: $indexPath. Bitte Frontend-Build pruefen."
    }

    $indexContent = Get-Content -LiteralPath $indexPath -Raw
    $assetNames = @(
        [regex]::Matches($indexContent, '(?:src|href)="/frontend/assets/(?<Name>[^"]+\.(?:css|js))"') |
            ForEach-Object { $_.Groups["Name"].Value } |
            Select-Object -Unique
    )
    if ($assetNames.Count -eq 0) {
        throw "Admin-Frontend enthaelt keine CSS/JS-Assets unter /frontend/assets/: $indexPath. FrontendBasePath muss /frontend/ sein."
    }

    $assetsDir = Join-Path (Split-Path -Parent $indexPath) "assets"
    foreach ($assetName in $assetNames) {
        $assetPath = Join-Path $assetsDir $assetName
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
            throw "Admin-Frontend-Asset fehlt: $assetPath"
        }
    }
}

function Assert-AppArchiveContainsFrontend {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $frontendEntry = "frontend/apps/admin-web/dist/index.html"
    $entries = & tar -tzf $ArchivePath
    if ($LASTEXITCODE -ne 0) {
        throw "app.tar.gz konnte nicht zur Pruefung gelesen werden. Exit Code: $LASTEXITCODE"
    }
    $hasFrontend = @($entries | ForEach-Object { $_.ToString().TrimStart("./") } | Where-Object { $_ -eq $frontendEntry }).Count -gt 0
    if (-not $hasFrontend) {
        throw "app.tar.gz enthaelt das Admin-Frontend nicht: $frontendEntry"
    }

    $entrySet = @{}
    foreach ($entry in $entries) {
        $entrySet[$entry.ToString().TrimStart("./")] = $true
    }

    $indexPath = Join-Path (Get-Location).Path "frontend\apps\admin-web\dist\index.html"
    $indexContent = Get-Content -LiteralPath $indexPath -Raw
    $assetNames = @(
        [regex]::Matches($indexContent, '(?:src|href)="/frontend/assets/(?<Name>[^"]+\.(?:css|js))"') |
            ForEach-Object { $_.Groups["Name"].Value } |
            Select-Object -Unique
    )
    if ($assetNames.Count -eq 0) {
        throw "app.tar.gz enthaelt kein fuer /frontend/ gebautes Admin-Frontend. FrontendBasePath muss /frontend/ sein."
    }
    foreach ($assetName in $assetNames) {
        $assetEntry = "frontend/apps/admin-web/dist/assets/$assetName"
        if (-not $entrySet.ContainsKey($assetEntry)) {
            throw "app.tar.gz enthaelt das Admin-Frontend-Asset nicht: $assetEntry"
        }
    }
}

function Assert-TemporalCompatibilitySources {
    $checks = @(
        @{ Path = "backend\packages\cv-status\src\cv_status\enums.py"; Tokens = @('TEMPORAL_SEQUENCE = "temporal_sequence"') },
        @{ Path = "backend\packages\cv-contracts\src\cv_contracts\dataset.py"; Tokens = @("class LabelProjectProvisionRequest") },
        @{ Path = "backend\packages\cv-datasets\src\cv_datasets\models.py"; Tokens = @("class CanonicalSequenceItem") },
        @{ Path = "backend\packages\cv-datasets\src\cv_datasets\annotation_schemas.py"; Tokens = @('schema_id="stem_orientation_angle.v1"', '<Video name="video"', "<TimelineLabels") },
        @{ Path = "backend\packages\cv-datasets\src\cv_datasets\temporal_import.py"; Tokens = @("def build_temporal_label_studio_task", "def temporal_import_idempotency_key") },
        @{ Path = "backend\packages\cv-datasets\src\cv_datasets\temporal_snapshot.py"; Tokens = @("def materialize_temporal_snapshot", "target_builder") },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\registry.py"; Tokens = @('node_type="inference.temporal_sequence"', 'node_type="analysis.stem_temporal_orientation"', 'node_type="analysis.stem_orientation_model_decision"', 'node_type="preprocessing.process_coordinate_rectify"', 'node_type="analysis.stem_measurement_eligibility"', 'node_type="analysis.stem_horizontal_curvature"', 'node_type="analysis.stem_surface_phase"', 'node_type="analysis.stem_curvature_phase_solver"', '"shadow_mode_enabled": False', '"stem_orientation_command_enabled": False') },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\high_rate_phase.py"; Tokens = @("class HighRateStemPhaseBridge", "class StemPhaseSeed", "PHASE_SEED_STALE", "PHASE_FRAME_BLURRED") },
        @{ Path = "backend\packages\cv-db\src\cv_db\pipeline_store.py"; Tokens = @('"slug": "template-realtime-stem-curvature-minimum"', '"shadow_mode_enabled": True', '"require_image_view_compatibility": True') },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\image_view.py"; Tokens = @("class ImageViewValidationError", "def calculate_image_view_profile", "POLYGON_MASKED_CROP", "def apply_image_view", "def restore_view_mask", "def view_overlay_geometry_to_source") },
        @{ Path = "backend\packages\cv-contracts\src\cv_contracts\runtime.py"; Tokens = @("class RuntimeImageViewProfile", "class RuntimeImageViewProfileCalculateRequest", "POLYGON_MASKED_CROP", "class RuntimeStemCurvatureCoordinateFrameDefinition", "ORTHOGONAL_XY_AXIS_ASSISTANT", "CANONICAL_VIEW_CORRIDOR_V1") },
        @{ Path = "backend\packages\cv-datasets\src\cv_datasets\conversion.py"; Tokens = @("def _task_image_view_metadata", "def _dataset_image_view_metadata") },
        @{ Path = "backend\services\runtime-admin-api\src\runtime_admin_api\api\routes.py"; Tokens = @("prefer_camera_stream_frame", "def _capture_calibration_frame_from_stream_ring", '"camera_media_stream_snapshot": True') },
        @{ Path = "frontend\apps\admin-web\src\features\runtime-pipelines\runtime-pipeline-designer-page.tsx"; Tokens = @("function ImageViewProfileEditor", "Preserve camera perspective (recommended)", "Coordinate-system assistant", "Known X distance", "Canonical view is the measurement corridor", "export function commissioningPreviewTargetNodeKey", "preferCameraStreamFrame: fullResolutionCalibrationSourceSelected") },
        @{ Path = "frontend\apps\admin-web\src\features\channels\channel-detail-page.tsx"; Tokens = @("type RuntimePreviewView = 'full' | 'model'", "Full camera view + overlays", "Model / Label Studio view + overlays", "PIPELINE_IMAGE_OUTPUT", "Actual Label Studio / model input") },
        @{ Path = "backend\services\capture-service\src\capture_service\executor.py"; Tokens = @("require_pipeline_image_output", "No current canonical pipeline image output", "require_eligibility") },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\payloads.py"; Tokens = @("class StemOrientationResultV3", 'algorithm: Literal["CURVATURE_PHASE_MINIMUM"]', "class SiemensSpsWriteStatusPayload") },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\siemens_sps.py"; Tokens = @("def _stem_orientation_command_gate", "STEM_SPS_REFERENCE_VALIDATION_NOT_PASSED", "PHASE_CONTROL_SPS_LIMITS_UNCONFIRMED") },
        @{ Path = "backend\services\realtime-executor\src\realtime_executor\live.py"; Tokens = @('"analysis.stem_horizontal_curvature"', '"analysis.stem_surface_phase"', '"analysis.stem_curvature_phase_solver"', "HighRateStemPhaseBridge", "PHASE_CAMERA_RECONNECT_RESET", "_stem_shadow_mode_enabled", "_source_overlay_from_image_view") },
        @{ Path = "backend\packages\cv-db\src\cv_db\stem_shadow_store.py"; Tokens = @("def stem_shadow_evaluation_response", "stem.shadow-evaluation-report.v1") },
        @{ Path = "backend\packages\cv-runtime\src\cv_runtime\temporal_samples.py"; Tokens = @("class TemporalSampleRecorderConfig", "sequence-manifest.json") },
        @{ Path = "backend\packages\cv-training\src\cv_training\backends\temporal_sequence.py"; Tokens = @("class TemporalSequenceTrainer", 'TEMPORAL_TRAINER_BACKEND = "pytorch-temporal-sequence"') },
        @{ Path = "backend\migrations\versions\20260803_0035_stem_orientation_reviews.py"; Tokens = @('revision = "20260803_0035"', 'down_revision = "20260724_0034"') },
        @{ Path = "frontend\packages\schemas\src\index.ts"; Tokens = @("'temporal_sequence'", "export interface LabelProjectProvisionRequest", "export interface RuntimeStemOrientationReview", "export interface RuntimeStemShadowEvaluationResponse") },
        @{ Path = "contracts\openapi\dataset-service.v1.yaml"; Tokens = @("/api/v1/label-projects:provision:", "temporal_sequence") },
        @{ Path = "contracts\openapi\result-service.v1.yaml"; Tokens = @("stem-orientation-reviews", "RuntimeStemOrientationReview", "stem-shadow-evaluation") },
        @{ Path = "docs\operations\stem-curvature-minimum-production-runbook.md"; Tokens = @("# Curvature-Minimum", "SPS-Dry-Run", "Rollback") },
        @{ Path = "docs\stem-orientation\canonical-measurement-view.md"; Tokens = @("Kanonischer Messkorridor", "PIPELINE_IMAGE_OUTPUT", "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW") }
    )

    foreach ($check in $checks) {
        $path = Join-Path (Get-Location).Path ([string]$check.Path)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Temporal-/Sequenz-Kompatibilitaetsdatei fehlt: $($check.Path)"
        }
        $content = Get-Content -LiteralPath $path -Raw
        foreach ($token in @($check.Tokens)) {
            if (-not $content.Contains([string]$token)) {
                throw "Temporal-/Sequenz-Kompatibilitaetsvertrag fehlt in $($check.Path): $token"
            }
        }
    }
    Write-Log "Temporal-Sequence-, Label-Studio-, Runtime- und Frontend-Vertraege sind fuer den Offline-Export vorhanden."
}

function Get-GitShortSha {
    $gitPath = Resolve-ExecutableCommand -Names @("git.exe", "git")
    if ([string]::IsNullOrWhiteSpace($gitPath)) {
        Write-Log "Git wurde nicht gefunden; AppVersion wird ohne Commit-SHA erzeugt."
        return ""
    }
    $sha = & $gitPath rev-parse --short=12 HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace([string]$sha)) {
        return ([string]$sha).Trim()
    }
    return ""
}

function New-AutoVersion {
    $timestamp = Get-Date -Format "yyyy.MM.dd-HHmmss"
    $sha = Get-GitShortSha
    if ([string]::IsNullOrWhiteSpace($sha)) {
        return $timestamp
    }
    return "$timestamp-g$sha"
}

if (-not [string]::IsNullOrWhiteSpace($Config)) {
    $configPath = Resolve-PathForExport -Path $Config -PathType Leaf -Description "Config-Datei"
    Write-Log "Lade Config: $configPath"
    . $configPath
}
else {
    $defaultConfig = Join-Path (Split-Path -Parent $PSCommandPath) "windows-offline.config.ps1"
    if (Test-Path -LiteralPath $defaultConfig -PathType Leaf) {
        Write-Log "Lade optionale Defaults aus: $defaultConfig"
        . $defaultConfig
    }
    else {
        Write-Log "Keine Config angegeben; verwende Standardwerte."
    }
}

$AppName = [string](Get-ConfigValue -Name "AppName" -DefaultValue "ybb-inferenzserver")
$AppVersion = [string](Get-ConfigValue -Name "AppVersion" -DefaultValue "auto")
$DistroName = [string](Get-ConfigValue -Name "DistroName" -DefaultValue "YBB-Inferenzserver")
$ServerBaseDir = [string](Get-ConfigValue -Name "ServerBaseDir" -DefaultValue "C:\YBB_Inferenzserver")
$AppHostName = [string](Get-ConfigValue -Name "AppHostName" -DefaultValue "127.0.0.1")
$WslNetworkingMode = [string](Get-ConfigValue -Name "WslNetworkingMode" -DefaultValue "unchanged")
$WslDnsTunneling = [bool](Get-ConfigValue -Name "WslDnsTunneling" -DefaultValue $true)
$WslFirewall = [bool](Get-ConfigValue -Name "WslFirewall" -DefaultValue $true)
$WslAutoProxy = [bool](Get-ConfigValue -Name "WslAutoProxy" -DefaultValue $false)
$WslHostAddressLoopback = [bool](Get-ConfigValue -Name "WslHostAddressLoopback" -DefaultValue $true)
$WslHyperVFirewallDefaultInboundAction = [string](Get-ConfigValue -Name "WslHyperVFirewallDefaultInboundAction" -DefaultValue "Unchanged")
$WslRestartAllowed = [bool](Get-ConfigValue -Name "WslRestartAllowed" -DefaultValue $true)
$BundlePartSizeBytes = [int64](Get-ConfigValue -Name "BundlePartSizeBytes" -DefaultValue 1GB)
$RuntimeDistroName = [string](Get-ConfigValue -Name "RuntimeDistroName" -DefaultValue (Get-ConfigValue -Name "ManagedRuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder"))
$RuntimeTarPath = [string](Get-ConfigValue -Name "RuntimeTarPath" -DefaultValue "")
$BuildFrontend = [bool](Get-ConfigValue -Name "BuildFrontend" -DefaultValue $true)
$FrontendAdminApiUrl = [string](Get-ConfigValue -Name "FrontendAdminApiUrl" -DefaultValue "")
$FrontendRuntimeAdminApiUrl = [string](Get-ConfigValue -Name "FrontendRuntimeAdminApiUrl" -DefaultValue "/runtime-api")
$FrontendResultServiceUrl = [string](Get-ConfigValue -Name "FrontendResultServiceUrl" -DefaultValue "/runtime-results")
$FrontendTriggerExecutorUrl = [string](Get-ConfigValue -Name "FrontendTriggerExecutorUrl" -DefaultValue "/runtime-trigger")
$FrontendInferenceGatewayRtUrl = [string](Get-ConfigValue -Name "FrontendInferenceGatewayRtUrl" -DefaultValue "/inference-rt")
$FrontendInferenceGatewayBatchUrl = [string](Get-ConfigValue -Name "FrontendInferenceGatewayBatchUrl" -DefaultValue "/inference-batch")
$FrontendAdminWsUrl = [string](Get-ConfigValue -Name "FrontendAdminWsUrl" -DefaultValue "")
$FrontendBasePath = [string](Get-ConfigValue -Name "FrontendBasePath" -DefaultValue "/frontend/")
$MediaMtxArchivePath = [string](Get-ConfigValue -Name "MediaMtxArchivePath" -DefaultValue "")
$PrepareRuntimeSkipMediaMtxCheck = [bool](Get-ConfigValue -Name "PrepareRuntimeSkipMediaMtxCheck" -DefaultValue $false)
$MediaMtxVersion = [string](Get-ConfigValue -Name "MediaMtxVersion" -DefaultValue "1.19.0")
$MediaMtxArchiveCacheDir = [string](Get-ConfigValue -Name "MediaMtxArchiveCacheDir" -DefaultValue ".runtime\mediamtx")
$MediaMtxDownloadUrl = [string](Get-ConfigValue -Name "MediaMtxDownloadUrl" -DefaultValue "")
$StreamingRtspPort = [int](Get-ConfigValue -Name "StreamingRtspPort" -DefaultValue 8554)
$StreamingWebRtcPort = [int](Get-ConfigValue -Name "StreamingWebRtcPort" -DefaultValue 8889)
$StreamingWebRtcIceTcpPort = [int](Get-ConfigValue -Name "StreamingWebRtcIceTcpPort" -DefaultValue 8189)
$StreamingTurnTcpPort = [int](Get-ConfigValue -Name "StreamingTurnTcpPort" -DefaultValue 3478)
$StreamAuthEnabled = [bool](Get-ConfigValue -Name "StreamAuthEnabled" -DefaultValue $false)
$PlcWritesAllowed = [bool](Get-ConfigValue -Name "PlcWritesAllowed" -DefaultValue $false)
$ManagedRuntimeDistroName = [string](Get-ConfigValue -Name "ManagedRuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder")
$GStreamerDebBundlePath = [string](Get-ConfigValue -Name "GStreamerDebBundlePath" -DefaultValue "")
$PrepareRuntimeSkipGStreamerCheck = [bool](Get-ConfigValue -Name "PrepareRuntimeSkipGStreamerCheck" -DefaultValue $false)
$GStreamerDebBundleCacheDir = [string](Get-ConfigValue -Name "GStreamerDebBundleCacheDir" -DefaultValue ".runtime\apt\gstreamer")
$GStreamerDebBundleDistroName = [string](Get-ConfigValue -Name "GStreamerDebBundleDistroName" -DefaultValue "")
$ConfiguredPythonWheelBundlePath = [string](Get-ConfigValue -Name "PythonWheelBundlePath" -DefaultValue "")
$PythonWheelBundleCacheDir = [string](Get-ConfigValue -Name "PythonWheelBundleCacheDir" -DefaultValue ".runtime\python-wheels")
if ([string]::IsNullOrWhiteSpace($GStreamerDebBundleDistroName)) {
    $GStreamerDebBundleDistroName = $ManagedRuntimeDistroName
}
if ([string]::IsNullOrWhiteSpace($GStreamerDebBundleDistroName)) {
    $GStreamerDebBundleDistroName = [string](Get-ConfigValue -Name "RuntimeDistroName" -DefaultValue $DistroName)
}
$GStreamerDebPackages = @(
    Get-ConfigValue -Name "GStreamerDebPackages" -DefaultValue @(
        "coturn",
        "ffmpeg",
        "python3-gi",
        "python3-gst-1.0",
        "gir1.2-gstreamer-1.0",
        "gir1.2-gst-rtsp-server-1.0",
        "gstreamer1.0-rtsp",
        "gstreamer1.0-tools",
        "gstreamer1.0-plugins-base",
        "gstreamer1.0-plugins-good",
        "gstreamer1.0-plugins-bad",
        "gstreamer1.0-plugins-ugly",
        "gstreamer1.0-libav"
    )
) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | ForEach-Object { [string]$_ }

if ([string]::IsNullOrWhiteSpace([string]$RuntimeTarPath) -and [string]::IsNullOrWhiteSpace([string]$RuntimeDistroName)) {
    throw "Setzen Sie RuntimeTarPath oder RuntimeDistroName in der Config."
}

if ([string]::IsNullOrWhiteSpace([string]$AppVersion) -or $AppVersion -ieq "auto") {
    $AppVersion = New-AutoVersion
    Write-Log "AppVersion automatisch gesetzt: $AppVersion"
}

if ($AppVersion -ieq "latest") {
    throw "Dieses Deployment verwendet keine latest-Version. Bitte eine feste AppVersion setzen."
}

Assert-TemporalCompatibilitySources

$invalidFileNameChars = [System.IO.Path]::GetInvalidFileNameChars()
if ($AppName.IndexOfAny($invalidFileNameChars) -ge 0 -or $AppVersion.IndexOfAny($invalidFileNameChars) -ge 0) {
    throw "AppName oder AppVersion enthaelt ungueltige Zeichen fuer Windows-Dateinamen."
}

if ($BuildFrontend) {
    Set-FrontendBuildEnv
    try {
        Invoke-FrontendBuild
    }
    finally {
        Restore-FrontendBuildEnv
    }
}
Assert-FrontendBuildOutput

$distRoot = Join-Path (Get-Location).Path "dist"
$releaseName = "${AppName}-${AppVersion}-windows-installer"
$releaseDir = Join-Path $distRoot $releaseName
$bundlePath = Join-Path $distRoot "${releaseName}.zip"

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

$appArchivePath = Join-Path $releaseDir "app.tar.gz"
New-AppArchive -OutputPath $appArchivePath
if ($BuildFrontend) {
    Assert-AppArchiveContainsFrontend -ArchivePath $appArchivePath
}
$appArchiveSha256 = (Get-FileHash -LiteralPath $appArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()

$runtimeTarStagedPath = Join-Path $releaseDir "runtime.tar"
if (-not [string]::IsNullOrWhiteSpace([string]$RuntimeTarPath)) {
    $resolvedRuntimeTarPath = Resolve-PathForExport -Path $RuntimeTarPath -PathType Leaf -Description "RuntimeTarPath"
    Write-Log "Kopiere vorbereitete Runtime: $resolvedRuntimeTarPath"
    Copy-Item -LiteralPath $resolvedRuntimeTarPath -Destination $runtimeTarStagedPath -Force
}
else {
    Write-Log "Exportiere WSL2-Runtime-Distribution: $RuntimeDistroName"
    Invoke-CheckedCommand -Description "Exportiere WSL2 Runtime nach runtime.tar." -FilePath "wsl" -Arguments @("--export", $RuntimeDistroName, $runtimeTarStagedPath)
}

$runtimeTarSha256 = (Get-FileHash -LiteralPath $runtimeTarStagedPath -Algorithm SHA256).Hash.ToLowerInvariant()
$runtimeInfo = Get-Item -LiteralPath $runtimeTarStagedPath
$runtimeArchiveMode = "file"
$runtimeArchiveParts = @()

if ($runtimeInfo.Length -gt [int64]$BundlePartSizeBytes) {
    $runtimeArchiveMode = "split"
    Write-Log ("runtime.tar ist {0:N2} GB gross und wird in {1:N2} GB Teile gesplittet." -f ($runtimeInfo.Length / 1GB), ([int64]$BundlePartSizeBytes / 1GB))
    $runtimeArchiveParts = Split-FileForBundle -SourcePath $runtimeTarStagedPath -OutputDir $releaseDir -BaseName "runtime.tar" -PartSizeBytes ([int64]$BundlePartSizeBytes)
    Remove-Item -LiteralPath $runtimeTarStagedPath -Force
}

Ensure-DeploymentUiExe
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\install-windows-offline.ps1") -Destination (Join-Path $releaseDir "install-windows-offline.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\update-windows-offline.ps1") -Destination (Join-Path $releaseDir "update-windows-offline.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\apply-windows-update.ps1") -Destination (Join-Path $releaseDir "apply-windows-update.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\configure-windows-portproxy.ps1") -Destination (Join-Path $releaseDir "configure-windows-portproxy.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\install-windows-autostart.ps1") -Destination (Join-Path $releaseDir "install-windows-autostart.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\install-windows-mediamtx.ps1") -Destination (Join-Path $releaseDir "install-windows-mediamtx.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\export-pretrained-models-update.ps1") -Destination (Join-Path $releaseDir "export-pretrained-models-update.ps1") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\YBB-Deployment-UI.exe") -Destination (Join-Path $releaseDir "YBB-Deployment-UI.exe") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\windows-deployment-ui.ps1") -Destination (Join-Path $releaseDir "windows-deployment-ui.ps1") -Force
New-Item -ItemType Directory -Path (Join-Path $releaseDir "wsl") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\wsl\install-offline.sh") -Destination (Join-Path $releaseDir "wsl\install-offline.sh") -Force
Copy-Item -LiteralPath (Join-Path (Get-Location).Path "deployment\wsl\apply-release.sh") -Destination (Join-Path $releaseDir "wsl\apply-release.sh") -Force

$mediaMtxArchiveBundlePath = ""
$mediaMtxArchiveSha256 = ""
$resolvedMediaMtxArchivePath = Resolve-MediaMtxArchiveForExport `
    -ArchivePath $MediaMtxArchivePath `
    -SkipMediaMtxCheck $PrepareRuntimeSkipMediaMtxCheck `
    -Version $MediaMtxVersion `
    -CacheDir (Resolve-OutputPath -Path $MediaMtxArchiveCacheDir -BaseDir (Get-Location).Path) `
    -DownloadUrl $MediaMtxDownloadUrl
if (-not [string]::IsNullOrWhiteSpace($resolvedMediaMtxArchivePath)) {
    $mediaRuntimeDir = Join-Path $releaseDir "runtime"
    New-Item -ItemType Directory -Path $mediaRuntimeDir -Force | Out-Null
    $mediaMtxArchiveBundlePath = "runtime/mediamtx.zip"
    Write-Log "Kopiere MediaMTX Archiv fuer Offline-Installer: $resolvedMediaMtxArchivePath"
    Copy-Item -LiteralPath $resolvedMediaMtxArchivePath -Destination (Join-Path $releaseDir $mediaMtxArchiveBundlePath) -Force
    $mediaMtxArchiveSha256 = (Get-FileHash -LiteralPath (Join-Path $releaseDir $mediaMtxArchiveBundlePath) -Algorithm SHA256).Hash.ToLowerInvariant()
}

$gStreamerDebBundlePath = ""
$gStreamerDebBundleSha256 = ""
$resolvedGStreamerDebBundlePath = Resolve-GStreamerDebBundleForExport `
    -BundlePath $GStreamerDebBundlePath `
    -SkipGStreamerCheck $PrepareRuntimeSkipGStreamerCheck `
    -DistroName $GStreamerDebBundleDistroName `
    -CacheDir (Resolve-OutputPath -Path $GStreamerDebBundleCacheDir -BaseDir (Get-Location).Path) `
    -PackageNames $GStreamerDebPackages
if (-not [string]::IsNullOrWhiteSpace($resolvedGStreamerDebBundlePath)) {
    $gstreamerRuntimeDir = Join-Path $releaseDir "runtime"
    New-Item -ItemType Directory -Path $gstreamerRuntimeDir -Force | Out-Null
    $gStreamerDebBundlePath = "runtime/gstreamer-debs.tar.gz"
    Write-Log "Kopiere GStreamer Debian-Paketbundle fuer Offline-Installer: $resolvedGStreamerDebBundlePath"
    Copy-Item -LiteralPath $resolvedGStreamerDebBundlePath -Destination (Join-Path $releaseDir $gStreamerDebBundlePath) -Force
    $gStreamerDebBundleSha256 = (Get-FileHash -LiteralPath (Join-Path $releaseDir $gStreamerDebBundlePath) -Algorithm SHA256).Hash.ToLowerInvariant()
}

$pythonWheelBundlePath = ""
$pythonWheelBundleSha256 = ""
$resolvedPythonWheelBundlePath = Resolve-PythonWheelBundleForExport `
    -BundlePath $ConfiguredPythonWheelBundlePath `
    -CacheDir $PythonWheelBundleCacheDir
if (-not [string]::IsNullOrWhiteSpace($resolvedPythonWheelBundlePath)) {
    $pythonRuntimeDir = Join-Path $releaseDir "runtime"
    New-Item -ItemType Directory -Path $pythonRuntimeDir -Force | Out-Null
    $pythonWheelBundlePath = "runtime/python-wheels.tar.gz"
    Write-Log "Kopiere Python-Wheel-Bundle fuer Offline-Installer: $resolvedPythonWheelBundlePath"
    Copy-Item -LiteralPath $resolvedPythonWheelBundlePath -Destination (Join-Path $releaseDir $pythonWheelBundlePath) -Force
    $pythonWheelBundleSha256 = (Get-FileHash -LiteralPath (Join-Path $releaseDir $pythonWheelBundlePath) -Algorithm SHA256).Hash.ToLowerInvariant()
}

$bundleManifest = [ordered]@{
    BundleType = "installer"
    AppName = $AppName
    AppVersion = $AppVersion
    DistroName = $DistroName
    ServerBaseDir = $ServerBaseDir
    AppHostName = $AppHostName
    WslNetworkingMode = $WslNetworkingMode
    WslDnsTunneling = $WslDnsTunneling
    WslFirewall = $WslFirewall
    WslAutoProxy = $WslAutoProxy
    WslHostAddressLoopback = $WslHostAddressLoopback
    WslHyperVFirewallDefaultInboundAction = $WslHyperVFirewallDefaultInboundAction
    WslRestartAllowed = $WslRestartAllowed
    BuildTimestamp = (Get-Date).ToUniversalTime().ToString("o")
    AppArchive = "app.tar.gz"
    AppArchiveSha256 = $appArchiveSha256
    UpdaterScript = "apply-windows-update.ps1"
    DeploymentUi = "YBB-Deployment-UI.exe"
    RuntimeArchiveMode = $runtimeArchiveMode
    RuntimeArchive = if ($runtimeArchiveMode -eq "file") { "runtime.tar" } else { $null }
    RuntimeArchiveParts = $runtimeArchiveParts
    RuntimeArchiveSha256 = $runtimeTarSha256
    RuntimeArchivePartSizeBytes = if ($runtimeArchiveMode -eq "split") { [int64]$BundlePartSizeBytes } else { 0 }
    MediaMtxArchive = if ([string]::IsNullOrWhiteSpace($mediaMtxArchiveBundlePath)) { $null } else { $mediaMtxArchiveBundlePath }
    MediaMtxArchiveSha256 = if ([string]::IsNullOrWhiteSpace($mediaMtxArchiveSha256)) { $null } else { $mediaMtxArchiveSha256 }
    MediaMtxSkipped = [string]::IsNullOrWhiteSpace($mediaMtxArchiveBundlePath)
    MediaMtxVersion = $MediaMtxVersion
    MediaMtxHostPlatform = "windows"
    GStreamerDebBundle = if ([string]::IsNullOrWhiteSpace($gStreamerDebBundlePath)) { $null } else { $gStreamerDebBundlePath }
    GStreamerDebBundleSha256 = if ([string]::IsNullOrWhiteSpace($gStreamerDebBundleSha256)) { $null } else { $gStreamerDebBundleSha256 }
    GStreamerDebBundleSkipped = [string]::IsNullOrWhiteSpace($gStreamerDebBundlePath)
    GStreamerDebPackages = $GStreamerDebPackages
    PythonWheelBundle = if ([string]::IsNullOrWhiteSpace($pythonWheelBundlePath)) { $null } else { $pythonWheelBundlePath }
    PythonWheelBundleSha256 = if ([string]::IsNullOrWhiteSpace($pythonWheelBundleSha256)) { $null } else { $pythonWheelBundleSha256 }
    PythonWheelBundleSkipped = [string]::IsNullOrWhiteSpace($pythonWheelBundlePath)
    StreamingRtspPort = $StreamingRtspPort
    StreamingWebRtcPort = $StreamingWebRtcPort
    StreamingWebRtcIceTcpPort = $StreamingWebRtcIceTcpPort
    StreamingTurnTcpPort = $StreamingTurnTcpPort
    StreamAuthEnabled = $StreamAuthEnabled
    PlcWritesAllowed = $PlcWritesAllowed
    CompatibilityProfile = "temporal-sequence.v1"
    MinimumDatabaseRevision = "20260803_0035"
    PersistentStateRoot = "/srv/cv-platform"
    TemporalSampleStorage = "/srv/cv-platform/runtime/captures/temporal-samples"
    TemporalSnapshotStorage = "/srv/cv-platform/control/snapshots"
    StemOrientationActivation = "pipeline_configuration_required"
    TemporalModelPromotion = "manual_shadow_only"
}
$bundleManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $releaseDir "windows-bundle-manifest.json") -Encoding utf8
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION") -Value $AppVersion -Encoding ascii

$bundleFileNames = @("app.tar.gz", "install-windows-offline.ps1", "update-windows-offline.ps1", "apply-windows-update.ps1", "configure-windows-portproxy.ps1", "install-windows-autostart.ps1", "install-windows-mediamtx.ps1", "export-pretrained-models-update.ps1", "YBB-Deployment-UI.exe", "windows-deployment-ui.ps1", "wsl/install-offline.sh", "wsl/apply-release.sh", "windows-bundle-manifest.json", "VERSION")
if ($runtimeArchiveMode -eq "split") {
    $bundleFileNames += $runtimeArchiveParts
}
else {
    $bundleFileNames += "runtime.tar"
}
if (-not [string]::IsNullOrWhiteSpace($mediaMtxArchiveBundlePath)) {
    $bundleFileNames += $mediaMtxArchiveBundlePath
}
if (-not [string]::IsNullOrWhiteSpace($gStreamerDebBundlePath)) {
    $bundleFileNames += $gStreamerDebBundlePath
}
if (-not [string]::IsNullOrWhiteSpace($pythonWheelBundlePath)) {
    $bundleFileNames += $pythonWheelBundlePath
}

New-Sha256Manifest -ReleaseDir $releaseDir -FileNames $bundleFileNames

Write-Log "Erstelle Windows/WSL2 Offline-Installer-Bundle."
New-ZipBundle -ReleaseDir $releaseDir -BundlePath $bundlePath -FileNames @($bundleFileNames + @("manifest.sha256"))

Write-Log "Installer-Bundle fertig: $bundlePath"
Write-Output $bundlePath
