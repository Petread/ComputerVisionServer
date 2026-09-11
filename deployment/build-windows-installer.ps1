[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$Version = "",
    [switch]$SkipRuntimePrepare,
    [switch]$SkipApt,
    [switch]$SkipTrainingRuntime,
    [switch]$SkipTritonCheck,
    [switch]$SkipMediaMtxCheck,
    [switch]$SkipGStreamerCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
trap {
    Write-Host ("FEHLER: {0}" -f $_.Exception.Message)
    exit 1
}

$script:BuildLockStream = $null
$script:BuildLockPath = ""

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Acquire-BuildLock {
    param([Parameter(Mandatory = $true)][string]$Path)

    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    try {
        $script:BuildLockStream = [System.IO.File]::Open($Path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $script:BuildLockPath = $Path
        $writer = New-Object System.IO.StreamWriter($script:BuildLockStream, [System.Text.Encoding]::UTF8, 1024, $true)
        $script:BuildLockStream.SetLength(0)
        $writer.WriteLine("ProcessId={0}" -f $PID)
        $writer.WriteLine("StartedAt={0}" -f (Get-Date).ToString("o"))
        $writer.Flush()
        $writer.Dispose()
    }
    catch [System.IO.IOException] {
        throw "Ein Windows-Installer-Build laeuft bereits. Bitte warten oder den alten Prozess beenden. Lock: $Path"
    }
}

function Release-BuildLock {
    if ($null -ne $script:BuildLockStream) {
        $script:BuildLockStream.Dispose()
        $script:BuildLockStream = $null
    }
    if (-not [string]::IsNullOrWhiteSpace($script:BuildLockPath) -and (Test-Path -LiteralPath $script:BuildLockPath -PathType Leaf)) {
        Remove-Item -LiteralPath $script:BuildLockPath -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-OptionalConfigPath {
    param(
        [string]$ConfigPath,
        [string]$DeploymentDir,
        [string]$InvocationDir
    )

    if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
        $candidate = if ([System.IO.Path]::IsPathRooted($ConfigPath)) { $ConfigPath } else { Join-Path $InvocationDir $ConfigPath }
        $fullPath = [System.IO.Path]::GetFullPath($candidate)
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Config-Datei wurde nicht gefunden: $fullPath"
        }
        return $fullPath
    }

    $defaultConfig = Join-Path $DeploymentDir "windows-offline.config.ps1"
    if (Test-Path -LiteralPath $defaultConfig -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($defaultConfig)
    }

    return ""
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

function Get-GitShortSha {
    $gitCommand = Get-Command git.exe, git -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $gitCommand) {
        Write-Log "Git wurde nicht gefunden; AppVersion wird ohne Commit-SHA erzeugt."
        return ""
    }
    $gitPath = if ($gitCommand.Source) { $gitCommand.Source } else { $gitCommand.Path }
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

function Resolve-OutputPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDir
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $BaseDir $Path }
    return [System.IO.Path]::GetFullPath($candidate)
}

function New-SafeFileName {
    param([Parameter(Mandatory = $true)][string]$Value)

    return ($Value -replace '[^A-Za-z0-9_.-]', '_')
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

function Resolve-DockerCommand {
    $pathCommand = Resolve-ExecutableCommand -Names @("docker.exe", "docker")
    if (-not [string]::IsNullOrWhiteSpace($pathCommand)) {
        $dockerDir = Split-Path -Parent $pathCommand
        if (@($env:PATH -split ';') -notcontains $dockerDir) {
            $env:PATH = "$dockerDir;$env:PATH"
        }
        return $pathCommand
    }

    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($target in @(
        [System.EnvironmentVariableTarget]::Process,
        [System.EnvironmentVariableTarget]::User,
        [System.EnvironmentVariableTarget]::Machine
    )) {
        $configuredDockerPath = [Environment]::GetEnvironmentVariable("YBB_DOCKER_CLI", $target)
        if (-not [string]::IsNullOrWhiteSpace($configuredDockerPath)) {
            [void]$candidates.Add([Environment]::ExpandEnvironmentVariables($configuredDockerPath.Trim().Trim('"')))
        }
    }

    # A long-running deployment UI keeps the PATH from its start time. Search the
    # current persisted user and machine PATH as well, so a newly installed Docker
    # CLI is found without restarting the UI or Explorer.
    foreach ($target in @(
        [System.EnvironmentVariableTarget]::User,
        [System.EnvironmentVariableTarget]::Machine
    )) {
        $configuredPath = [Environment]::GetEnvironmentVariable("Path", $target)
        foreach ($directory in @($configuredPath -split ';')) {
            $expandedDirectory = [Environment]::ExpandEnvironmentVariables(([string]$directory).Trim().Trim('"'))
            if (-not [string]::IsNullOrWhiteSpace($expandedDirectory)) {
                [void]$candidates.Add((Join-Path $expandedDirectory "docker.exe"))
            }
        }
    }

    $programFilesRoots = @(
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        [Environment]::GetEnvironmentVariable("ProgramFiles", [System.EnvironmentVariableTarget]::Machine),
        [Environment]::GetEnvironmentVariable("ProgramFiles(x86)", [System.EnvironmentVariableTarget]::Machine)
    )
    foreach ($programFilesRoot in $programFilesRoots) {
        if (-not [string]::IsNullOrWhiteSpace([string]$programFilesRoot)) {
            [void]$candidates.Add((Join-Path ([string]$programFilesRoot) "Docker\Docker\resources\bin\docker.exe"))
        }
    }

    $programDataRoots = @(
        $env:ProgramData,
        [Environment]::GetEnvironmentVariable("ProgramData", [System.EnvironmentVariableTarget]::Machine)
    )
    foreach ($programDataRoot in $programDataRoots) {
        if (-not [string]::IsNullOrWhiteSpace([string]$programDataRoot)) {
            [void]$candidates.Add((Join-Path ([string]$programDataRoot) "DockerDesktop\version-bin\docker.exe"))
        }
    }

    foreach ($registryPath in @(
        "Registry::HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\docker.exe",
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\docker.exe"
    )) {
        try {
            $registryKey = Get-Item -LiteralPath $registryPath -ErrorAction Stop
            $registeredDockerPath = [string]$registryKey.GetValue("")
            if (-not [string]::IsNullOrWhiteSpace($registeredDockerPath)) {
                [void]$candidates.Add([Environment]::ExpandEnvironmentVariables($registeredDockerPath.Trim().Trim('"')))
            }
        }
        catch {
            # Docker does not necessarily register an App Paths entry.
        }
    }

    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $candidate = Join-Path $candidate "docker.exe"
        }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolvedCandidate = [System.IO.Path]::GetFullPath($candidate)
            $dockerDir = Split-Path -Parent $resolvedCandidate
            if (@($env:PATH -split ';') -notcontains $dockerDir) {
                $env:PATH = "$dockerDir;$env:PATH"
            }
            return $resolvedCandidate
        }
    }
    return ""
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = (Get-Location).Path
    )

    Write-Log $Description
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Description fehlgeschlagen. Exit Code: $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
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

