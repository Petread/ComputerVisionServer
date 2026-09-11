[CmdletBinding()]
param(
    [string[]]$Models = @(),
    [switch]$All,
    [string]$OutputDir = "",
    [string]$CacheDir = "",
    [string]$SourceDirectory = "",
    [string]$CatalogPath = "",
    [switch]$Force,
    [switch]$List,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function New-ModelEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$TaskType,
        [Parameter(Mandatory = $true)][string]$Family,
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$Urls,
        [bool]$Recommended = $false
    )

    if ([System.IO.Path]::IsPathRooted($FileName) -or $FileName -match '[\\/]' -or $FileName -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Ungueltiger Modell-Dateiname im Katalog: $FileName"
    }

    return [pscustomobject]([ordered]@{
        Id = $Id
        TaskType = $TaskType
        Family = $Family
        DisplayName = $DisplayName
        FileName = $FileName
        Urls = @($Urls)
        Recommended = $Recommended
    })
}

function Get-UltralyticsUrls {
    param([Parameter(Mandatory = $true)][string]$FileName)

    $baseUrls = @("https://github.com/ultralytics/assets/releases/download/v8.4.0")
    if ($FileName -like "yolo11*" -or $FileName -like "yolov8*") {
        $baseUrls += "https://github.com/ultralytics/assets/releases/download/v8.3.0"
    }
    return @($baseUrls | ForEach-Object { "$_/$FileName" })
}

