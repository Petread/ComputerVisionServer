param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [string]$InstallRoot = "C:\Program Files\YBB Edge",
    [string]$StateRoot = "C:\ProgramData\YBB\Edge",
    [string]$PythonExecutable = "",
    [string]$DeviceConfigPath = "",
    [switch]$VerifyOnly,
    [string]$OpenSslExecutable = "openssl.exe",
    [string]$TrustedReleasePublicKey = "C:\ProgramData\YBB\Trust\release-signing-public.pem"
)
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer from an elevated PowerShell."
}
$wheelhouse = Join-Path $BundleRoot "wheelhouse"
$bundleConfig = Join-Path $BundleRoot "device.env"
$winsw = Join-Path $BundleRoot "winsw.exe"
$pythonRuntime = Join-Path $BundleRoot "python-runtime.zip"
$cudaInstaller = Join-Path $BundleRoot "cuda-runtime-installer.exe"
if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) { throw "wheelhouse missing" }
if (-not (Test-Path -LiteralPath $bundleConfig -PathType Leaf)) { throw "bundled device.env defaults missing" }
if (-not (Test-Path -LiteralPath $winsw -PathType Leaf)) { throw "winsw.exe missing" }
if (-not (Test-Path -LiteralPath $pythonRuntime -PathType Leaf)) { throw "python-runtime.zip missing" }
if (-not (Test-Path -LiteralPath $cudaInstaller -PathType Leaf)) { throw "cuda-runtime-installer.exe missing" }
$checksumFile = Join-Path $BundleRoot "SHA256SUMS"
if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) { throw "Mandatory SHA256SUMS missing" }
$signaturePolicyFile = Join-Path $BundleRoot "SIGNATURE_POLICY"
if (-not (Test-Path -LiteralPath $signaturePolicyFile -PathType Leaf)) { throw "SIGNATURE_POLICY missing" }
$resolvedBundle = (Resolve-Path -LiteralPath $BundleRoot).Path.TrimEnd('\') + '\'
Get-Content -LiteralPath $checksumFile | ForEach-Object {
    if ($_ -notmatch '^([a-fA-F0-9]{64})\s+\*?(.+)$') { throw "Invalid SHA256SUMS line: $_" }
    $expected = $matches[1].ToUpperInvariant()
    $candidate = (Resolve-Path -LiteralPath (Join-Path $BundleRoot $matches[2])).Path
    if (-not $candidate.StartsWith($resolvedBundle, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Checksum path leaves bundle root: $candidate"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash
    if ($actual -ne $expected) { throw "Checksum mismatch: $candidate" }
}
$signaturePolicy = (Get-Content -LiteralPath $signaturePolicyFile -Raw).Trim()
if ($signaturePolicy -in @("signed", "required")) {
    $signature = Join-Path $BundleRoot "SHA256SUMS.sig"
    if (-not (Test-Path -LiteralPath $TrustedReleasePublicKey -PathType Leaf) -or -not (Test-Path -LiteralPath $signature -PathType Leaf)) {
        throw "Signed release is missing its signature or pinned release public key"
    }
    if (-not (Get-Command $OpenSslExecutable -ErrorAction SilentlyContinue)) { throw "OpenSSL is required for signed releases" }
    & $OpenSslExecutable dgst -sha256 -verify $TrustedReleasePublicKey -signature $signature $checksumFile
    if ($LASTEXITCODE -ne 0) { throw "Release signature verification failed" }
} elseif ($signaturePolicy -ne "prepared") {
    throw "Unsupported SIGNATURE_POLICY: $signaturePolicy"
}
if ($VerifyOnly) {
    Write-Host "Bundle checksums and release signature policy verified."
    exit 0
}
$configSource = if ($DeviceConfigPath) { $DeviceConfigPath } else { $bundleConfig }
$existingConfig = Join-Path $InstallRoot "device.env"
if (-not $DeviceConfigPath -and (Test-Path -LiteralPath $existingConfig -PathType Leaf)) {
    $configSource = $existingConfig
}
if (-not (Test-Path -LiteralPath $configSource -PathType Leaf)) {
    throw "UI-generated device configuration missing: $configSource"
}

$cudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.2"
$cudaBin = Join-Path $cudaRoot "bin"
$cudaRuntimePresent = (Test-Path -LiteralPath (Join-Path $cudaBin "cudart64_13.dll") -PathType Leaf)
$cudaNvrtcPresent = @(Get-ChildItem -LiteralPath $cudaBin -Filter "nvrtc64_*.dll" -ErrorAction SilentlyContinue).Count -gt 0
$rebootRequired = $false
if (-not $cudaRuntimePresent -or -not $cudaNvrtcPresent) {
    $cudaComponents = @(
        "crt_13.2", "cudart_13.2", "cublas_13.2", "cufft_13.2", "curand_13.2",
        "cusolver_13.2", "cusparse_13.2", "npp_13.2", "nvfatbin_13.2",
        "nvjitlink_13.2", "nvptxcompiler_13.2", "nvrtc_13.2"
    )
    $cudaArguments = if (Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue) {
        @("-s") + $cudaComponents
    } else {
        Write-Host "No NVIDIA driver detected; installing the complete bundled CUDA package including its driver."
        @("-s")
    }
    $cudaProcess = Start-Process -FilePath $cudaInstaller -ArgumentList $cudaArguments `
        -Wait -PassThru -WindowStyle Hidden
    if ($cudaProcess.ExitCode -notin @(0, 1641, 3010)) {
        throw "Bundled CUDA 13.2 installation failed with exit code $($cudaProcess.ExitCode)"
    }
    $rebootRequired = $cudaProcess.ExitCode -in @(1641, 3010)
}
if (-not (Test-Path -LiteralPath $cudaBin -PathType Container)) {
    throw "Bundled CUDA 13.2 installation did not create $cudaBin"
}
$env:CUDA_PATH = $cudaRoot
$env:PATH = "$cudaBin;$env:PATH"

New-Item -ItemType Directory -Force -Path $InstallRoot, $StateRoot | Out-Null
$existingService = Get-Service -Name YBBEdgeAgent -ErrorAction SilentlyContinue
if ($existingService) { Stop-Service -Name YBBEdgeAgent -Force }
Copy-Item -LiteralPath $winsw -Destination (Join-Path $InstallRoot "ybb-edge-service.exe") -Force
if (-not $PythonExecutable) {
    $runtimeRoot = Join-Path $InstallRoot "python-runtime"
    if (Test-Path -LiteralPath $runtimeRoot) { Remove-Item -LiteralPath $runtimeRoot -Recurse -Force }
    Expand-Archive -LiteralPath $pythonRuntime -DestinationPath $runtimeRoot -Force
    $embeddedPython = Get-ChildItem -LiteralPath $runtimeRoot -Filter python.exe -Recurse | Select-Object -First 1
    if (-not $embeddedPython) { throw "python.exe missing in python-runtime.zip" }
    $PythonExecutable = $embeddedPython.FullName
}
$config = Join-Path $InstallRoot "device.env"
$configValidator = Join-Path $BundleRoot "deployment\edge\validate-device-env.py"
$platformProfile = Join-Path $BundleRoot "platform-profile.json"
if (-not (Test-Path -LiteralPath $configValidator -PathType Leaf)) {
    throw "Device configuration validator missing"
}
& $PythonExecutable $configValidator --input $configSource --output $config --profile $platformProfile
if ($LASTEXITCODE -ne 0) { throw "UI-generated device configuration is invalid" }
& $PythonExecutable -m venv (Join-Path $InstallRoot "venv")
if ($LASTEXITCODE -ne 0) { throw "Bundled Python runtime could not create the service venv" }
$python = Join-Path $InstallRoot "venv\Scripts\python.exe"
$runtimeRequirements = Join-Path $BundleRoot "runtime-requirements.txt"
if (-not (Test-Path -LiteralPath $runtimeRequirements -PathType Leaf)) { throw "runtime-requirements.txt missing" }
& $python -m pip install --no-index --upgrade --force-reinstall --find-links $wheelhouse --disable-pip-version-check --requirement $runtimeRequirements
if ($LASTEXITCODE -ne 0) { throw "Offline Python dependency installation failed" }
& $python -m edge_agent.tensorrt_smoke
if ($LASTEXITCODE -ne 0) {
    if ($rebootRequired) {
        Write-Warning "TensorRT GPU smoke test is pending because the bundled NVIDIA driver requested a reboot."
        [IO.File]::WriteAllText(
            (Join-Path $StateRoot "tensorrt-smoke-pending"),
            "Run '$python -m edge_agent.tensorrt_smoke' after restart.`r`n",
            [Text.UTF8Encoding]::new($false)
        )
    } else {
        throw "Bundled TensorRT build and GPU inference smoke test failed"
    }
}

$values = @{}
Get-Content -LiteralPath $config | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
$port = if ($values.YBB_EDGE_CONTROL_PORT) { $values.YBB_EDGE_CONTROL_PORT } else { "9443" }
$plantPort = if ($values.YBB_EDGE_PLANT_API_PORT) { $values.YBB_EDGE_PLANT_API_PORT } else { "9444" }
$serverIp = $values.YBB_EDGE_SERVER_IP
if (-not $serverIp) { throw "YBB_EDGE_SERVER_IP is required" }
$plantAllowed = if ($values.YBB_EDGE_PLANT_ALLOWED_IPS) {
    @($values.YBB_EDGE_PLANT_ALLOWED_IPS.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} else { @() }
foreach ($candidate in $plantAllowed) {
    $parsedIp = $null
    if (-not [Net.IPAddress]::TryParse($candidate, [ref]$parsedIp)) {
        throw "Invalid YBB_EDGE_PLANT_ALLOWED_IPS entry: $candidate"
    }
}
$values["YBB_EDGE_DATA_ROOT"] = $StateRoot
$opensslCommand = Get-Command $OpenSslExecutable -ErrorAction SilentlyContinue
if ($opensslCommand) { $values["YBB_EDGE_OPENSSL_EXECUTABLE"] = $opensslCommand.Source }
$values["YBB_EDGE_TRUSTED_RELEASE_KEY"] = $TrustedReleasePublicKey
$environmentXml = ($values.GetEnumerator() | Sort-Object Name | ForEach-Object {
    $name = [Security.SecurityElement]::Escape([string]$_.Name)
    $value = [Security.SecurityElement]::Escape([string]$_.Value)
    "  <env name=`"$name`" value=`"$value`" />"
}) -join "`r`n"
$launcher = Join-Path $InstallRoot "edge-runtime-launcher.py"
Copy-Item -LiteralPath (Join-Path $BundleRoot "deployment\edge\edge-runtime-launcher.py") -Destination $launcher -Force
Copy-Item -LiteralPath (Join-Path $BundleRoot "deployment\edge\check-runtime-dependencies.py") -Destination $InstallRoot -Force
$xml = @"
<service>
  <id>YBBEdgeAgent</id><name>YBB Edge Agent</name>
  <description>YBB durable edge runtime agent</description>
  <executable>$python</executable>
  <arguments>&quot;$launcher&quot;</arguments>
  <workingdirectory>$StateRoot</workingdirectory>
$environmentXml
  <startmode>Automatic</startmode><onfailure action="restart" delay="3 sec" />
</service>
"@
$xmlPath = Join-Path $InstallRoot "ybb-edge-service.xml"
[IO.File]::WriteAllText($xmlPath, $xml, [Text.UTF8Encoding]::new($false))
if (-not $existingService) {
    & (Join-Path $InstallRoot "ybb-edge-service.exe") install
    if ($LASTEXITCODE -ne 0) { throw "WinSW service installation failed" }
}
& (Join-Path $InstallRoot "ybb-edge-service.exe") start
if ($LASTEXITCODE -ne 0) { throw "WinSW service start failed" }
New-NetFirewallRule -DisplayName "YBB Edge Control" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $port -RemoteAddress $serverIp -Profile Any | Out-Null
if ($plantAllowed.Count -gt 0) {
    New-NetFirewallRule -DisplayName "YBB Edge Plant Trigger" -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $plantPort -RemoteAddress $plantAllowed -Profile Any | Out-Null
} else {
    Write-Host "No remote SPS/HTTP trigger clients are allowed on TCP $plantPort."
}
Write-Host "YBB Edge Runtime installed fully offline."
if ($rebootRequired) {
    Write-Host "A Windows restart is required to activate the bundled NVIDIA driver."
}