function Remove-DirectoryUnderParent {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return
    }
    if (-not (Test-IsPathUnderDirectory -Child $Path -Parent $Parent)) {
        throw "Sicherheitsabbruch: Temp-Verzeichnis liegt nicht unterhalb von ${Parent}: $Path"
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Invoke-CommandStdoutToFile {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$OutputPath
    )

    Write-Log $Description
    $stderrPath = "$OutputPath.stderr"
    Remove-Item -LiteralPath $OutputPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -NoNewWindow `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $OutputPath `
        -RedirectStandardError $stderrPath
    if ($process.ExitCode -ne 0) {
        $stderr = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "$Description fehlgeschlagen. Exit Code: $($process.ExitCode). $stderr"
    }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf) -or ((Get-Item -LiteralPath $OutputPath).Length -le 0)) {
        throw "$Description hat keine Ausgabedatei erzeugt: $OutputPath"
    }
    Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
}

function New-TritonServerArchiveFromDockerImage {
    param(
        [Parameter(Mandatory = $true)][string]$Image,
        [Parameter(Mandatory = $true)][string]$CacheDir
    )

    $dockerPath = Resolve-DockerCommand
    if ([string]::IsNullOrWhiteSpace($dockerPath)) {
        throw "Docker CLI wurde nicht gefunden. Bei einem Update mit bereits installiertem Triton aktivieren Sie 'Triton-Archiv ueberspringen (kein Docker)'. Andernfalls setzen Sie YBB_DOCKER_CLI oder TritonServerArchivePath auf einen gueltigen Pfad."
    }
    $tarPath = Resolve-ExecutableCommand -Names @("tar.exe", "tar")
    if ([string]::IsNullOrWhiteSpace($tarPath)) {
        throw "tar wurde nicht gefunden. Setzen Sie TritonServerArchivePath manuell oder stellen Sie Windows tar.exe bereit."
    }

    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
    $archivePath = Join-Path $CacheDir ("tritonserver-{0}-with-external-libs.tar.gz" -f (New-SafeFileName -Value $Image))
    if ((Test-Path -LiteralPath $archivePath -PathType Leaf) -and ((Get-Item -LiteralPath $archivePath).Length -gt 0)) {
        $cachedListing = & $tarPath -tzf $archivePath 2>$null
        $hasTrtexec = $LASTEXITCODE -eq 0 -and [bool]($cachedListing | Where-Object { $_ -eq "tritonserver/bin/trtexec" } | Select-Object -First 1)
        $hasTensorRtLib = $LASTEXITCODE -eq 0 -and [bool]($cachedListing | Where-Object { $_ -like "tritonserver/external-libs/libnvinfer.so*" } | Select-Object -First 1)
        $hasTensorRtBuilderResource = $LASTEXITCODE -eq 0 -and [bool]($cachedListing | Where-Object { $_ -like "tritonserver/external-libs/libnvinfer_builder_resource.so*" } | Select-Object -First 1)
        if ($hasTrtexec -and $hasTensorRtLib -and $hasTensorRtBuilderResource) {
            Write-Log "Verwende gecachtes Triton Server Archiv: $archivePath"
            return $archivePath
        }
        Write-Log "Ignoriere gecachtes Triton Server Archiv ohne trtexec/vollstaendige TensorRT-Libraries: $archivePath"
        Remove-Item -LiteralPath $archivePath -Force
    }

    Write-Log "Erzeuge Triton Server Archiv aus Docker-Image: $Image"
    Invoke-CheckedCommand -Description "Pruefe Docker CLI." -FilePath $dockerPath -Arguments @("version") | Out-Host
    Invoke-CheckedCommand -Description "Lade Triton Server Docker-Image." -FilePath $dockerPath -Arguments @("pull", $Image) | Out-Host

    $archiveScript = @'
set -euo pipefail
tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "$tmp_dir"; }
trap cleanup EXIT

