[CmdletBinding()]
param(
    [ValidateSet("Auto", "Export", "Install", "Update", "Models")]
    [string]$Mode = "Auto"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:CrashLogPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ybb-deployment-ui-crash-{0}.log" -f $PID)

function Initialize-CrashLog {
    try {
        $parent = Split-Path -Parent $script:CrashLogPath
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        [System.IO.File]::WriteAllText(
            $script:CrashLogPath,
            ("[{0}] YBB Deployment UI gestartet.{1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), [Environment]::NewLine),
            [System.Text.Encoding]::UTF8
        )
    }
    catch {
        # Crash logging is best effort during startup.
    }
}

function Write-CrashLog {
    param([Parameter(Mandatory = $true)]$ErrorRecord)

    try {
        $message = (
            ("[{0}] UI-Fehler" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")),
            ($ErrorRecord | Out-String),
            ""
        ) -join [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:CrashLogPath, $message, [System.Text.Encoding]::UTF8)
    }
    catch {
        # Last-resort logging must never create a second failure.
    }
}

Initialize-CrashLog

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

trap {
    Write-CrashLog $_
    try {
        [System.Windows.Forms.MessageBox]::Show(("UI-Fehler: {0}`n`nDetails: {1}" -f $_.Exception.Message, $script:CrashLogPath), "YBB Deployment", "OK", "Error") | Out-Null
    }
    catch {
        # Ignore UI errors while handling a UI error.
    }
    continue
}

$script:RootDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$script:CurrentProcess = $null
$script:ActiveTimer = $null
$script:ActiveTickHandler = $null
$script:StatusLabel = $null

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-RepoRoot {
    if (Test-Path -LiteralPath (Join-Path $script:RootDir "build-windows-installer.ps1") -PathType Leaf) {
        return [System.IO.Path]::GetFullPath((Split-Path -Parent $script:RootDir))
    }
    if (Test-Path -LiteralPath (Join-Path $script:RootDir "deployment\build-windows-installer.ps1") -PathType Leaf) {
        return $script:RootDir
    }
    return [System.IO.Path]::GetFullPath((Get-Location).Path)
}

function Resolve-DeploymentDir {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $candidate = Join-Path $RepoRoot "deployment"
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        return [System.IO.Path]::GetFullPath($candidate)
    }
    return $script:RootDir
}

function Resolve-AppDir {
    $leaf = Split-Path -Leaf $script:RootDir
    if ($leaf -ieq "updater") {
        return [System.IO.Path]::GetFullPath((Split-Path -Parent $script:RootDir))
    }
    if (Test-Path -LiteralPath (Join-Path $script:RootDir "install-state.json") -PathType Leaf) {
        return $script:RootDir
    }
    return "C:\YBB_Inferenzserver\ybb-inferenzserver"
}

function Resolve-DeploymentScriptPath {
    param([Parameter(Mandatory = $true)][string]$FileName)

    foreach ($candidate in @(
        (Join-Path $script:RootDir $FileName),
        (Join-Path $script:RootDir "updater\$FileName"),
        (Join-Path (Resolve-AppDir) "updater\$FileName")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }

    throw "Skript wurde nicht gefunden: $FileName"
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-DefaultDistroName {
    $manifest = Read-JsonFile -Path (Join-Path $script:RootDir "windows-bundle-manifest.json")
    if ($null -ne $manifest -and $manifest.PSObject.Properties.Name -contains "DistroName" -and -not [string]::IsNullOrWhiteSpace([string]$manifest.DistroName)) {
        return [string]$manifest.DistroName
    }
    $state = Read-JsonFile -Path (Join-Path (Resolve-AppDir) "install-state.json")
    if ($null -ne $state -and $state.PSObject.Properties.Name -contains "DistroName" -and -not [string]::IsNullOrWhiteSpace([string]$state.DistroName)) {
        return [string]$state.DistroName
    }
    return "YBB-Inferenzserver"
}

function Split-PublicAddress {
    param([string]$Value)

    $authority = if ([string]::IsNullOrWhiteSpace($Value)) { "127.0.0.1:443" } else { $Value.Trim() }
    $authority = $authority -replace "^[A-Za-z][A-Za-z0-9+.-]*://", ""
    $authority = ($authority -split "/", 2)[0]
    if ($authority -match "^\[(?<Host>[^\]]+)\]:(?<Port>\d+)$") {
        return @{ Host = $Matches.Host; Port = $Matches.Port }
    }
    if ($authority -match "^(?<Host>\d{1,3}(?:\.\d{1,3}){3}|[^:]+):(?<Port>\d+)$") {
        return @{ Host = $Matches.Host; Port = $Matches.Port }
    }
    return @{ Host = $authority; Port = "443" }
}

function Join-PublicAddress {
    param(
        [Parameter(Mandatory = $true)][string]$AddressHost,
        [Parameter(Mandatory = $true)][string]$Port
    )

    $cleanHost = $AddressHost.Trim()
    $cleanPort = $Port.Trim()
    if ([string]::IsNullOrWhiteSpace($cleanHost)) {
        throw "IP-Adresse/Host darf nicht leer sein."
    }
    if ($cleanPort -notmatch "^\d{1,5}$") {
        throw "Port muss eine Zahl sein."
    }
    if ($cleanPort -eq "443") {
        if ($cleanHost -match ":" -and -not $cleanHost.StartsWith("[")) {
            return "[${cleanHost}]"
        }
        return $cleanHost
    }
    if ($cleanHost -match ":" -and -not $cleanHost.StartsWith("[")) {
        return "[${cleanHost}]:$cleanPort"
    }
    return "${cleanHost}:$cleanPort"
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
    return "'" + ([string]$Value).Replace("'", "''") + "'"
}

function New-TemporaryExportConfig {
    param(
        [string]$RepoRoot,
        [string]$BaseConfig,
        [string]$AppName,
        [string]$AppVersion,
        [string]$DistroName,
        [string]$ServerBaseDir,
        [string]$PublicAddress,
        [bool]$BuildFrontend,
        [bool]$SkipTritonCheck,
        [bool]$SkipMediaMtxCheck,
        [bool]$SkipGStreamerCheck,
        [bool]$WslMirroredNetworking,
        [bool]$PlcWritesAllowed,
        [AllowEmptyString()][string]$MediaMtxArchivePath
    )

    $dir = Join-Path ([System.IO.Path]::GetTempPath()) "ybb-deployment-ui"
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $path = Join-Path $dir ("windows-offline-ui-{0}.config.ps1" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Automatisch erzeugt durch YBB-Deployment-UI.")
    if (-not [string]::IsNullOrWhiteSpace($BaseConfig)) {
        $fullConfig = [System.IO.Path]::GetFullPath($BaseConfig)
        $lines.Add(". " + (ConvertTo-PowerShellLiteral $fullConfig))
    }
    else {
        $defaultConfig = Join-Path (Resolve-DeploymentDir -RepoRoot $RepoRoot) "windows-offline.config.ps1"
        if (Test-Path -LiteralPath $defaultConfig -PathType Leaf) {
            $lines.Add(". " + (ConvertTo-PowerShellLiteral ([System.IO.Path]::GetFullPath($defaultConfig))))
        }
    }
    $lines.Add('$AppName = ' + (ConvertTo-PowerShellLiteral $AppName))
    $lines.Add('$AppVersion = ' + (ConvertTo-PowerShellLiteral $AppVersion))
    $lines.Add('$DistroName = ' + (ConvertTo-PowerShellLiteral $DistroName))
    $lines.Add('$ServerBaseDir = ' + (ConvertTo-PowerShellLiteral $ServerBaseDir))
    $lines.Add('$AppHostName = ' + (ConvertTo-PowerShellLiteral $PublicAddress))
    $lines.Add('$BuildFrontend = ' + (ConvertTo-PowerShellLiteral $BuildFrontend))
    $lines.Add('$PrepareRuntimeSkipTritonCheck = ' + (ConvertTo-PowerShellLiteral $SkipTritonCheck))
    $lines.Add('$PrepareRuntimeSkipMediaMtxCheck = ' + (ConvertTo-PowerShellLiteral $SkipMediaMtxCheck))
    $lines.Add('$PrepareRuntimeSkipGStreamerCheck = ' + (ConvertTo-PowerShellLiteral $SkipGStreamerCheck))
    if ($WslMirroredNetworking) {
        $lines.Add('$WslNetworkingMode = "mirrored"')
        $lines.Add('$WslDnsTunneling = $true')
        $lines.Add('$WslFirewall = $true')
        $lines.Add('$WslAutoProxy = $false')
        $lines.Add('$WslHostAddressLoopback = $true')
        $lines.Add('$WslHyperVFirewallDefaultInboundAction = "Allow"')
        $lines.Add('$WslRestartAllowed = $true')
    }
    else {
        $lines.Add('$WslNetworkingMode = "unchanged"')
        $lines.Add('$WslHyperVFirewallDefaultInboundAction = "Unchanged"')
    }
    if (-not [string]::IsNullOrWhiteSpace($MediaMtxArchivePath)) {
        $lines.Add('$MediaMtxArchivePath = ' + (ConvertTo-PowerShellLiteral ([System.IO.Path]::GetFullPath($MediaMtxArchivePath))))
    }
    $lines.Add('$StreamAuthEnabled = $false')
    $lines.Add('$PlcWritesAllowed = ' + (ConvertTo-PowerShellLiteral $PlcWritesAllowed))
    $lines.Add('$FrontendAdminApiUrl = ""')
    $lines.Add('$FrontendRuntimeAdminApiUrl = "/runtime-api"')
    $lines.Add('$FrontendResultServiceUrl = "/runtime-results"')
    $lines.Add('$FrontendTriggerExecutorUrl = "/runtime-trigger"')
    $lines.Add('$FrontendInferenceGatewayRtUrl = "/inference-rt"')
    $lines.Add('$FrontendInferenceGatewayBatchUrl = "/inference-batch"')
    $lines.Add('$FrontendAdminWsUrl = ""')
    $lines.Add('$FrontendBasePath = "/frontend/"')
    Set-Content -LiteralPath $path -Value $lines -Encoding ascii
    return $path
}

function Quote-ProcessArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function New-Label {
    param([AllowEmptyString()][string]$Text, [int]$X, [int]$Y, [int]$W = 130, [int]$H = 22)
    $control = New-Object System.Windows.Forms.Label
    $control.Text = $Text
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($W, $H)
    return $control
}

function New-TextBox {
    param([AllowEmptyString()][string]$Text, [int]$X, [int]$Y, [int]$W = 430)
    $control = New-Object System.Windows.Forms.TextBox
    $control.Text = $Text
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($W, 24)
    return $control
}

function New-Button {
    param([AllowEmptyString()][string]$Text, [int]$X, [int]$Y, [int]$W = 130)
    $control = New-Object System.Windows.Forms.Button
    $control.Text = $Text
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($W, 28)
    return $control
}

function New-CheckBox {
    param([AllowEmptyString()][string]$Text, [bool]$Checked, [int]$X, [int]$Y, [int]$W = 240)
    $control = New-Object System.Windows.Forms.CheckBox
    $control.Text = $Text
    $control.Checked = $Checked
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($W, 24)
    return $control
}

function Append-Log {
    param(
        [Parameter(Mandatory = $true)][System.Windows.Forms.TextBox]$LogBox,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    $LogBox.AppendText($Text + [Environment]::NewLine)
    $LogBox.SelectionStart = $LogBox.TextLength
    $LogBox.ScrollToCaret()
}

function Read-TextFileShared {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $reader = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $reader = New-Object System.IO.StreamReader -ArgumentList $stream, ([System.Text.Encoding]::UTF8), $true
        $stream = $null
        return $reader.ReadToEnd()
    }
    finally {
        if ($null -ne $reader) {
            $reader.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Set-UiStatus {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    if ($null -ne $script:StatusLabel -and -not $script:StatusLabel.IsDisposed) {
        $script:StatusLabel.Text = $Text
    }
}

function ConvertTo-ProcessScriptArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -match "^-[A-Za-z][A-Za-z0-9]*$") {
        return $Value
    }
    return ConvertTo-PowerShellLiteral $Value
}

function ConvertTo-PowerShellArrayLiteral {
    param([Parameter(Mandatory = $true)][string[]]$Values)

    return "@(" + (($Values | ForEach-Object { ConvertTo-PowerShellLiteral $_ }) -join ", ") + ")"
}

function Append-NewProcessLog {
    param(
        [Parameter(Mandatory = $true)][System.Windows.Forms.TextBox]$LogBox,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$State
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    try {
        $text = Read-TextFileShared -Path $Path
    }
    catch {
        return
    }
    $position = [int64]$State["Position"]
    if ([string]::IsNullOrEmpty($text) -or $text.Length -le $position) {
        return
    }

    $newText = $text.Substring([int]$position)
    $State["Position"] = [int64]$text.Length
    $LogBox.AppendText($newText)
    $LogBox.SelectionStart = $LogBox.TextLength
    $LogBox.ScrollToCaret()
}

function Start-UiProcess {
    param(
        [Parameter(Mandatory = $true)][System.Windows.Forms.Form]$Form,
        [Parameter(Mandatory = $true)][System.Windows.Forms.TextBox]$LogBox,
        [Parameter(Mandatory = $true)][System.Windows.Forms.Button]$Button,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    if ($null -ne $script:CurrentProcess -and -not $script:CurrentProcess.HasExited) {
        [System.Windows.Forms.MessageBox]::Show("Es laeuft bereits ein Vorgang.", "YBB Deployment", "OK", "Information") | Out-Null
        return
    }
    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
        [System.Windows.Forms.MessageBox]::Show("Skript nicht gefunden:`n$ScriptPath", "YBB Deployment", "OK", "Error") | Out-Null
        return
    }

    $runDir = Join-Path ([System.IO.Path]::GetTempPath()) "ybb-deployment-ui"
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $runId = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 8))
    $runLogPath = Join-Path $runDir ("process-$runId.log")
    $runnerPath = Join-Path $runDir ("run-$runId.ps1")
    $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

    $childArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) + $Arguments
    $childArgsLiteral = ConvertTo-PowerShellArrayLiteral -Values $childArgs
    $childPowerShellLiteral = ConvertTo-PowerShellLiteral $powerShell
    $runLogPathLiteral = ConvertTo-PowerShellLiteral $runLogPath
    $runnerLines = @(
        '$ErrorActionPreference = "Stop"',
        'function Join-ProcessArguments {',
        '    param([Parameter(Mandatory = $true)][string[]]$Values)',
        '    $quoted = foreach ($value in $Values) {',
        '        if ($null -eq $value) { $value = "" }',
        '        if ($value.Length -gt 0 -and $value -notmatch ''[\s"]'') {',
        '            $value',
        '            continue',
        '        }',
        '        $escaped = $value -replace ''(\\*)"'', ''$1$1\"''',
        '        $escaped = $escaped -replace ''(\\+)$'', ''$1$1''',
        '        ''"'' + $escaped + ''"''',
        '    }',
        '    return ($quoted -join '' '')',
        '}',
        'function Read-TextFileShared {',
        '    param([Parameter(Mandatory = $true)][string]$Path)',
        '    $stream = $null',
        '    $reader = $null',
        '    try {',
        '        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)',
        '        $reader = New-Object System.IO.StreamReader -ArgumentList $stream, ([System.Text.Encoding]::UTF8), $true',
        '        $stream = $null',
        '        return $reader.ReadToEnd()',
        '    }',
        '    finally {',
        '        if ($null -ne $reader) { $reader.Dispose() }',
        '        if ($null -ne $stream) { $stream.Dispose() }',
        '    }',
        '}',
        'function Write-SharedLogText {',
        '    param(',
        '        [Parameter(Mandatory = $true)][string]$Path,',
        '        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text',
        '    )',
        '    if ($Text.Length -eq 0) { return }',
        '    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)',
        '    for ($attempt = 0; $attempt -lt 30; $attempt++) {',
        '        $stream = $null',
        '        try {',
        '            $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)',
        '            $stream.Write($bytes, 0, $bytes.Length)',
        '            return',
        '        }',
        '        catch [System.IO.IOException] {',
        '            Start-Sleep -Milliseconds 100',
        '        }',
        '        finally {',
        '            if ($null -ne $stream) { $stream.Dispose() }',
        '        }',
        '    }',
        '    throw "Logdatei konnte nicht geschrieben werden: $Path"',
        '}',
        'function Append-NewRedirectedText {',
        '    param(',
        '        [Parameter(Mandatory = $true)][string]$SourcePath,',
        '        [Parameter(Mandatory = $true)][string]$DestinationPath,',
        '        [Parameter(Mandatory = $true)][hashtable]$Positions,',
        '        [Parameter(Mandatory = $true)][string]$Key',
        '    )',
        '    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) { return }',
        '    try {',
        '        $text = Read-TextFileShared -Path $SourcePath',
        '    }',
        '    catch {',
        '        return',
        '    }',
        '    $position = [int64]$Positions[$Key]',
        '    if ([string]::IsNullOrEmpty($text) -or $text.Length -le $position) { return }',
        '    $newText = $text.Substring([int]$position)',
        '    Write-SharedLogText -Path $DestinationPath -Text $newText',
        '    $Positions[$Key] = [int64]$text.Length',
        '}',
        'function Append-RedirectedLogs {',
        '    param(',
        '        [Parameter(Mandatory = $true)][string]$StdoutPath,',
        '        [Parameter(Mandatory = $true)][string]$StderrPath,',
        '        [Parameter(Mandatory = $true)][string]$LogPath,',
        '        [Parameter(Mandatory = $true)][hashtable]$Positions',
        '    )',
        '    Append-NewRedirectedText -SourcePath $StdoutPath -DestinationPath $LogPath -Positions $Positions -Key "stdout"',
        '    Append-NewRedirectedText -SourcePath $StderrPath -DestinationPath $LogPath -Positions $Positions -Key "stderr"',
        '}',
        '$exitCode = 0',
        '$process = $null',
        '$stdoutPath = $null',
        '$stderrPath = $null',
        'try {',
        ("    `$powerShell = {0}" -f $childPowerShellLiteral),
        ("    `$childArgs = {0}" -f $childArgsLiteral),
        ("    `$logPath = {0}" -f $runLogPathLiteral),
        '    $stdoutPath = "$logPath.stdout"',
        '    $stderrPath = "$logPath.stderr"',
        '    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue',
        '    New-Item -ItemType File -Path $logPath -Force | Out-Null',
        '    $argumentLine = Join-ProcessArguments -Values $childArgs',
        '    $process = Start-Process -FilePath $powerShell -ArgumentList $argumentLine -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -NoNewWindow -PassThru',
        '    $positions = @{ stdout = [int64]0; stderr = [int64]0 }',
        '    while (-not $process.HasExited) {',
        '        Append-RedirectedLogs -StdoutPath $stdoutPath -StderrPath $stderrPath -LogPath $logPath -Positions $positions',
        '        Start-Sleep -Milliseconds 500',
        '    }',
        '    $process.WaitForExit()',
        '    $process.Refresh()',
        '    Append-RedirectedLogs -StdoutPath $stdoutPath -StderrPath $stderrPath -LogPath $logPath -Positions $positions',
        '    $exitCode = [int]$process.ExitCode',
        '    if ($exitCode -eq 0) {',
        '        try {',
        '            $logText = Read-TextFileShared -Path $logPath',
        '            if ($logText -match "(?m)(^|\] )FEHLER:|fehlgeschlagen\. Exit Code: [1-9]") {',
        '                $exitCode = 1',
        '            }',
        '        }',
        '        catch { }',
        '    }',
        '}',
        'catch {',
        '    if ($null -ne $process -and -not $process.HasExited) {',
        '        try { $process.Kill() } catch { }',
        '    }',
        '    Write-SharedLogText -Path $logPath -Text ("RUNNER-ERROR:" + [Environment]::NewLine)',
        '    Write-SharedLogText -Path $logPath -Text ($_ | Out-String)',
        '    $exitCode = 1',
        '}',
        'finally {',
        '    if ($null -ne $process) { $process.Dispose() }',
        '    if ($null -ne $stdoutPath -and $null -ne $stderrPath) {',
        '        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue',
        '    }',
        '}',
        'exit $exitCode'
    )
    Set-Content -LiteralPath $runnerPath -Value $runnerLines -Encoding ascii

    $processArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runnerPath)
    $argumentLine = ($processArgs | ForEach-Object { Quote-ProcessArgument $_ }) -join " "

    Append-Log -LogBox $LogBox -Text ""
    Append-Log -LogBox $LogBox -Text ("> powershell.exe {0}" -f ((@("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) + $Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " "))
    Append-Log -LogBox $LogBox -Text ("Lauf-Logdatei: {0}" -f $runLogPath)

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $powerShell
    $startInfo.Arguments = $argumentLine
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $false
    $startInfo.RedirectStandardError = $false
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $process.EnableRaisingEvents = $true

    try {
        [void]$process.Start()
        $script:CurrentProcess = $process
        $Button.Enabled = $false
        Set-UiStatus -Text ("Laeuft. Prozess-ID: {0}" -f $process.Id)
        Append-Log -LogBox $LogBox -Text ("Vorgang gestartet. Prozess-ID: {0}" -f $process.Id)

        $state = @{
            Position = [int64]0
        }
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 500
        $tickHandler = {
            try {
                Append-NewProcessLog -LogBox $LogBox -Path $runLogPath -State $state
                if ($process.HasExited) {
                    Append-NewProcessLog -LogBox $LogBox -Path $runLogPath -State $state
                    $timer.Stop()
                    $timer.Dispose()
                    $exitCode = $process.ExitCode
                    Append-Log -LogBox $LogBox -Text ("Vorgang beendet. Exit Code: {0}" -f $exitCode)
                    Set-UiStatus -Text ("Beendet. Exit Code: {0}" -f $exitCode)
                    $Button.Enabled = $true
                    $script:CurrentProcess = $null
                    $script:ActiveTimer = $null
                    $script:ActiveTickHandler = $null
                }
            }
            catch {
                Write-CrashLog $_
                Append-Log -LogBox $LogBox -Text ("WARNUNG: UI-Loganzeige konnte nicht aktualisiert werden: {0}" -f $_.Exception.Message)
                Append-Log -LogBox $LogBox -Text ("Details: {0}" -f $script:CrashLogPath)
            }
        }.GetNewClosure()
        $timer.Add_Tick($tickHandler)
        $script:ActiveTimer = $timer
        $script:ActiveTickHandler = $tickHandler
        $timer.Start()
    }
    catch {
        $Button.Enabled = $true
        Set-UiStatus -Text "Fehler beim Start."
        throw
    }
}

function Select-File {
    param([string]$Filter = "Alle Dateien (*.*)|*.*")
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Filter = $Filter
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.FileName
    }
    return ""
}

function Select-Folder {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    return ""
}

function Add-PretrainedModelCatalogItem {
    param(
        [AllowEmptyCollection()][System.Collections.Generic.List[object]]$Items,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Label,
        [bool]$Recommended = $false
    )

    $Items.Add([pscustomobject]@{
        Id = $Id
        Label = $Label
        Recommended = $Recommended
    })
}

function Get-PretrainedModelUiCatalog {
    $items = New-Object System.Collections.Generic.List[object]
    $sizes = @("n", "s", "m", "l", "x")
    $sizeNames = @{
        n = "Nano"
        s = "Small"
        m = "Medium"
        l = "Large"
        x = "X-Large"
    }

    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-classification-resnet18" -Label "Classification / ResNet-18" -Recommended $true
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-classification-resnet50" -Label "Classification / ResNet-50"
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-classification-mobilenet-v3-small" -Label "Classification / MobileNetV3 Small"
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-classification-mobilenet-v3-large" -Label "Classification / MobileNetV3 Large"
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-classification-efficientnet-b0" -Label "Classification / EfficientNet-B0"
    foreach ($version in @("11", "26")) {
        foreach ($size in $sizes) {
            Add-PretrainedModelCatalogItem -Items $items -Id ("ultralytics-classification-yolo{0}-{1}" -f $version, $size) -Label ("Classification / YOLO{0} {1}" -f $version, $sizeNames[$size])
        }
    }

    foreach ($size in $sizes) {
        Add-PretrainedModelCatalogItem -Items $items -Id ("openmmlab-detection-yolov8-{0}" -f $size) -Label ("Detection / YOLOv8 {0} (OpenMMLab)" -f $sizeNames[$size]) -Recommended ($size -eq "n")
    }
    foreach ($version in @("11", "26")) {
        foreach ($size in $sizes) {
            Add-PretrainedModelCatalogItem -Items $items -Id ("ultralytics-detection-yolo{0}-{1}" -f $version, $size) -Label ("Detection / YOLO{0} {1}" -f $version, $sizeNames[$size])
        }
    }
    foreach ($size in $sizes) {
        Add-PretrainedModelCatalogItem -Items $items -Id ("ultralytics-oriented-detection-yolo26-{0}" -f $size) -Label ("Oriented Detection / YOLO26 {0} OBB" -f $sizeNames[$size]) -Recommended ($size -eq "n")
    }

    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-segmentation-deeplabv3-resnet50" -Label "Segmentation / DeepLabV3 ResNet-50" -Recommended $true
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-segmentation-fcn-resnet50" -Label "Segmentation / FCN ResNet-50"
    Add-PretrainedModelCatalogItem -Items $items -Id "torchvision-segmentation-lraspp-mobilenet-v3-large" -Label "Segmentation / LRASPP MobileNetV3"
    foreach ($version in @("v8", "11", "26")) {
        foreach ($size in $sizes) {
            Add-PretrainedModelCatalogItem -Items $items -Id ("ultralytics-segmentation-yolo{0}-{1}" -f $version, $size) -Label ("Segmentation / YOLO{0} {1}" -f $version, $sizeNames[$size])
        }
    }

    Add-PretrainedModelCatalogItem -Items $items -Id "anomalib-patchcore-wide-resnet50-2" -Label "Anomaly / PatchCore WideResNet-50-2" -Recommended $true
    return @($items.ToArray())
}

function Set-CheckedListState {
    param(
        [Parameter(Mandatory = $true)][System.Windows.Forms.CheckedListBox]$List,
        [Parameter(Mandatory = $true)][object[]]$Catalog,
        [Parameter(Mandatory = $true)][ValidateSet("All", "None", "Recommended")][string]$Mode
    )

    for ($index = 0; $index -lt $List.Items.Count; $index++) {
        $checked = switch ($Mode) {
            "All" { $true }
            "None" { $false }
            default { [bool]$Catalog[$index].Recommended }
        }
        $List.SetItemChecked($index, $checked)
    }
}

function Get-CheckedPretrainedModelIds {
    param(
        [Parameter(Mandatory = $true)][System.Windows.Forms.CheckedListBox]$List,
        [Parameter(Mandatory = $true)][object[]]$Catalog
    )

    $ids = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt $List.Items.Count; $index++) {
        if ($List.GetItemChecked($index)) {
            $ids.Add([string]$Catalog[$index].Id)
        }
    }
    return @($ids)
}

$repoRoot = Resolve-RepoRoot
$deploymentDir = Resolve-DeploymentDir -RepoRoot $repoRoot
$defaultAppDir = Resolve-AppDir
$defaultDistro = Get-DefaultDistroName
$defaultAddress = Split-PublicAddress -Value "127.0.0.1:443"

$form = New-Object System.Windows.Forms.Form
$form.Text = "YBB Inferenzserver Deployment"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(980, 780)
$form.MinimumSize = New-Object System.Drawing.Size(900, 700)
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$form.Add_FormClosing({
    if ($null -ne $script:CurrentProcess -and -not $script:CurrentProcess.HasExited) {
        $answer = [System.Windows.Forms.MessageBox]::Show("Es laeuft noch ein Vorgang. Soll er beendet werden?", "YBB Deployment", "YesNo", "Warning")
        if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
            try { $script:CurrentProcess.Kill() } catch {}
        }
        else {
            $_.Cancel = $true
        }
    }
})

$title = New-Object System.Windows.Forms.Label
$title.Text = "YBB Inferenzserver Deployment"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(14, 12)
$title.Size = New-Object System.Drawing.Size(420, 28)
$form.Controls.Add($title)

$adminLabel = New-Object System.Windows.Forms.Label
$adminLabel.Text = if (Test-IsAdministrator) { "Administratorrechte: ja" } else { "Administratorrechte: nein, Portproxy wird nicht automatisch gesetzt" }
$adminLabel.Location = New-Object System.Drawing.Point(560, 16)
$adminLabel.Size = New-Object System.Drawing.Size(380, 22)
$form.Controls.Add($adminLabel)

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Location = New-Object System.Drawing.Point(14, 50)
$tabs.Size = New-Object System.Drawing.Size(935, 535)
$form.Controls.Add($tabs)

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly = $true
$script:StatusLabel = New-Object System.Windows.Forms.Label
$script:StatusLabel.Text = "Bereit."
$script:StatusLabel.Location = New-Object System.Drawing.Point(14, 592)
$script:StatusLabel.Size = New-Object System.Drawing.Size(935, 22)
$script:StatusLabel.Anchor = "Left,Right,Bottom"
$form.Controls.Add($script:StatusLabel)

$logBox.Location = New-Object System.Drawing.Point(14, 618)
$logBox.Size = New-Object System.Drawing.Size(935, 112)
$logBox.Anchor = "Left,Right,Bottom"
$form.Controls.Add($logBox)

$exportTab = New-Object System.Windows.Forms.TabPage
$exportTab.Text = "Export"
$tabs.TabPages.Add($exportTab)

$installTab = New-Object System.Windows.Forms.TabPage
$installTab.Text = "Installation"
$tabs.TabPages.Add($installTab)

$updateTab = New-Object System.Windows.Forms.TabPage
$updateTab.Text = "Update"
$tabs.TabPages.Add($updateTab)

$modelsTab = New-Object System.Windows.Forms.TabPage
$modelsTab.Text = "Modelle"
$modelsTab.AutoScroll = $true
$tabs.TabPages.Add($modelsTab)

# Export tab
$exportTab.Controls.Add((New-Label "Repo-Pfad" 18 24))
$exportRepo = New-TextBox $repoRoot 160 22 610
$exportTab.Controls.Add($exportRepo)
$browseRepo = New-Button "Durchsuchen" 785 20 115
$browseRepo.Add_Click({ $path = Select-Folder; if ($path) { $exportRepo.Text = $path } })
$exportTab.Controls.Add($browseRepo)

$exportTab.Controls.Add((New-Label "Config optional" 18 60))
$exportConfig = New-TextBox "" 160 58 610
$exportTab.Controls.Add($exportConfig)
$browseConfig = New-Button "Datei..." 785 56 115
$browseConfig.Add_Click({ $path = Select-File "PowerShell Config (*.ps1)|*.ps1|Alle Dateien (*.*)|*.*"; if ($path) { $exportConfig.Text = $path } })
$exportTab.Controls.Add($browseConfig)

$exportTab.Controls.Add((New-Label "App-Name" 18 96))
$exportAppName = New-TextBox "ybb-inferenzserver" 160 94 220
$exportTab.Controls.Add($exportAppName)
$exportTab.Controls.Add((New-Label "Version" 420 96 70))
$exportVersion = New-TextBox "auto" 490 94 180
$exportTab.Controls.Add($exportVersion)

$exportTab.Controls.Add((New-Label "Ziel-Basisordner" 18 132))
$exportBaseDir = New-TextBox "C:\YBB_Inferenzserver" 160 130 310
$exportTab.Controls.Add($exportBaseDir)
$exportTab.Controls.Add((New-Label "Distro" 500 132 60))
$exportDistro = New-TextBox $defaultDistro 560 130 210
$exportTab.Controls.Add($exportDistro)

$exportTab.Controls.Add((New-Label "IP/Host" 18 168))
$exportHost = New-TextBox $defaultAddress.Host 160 166 220
$exportTab.Controls.Add($exportHost)
$exportTab.Controls.Add((New-Label "Port" 420 168 70))
$exportPort = New-TextBox $defaultAddress.Port 490 166 90
$exportTab.Controls.Add($exportPort)

$exportCheckboxLeftX = 160
$exportCheckboxRightX = 535
$exportCheckboxLeftW = 285
$exportCheckboxRightW = 355
$exportBuildFrontend = New-CheckBox "Frontend bauen" $true $exportCheckboxLeftX 206 $exportCheckboxLeftW
$exportSkipRuntime = New-CheckBox "Runtime-Vorbereitung ueberspringen" $false $exportCheckboxRightX 206 $exportCheckboxRightW
$exportSkipApt = New-CheckBox "APT ueberspringen" $false $exportCheckboxLeftX 236 $exportCheckboxLeftW
$exportSkipTraining = New-CheckBox "Training-Runtime ueberspringen" $false $exportCheckboxRightX 236 $exportCheckboxRightW
$exportSkipTriton = New-CheckBox "Triton-Archiv ueberspringen (kein Docker)" $true $exportCheckboxLeftX 266 $exportCheckboxLeftW
$exportSkipMediaMtx = New-CheckBox "MediaMTX ueberspringen" $false $exportCheckboxRightX 266 $exportCheckboxRightW
$exportSkipGStreamer = New-CheckBox "GStreamer ueberspringen" $false $exportCheckboxLeftX 296 $exportCheckboxLeftW
$exportPlcWritesAllowed = New-CheckBox "PLC-Schreiben erlauben" $false $exportCheckboxRightX 296 $exportCheckboxRightW
$exportWslMirrored = New-CheckBox "WSL Mirrored Networking" $true $exportCheckboxLeftX 326 $exportCheckboxLeftW
foreach ($control in @($exportBuildFrontend, $exportSkipRuntime, $exportSkipApt, $exportSkipTraining, $exportSkipTriton, $exportSkipMediaMtx, $exportSkipGStreamer, $exportPlcWritesAllowed, $exportWslMirrored)) {
    $exportTab.Controls.Add($control)
}

$exportTab.Controls.Add((New-Label "MediaMTX-Archiv" 18 365))
$exportMediaMtxArchive = New-TextBox "" 160 363 610
$exportTab.Controls.Add($exportMediaMtxArchive)
$browseMediaMtxArchive = New-Button "Datei..." 785 361 115
$browseMediaMtxArchive.Add_Click({ $path = Select-File "MediaMTX Windows Archiv (*.zip)|*.zip|Alle Dateien (*.*)|*.*"; if ($path) { $exportMediaMtxArchive.Text = $path } })
$exportTab.Controls.Add($browseMediaMtxArchive)
$exportTab.Controls.Add((New-Label "leer = aus Config oder automatisch nach .runtime; Stream-Auth bleibt aus" 160 393 620))

$exportInstallerButton = New-Button "Installer exportieren" 160 430 170
$exportUpdateButton = New-Button "Update exportieren" 350 430 160
$exportTab.Controls.Add($exportInstallerButton)
$exportTab.Controls.Add($exportUpdateButton)

$exportInstallerButton.Add_Click({
    try {
        $publicAddress = Join-PublicAddress -AddressHost $exportHost.Text -Port $exportPort.Text
        $configPath = New-TemporaryExportConfig -RepoRoot $exportRepo.Text -BaseConfig $exportConfig.Text -AppName $exportAppName.Text -AppVersion $exportVersion.Text -DistroName $exportDistro.Text -ServerBaseDir $exportBaseDir.Text -PublicAddress $publicAddress -BuildFrontend $exportBuildFrontend.Checked -SkipTritonCheck $exportSkipTriton.Checked -SkipMediaMtxCheck $exportSkipMediaMtx.Checked -SkipGStreamerCheck $exportSkipGStreamer.Checked -WslMirroredNetworking $exportWslMirrored.Checked -PlcWritesAllowed $exportPlcWritesAllowed.Checked -MediaMtxArchivePath $exportMediaMtxArchive.Text
        $scriptPath = Join-Path (Resolve-DeploymentDir -RepoRoot $exportRepo.Text) "build-windows-installer.ps1"
        $args = @("-Config", $configPath)
        if (-not [string]::IsNullOrWhiteSpace($exportVersion.Text) -and $exportVersion.Text -ine "auto") { $args += @("-Version", $exportVersion.Text) }
        if ($exportSkipRuntime.Checked) { $args += "-SkipRuntimePrepare" }
        if ($exportSkipApt.Checked) { $args += "-SkipApt" }
        if ($exportSkipTraining.Checked) { $args += "-SkipTrainingRuntime" }
        if ($exportSkipTriton.Checked) { $args += "-SkipTritonCheck" }
        if ($exportSkipMediaMtx.Checked) { $args += "-SkipMediaMtxCheck" }
        if ($exportSkipGStreamer.Checked) { $args += "-SkipGStreamerCheck" }
        Start-UiProcess -Form $form -LogBox $logBox -Button $exportInstallerButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $exportRepo.Text
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

$exportUpdateButton.Add_Click({
    try {
        $publicAddress = Join-PublicAddress -AddressHost $exportHost.Text -Port $exportPort.Text
        $configPath = New-TemporaryExportConfig -RepoRoot $exportRepo.Text -BaseConfig $exportConfig.Text -AppName $exportAppName.Text -AppVersion $exportVersion.Text -DistroName $exportDistro.Text -ServerBaseDir $exportBaseDir.Text -PublicAddress $publicAddress -BuildFrontend $exportBuildFrontend.Checked -SkipTritonCheck $exportSkipTriton.Checked -SkipMediaMtxCheck $exportSkipMediaMtx.Checked -SkipGStreamerCheck $exportSkipGStreamer.Checked -WslMirroredNetworking $exportWslMirrored.Checked -PlcWritesAllowed $exportPlcWritesAllowed.Checked -MediaMtxArchivePath $exportMediaMtxArchive.Text
        $scriptPath = Join-Path (Resolve-DeploymentDir -RepoRoot $exportRepo.Text) "export-windows-update.ps1"
        Start-UiProcess -Form $form -LogBox $logBox -Button $exportUpdateButton -ScriptPath $scriptPath -Arguments @("-Config", $configPath) -WorkingDirectory $exportRepo.Text
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

# Install tab
$installTab.Controls.Add((New-Label "Bundle-ZIP optional" 18 24))
$installBundle = New-TextBox "" 170 22 600
$installTab.Controls.Add($installBundle)
$browseInstallBundle = New-Button "Datei..." 785 20 115
$browseInstallBundle.Add_Click({ $path = Select-File "Windows Installer (*.zip)|*-windows-installer.zip|ZIP Dateien (*.zip)|*.zip|Alle Dateien (*.*)|*.*"; if ($path) { $installBundle.Text = $path } })
$installTab.Controls.Add($browseInstallBundle)

$installTab.Controls.Add((New-Label "Installationsbasis" 18 62))
$installRoot = New-TextBox "C:\YBB_Inferenzserver" 170 60 330
$installTab.Controls.Add($installRoot)
$installTab.Controls.Add((New-Label "Distro" 530 62 55))
$installDistro = New-TextBox $defaultDistro 590 60 180
$installTab.Controls.Add($installDistro)

$installTab.Controls.Add((New-Label "IP/Host" 18 100))
$installHost = New-TextBox $defaultAddress.Host 170 98 220
$installTab.Controls.Add($installHost)
$installTab.Controls.Add((New-Label "Port" 420 100 55))
$installPort = New-TextBox $defaultAddress.Port 475 98 80
$installTab.Controls.Add($installPort)

$installPortProxy = New-CheckBox "Windows-Portproxy einrichten" $true 170 138 260
$installSkipHosts = New-CheckBox "Hosts-Datei nicht aendern" $true 450 138 240
$installTab.Controls.Add($installPortProxy)
$installTab.Controls.Add($installSkipHosts)

$installButton = New-Button "Installieren" 170 198 150
$portProxyButton = New-Button "Portproxy setzen" 340 198 150
$installTab.Controls.Add($installButton)
$installTab.Controls.Add($portProxyButton)

$installButton.Add_Click({
    try {
        $publicAddress = Join-PublicAddress -AddressHost $installHost.Text -Port $installPort.Text
        $scriptPath = Join-Path $script:RootDir "install-windows-offline.ps1"
        $args = @()
        if (-not [string]::IsNullOrWhiteSpace($installBundle.Text)) { $args += @("-BundlePath", $installBundle.Text) }
        if (-not [string]::IsNullOrWhiteSpace($installRoot.Text)) { $args += @("-InstallRoot", $installRoot.Text) }
        if (-not [string]::IsNullOrWhiteSpace($installDistro.Text)) { $args += @("-DistroName", $installDistro.Text) }
        $args += @("-PublicAddress", $publicAddress, "-ListenPort", $installPort.Text)
        if ($installSkipHosts.Checked) { $args += "-SkipHostsEntry" }
        if (-not $installPortProxy.Checked) { $args += "-SkipPortProxy" }
        Start-UiProcess -Form $form -LogBox $logBox -Button $installButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $script:RootDir
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

$portProxyButton.Add_Click({
    try {
        $scriptPath = Resolve-DeploymentScriptPath -FileName "configure-windows-portproxy.ps1"
        $connectPort = if ($installPort.Text.Trim() -eq "80") { "80" } else { "443" }
        $args = @("-DistroName", $installDistro.Text, "-ListenPort", $installPort.Text, "-ConnectPort", $connectPort)
        $listenHost = $installHost.Text.Trim()
        if ($listenHost -match "^\d{1,3}(\.\d{1,3}){3}$" -and $listenHost -ne "127.0.0.1") {
            $args += @("-ListenAddress", $listenHost)
            $args += "-WildcardListenAddress"
        }
        Start-UiProcess -Form $form -LogBox $logBox -Button $portProxyButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $script:RootDir
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

# Update tab
$updateTab.Controls.Add((New-Label "App-Verzeichnis" 18 24))
$updateAppDir = New-TextBox $defaultAppDir 170 22 600
$updateTab.Controls.Add($updateAppDir)
$browseUpdateAppDir = New-Button "Durchsuchen" 785 20 115
$browseUpdateAppDir.Add_Click({ $path = Select-Folder; if ($path) { $updateAppDir.Text = $path } })
$updateTab.Controls.Add($browseUpdateAppDir)

$updateTab.Controls.Add((New-Label "Update-ZIP optional" 18 62))
$updateBundle = New-TextBox "" 170 60 600
$updateTab.Controls.Add($updateBundle)
$browseUpdateBundle = New-Button "Datei..." 785 58 115
$browseUpdateBundle.Add_Click({ $path = Select-File "Windows Update (*.zip)|*-windows-update.zip|ZIP Dateien (*.zip)|*.zip|Alle Dateien (*.*)|*.*"; if ($path) { $updateBundle.Text = $path } })
$updateTab.Controls.Add($browseUpdateBundle)

$updateTab.Controls.Add((New-Label "Distro optional" 18 100))
$updateDistro = New-TextBox $defaultDistro 170 98 240
$updateTab.Controls.Add($updateDistro)
$updateKeepBundle = New-CheckBox "Update-ZIP nicht verschieben" $false 450 98 250
$updateTab.Controls.Add($updateKeepBundle)

$updateTab.Controls.Add((New-Label "IP/Host optional" 18 138))
$updateHost = New-TextBox "" 170 136 220
$updateTab.Controls.Add($updateHost)
$updateTab.Controls.Add((New-Label "Port" 420 138 55))
$updatePort = New-TextBox "443" 475 136 80
$updateTab.Controls.Add($updatePort)

$updateButton = New-Button "Update einspielen" 170 200 160
$openIncomingButton = New-Button "Incoming oeffnen" 350 200 150
$updateTab.Controls.Add($updateButton)
$updateTab.Controls.Add($openIncomingButton)

$updateButton.Add_Click({
    try {
        $scriptPath = Resolve-DeploymentScriptPath -FileName "apply-windows-update.ps1"
        $args = @()
        if (-not [string]::IsNullOrWhiteSpace($updateAppDir.Text)) { $args += @("-AppDir", $updateAppDir.Text) }
        if (-not [string]::IsNullOrWhiteSpace($updateBundle.Text)) { $args += @("-BundlePath", $updateBundle.Text) }
        if (-not [string]::IsNullOrWhiteSpace($updateDistro.Text)) { $args += @("-DistroName", $updateDistro.Text) }
        if (-not [string]::IsNullOrWhiteSpace($updateHost.Text)) {
            $publicAddress = Join-PublicAddress -AddressHost $updateHost.Text -Port $updatePort.Text
            $args += @("-PublicAddress", $publicAddress)
        }
        if ($updateKeepBundle.Checked) { $args += "-KeepBundle" }
        Start-UiProcess -Form $form -LogBox $logBox -Button $updateButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $script:RootDir
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

$openIncomingButton.Add_Click({
    try {
        $incoming = Join-Path $updateAppDir.Text "incoming"
        New-Item -ItemType Directory -Path $incoming -Force | Out-Null
        Start-Process explorer.exe $incoming
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

# Models tab
$modelsCatalog = Get-PretrainedModelUiCatalog
$modelsTab.Controls.Add((New-Label "Repo-Pfad" 18 20))
$modelsRepo = New-TextBox $repoRoot 170 18 600
$modelsTab.Controls.Add($modelsRepo)
$browseModelsRepo = New-Button "Durchsuchen" 785 16 115
$browseModelsRepo.Add_Click({ $path = Select-Folder; if ($path) { $modelsRepo.Text = $path } })
$modelsTab.Controls.Add($browseModelsRepo)

$modelsTab.Controls.Add((New-Label "Paket-Zielordner" 18 56))
$modelsOutputDir = New-TextBox (Join-Path $repoRoot "dist") 170 54 600
$modelsTab.Controls.Add($modelsOutputDir)
$browseModelsOutput = New-Button "Durchsuchen" 785 52 115
$browseModelsOutput.Add_Click({ $path = Select-Folder; if ($path) { $modelsOutputDir.Text = $path } })
$modelsTab.Controls.Add($browseModelsOutput)

$modelsTab.Controls.Add((New-Label "Quellordner optional" 18 92))
$modelsSourceDir = New-TextBox "" 170 90 600
$modelsTab.Controls.Add($modelsSourceDir)
$browseModelsSource = New-Button "Durchsuchen" 785 88 115
$browseModelsSource.Add_Click({ $path = Select-Folder; if ($path) { $modelsSourceDir.Text = $path } })
$modelsTab.Controls.Add($browseModelsSource)

$modelsTab.Controls.Add((New-Label "Modellauswahl" 18 128))
$modelsList = New-Object System.Windows.Forms.CheckedListBox
$modelsList.Location = New-Object System.Drawing.Point(170, 126)
$modelsList.Size = New-Object System.Drawing.Size(600, 205)
$modelsList.CheckOnClick = $true
$modelsList.IntegralHeight = $false
$modelsList.HorizontalScrollbar = $true
foreach ($item in $modelsCatalog) {
    [void]$modelsList.Items.Add([string]$item.Label, [bool]$item.Recommended)
}
$modelsTab.Controls.Add($modelsList)

$modelsAllButton = New-Button "Alle" 785 126 115
$modelsRecommendedButton = New-Button "Empfohlen" 785 162 115
$modelsNoneButton = New-Button "Keine" 785 198 115
$modelsTab.Controls.Add($modelsAllButton)
$modelsTab.Controls.Add($modelsRecommendedButton)
$modelsTab.Controls.Add($modelsNoneButton)
$modelsAllButton.Add_Click({ Set-CheckedListState -List $modelsList -Catalog $modelsCatalog -Mode "All" })
$modelsRecommendedButton.Add_Click({ Set-CheckedListState -List $modelsList -Catalog $modelsCatalog -Mode "Recommended" })
$modelsNoneButton.Add_Click({ Set-CheckedListState -List $modelsList -Catalog $modelsCatalog -Mode "None" })

$exportModelsButton = New-Button "Modell-Update exportieren" 170 348 210
$modelsTab.Controls.Add($exportModelsButton)
$exportModelsButton.Add_Click({
    try {
        $selectedIds = Get-CheckedPretrainedModelIds -List $modelsList -Catalog $modelsCatalog
        if ($selectedIds.Count -eq 0) {
            throw "Bitte mindestens ein Modell auswaehlen."
        }
        $scriptPath = Join-Path (Resolve-DeploymentDir -RepoRoot $modelsRepo.Text) "export-pretrained-models-update.ps1"
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            $scriptPath = Resolve-DeploymentScriptPath -FileName "export-pretrained-models-update.ps1"
        }
        $args = @("-OutputDir", $modelsOutputDir.Text, "-Models", ($selectedIds -join ","))
        if (-not [string]::IsNullOrWhiteSpace($modelsSourceDir.Text)) {
            $args += @("-SourceDirectory", $modelsSourceDir.Text)
        }
        Start-UiProcess -Form $form -LogBox $logBox -Button $exportModelsButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $modelsRepo.Text
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

$modelsTab.Controls.Add((New-Label "App-Verzeichnis" 18 374))
$modelsAppDir = New-TextBox $defaultAppDir 170 372 600
$modelsTab.Controls.Add($modelsAppDir)
$browseModelsAppDir = New-Button "Durchsuchen" 785 370 115
$browseModelsAppDir.Add_Click({ $path = Select-Folder; if ($path) { $modelsAppDir.Text = $path } })
$modelsTab.Controls.Add($browseModelsAppDir)

$modelsTab.Controls.Add((New-Label "Modellupdate-ZIP" 18 410))
$modelsBundle = New-TextBox "" 170 408 600
$modelsTab.Controls.Add($modelsBundle)
$browseModelsBundle = New-Button "Datei..." 785 406 115
$browseModelsBundle.Add_Click({ $path = Select-File "Pretrained Modelle (*.zip)|*pretrained-models-update*.zip|ZIP Dateien (*.zip)|*.zip|Alle Dateien (*.*)|*.*"; if ($path) { $modelsBundle.Text = $path } })
$modelsTab.Controls.Add($browseModelsBundle)

$modelsTab.Controls.Add((New-Label "Distro optional" 18 446))
$modelsDistro = New-TextBox $defaultDistro 170 444 240
$modelsTab.Controls.Add($modelsDistro)
$modelsKeepBundle = New-CheckBox "Modellupdate-ZIP nicht verschieben" $false 450 442 270
$modelsTab.Controls.Add($modelsKeepBundle)

$applyModelsButton = New-Button "Modelle einspielen" 170 482 170
$openModelsIncomingButton = New-Button "Incoming oeffnen" 360 482 150
$modelsTab.Controls.Add($applyModelsButton)
$modelsTab.Controls.Add($openModelsIncomingButton)

$applyModelsButton.Add_Click({
    try {
        $scriptPath = Resolve-DeploymentScriptPath -FileName "apply-windows-update.ps1"
        $args = @()
        if (-not [string]::IsNullOrWhiteSpace($modelsAppDir.Text)) { $args += @("-AppDir", $modelsAppDir.Text) }
        if (-not [string]::IsNullOrWhiteSpace($modelsBundle.Text)) { $args += @("-BundlePath", $modelsBundle.Text) }
        if (-not [string]::IsNullOrWhiteSpace($modelsDistro.Text)) { $args += @("-DistroName", $modelsDistro.Text) }
        if ($modelsKeepBundle.Checked) { $args += "-KeepBundle" }
        Start-UiProcess -Form $form -LogBox $logBox -Button $applyModelsButton -ScriptPath $scriptPath -Arguments $args -WorkingDirectory $script:RootDir
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

$openModelsIncomingButton.Add_Click({
    try {
        $incoming = Join-Path $modelsAppDir.Text "incoming"
        New-Item -ItemType Directory -Path $incoming -Force | Out-Null
        Start-Process explorer.exe $incoming
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "YBB Deployment", "OK", "Error") | Out-Null
    }
})

if ($Mode -eq "Auto") {
    $manifest = Read-JsonFile -Path (Join-Path $script:RootDir "windows-bundle-manifest.json")
    $modelsManifest = Read-JsonFile -Path (Join-Path $script:RootDir "pretrained-models-manifest.json")
    if ($null -ne $modelsManifest -and $modelsManifest.PSObject.Properties.Name -contains "package_type" -and [string]$modelsManifest.package_type -eq "pretrained-models-update") {
        $tabs.SelectedTab = $modelsTab
    }
    elseif ($null -ne $manifest -and $manifest.PSObject.Properties.Name -contains "BundleType" -and [string]$manifest.BundleType -eq "installer") {
        $tabs.SelectedTab = $installTab
    }
    elseif ((Split-Path -Leaf $script:RootDir) -ieq "updater" -or (Test-Path -LiteralPath (Join-Path $defaultAppDir "install-state.json") -PathType Leaf)) {
        $tabs.SelectedTab = $updateTab
    }
    else {
        $tabs.SelectedTab = $exportTab
    }
}
elseif ($Mode -eq "Export") {
    $tabs.SelectedTab = $exportTab
}
elseif ($Mode -eq "Install") {
    $tabs.SelectedTab = $installTab
}
elseif ($Mode -eq "Update") {
    $tabs.SelectedTab = $updateTab
}
elseif ($Mode -eq "Models") {
    $tabs.SelectedTab = $modelsTab
}

Append-Log -LogBox $logBox -Text "Bereit. EXE/Script-Pfad: $script:RootDir"
Append-Log -LogBox $logBox -Text "UI-Crash-Log: $script:CrashLogPath"
[void][System.Windows.Forms.Application]::Run($form)
