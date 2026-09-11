$root = Resolve-Path (Join-Path $PSScriptRoot "..\\..")
$output = Join-Path $root "docs\\generated\\monorepo-file-tree.md"
Push-Location $root
try {
  $tracked = git ls-files
  $untracked = git ls-files --others --exclude-standard
  $files = @($tracked + $untracked) |
    Where-Object {
      $_ -and
      (Test-Path -LiteralPath (Join-Path $root $_)) -and
      $_ -notmatch '(^|/)\.pytest_cache/' -and
      $_ -notmatch '(^|/)\.ruff_cache/' -and
      $_ -notmatch '(^|/)node_modules/' -and
      $_ -notmatch '(^|/)\.venv/' -and
      $_ -notmatch '(^|/)__pycache__/' -and
      $_ -notmatch '(^|/)runs/mlflow/' -and
      $_ -notmatch '(^|/)\.local/'
    } |
    Sort-Object
} finally {
  Pop-Location
}

$lines = @('# Monorepo File Tree', '', '```text')
foreach ($file in $files) {
  $lines += $file.Replace("\", "/")
}
$lines += '```'
Set-Content -Path $output -Value $lines -Encoding utf8
Write-Host "Wrote $output"
