param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "dist")
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$references = @(
    "System.dll",
    "System.Core.dll",
    "System.Drawing.dll",
    "System.IO.Compression.dll",
    "System.IO.Compression.FileSystem.dll",
    "System.Windows.Forms.dll",
    "System.Web.Extensions.dll"
)

function Build-Application {
    param(
        [Parameter(Mandatory = $true)][string]$SourceName,
        [Parameter(Mandatory = $true)][string]$OutputName
    )
    $source = Join-Path $PSScriptRoot $SourceName
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "C# source missing: $source"
    }
    $output = Join-Path $OutputDirectory $OutputName
    $candidate = Join-Path $OutputDirectory ($OutputName + ".build.exe")
    if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Force }
    Add-Type -Path $source -OutputAssembly $candidate -OutputType WindowsApplication `
        -ReferencedAssemblies $references
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Native Windows application was not created: $OutputName"
    }
    try {
        Move-Item -LiteralPath $candidate -Destination $output -Force -ErrorAction Stop
    }
    catch {
        if (-not ($_.Exception -is [System.UnauthorizedAccessException]) -and
            -not ($_.Exception -is [System.IO.IOException])) {
            throw
        }
        $output = Join-Path $OutputDirectory ([IO.Path]::GetFileNameWithoutExtension($OutputName) + ".updated.exe")
        Move-Item -LiteralPath $candidate -Destination $output -Force
        Write-Warning "The existing application is running. Close it and start the updated executable."
    }
    Write-Host "Created $output"
}

Build-Application -SourceName "YBB.EdgeBundleBuilder.cs" -OutputName "YBB-Edge-Bundle-Builder.exe"
Build-Application -SourceName "YBB.EdgeWindowsInstaller.cs" -OutputName "YBB-Edge-Windows-Installer.exe"