function Get-BuiltInModelCatalog {
    $items = New-Object System.Collections.Generic.List[object]
    $sizes = @("n", "s", "m", "l", "x")
    $sizeNames = @{
        n = "Nano"
        s = "Small"
        m = "Medium"
        l = "Large"
        x = "X-Large"
    }

    foreach ($entry in @(
        @{ Id = "torchvision-classification-resnet18"; Name = "ResNet-18 Classifier"; File = "resnet18-f37072fd.pth" },
        @{ Id = "torchvision-classification-resnet50"; Name = "ResNet-50 Classifier"; File = "resnet50-11ad3fa6.pth" },
        @{ Id = "torchvision-classification-mobilenet-v3-small"; Name = "MobileNetV3 Small Classifier"; File = "mobilenet_v3_small-047dcff4.pth" },
        @{ Id = "torchvision-classification-mobilenet-v3-large"; Name = "MobileNetV3 Large Classifier"; File = "mobilenet_v3_large-8738ca79.pth" },
        @{ Id = "torchvision-classification-efficientnet-b0"; Name = "EfficientNet-B0 Classifier"; File = "efficientnet_b0_rwightman-7f5810bc.pth" }
    )) {
        $items.Add((New-ModelEntry `
            -Id $entry.Id `
            -TaskType "classification" `
            -Family "torchvision" `
            -DisplayName $entry.Name `
            -FileName $entry.File `
            -Urls @("https://download.pytorch.org/models/$($entry.File)") `
            -Recommended ($entry.Id -eq "torchvision-classification-resnet18")))
    }

    foreach ($version in @("11", "26")) {
        foreach ($size in $sizes) {
            $fileName = "yolo{0}{1}-cls.pt" -f $version, $size
            $items.Add((New-ModelEntry `
                -Id ("ultralytics-classification-yolo{0}-{1}" -f $version, $size) `
                -TaskType "classification" `
                -Family "ultralytics-yolo" `
                -DisplayName ("YOLO{0} {1} Classifier" -f $version, $sizeNames[$size]) `
                -FileName $fileName `
                -Urls (Get-UltralyticsUrls -FileName $fileName)))
        }
    }

    $openMmlab = @(
        @{ Id = "openmmlab-detection-yolov8-n"; Name = "YOLOv8 Nano Detector"; File = "yolov8_n_syncbn_fast_8xb16-500e_coco_20230114_131804-88c11cdb.pth"; Url = "https://download.openmmlab.com/mmyolo/v0/yolov8/yolov8_n_syncbn_fast_8xb16-500e_coco/yolov8_n_syncbn_fast_8xb16-500e_coco_20230114_131804-88c11cdb.pth"; Recommended = $true },
        @{ Id = "openmmlab-detection-yolov8-s"; Name = "YOLOv8 Small Detector"; File = "yolov8_s_syncbn_fast_8xb16-500e_coco_20230117_180101-5aa5f0f1.pth"; Url = "https://download.openmmlab.com/mmyolo/v0/yolov8/yolov8_s_syncbn_fast_8xb16-500e_coco/yolov8_s_syncbn_fast_8xb16-500e_coco_20230117_180101-5aa5f0f1.pth"; Recommended = $false },
        @{ Id = "openmmlab-detection-yolov8-m"; Name = "YOLOv8 Medium Detector"; File = "yolov8_m_syncbn_fast_8xb16-500e_coco_20230115_192200-c22e560a.pth"; Url = "https://download.openmmlab.com/mmyolo/v0/yolov8/yolov8_m_syncbn_fast_8xb16-500e_coco/yolov8_m_syncbn_fast_8xb16-500e_coco_20230115_192200-c22e560a.pth"; Recommended = $false },
        @{ Id = "openmmlab-detection-yolov8-l"; Name = "YOLOv8 Large Detector"; File = "yolov8_l_syncbn_fast_8xb16-500e_coco_20230217_182526-189611b6.pth"; Url = "https://download.openmmlab.com/mmyolo/v0/yolov8/yolov8_l_syncbn_fast_8xb16-500e_coco/yolov8_l_syncbn_fast_8xb16-500e_coco_20230217_182526-189611b6.pth"; Recommended = $false },
        @{ Id = "openmmlab-detection-yolov8-x"; Name = "YOLOv8 X-Large Detector"; File = "yolov8_x_syncbn_fast_8xb16-500e_coco_20230218_023338-5674673c.pth"; Url = "https://download.openmmlab.com/mmyolo/v0/yolov8/yolov8_x_syncbn_fast_8xb16-500e_coco/yolov8_x_syncbn_fast_8xb16-500e_coco_20230218_023338-5674673c.pth"; Recommended = $false }
    )
    foreach ($entry in $openMmlab) {
        $items.Add((New-ModelEntry `
            -Id $entry.Id `
            -TaskType "detection" `
            -Family "openmmlab-mmyolo" `
            -DisplayName $entry.Name `
            -FileName $entry.File `
            -Urls @($entry.Url) `
            -Recommended ([bool]$entry.Recommended)))
    }

    foreach ($version in @("11", "26")) {
        foreach ($size in $sizes) {
            $fileName = "yolo{0}{1}.pt" -f $version, $size
            $items.Add((New-ModelEntry `
                -Id ("ultralytics-detection-yolo{0}-{1}" -f $version, $size) `
                -TaskType "detection" `
                -Family "ultralytics-yolo" `
                -DisplayName ("YOLO{0} {1} Detector" -f $version, $sizeNames[$size]) `
                -FileName $fileName `
                -Urls (Get-UltralyticsUrls -FileName $fileName)))
        }
    }

    foreach ($size in $sizes) {
        $fileName = "yolo26{0}-obb.pt" -f $size
        $items.Add((New-ModelEntry `
            -Id ("ultralytics-oriented-detection-yolo26-{0}" -f $size) `
            -TaskType "oriented_detection" `
            -Family "ultralytics-yolo" `
            -DisplayName ("YOLO26 {0} OBB Detector" -f $sizeNames[$size]) `
            -FileName $fileName `
            -Urls (Get-UltralyticsUrls -FileName $fileName) `
            -Recommended ($size -eq "n")))
    }

    foreach ($entry in @(
        @{ Id = "torchvision-segmentation-deeplabv3-resnet50"; Name = "DeepLabV3 ResNet-50 Segmenter"; File = "deeplabv3_resnet50_coco-cd0a2569.pth"; Recommended = $true },
        @{ Id = "torchvision-segmentation-fcn-resnet50"; Name = "FCN ResNet-50 Segmenter"; File = "fcn_resnet50_coco-1167a1af.pth"; Recommended = $false },
        @{ Id = "torchvision-segmentation-lraspp-mobilenet-v3-large"; Name = "LRASPP MobileNetV3 Segmenter"; File = "lraspp_mobilenet_v3_large-d234d4ea.pth"; Recommended = $false }
    )) {
        $items.Add((New-ModelEntry `
            -Id $entry.Id `
            -TaskType "segmentation" `
            -Family "torchvision" `
            -DisplayName $entry.Name `
            -FileName $entry.File `
            -Urls @("https://download.pytorch.org/models/$($entry.File)") `
            -Recommended ([bool]$entry.Recommended)))
    }

    foreach ($version in @("v8", "11", "26")) {
        foreach ($size in $sizes) {
            $fileName = if ($version -eq "v8") { "yolov8{0}-seg.pt" -f $size } else { "yolo{0}{1}-seg.pt" -f $version, $size }
            $versionLabel = if ($version -eq "v8") { "v8" } else { $version }
            $items.Add((New-ModelEntry `
                -Id ("ultralytics-segmentation-yolo{0}-{1}" -f $versionLabel, $size) `
                -TaskType "segmentation" `
                -Family "ultralytics-yolo" `
                -DisplayName ("YOLO{0} {1} Segmenter" -f $versionLabel, $sizeNames[$size]) `
                -FileName $fileName `
                -Urls (Get-UltralyticsUrls -FileName $fileName)))
        }
    }

    $items.Add((New-ModelEntry `
        -Id "anomalib-patchcore-wide-resnet50-2" `
        -TaskType "anomaly" `
        -Family "anomalib-patchcore" `
        -DisplayName "PatchCore WideResNet-50-2 Backbone" `
        -FileName "wide_resnet50_2-95faca4d.pth" `
        -Urls @("https://download.pytorch.org/models/wide_resnet50_2-95faca4d.pth") `
        -Recommended $true))

    return @($items.ToArray())
}

function Import-ExternalCatalog {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Katalog wurde nicht gefunden: $fullPath"
    }
    $raw = Get-Content -LiteralPath $fullPath -Raw | ConvertFrom-Json
    $items = if ($raw.PSObject.Properties.Name -contains "models") { @($raw.models) } else { @($raw) }
    return @(
        foreach ($item in $items) {
            New-ModelEntry `
                -Id ([string]$item.id) `
                -TaskType ([string]$item.task_type) `
                -Family ([string]$item.family) `
                -DisplayName ([string]$item.display_name) `
                -FileName ([string]$item.file_name) `
                -Urls @($item.urls | ForEach-Object { [string]$_ }) `
                -Recommended ([bool]($item.recommended))
        }
    )
}

