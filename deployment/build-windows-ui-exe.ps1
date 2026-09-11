[CmdletBinding()]
param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$deploymentDir = Split-Path -Parent $PSCommandPath
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $deploymentDir "YBB-Deployment-UI.exe"
}
$uiScriptPath = Join-Path $deploymentDir "windows-deployment-ui.ps1"
if (-not (Test-Path -LiteralPath $uiScriptPath -PathType Leaf)) {
    throw "UI-Skript wurde nicht gefunden: $uiScriptPath"
}

$source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = FindScript(exeDir);
        if (String.IsNullOrWhiteSpace(scriptPath))
        {
            MessageBox.Show("windows-deployment-ui.ps1 wurde nicht neben der EXE gefunden.", "YBB Deployment", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        string powerShell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
        if (!File.Exists(powerShell))
        {
            powerShell = "powershell.exe";
        }

        StringBuilder arguments = new StringBuilder();
        arguments.Append("-NoProfile -STA -ExecutionPolicy Bypass -File ");
        arguments.Append(Quote(scriptPath));
        foreach (string arg in args)
        {
            arguments.Append(" ");
            arguments.Append(Quote(arg));
        }

        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = powerShell;
        startInfo.Arguments = arguments.ToString();
        startInfo.WorkingDirectory = exeDir;
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.WindowStyle = ProcessWindowStyle.Hidden;

        try
        {
            Process process = Process.Start(startInfo);
            if (process == null)
            {
                return 1;
            }
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "YBB Deployment", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string FindScript(string exeDir)
    {
        string[] candidates = new string[]
        {
            Path.Combine(exeDir, "windows-deployment-ui.ps1"),
            Path.Combine(exeDir, "deployment", "windows-deployment-ui.ps1"),
            Path.Combine(Directory.GetParent(exeDir.TrimEnd(Path.DirectorySeparatorChar)) == null ? exeDir : Directory.GetParent(exeDir.TrimEnd(Path.DirectorySeparatorChar)).FullName, "deployment", "windows-deployment-ui.ps1")
        };
        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        return "";
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
'@

New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
Add-Type `
    -TypeDefinition $source `
    -Language CSharp `
    -OutputAssembly $OutputPath `
    -OutputType WindowsApplication `
    -ReferencedAssemblies @("System.Windows.Forms.dll", "System.Drawing.dll")

Write-Host "Windows-UI-EXE erstellt: $OutputPath"