mkdir -p "$tmp_dir/tritonserver" "$tmp_dir/tritonserver/external-libs"
cp -a /opt/tritonserver/. "$tmp_dir/tritonserver/"
for trtexec_candidate in \
  /opt/tritonserver/bin/trtexec \
  /usr/src/tensorrt/bin/trtexec \
  /usr/local/tensorrt/bin/trtexec \
  /usr/bin/trtexec
do
  if [[ -x "$trtexec_candidate" ]]; then
    mkdir -p "$tmp_dir/tritonserver/bin"
    cp -aL "$trtexec_candidate" "$tmp_dir/tritonserver/bin/trtexec"
    break
  fi
done

libs_file="$tmp_dir/ldd-libs.txt"
: > "$libs_file"
if [[ -x /opt/tritonserver/bin/tritonserver ]]; then
  ldd /opt/tritonserver/bin/tritonserver 2>/dev/null | awk '/=> \// { print $3 } /^\// { print $1 }' >> "$libs_file" || true
fi
if [[ -x "$tmp_dir/tritonserver/bin/trtexec" ]]; then
  ldd "$tmp_dir/tritonserver/bin/trtexec" 2>/dev/null | awk '/=> \// { print $3 } /^\// { print $1 }' >> "$libs_file" || true
fi
if [[ -d /opt/tritonserver/backends ]]; then
  while IFS= read -r -d '' so_file; do
    ldd "$so_file" 2>/dev/null | awk '/=> \// { print $3 } /^\// { print $1 }' >> "$libs_file" || true
  done < <(find /opt/tritonserver/backends -type f \( -name '*.so' -o -name '*.so.*' \) -print0)
fi
for extra in \
  /usr/lib/x86_64-linux-gnu/libb64.so* \
  /usr/lib/x86_64-linux-gnu/libnuma.so* \
  /usr/lib/x86_64-linux-gnu/libdcgm.so* \
  /usr/lib/x86_64-linux-gnu/libnvinfer*.so* \
  /usr/lib/x86_64-linux-gnu/libnvonnxparser*.so* \
  /usr/lib/x86_64-linux-gnu/libnvparsers*.so* \
  /usr/local/cuda*/lib64/libcudart.so* \
  /usr/local/cuda*/targets/x86_64-linux/lib/libcudart.so* \
  /usr/local/cuda*/compat/libcudart.so*
