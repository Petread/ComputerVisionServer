from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
POWERSHELL = (
    Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
)
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows exporter integration")


def _literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _functions() -> str:
    return f"""
$ErrorActionPreference = 'Stop'
$source = {_literal(ROOT / "deployment/export-windows-update.ps1")}
$tree = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$null, [ref]$null)
foreach ($name in @('Export-EdgeRuntimePackages', 'New-AppArchive')) {{
    $function = $tree.Find({{
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }}, $true)
    Invoke-Expression $function.Extent.Text
}}
"""


def _run(tmp_path: Path, script: str, *, cwd: Path) -> str:
    path = tmp_path / "check.ps1"
    path.write_text(_functions() + script, encoding="utf-8-sig")
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_signing_input_reaches_python_as_utf8_without_secret_in_config_or_log(
    tmp_path: Path,
) -> None:
    working = tmp_path / "working space & quotes'"
    helper = working / "scripts/release/build_central_edge_updates.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        """import argparse, hashlib, json, sys
from pathlib import Path
p = argparse.ArgumentParser()
for name in ('config', 'output', 'central-release-id', 'app-archive'):
    p.add_argument('--' + name)
a = p.parse_args()
secret = sys.stdin.buffer.readline().rstrip(b'\\n')
settings = json.loads(Path(a.config).read_text(encoding='utf-8-sig'))
result = {'secret_digest': hashlib.sha256(secret).hexdigest(), 'settings': settings}
Path(a.output, 'targets.json').write_text(json.dumps(result))
print('Edge helper finished')
""",
        encoding="utf-8",
    )
    secret = "Only test: äöäß 42! ' $()"
    output = tmp_path / "output space"
    log = _run(
        tmp_path,
        rf"""
function Resolve-PythonForWheelDownload {{
    return [pscustomobject]@{{FilePath={_literal(sys.executable)}; Arguments=@()}}
}}
$EdgeRuntimeBundles = @('C:\fixture\base.tar.gz')
$EdgeSigningKeyPath = 'C:\fixture\private.pem'
$EdgeOpenSslExecutable = 'openssl'
$EdgePackageIndexUrl = 'https://pypi.org/simple'
$EdgeAdditionalWheelDirectory = ''
$files = @(Export-EdgeRuntimePackages -ReleaseDirectory {_literal(output)} -Version 'central-001' `
    -AppArchive 'fixture.tar.gz' -Passphrase {_literal(secret)})
if ($files.Count -ne 1 -or $files[0] -ne 'edge-runtime/targets.json') {{
    throw 'Unexpected exporter output'
}}
""",
        cwd=working,
    )
    assert secret not in log
    result = json.loads((output / "edge-runtime/targets.json").read_text())
    assert result["secret_digest"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(result["settings"])
    assert result["settings"]["bundles"] == [r"C:\fixture\base.tar.gz"]


def test_application_archive_excludes_signing_key_and_edge_installation_bundles(
    tmp_path: Path,
) -> None:
    working = tmp_path / "repository"
    working.mkdir()
    key = working / "keys/private secret.pem"
    key.parent.mkdir()
    key.write_text("PRIVATE KEY MUST NOT SHIP")
    edge = working / "deployment/edge"
    edge.mkdir(parents=True)
    (edge / "ybb-edge-jetson.tar.gz").write_text("FULL INSTALLATION MUST NOT SHIP")
    (edge / "ybb-edge-windows.zip").write_text("FULL INSTALLATION MUST NOT SHIP")
    (edge / "edge-runtime-launcher.py").write_text("launcher_code = True")
    (working / "application.py").write_text("code = True")
    output = tmp_path / "app.tar.gz"
    _run(
        tmp_path,
        rf"""
function Invoke-CheckedCommand {{
    param($Description, $FilePath, $Arguments, $WorkingDirectory)
    & "$env:WINDIR\System32\tar.exe" @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw 'Archive command failed' }}
}}
New-AppArchive -OutputPath {_literal(output)} -ExcludedInputs @({_literal(key)})
""",
        cwd=working,
    )
    with tarfile.open(output, "r:gz") as archive:
        files = {member.name.removeprefix("./") for member in archive if member.isfile()}
    assert files == {"application.py", "deployment/edge/edge-runtime-launcher.py"}


def test_ui_runner_forwards_secret_via_stdin_and_retains_process_output(tmp_path: Path) -> None:
    worker = tmp_path / "worker.ps1"
    secret = "Stdin only: äöß ' $() 42"
    expected = hashlib.sha256(secret.encode()).hexdigest()
    worker.write_text(
        """
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
$secret = [Console]::In.ReadLine()
if ($env:YBB_EDGE_SIGNING_PASSPHRASE) { throw 'Secret entered environment' }
$hash = [Security.Cryptography.SHA256]::Create()
$digest = $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($secret))
Write-Output ([BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant())
[Console]::Error.WriteLine('stderr retained')
""",
        encoding="utf-8-sig",
    )
    log = _run(
        tmp_path,
        rf"""
Add-Type -AssemblyName System.Windows.Forms
$ui = {_literal(ROOT / "deployment/windows-deployment-ui.ps1")}
$tree = [Management.Automation.Language.Parser]::ParseFile($ui, [ref]$null, [ref]$null)
foreach ($name in @('Start-UiProcess', 'ConvertTo-PowerShellLiteral',
                    'ConvertTo-PowerShellArrayLiteral', 'Quote-ProcessArgument')) {{
    $function = $tree.Find({{
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }}, $true)
    Invoke-Expression $function.Extent.Text
}}
function Append-Log {{ param($LogBox, $Text) $LogBox.AppendText($Text + "`n") }}
function Set-UiStatus {{ param($Text) }}
$env:TEMP = {_literal(tmp_path)}
$env:TMP = $env:TEMP
$form = New-Object Windows.Forms.Form
$logBox = New-Object Windows.Forms.TextBox
$button = New-Object Windows.Forms.Button
Start-UiProcess -Form $form -LogBox $logBox -Button $button -ScriptPath {_literal(worker)} `
    -Arguments @('-Fixture') -WorkingDirectory {_literal(tmp_path)} `
    -EdgeSigningPassphrase {_literal(secret)}
if (-not $script:CurrentProcess.WaitForExit(20000)) {{
    $script:CurrentProcess.Kill()
    throw 'UI runner did not complete'
}}
if ($script:CurrentProcess.ExitCode -ne 0) {{ throw 'UI runner failed' }}
Get-Content -LiteralPath (Get-ChildItem "$env:TEMP/ybb-deployment-ui/process-*.log").FullName
$script:ActiveTimer.Stop()
$script:ActiveTimer.Dispose()
$form.Dispose()
""",
        cwd=tmp_path,
    )
    assert expected in log
    assert "stderr retained" in log
    assert secret not in log
    for path in (tmp_path / "ybb-deployment-ui").glob("*"):
        assert secret not in path.read_text(encoding="utf-8-sig")
