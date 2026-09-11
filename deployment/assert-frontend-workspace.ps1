function Assert-ProductionWorkspaceBuild {
    param([Parameter(Mandatory = $true)][string]$DistPath)

    $migration = Join-Path $PSScriptRoot '..\backend\migrations\versions\20260907_0037_runtime_places.py'
    if (-not (Test-Path -LiteralPath $migration -PathType Leaf)) {
        throw 'Die Datenbankmigration fuer Bereiche und Anlagen fehlt im Anwendungscode.'
    }

    $manifestPath = Join-Path $DistPath '.vite\manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Produktions-Frontend-Manifest fehlt. Bitte den normalen Admin-Web-Build ausfuehren: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $javascript = [System.Text.StringBuilder]::new()
    foreach ($entry in $manifest.PSObject.Properties) {
        if ($entry.Name -match '(^|/)demo/') {
            throw 'Ein Demo-Frontend darf nicht als Produktionsupdate exportiert werden.'
        }
        $item = $entry.Value
        $files = @([string]$item.file)
        if ($item.PSObject.Properties.Name -contains 'css') { $files += @($item.css) }
        if ($item.PSObject.Properties.Name -contains 'assets') { $files += @($item.assets) }
        foreach ($file in $files) {
            $asset = [System.IO.Path]::GetFullPath((Join-Path $DistPath $file))
            $root = [System.IO.Path]::GetFullPath($DistPath).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
            if (-not $asset.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $asset -PathType Leaf)) {
                throw "Produktions-Frontend-Asset fehlt oder liegt ausserhalb des Builds: $file"
            }
            if ($file.EndsWith('.js')) { [void]$javascript.Append((Get-Content -LiteralPath $asset -Raw)) }
        }
        foreach ($kind in @('imports', 'dynamicImports')) {
            if ($item.PSObject.Properties.Name -contains $kind) {
                foreach ($dependency in @($item.$kind)) {
                    if (-not ($manifest.PSObject.Properties.Name -contains $dependency)) {
                        throw "Frontend-Chunk fehlt im Manifest: $dependency"
                    }
                }
            }
        }
    }
    if (-not $javascript.ToString().Contains('cv-production-workspace-v1')) {
        throw 'Der Build enthaelt nicht den produktiven Workspace-Einstieg.'
    }
}
