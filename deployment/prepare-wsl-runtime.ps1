[CmdletBinding()]
param(
    [string]$Config = "",

    [switch]$SkipApt,
    [switch]$SkipTrainingRuntime,
    [switch]$SkipTritonCheck,
    [switch]$NoAutoCreateRuntime
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Resolve-PathForRuntime {
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

function Resolve-OutputPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDir
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $BaseDir $Path }
    return [System.IO.Path]::GetFullPath($candidate)
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

function Test-WslDistroHealthy {
    param([Parameter(Mandatory = $true)][string]$DistroName)

    $output = & wsl -d $DistroName -u root -- bash -lc "true" 2>&1
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    foreach ($item in $output) {
        $text = $item.ToString()
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            Write-Log $text
        }
    }
    return $false
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

function New-SafeFileName {
    param([Parameter(Mandatory = $true)][string]$Value)

    return ($Value -replace '[^A-Za-z0-9_.-]', '_')
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
    Invoke-CheckedCommand -Description "Pruefe Docker CLI." -FilePath $dockerPath -Arguments @("version")
    Invoke-CheckedCommand -Description "Lade Triton Server Docker-Image." -FilePath $dockerPath -Arguments @("pull", $Image)

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

    $tmpArchivePath = "$archivePath.tmp"
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
        Invoke-CheckedCommand -Description "Pruefe Triton Server Archiv." -FilePath $tarPath -Arguments @("-tzf", $tmpArchivePath, "tritonserver/bin/tritonserver")
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

function Remove-ManagedRuntimeInstallRoot {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $installRootFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    $allowedParent = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ".runtime\wsl")).TrimEnd('\') + '\'
    if (-not ($installRootFull + '\').StartsWith($allowedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Log "Ueberspringe automatisches Loeschen von RuntimeDistroInstallRoot ausserhalb von ${allowedParent}: $installRootFull"
        return
    }

    if (Test-Path -LiteralPath $installRootFull) {
        Write-Log "Entferne defektes internes Runtime-Verzeichnis: $installRootFull"
        Remove-Item -LiteralPath $installRootFull -Recurse -Force
    }
}

function Install-ManagedRuntimeDistro {
    param(
        [Parameter(Mandatory = $true)][string]$DistroName,
        [Parameter(Mandatory = $true)][string]$InstallRoot
    )

    $installRootParent = Split-Path -Parent $InstallRoot
    New-Item -ItemType Directory -Path $installRootParent -Force | Out-Null

    Write-Log "Runtime-Distribution '$DistroName' wurde nicht gefunden; installiere Ubuntu-24.04 automatisch."
    Write-Log "Ablage der Builder-Distribution: $InstallRoot"

    & wsl --set-default-version 2 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "WSL konnte nicht auf Version 2 als Standard gesetzt werden. Exit Code: $LASTEXITCODE"
    }

    $arguments = @(
        "--install",
        "Ubuntu-24.04",
        "--name",
        $DistroName,
        "--location",
        $InstallRoot,
        "--version",
        "2",
        "--no-launch",
        "--web-download"
    )

    Write-Log "Fuehre aus: wsl $($arguments -join ' ')"
    & wsl @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Die automatische Ubuntu-24.04-Installation ist fehlgeschlagen. Der Entwicklungsrechner braucht dafuer Internetzugang und je nach Windows-Setup Administratorrechte bzw. einen Neustart. Exit Code: $LASTEXITCODE"
    }

    Write-Log "Initialisiere neue Runtime-Distribution '$DistroName'."
    & wsl -d $DistroName -u root -- bash -lc "true"
    if ($LASTEXITCODE -ne 0) {
        throw "Die neue Runtime-Distribution '$DistroName' konnte nicht initialisiert werden. Exit Code: $LASTEXITCODE"
    }
}

$deploymentDir = Split-Path -Parent $PSCommandPath
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $deploymentDir ".."))

if (-not [string]::IsNullOrWhiteSpace($Config)) {
    $configPath = Resolve-PathForRuntime -Path $Config -PathType Leaf -Description "Config-Datei"
    Write-Log "Lade Config: $configPath"
    . $configPath
}
else {
    $defaultConfig = Join-Path $deploymentDir "windows-offline.config.ps1"
    if (Test-Path -LiteralPath $defaultConfig -PathType Leaf) {
        Write-Log "Lade optionale Defaults aus: $defaultConfig"
        . $defaultConfig
    }
    else {
        Write-Log "Keine Config angegeben; verwende Standardwerte."
    }
}

$RuntimeDistroName = [string](Get-ConfigValue -Name "RuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder")
$RuntimeDistroInstallRoot = [string](Get-ConfigValue -Name "RuntimeDistroInstallRoot" -DefaultValue (Join-Path $repoRoot ".runtime\wsl\$RuntimeDistroName"))
$RuntimeDistroInstallRoot = Resolve-OutputPath -Path $RuntimeDistroInstallRoot -BaseDir $repoRoot
$AutoResetRuntimeDistro = [bool](Get-ConfigValue -Name "AutoResetRuntimeDistro" -DefaultValue $false)

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw "wsl.exe wurde nicht gefunden."
}

