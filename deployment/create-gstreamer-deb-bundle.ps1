[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DistroName,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [AllowEmptyString()][string]$PackageListPath = "",
    [string[]]$PackageNames = @(
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
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
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

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw "wsl.exe wurde nicht gefunden."
}

$resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDir = Split-Path -Parent $resolvedOutputPath
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-gstreamer-debs-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
$packageListPath = if ([string]::IsNullOrWhiteSpace($PackageListPath)) {
    Join-Path $tempDir "root-packages.txt"
}
else {
    [System.IO.Path]::GetFullPath($PackageListPath)
}
$scriptPath = Join-Path $tempDir "create-bundle.sh"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
if ([string]::IsNullOrWhiteSpace($PackageListPath)) {
    $rootPackages = [string[]]@($PackageNames | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
    [System.IO.File]::WriteAllLines($packageListPath, $rootPackages, $utf8NoBom)
}
elseif (-not (Test-Path -LiteralPath $packageListPath -PathType Leaf)) {
    throw "PackageListPath wurde nicht gefunden: $packageListPath"
}

$bashScript = @'
set -euo pipefail

root_packages_file="$1"
output_archive="$2"
work_dir="$(mktemp -d /tmp/ybb-gstreamer-debs.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$work_dir/debs"
chmod 755 "$work_dir" "$work_dir/debs"

export DEBIAN_FRONTEND=noninteractive

enable_ubuntu_components() {
  if [[ ! -f /etc/os-release ]]; then
    return
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    return
  fi
  if [[ -f /etc/apt/sources.list.d/ubuntu.sources ]]; then
    python3 - <<'PY'
from pathlib import Path

path = Path("/etc/apt/sources.list.d/ubuntu.sources")
text = path.read_text()
changed = False
lines = []
for line in text.splitlines():
    if line.startswith("Components:"):
        parts = line.split()
        components = parts[1:]
        for component in ("universe", "multiverse"):
            if component not in components:
                components.append(component)
                changed = True
        line = "Components: " + " ".join(components)
    lines.append(line)
if changed:
    path.write_text("\n".join(lines) + "\n")
PY
  elif [[ -f /etc/apt/sources.list ]]; then
    sed -i -E 's/(^deb .* )(main)(.*)$/\1main universe multiverse\3/' /etc/apt/sources.list
  fi
}

has_package_candidate() {
  local package_name="$1"
  apt-cache policy "$package_name" 2>/dev/null |
    awk '
      /^[[:space:]]*Candidate:/ { found = 1; candidate = $2 }
      END { exit (!(found && candidate != "" && candidate != "(none)")) }
    '
}

enable_ubuntu_components
apt-get update

mapfile -t root_packages < "$root_packages_file"
if ((${#root_packages[@]} == 0)); then
  echo "No GStreamer packages requested." >&2
  exit 1
fi

requested_packages=()
for root_package in "${root_packages[@]}"; do
  root_package="${root_package//$'\r'/}"
  if [[ -z "$root_package" || "$root_package" =~ ^# ]]; then
    continue
  fi
  if ! has_package_candidate "$root_package"; then
    echo "WARN: requested GStreamer package is not available in this Builder-WSL apt cache: $root_package" >&2
    continue
  fi
  requested_packages+=("$root_package")
  printf '%s\n' "$root_package" >> "$work_dir/packages.raw.txt"
done

if ((${#requested_packages[@]} == 0)); then
  echo "No resolvable GStreamer Debian packages were found in the Builder-WSL apt cache." >&2
  echo "Check that the Builder-WSL is Ubuntu with main/universe/multiverse repositories enabled and has internet access." >&2
  exit 100
fi
sed '/^$/d' "$work_dir/packages.raw.txt" | sort -u > "$work_dir/root-packages.resolved.txt"

mkdir -p "$work_dir/debs/partial"
touch "$work_dir/status"
apt-get \
  -o Dir::State::status="$work_dir/status" \
  -o Dir::Cache::archives="$work_dir/debs" \
  -o Debug::NoLocking=1 \
  -o APT::Sandbox::User=root \
  --download-only \
  --no-install-recommends \
  install -y "${requested_packages[@]}"

shopt -s nullglob
deb_files=("$work_dir"/debs/*.deb)
downloaded=${#deb_files[@]}

if ((downloaded == 0)); then
  echo "No Debian packages were downloaded for the GStreamer bundle." >&2
  exit 1
fi

for deb_file in "${deb_files[@]}"; do
  dpkg-deb -f "$deb_file" Package
done | sort -u > "$work_dir/packages.txt"
cp "$root_packages_file" "$work_dir/root-packages.txt"
{
  echo "bundle_type=gstreamer-deb-bundle"
  echo "generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "distro_id=$(grep -E '^ID=' /etc/os-release | cut -d= -f2 | tr -d '\"')"
  echo "distro_version=$(grep -E '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '\"')"
  echo "architecture=$(dpkg --print-architecture)"
  echo "package_count=$downloaded"
} > "$work_dir/manifest.properties"

tar -C "$work_dir" -czf "$output_archive" manifest.properties root-packages.txt packages.txt debs
if [[ ! -s "$output_archive" ]]; then
  echo "GStreamer Debian package bundle was not created." >&2
  exit 1
fi
'@
[System.IO.File]::WriteAllText($scriptPath, ($bashScript -replace "`r`n", "`n"), $utf8NoBom)

try {
    $wslPackageListPath = ConvertTo-WslPath -WindowsPath $packageListPath
    $wslScriptPath = ConvertTo-WslPath -WindowsPath $scriptPath
    $wslOutputPath = ConvertTo-WslPath -WindowsPath $resolvedOutputPath
    $command = "bash $(Quote-Bash $wslScriptPath) $(Quote-Bash $wslPackageListPath) $(Quote-Bash $wslOutputPath)"
    Write-Log "Erzeuge GStreamer Debian-Paketbundle in WSL '$DistroName': $resolvedOutputPath"
    & wsl -d $DistroName -u root -- bash -lc $command
    if ($LASTEXITCODE -ne 0) {
        throw "GStreamer Debian-Paketbundle konnte nicht erzeugt werden. Exit Code: $LASTEXITCODE"
    }
    Write-Output $resolvedOutputPath
}
finally {
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}
