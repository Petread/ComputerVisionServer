param([int]$Port = 4173)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$appRoot = Join-Path $repoRoot 'frontend/apps/admin-web'
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$nodePath = if ($nodeCommand) { $nodeCommand.Source } else { Join-Path $env:ProgramFiles 'nodejs/node.exe' }
if (-not (Test-Path -LiteralPath $nodePath)) { throw 'Node.js fehlt. Node.js installieren und pnpm install im Repository ausführen.' }
$vitePath = Join-Path $appRoot 'node_modules/vite/bin/vite.js'
if (-not (Test-Path -LiteralPath $vitePath)) { throw 'Frontend-Abhängigkeiten fehlen. Zuerst pnpm install --frozen-lockfile im Repository ausführen.' }
$env:PATH = "$(Split-Path $nodePath);$env:PATH"
Write-Host "Lokales Dummy-Frontend: http://127.0.0.1:$Port/"
Write-Host 'Nur Beispieldaten. Keine Backend-, Kamera- oder Serververbindung. Beenden mit Strg+C.'
Push-Location $appRoot
try {
    & $nodePath $vitePath --mode demo --host 127.0.0.1 --port $Port --strictPort
    if ($LASTEXITCODE -ne 0) { throw "Demo-Frontend konnte nicht gestartet werden (Exit $LASTEXITCODE)." }
} finally { Pop-Location }