function Resolve-SelectedModels {
    param(
        [Parameter(Mandatory = $true)][object[]]$Catalog,
        [string[]]$RequestedModels,
        [bool]$SelectAll
    )

    $tokens = New-Object System.Collections.Generic.List[string]
    foreach ($value in @($RequestedModels)) {
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        foreach ($part in ([string]$value -split ",")) {
            $trimmed = $part.Trim()
            if (-not [string]::IsNullOrWhiteSpace($trimmed)) {
                $tokens.Add($trimmed)
            }
        }
    }
    if ($SelectAll -or $tokens.Count -eq 0 -or @($tokens | Where-Object { $_ -ieq "all" }).Count -gt 0) {
        return @($Catalog)
    }

    $byId = @{}
    foreach ($item in $Catalog) {
        $byId[[string]$item.Id] = $item
    }

    $selected = New-Object System.Collections.Generic.List[object]
    $unknown = New-Object System.Collections.Generic.List[string]
    foreach ($token in $tokens) {
        $match = $null
        foreach ($key in $byId.Keys) {
            if ($key -ieq $token) {
                $match = $byId[$key]
                break
            }
        }
        if ($null -eq $match) {
            $unknown.Add($token)
        }
        else {
            $selected.Add($match)
        }
    }
    if ($unknown.Count -gt 0) {
        throw "Unbekannte Modell-ID(s): $($unknown -join ', ')"
    }
    return @($selected | Sort-Object Id -Unique)
}