$distros = Get-WslDistros
if ($distros -notcontains $RuntimeDistroName) {
    if ($NoAutoCreateRuntime) {
        throw "Die Runtime-Distribution '$RuntimeDistroName' wurde nicht gefunden."
    }

    Install-ManagedRuntimeDistro -DistroName $RuntimeDistroName -InstallRoot $RuntimeDistroInstallRoot
    $distros = Get-WslDistros
    if ($distros -notcontains $RuntimeDistroName) {
        throw "Die Runtime-Distribution '$RuntimeDistroName' wurde nach der automatischen Installation nicht gefunden."
    }
}
elseif (-not (Test-WslDistroHealthy -DistroName $RuntimeDistroName)) {
    if (-not $AutoResetRuntimeDistro) {
        throw "Die Runtime-Distribution '$RuntimeDistroName' existiert, startet aber nicht korrekt. Bitte WSL pruefen oder die interne Builder-Distro neu erzeugen."
    }

    Write-Log "Runtime-Distribution '$RuntimeDistroName' startet nicht korrekt; interne Builder-Distro wird neu erstellt."
    & wsl --terminate $RuntimeDistroName 2>$null | Out-Null
    & wsl --unregister $RuntimeDistroName | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Die defekte Runtime-Distribution '$RuntimeDistroName' konnte nicht entfernt werden. Exit Code: $LASTEXITCODE"
    }
    Remove-ManagedRuntimeInstallRoot -InstallRoot $RuntimeDistroInstallRoot -RepoRoot $repoRoot
    Install-ManagedRuntimeDistro -DistroName $RuntimeDistroName -InstallRoot $RuntimeDistroInstallRoot
    $distros = Get-WslDistros
    if ($distros -notcontains $RuntimeDistroName) {
        throw "Die Runtime-Distribution '$RuntimeDistroName' wurde nach dem Neuaufbau nicht gefunden."
    }
}

$installApt = $true
if ((Get-Variable -Name "PrepareRuntimeInstallAptPackages" -ErrorAction SilentlyContinue) -and -not $PrepareRuntimeInstallAptPackages) {
    $installApt = $false
}
if ($SkipApt) {
    $installApt = $false
}

$installTraining = $true
if ((Get-Variable -Name "PrepareRuntimeInstallTrainingRuntime" -ErrorAction SilentlyContinue) -and -not $PrepareRuntimeInstallTrainingRuntime) {
    $installTraining = $false
}
if ($SkipTrainingRuntime) {
    $installTraining = $false
}

$skipTriton = $false
if ((Get-Variable -Name "PrepareRuntimeSkipTritonCheck" -ErrorAction SilentlyContinue) -and $PrepareRuntimeSkipTritonCheck) {
    $skipTriton = $true
}
if ($SkipTritonCheck) {
    $skipTriton = $true
}

$repoWslPath = ConvertTo-WslPath -WindowsPath $repoRoot
$scriptWslPath = "$repoWslPath/deployment/wsl/prepare-runtime.sh"

$arguments = @(
    "--repo", $repoWslPath
)
if (-not $installApt) {
    $arguments += "--skip-apt"
}
if (-not $installTraining) {
    $arguments += "--skip-training-runtime"
}
if ($skipTriton) {
    $arguments += "--skip-triton-check"
}

$tritonArchive = ""
if ((Get-Variable -Name "TritonServerArchivePath" -ErrorAction SilentlyContinue) -and -not [string]::IsNullOrWhiteSpace([string]$TritonServerArchivePath)) {
    $tritonArchive = Resolve-PathForRuntime -Path $TritonServerArchivePath -PathType Leaf -Description "TritonServerArchivePath"
}
elseif (-not $skipTriton) {
    $tritonServerImage = [string](Get-ConfigValue -Name "TritonServerImage" -DefaultValue "nvcr.io/nvidia/tritonserver:24.08-py3")
    $tritonServerArchiveCacheDir = Resolve-OutputPath -Path ([string](Get-ConfigValue -Name "TritonServerArchiveCacheDir" -DefaultValue ".runtime\triton")) -BaseDir $repoRoot
    $tritonArchive = New-TritonServerArchiveFromDockerImage -Image $tritonServerImage -CacheDir $tritonServerArchiveCacheDir
}
if (-not [string]::IsNullOrWhiteSpace($tritonArchive)) {
    $arguments += @("--triton-archive", (ConvertTo-WslPath -WindowsPath $tritonArchive))
}

$quotedArgs = ($arguments | ForEach-Object { Quote-Bash ([string]$_) }) -join " "
$command = "bash $(Quote-Bash $scriptWslPath) $quotedArgs"

Invoke-CheckedCommand `
    -Description "Bereite WSL2-Runtime '$RuntimeDistroName' vor." `
    -FilePath "wsl" `
    -Arguments @("-d", $RuntimeDistroName, "-u", "root", "--", "bash", "-lc", $command)

Write-Log "Runtime-Distribution ist vorbereitet: $RuntimeDistroName"