do
  if [[ -e "$extra" ]]; then
    printf '%s\n' "$extra" >> "$libs_file"
  fi
done
sort -u "$libs_file" -o "$libs_file"

while IFS= read -r lib; do
  [[ -e "$lib" ]] || continue
  base="$(basename "$lib")"
  case "$base" in
    libcudart.so*|libb64.so*|libnuma.so*|libdcgm.so*|libcuda.so*|libnvidia-ml.so*|libnvrtc.so*|libnvrtc-builtins.so*|libcublas.so*|libcublasLt.so*|libcufft.so*|libcurand.so*|libcusolver.so*|libcusparse.so*|libnpp*.so*|libnvjpeg.so*|libnvinfer*.so*|libnvonnxparser*.so*|libnvparsers*.so*)
      cp -aL "$lib" "$tmp_dir/tritonserver/external-libs/$base"
      ;;
  esac
done < "$libs_file"

tar -C "$tmp_dir" -czf "/ybb-out/${YBB_TRITON_ARCHIVE_NAME:?}" tritonserver
'@

    $tmpArchivePath = "$archivePath.tmp.zip"
    Remove-Item -LiteralPath $tmpArchivePath -Force -ErrorAction SilentlyContinue
    $tmpArchiveName = Split-Path -Leaf $tmpArchivePath
    $archiveScriptName = "build-triton-archive-$([System.Guid]::NewGuid().ToString("N")).sh"
    $archiveScriptPath = Join-Path $CacheDir $archiveScriptName
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($archiveScriptPath, ($archiveScript -replace "`r`n", "`n"), $utf8NoBom)
    try {
        Invoke-CheckedCommand `
            -Description "Erzeuge Triton Server Archiv im Docker-Container." `
            -FilePath $dockerPath `
            -Arguments @("run", "--rm", "-v", "${CacheDir}:/ybb-out", "-e", "YBB_TRITON_ARCHIVE_NAME=$tmpArchiveName", "--entrypoint", "bash", $Image, "/ybb-out/$archiveScriptName") | Out-Host
        Invoke-CheckedCommand -Description "Pruefe Triton Server Archiv." -FilePath $tarPath -Arguments @("-tzf", $tmpArchivePath, "tritonserver/bin/tritonserver") | Out-Host
        $archiveListing = & $tarPath -tzf $tmpArchivePath
        if ($LASTEXITCODE -ne 0) {
            throw "Triton Server Archiv konnte nicht gelesen werden."
        }
        if (-not ($archiveListing | Where-Object { $_ -eq "tritonserver/bin/trtexec" } | Select-Object -First 1)) {
            throw "Triton Server Archiv enthaelt kein trtexec fuer Deployment-Engine-Builds."
        }
        if (-not ($archiveListing | Where-Object { $_ -like "tritonserver/external-libs/libnvinfer.so*" } | Select-Object -First 1)) {
            throw "Triton Server Archiv enthaelt keine TensorRT libnvinfer Libraries."
        }
        if (-not ($archiveListing | Where-Object { $_ -like "tritonserver/external-libs/libnvinfer_builder_resource.so*" } | Select-Object -First 1)) {
            throw "Triton Server Archiv enthaelt keine TensorRT Builder-Resource-Library."
        }
    }
    catch {
        Remove-Item -LiteralPath $tmpArchivePath -Force -ErrorAction SilentlyContinue
        throw
    }
    finally {
        Remove-Item -LiteralPath $archiveScriptPath -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $tmpArchivePath -Destination $archivePath -Force
    Write-Log "Triton Server Archiv erstellt: $archivePath"
    return $archivePath
}

function Resolve-TritonServerArchiveForBuild {
    param(
        [AllowEmptyString()][string]$ArchivePath,
        [bool]$SkipTritonCheck,
        [Parameter(Mandatory = $true)][string]$Image,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [Parameter(Mandatory = $true)][string]$BaseDir
    )

    if (-not [string]::IsNullOrWhiteSpace($ArchivePath)) {
        $resolvedArchivePath = Resolve-OutputPath -Path $ArchivePath -BaseDir $BaseDir
        if (-not (Test-Path -LiteralPath $resolvedArchivePath -PathType Leaf)) {
            throw "TritonServerArchivePath wurde nicht gefunden: $resolvedArchivePath"
        }
        return $resolvedArchivePath
    }
    if ($SkipTritonCheck) {
        return ""
    }
    return New-TritonServerArchiveFromDockerImage -Image $Image -CacheDir $CacheDir
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

function Resolve-MediaMtxArchiveForBuild {
    param(
        [AllowEmptyString()][string]$ArchivePath,
        [bool]$SkipMediaMtxCheck,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [AllowEmptyString()][string]$DownloadUrl,
        [Parameter(Mandatory = $true)][string]$BaseDir
    )

    if (-not [string]::IsNullOrWhiteSpace($ArchivePath)) {
        $resolvedArchivePath = Resolve-OutputPath -Path $ArchivePath -BaseDir $BaseDir
        if (-not (Test-Path -LiteralPath $resolvedArchivePath -PathType Leaf)) {
            throw "MediaMtxArchivePath wurde nicht gefunden: $resolvedArchivePath"
        }
        return $resolvedArchivePath
    }
    if ($SkipMediaMtxCheck) {
        return ""
    }
    return New-MediaMtxArchiveFromRelease -Version $Version -CacheDir $CacheDir -DownloadUrl $DownloadUrl
}

function ConvertTo-PowerShellLiteral {
    param([AllowNull()]$Value)

    if ($null -eq $Value) {
        return '$null'
    }
    if ($Value -is [bool]) {
        if ($Value) { return '$true' }
        return '$false'
    }
    if ($Value -is [byte] -or $Value -is [int16] -or $Value -is [int] -or $Value -is [int64] -or $Value -is [decimal] -or $Value -is [double]) {
        return [string]$Value
    }
    if ($Value -is [System.Array]) {
        $items = @($Value | ForEach-Object { ConvertTo-PowerShellLiteral $_ })
        return "@(" + ($items -join ", ") + ")"
    }

    $text = [string]$Value
    return "'" + $text.Replace("'", "''") + "'"
}

function New-GeneratedConfig {
    param(
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Automatisch erzeugt durch deployment/build-windows-installer.ps1.")
    $lines.Add("# Diese Datei ist ein Build-Artefakt; fuer normale Exporte muss sie nicht bearbeitet werden.")
    $lines.Add("")

    foreach ($name in @(
        "AppName",
        "AppVersion",
        "DistroName",
        "ServerBaseDir",
        "AppHostName",
        "WslNetworkingMode",
        "WslDnsTunneling",
        "WslFirewall",
        "WslAutoProxy",
        "WslHostAddressLoopback",
        "WslHyperVFirewallDefaultInboundAction",
        "WslRestartAllowed",
        "RuntimeDistroName",
        "RuntimeDistroInstallRoot",
        "AutoResetRuntimeDistro",
        "RuntimeTarPath",
        "PrepareRuntimeInstallAptPackages",
        "PrepareRuntimeInstallTrainingRuntime",
        "TritonServerArchivePath",
        "PrepareRuntimeSkipTritonCheck",
        "TritonServerImage",
        "TritonServerArchiveCacheDir",
        "MediaMtxArchivePath",
        "PrepareRuntimeSkipMediaMtxCheck",
        "MediaMtxVersion",
        "MediaMtxArchiveCacheDir",
        "MediaMtxDownloadUrl",
        "GStreamerDebBundlePath",
        "PrepareRuntimeSkipGStreamerCheck",
        "GStreamerDebBundleCacheDir",
        "GStreamerDebBundleDistroName",
        "GStreamerDebPackages",
        "StreamingRtspPort",
        "StreamingWebRtcPort",
        "StreamingWebRtcIceTcpPort",
        "StreamingTurnTcpPort",
        "StreamAuthEnabled",
        "BundlePartSizeBytes",
        "BuildFrontend",
        "FrontendAdminApiUrl",
        "FrontendRuntimeAdminApiUrl",
        "FrontendResultServiceUrl",
        "FrontendTriggerExecutorUrl",
        "FrontendInferenceGatewayRtUrl",
        "FrontendInferenceGatewayBatchUrl",
        "FrontendAdminWsUrl",
        "FrontendBasePath"
    )) {
        $lines.Add('$' + $name + ' = ' + (ConvertTo-PowerShellLiteral $Values[$name]))
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
    Set-Content -LiteralPath $OutputPath -Value $lines -Encoding ascii
}

function Invoke-PowerShellScript {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][hashtable]$Parameters
    )

    $powerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $powerShellExe -PathType Leaf)) {
        throw "Windows PowerShell wurde nicht gefunden: $powerShellExe"
    }
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $ScriptPath
    )

    foreach ($entry in $Parameters.GetEnumerator()) {
        $arguments += "-$($entry.Key)"
        if ($entry.Value -is [System.Management.Automation.SwitchParameter]) {
            if (-not $entry.Value.IsPresent) {
                $arguments = $arguments[0..($arguments.Count - 2)]
            }
        }
        elseif ($entry.Value -is [bool]) {
            if (-not $entry.Value) {
                $arguments = $arguments[0..($arguments.Count - 2)]
            }
        }
        elseif ($null -ne $entry.Value) {
            $arguments += [string]$entry.Value
        }
    }

    Write-Log $Description
    & $powerShellExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description fehlgeschlagen. Exit Code: $LASTEXITCODE"
    }
}

$deploymentDir = Split-Path -Parent $PSCommandPath
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $deploymentDir ".."))
$invocationDir = (Get-Location).Path
Acquire-BuildLock -Path (Join-Path $repoRoot ".runtime\windows-installer-build.lock")

$configPath = Resolve-OptionalConfigPath -ConfigPath $Config -DeploymentDir $deploymentDir -InvocationDir $invocationDir
if (-not [string]::IsNullOrWhiteSpace($configPath)) {
    Write-Log "Lade optionale Defaults aus: $configPath"
    . $configPath
}
else {
    Write-Log "Keine Config angegeben; verwende Standardwerte."
}

$appName = [string](Get-ConfigValue -Name "AppName" -DefaultValue "ybb-inferenzserver")
$appVersion = if (-not [string]::IsNullOrWhiteSpace($Version)) { $Version } else { New-AutoVersion }
$distroName = [string](Get-ConfigValue -Name "DistroName" -DefaultValue "YBB-Inferenzserver")
$serverBaseDir = [string](Get-ConfigValue -Name "ServerBaseDir" -DefaultValue "C:\YBB_Inferenzserver")
$appHostName = [string](Get-ConfigValue -Name "AppHostName" -DefaultValue "127.0.0.1")
$wslNetworkingMode = [string](Get-ConfigValue -Name "WslNetworkingMode" -DefaultValue "mirrored")
$wslDnsTunneling = [bool](Get-ConfigValue -Name "WslDnsTunneling" -DefaultValue $true)
$wslFirewall = [bool](Get-ConfigValue -Name "WslFirewall" -DefaultValue $true)
$wslAutoProxy = [bool](Get-ConfigValue -Name "WslAutoProxy" -DefaultValue $false)
$wslHostAddressLoopback = [bool](Get-ConfigValue -Name "WslHostAddressLoopback" -DefaultValue $true)
$wslHyperVFirewallDefaultInboundAction = [string](Get-ConfigValue -Name "WslHyperVFirewallDefaultInboundAction" -DefaultValue "Allow")
$wslRestartAllowed = [bool](Get-ConfigValue -Name "WslRestartAllowed" -DefaultValue $true)

# Der One-Command-Build verwaltet die Builder-Distribution selbst. Ein alter RuntimeDistroName aus einer
# kopierten Config wird bewusst ignoriert, damit keine manuelle Namenspflege noetig ist.
$managedRuntimeDistroName = [string](Get-ConfigValue -Name "ManagedRuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder")
$runtimeDistroInstallRoot = [string](Get-ConfigValue -Name "RuntimeDistroInstallRoot" -DefaultValue (Join-Path $repoRoot ".runtime\wsl\$managedRuntimeDistroName"))
$runtimeDistroInstallRoot = Resolve-OutputPath -Path $runtimeDistroInstallRoot -BaseDir $repoRoot
$tritonServerArchivePath = [string](Get-ConfigValue -Name "TritonServerArchivePath" -DefaultValue "")
$defaultSkipTritonCheck = [string]::IsNullOrWhiteSpace($tritonServerArchivePath)
$prepareRuntimeSkipTritonCheck = [bool](Get-ConfigValue -Name "PrepareRuntimeSkipTritonCheck" -DefaultValue $defaultSkipTritonCheck)
if ($SkipTritonCheck) {
    $prepareRuntimeSkipTritonCheck = $true
}
$tritonServerImage = [string](Get-ConfigValue -Name "TritonServerImage" -DefaultValue "nvcr.io/nvidia/tritonserver:24.08-py3")
$tritonServerArchiveCacheDir = Resolve-OutputPath -Path ([string](Get-ConfigValue -Name "TritonServerArchiveCacheDir" -DefaultValue ".runtime\triton")) -BaseDir $repoRoot
$tritonServerArchivePath = Resolve-TritonServerArchiveForBuild `
    -ArchivePath $tritonServerArchivePath `
    -SkipTritonCheck $prepareRuntimeSkipTritonCheck `
    -Image $tritonServerImage `
    -CacheDir $tritonServerArchiveCacheDir `
    -BaseDir $repoRoot
$mediaMtxArchivePath = [string](Get-ConfigValue -Name "MediaMtxArchivePath" -DefaultValue "")
$prepareRuntimeSkipMediaMtxCheck = [bool](Get-ConfigValue -Name "PrepareRuntimeSkipMediaMtxCheck" -DefaultValue $false)
if ($SkipMediaMtxCheck) {
    $prepareRuntimeSkipMediaMtxCheck = $true
}
$mediaMtxVersion = [string](Get-ConfigValue -Name "MediaMtxVersion" -DefaultValue "1.19.0")
$mediaMtxArchiveCacheDir = Resolve-OutputPath -Path ([string](Get-ConfigValue -Name "MediaMtxArchiveCacheDir" -DefaultValue ".runtime\mediamtx")) -BaseDir $repoRoot
$mediaMtxDownloadUrl = [string](Get-ConfigValue -Name "MediaMtxDownloadUrl" -DefaultValue "")
$mediaMtxArchivePath = Resolve-MediaMtxArchiveForBuild `
    -ArchivePath $mediaMtxArchivePath `
    -SkipMediaMtxCheck $prepareRuntimeSkipMediaMtxCheck `
    -Version $mediaMtxVersion `
    -CacheDir $mediaMtxArchiveCacheDir `
    -DownloadUrl $mediaMtxDownloadUrl `
    -BaseDir $repoRoot
$gStreamerDebBundlePath = [string](Get-ConfigValue -Name "GStreamerDebBundlePath" -DefaultValue "")
$prepareRuntimeSkipGStreamerCheck = [bool](Get-ConfigValue -Name "PrepareRuntimeSkipGStreamerCheck" -DefaultValue $false)
if ($SkipGStreamerCheck) {
    $prepareRuntimeSkipGStreamerCheck = $true
}
$gStreamerDebBundleCacheDir = Resolve-OutputPath -Path ([string](Get-ConfigValue -Name "GStreamerDebBundleCacheDir" -DefaultValue ".runtime\apt\gstreamer")) -BaseDir $repoRoot
$gStreamerDebBundleDistroName = [string](Get-ConfigValue -Name "GStreamerDebBundleDistroName" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($gStreamerDebBundleDistroName)) {
    $gStreamerDebBundleDistroName = $managedRuntimeDistroName
}
$gStreamerDebPackages = @(
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

$values = @{
    AppName = $appName
    AppVersion = $appVersion
    DistroName = $distroName
    ServerBaseDir = $serverBaseDir
    AppHostName = $appHostName
    WslNetworkingMode = $wslNetworkingMode
    WslDnsTunneling = $wslDnsTunneling
    WslFirewall = $wslFirewall
    WslAutoProxy = $wslAutoProxy
    WslHostAddressLoopback = $wslHostAddressLoopback
    WslHyperVFirewallDefaultInboundAction = $wslHyperVFirewallDefaultInboundAction
    WslRestartAllowed = $wslRestartAllowed
    RuntimeDistroName = $managedRuntimeDistroName
    RuntimeDistroInstallRoot = $runtimeDistroInstallRoot
    AutoResetRuntimeDistro = [bool](Get-ConfigValue -Name "AutoResetRuntimeDistro" -DefaultValue $true)
    RuntimeTarPath = [string](Get-ConfigValue -Name "RuntimeTarPath" -DefaultValue "")
    PrepareRuntimeInstallAptPackages = [bool](Get-ConfigValue -Name "PrepareRuntimeInstallAptPackages" -DefaultValue $true)
    PrepareRuntimeInstallTrainingRuntime = [bool](Get-ConfigValue -Name "PrepareRuntimeInstallTrainingRuntime" -DefaultValue $true)
    TritonServerArchivePath = $tritonServerArchivePath
    PrepareRuntimeSkipTritonCheck = $prepareRuntimeSkipTritonCheck
    TritonServerImage = $tritonServerImage
    TritonServerArchiveCacheDir = $tritonServerArchiveCacheDir
    MediaMtxArchivePath = $mediaMtxArchivePath
    PrepareRuntimeSkipMediaMtxCheck = $prepareRuntimeSkipMediaMtxCheck
    MediaMtxVersion = $mediaMtxVersion
    MediaMtxArchiveCacheDir = $mediaMtxArchiveCacheDir
    MediaMtxDownloadUrl = $mediaMtxDownloadUrl
    GStreamerDebBundlePath = $gStreamerDebBundlePath
    PrepareRuntimeSkipGStreamerCheck = $prepareRuntimeSkipGStreamerCheck
    GStreamerDebBundleCacheDir = $gStreamerDebBundleCacheDir
    GStreamerDebBundleDistroName = $gStreamerDebBundleDistroName
    GStreamerDebPackages = $gStreamerDebPackages
    StreamingRtspPort = [int](Get-ConfigValue -Name "StreamingRtspPort" -DefaultValue 8554)
    StreamingWebRtcPort = [int](Get-ConfigValue -Name "StreamingWebRtcPort" -DefaultValue 8889)
    StreamingWebRtcIceTcpPort = [int](Get-ConfigValue -Name "StreamingWebRtcIceTcpPort" -DefaultValue 8189)
    StreamingTurnTcpPort = [int](Get-ConfigValue -Name "StreamingTurnTcpPort" -DefaultValue 3478)
    StreamAuthEnabled = [bool](Get-ConfigValue -Name "StreamAuthEnabled" -DefaultValue $false)
    BundlePartSizeBytes = [int64](Get-ConfigValue -Name "BundlePartSizeBytes" -DefaultValue 1GB)
    BuildFrontend = [bool](Get-ConfigValue -Name "BuildFrontend" -DefaultValue $true)
    FrontendAdminApiUrl = [string](Get-ConfigValue -Name "FrontendAdminApiUrl" -DefaultValue "")
    FrontendRuntimeAdminApiUrl = [string](Get-ConfigValue -Name "FrontendRuntimeAdminApiUrl" -DefaultValue "/runtime-api")
    FrontendResultServiceUrl = [string](Get-ConfigValue -Name "FrontendResultServiceUrl" -DefaultValue "/runtime-results")
    FrontendTriggerExecutorUrl = [string](Get-ConfigValue -Name "FrontendTriggerExecutorUrl" -DefaultValue "/runtime-trigger")
    FrontendInferenceGatewayRtUrl = [string](Get-ConfigValue -Name "FrontendInferenceGatewayRtUrl" -DefaultValue "/inference-rt")
    FrontendInferenceGatewayBatchUrl = [string](Get-ConfigValue -Name "FrontendInferenceGatewayBatchUrl" -DefaultValue "/inference-batch")
    FrontendAdminWsUrl = [string](Get-ConfigValue -Name "FrontendAdminWsUrl" -DefaultValue "")
    FrontendBasePath = [string](Get-ConfigValue -Name "FrontendBasePath" -DefaultValue "/frontend/")
}

$invalidFileNameChars = [System.IO.Path]::GetInvalidFileNameChars()
if ($appName.IndexOfAny($invalidFileNameChars) -ge 0 -or $appVersion.IndexOfAny($invalidFileNameChars) -ge 0) {
    throw "AppName oder automatisch erzeugte Version enthaelt ungueltige Zeichen fuer Windows-Dateinamen."
}

$generatedConfigPath = Join-Path $repoRoot "dist\.generated\windows-offline.$appVersion.config.ps1"
New-GeneratedConfig -OutputPath $generatedConfigPath -Values $values

Write-Log "Build-Version: $appVersion"
Write-Log "Interne Builder-WSL: $managedRuntimeDistroName"
Write-Log "Generierte Build-Config: $generatedConfigPath"

Push-Location $repoRoot
try {
    if (-not $SkipRuntimePrepare) {
        $prepareParams = @{
            Config = $generatedConfigPath
        }
        if ($SkipApt) {
            $prepareParams.SkipApt = $true
        }
        if ($SkipTrainingRuntime) {
            $prepareParams.SkipTrainingRuntime = $true
        }
        if ($SkipTritonCheck) {
            $prepareParams.SkipTritonCheck = $true
        }

        Invoke-PowerShellScript `
            -Description "Starte Runtime-Vorbereitung." `
            -ScriptPath (Join-Path $deploymentDir "prepare-wsl-runtime.ps1") `
            -Parameters $prepareParams
    }

    Invoke-PowerShellScript `
        -Description "Starte Installer-Export." `
        -ScriptPath (Join-Path $deploymentDir "export-windows-installer.ps1") `
        -Parameters @{ Config = $generatedConfigPath }
}
finally {
    Pop-Location
    Release-BuildLock
}