function Resolve-ExistingSourceFile {
    param(
        [string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$FileName
    )

    if ([string]::IsNullOrWhiteSpace($SourceDir)) {
        return ""
    }
    $fullSourceDir = [System.IO.Path]::GetFullPath($SourceDir)
    $candidate = Join-Path $fullSourceDir $FileName
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($candidate)
    }
    return ""
}

function Get-ModelFile {
    param(
        [Parameter(Mandatory = $true)]$Model,
        [Parameter(Mandatory = $true)][string]$ResolvedCacheDir,
        [string]$ResolvedSourceDirectory,
        [bool]$ForceDownload
    )

    $fileName = [string]$Model.FileName
    $sourceFile = Resolve-ExistingSourceFile -SourceDir $ResolvedSourceDirectory -FileName $fileName
    if (-not [string]::IsNullOrWhiteSpace($sourceFile)) {
        Write-Log "Verwende lokale Quelldatei: $fileName"
        return [pscustomobject]@{
            Path = $sourceFile
            Source = $sourceFile
        }
    }

    New-Item -ItemType Directory -Path $ResolvedCacheDir -Force | Out-Null
    $targetPath = Join-Path $ResolvedCacheDir $fileName
    if ((-not $ForceDownload) -and (Test-Path -LiteralPath $targetPath -PathType Leaf) -and ((Get-Item -LiteralPath $targetPath).Length -gt 0)) {
        Write-Log "Verwende Cache-Datei: $fileName"
        return [pscustomobject]@{
            Path = [System.IO.Path]::GetFullPath($targetPath)
            Source = "cache"
        }
    }

    $urls = @($Model.Urls | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($urls.Count -eq 0) {
        throw "Fuer $($Model.Id) ist keine Download-URL im Katalog hinterlegt."
    }

    $tmpPath = "$targetPath.download"
    Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
    $lastError = ""
    foreach ($url in $urls) {
        try {
            Write-Log "Lade $fileName von $url"
            Invoke-WebRequest -Uri $url -OutFile $tmpPath -UseBasicParsing
            if (-not (Test-Path -LiteralPath $tmpPath -PathType Leaf) -or ((Get-Item -LiteralPath $tmpPath).Length -le 0)) {
                throw "Download lieferte keine Daten."
            }
            Move-Item -LiteralPath $tmpPath -Destination $targetPath -Force
            return [pscustomobject]@{
                Path = [System.IO.Path]::GetFullPath($targetPath)
                Source = $url
            }
        }
        catch {
            $lastError = $_.Exception.Message
            Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
            Write-Log "WARNUNG: Download fehlgeschlagen: $lastError"
        }
    }
    throw "Download fehlgeschlagen fuer $fileName. Letzter Fehler: $lastError"
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
        "$hash  $($fileName.Replace('\', '/'))"
    }
    Set-Content -LiteralPath (Join-Path $ReleaseDir "manifest.sha256") -Value $lines -Encoding ascii
}

function Add-FileToZipBundle {
    param(
        [Parameter(Mandatory = $true)][System.IO.Compression.ZipArchive]$ZipArchive,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$EntryName
    )

    $extension = [System.IO.Path]::GetExtension($EntryName).ToLowerInvariant()
    $compressionLevel = if ($extension -in @(".pt", ".pth")) {
        [System.IO.Compression.CompressionLevel]::NoCompression
    }
    else {
        [System.IO.Compression.CompressionLevel]::Optimal
    }
    $entry = $ZipArchive.CreateEntry($EntryName.Replace("\", "/"), $compressionLevel)
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
            Write-Log "Fuege zum ZIP hinzu: $fileName"
            Add-FileToZipBundle -ZipArchive $zipArchive -SourcePath $filePath -EntryName $fileName
        }
    }
    finally {
        $zipArchive.Dispose()
        $zipStream.Dispose()
    }
}

$catalog = if ([string]::IsNullOrWhiteSpace($CatalogPath)) {
    Get-BuiltInModelCatalog
}
else {
    Import-ExternalCatalog -Path $CatalogPath
}

if ($List) {
    $view = @($catalog | Sort-Object TaskType, DisplayName | Select-Object Id, TaskType, Family, DisplayName, FileName, Recommended)
    if ($Json) {
        $view | ConvertTo-Json -Depth 6
    }
    else {
        $view | Format-Table -AutoSize
    }
    return
}

$selectedModels = @(Resolve-SelectedModels -Catalog $catalog -RequestedModels $Models -SelectAll ([bool]$All))
if ($selectedModels.Count -eq 0) {
    throw "Keine Modelle fuer das Update ausgewaehlt."
}

$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repoRoot "dist"
}
if ([string]::IsNullOrWhiteSpace($CacheDir)) {
    $CacheDir = Join-Path $OutputDir "pretrained-models-cache"
}
$resolvedOutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$resolvedCacheDir = [System.IO.Path]::GetFullPath($CacheDir)
$resolvedSourceDirectory = if ([string]::IsNullOrWhiteSpace($SourceDirectory)) { "" } else { [System.IO.Path]::GetFullPath($SourceDirectory) }

if (-not [string]::IsNullOrWhiteSpace($resolvedSourceDirectory) -and -not (Test-Path -LiteralPath $resolvedSourceDirectory -PathType Container)) {
    throw "Quellordner wurde nicht gefunden: $resolvedSourceDirectory"
}

New-Item -ItemType Directory -Path $resolvedOutputDir -Force | Out-Null
New-Item -ItemType Directory -Path $resolvedCacheDir -Force | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$packageVersion = "pretrained-$timestamp"
$bundlePath = Join-Path $resolvedOutputDir ("ybb-{0}-pretrained-models-update.zip" -f $timestamp)
$releaseDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-pretrained-models-update-" + [System.Guid]::NewGuid().ToString("N"))
$modelDir = Join-Path $releaseDir "models"

try {
    New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
    $manifestModels = New-Object System.Collections.Generic.List[object]
    $bundleFiles = New-Object System.Collections.Generic.List[string]
    $bundleFiles.Add("pretrained-models-manifest.json")

    foreach ($model in $selectedModels) {
        $file = Get-ModelFile -Model $model -ResolvedCacheDir $resolvedCacheDir -ResolvedSourceDirectory $resolvedSourceDirectory -ForceDownload ([bool]$Force)
        $stagedRelativePath = "models/$($model.FileName)"
        $stagedPath = Join-Path $releaseDir $stagedRelativePath
        Copy-Item -LiteralPath $file.Path -Destination $stagedPath -Force
        $fileInfo = Get-Item -LiteralPath $stagedPath
        $sha256 = (Get-FileHash -LiteralPath $stagedPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifestModels.Add([pscustomobject]([ordered]@{
            id = [string]$model.Id
            task_type = [string]$model.TaskType
            family = [string]$model.Family
            display_name = [string]$model.DisplayName
            file_name = [string]$model.FileName
            source_relative_path = $stagedRelativePath
            target_relative_path = [string]$model.FileName
            source = [string]$file.Source
            size_bytes = [int64]$fileInfo.Length
            sha256 = $sha256
        }))
        $bundleFiles.Add($stagedRelativePath)
    }

    $manifest = [ordered]@{
        package_type = "pretrained-models-update"
        version = $packageVersion
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        target_root = "/srv/cv-platform/control/pretrained-checkpoints"
        model_count = $manifestModels.Count
        models = @($manifestModels.ToArray())
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $releaseDir "pretrained-models-manifest.json") -Encoding utf8
    New-Sha256Manifest -ReleaseDir $releaseDir -FileNames @($bundleFiles.ToArray())
    $bundleFiles.Add("manifest.sha256")

    if (Test-Path -LiteralPath $bundlePath -PathType Leaf) {
        Remove-Item -LiteralPath $bundlePath -Force
    }
    Write-Log "Erstelle Pretrained-Modellupdate: $bundlePath"
    New-ZipBundle -ReleaseDir $releaseDir -BundlePath $bundlePath -FileNames @($bundleFiles.ToArray())
    Write-Log "Pretrained-Modellupdate fertig: $bundlePath"
}
finally {
    if (Test-Path -LiteralPath $releaseDir -PathType Container) {
        Remove-Item -LiteralPath $releaseDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
