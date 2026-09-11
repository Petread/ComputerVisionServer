using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace Ybb.Edge.WindowsInstaller
{
    internal sealed class InstallerForm : Form
    {
        private readonly string bundleRoot;
        private readonly Dictionary<string, string> environment;
        private readonly Dictionary<string, object> provisioning;
        private readonly Dictionary<string, TextBox> textFields =
            new Dictionary<string, TextBox>(StringComparer.OrdinalIgnoreCase);
        private readonly ComboBox interfaceAlias = new ComboBox();
        private readonly NumericUpDown prefixLength = new NumericUpDown();
        private readonly NumericUpDown controlPort = new NumericUpDown();
        private readonly NumericUpDown plantPort = new NumericUpDown();
        private readonly NumericUpDown rtspPort = new NumericUpDown();
        private readonly CheckBox allowPlcWrites = new CheckBox();
        private readonly CheckBox applyBaseProvisioning = new CheckBox();
        private readonly RichTextBox log = new RichTextBox();
        private readonly Button installButton = new Button();
        private readonly Button closeButton = new Button();
        private bool busy;

        public InstallerForm(string root)
        {
            bundleRoot = root;
            environment = ReadEnvironment(Path.Combine(root, "device.env"));
            provisioning = ReadJson(Path.Combine(root, "provisioning.json"));
            ValidateBundlePlatform();

            Text = "YBB Edge Windows Installer";
            Width = 940;
            Height = 820;
            MinimumSize = new Size(800, 650);
            StartPosition = FormStartPosition.CenterScreen;

            var split = new SplitContainer
            {
                Dock = DockStyle.Fill,
                Orientation = Orientation.Horizontal,
                SplitterDistance = 570
            };
            var fields = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoScroll = true,
                ColumnCount = 2,
                Padding = new Padding(14)
            };
            fields.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 300));
            fields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

            AddTextRow(fields, "hostname", "Gerätename", Env("YBB_EDGE_HOSTNAME", "ybb-edge-01"));
            ConfigureInterfaces();
            AddControlRow(fields, "Windows-Netzwerkadapter", interfaceAlias);
            AddTextRow(fields, "device_ipv4", "Edge-Geräte-IP", Env("YBB_EDGE_MANAGEMENT_IP", "192.168.50.21"));
            ConfigureNumber(prefixLength, 8, 32, Number("prefix_length", 24));
            AddControlRow(fields, "IPv4-Präfixlänge", prefixLength);
            AddTextRow(fields, "gateway", "Gateway", Provision("gateway_ipv4", "192.168.50.1"));
            AddTextRow(fields, "dns", "DNS-Server (Komma)", Provision("dns_servers", "192.168.50.1"));
            AddTextRow(fields, "server_ipv4", "Orchestrierungsserver-IP", Env("YBB_EDGE_SERVER_IP", "192.168.50.10"));
            AddTextRow(fields, "plant_ips", "Erlaubte HTTP-Trigger-Clients (Komma)", Env("YBB_EDGE_PLANT_ALLOWED_IPS", ""));
            ConfigureNumber(controlPort, 1, 65535, IntEnv("YBB_EDGE_CONTROL_PORT", 9443));
            AddControlRow(fields, "Edge-Control-Port", controlPort);
            ConfigureNumber(plantPort, 1, 65535, IntEnv("YBB_EDGE_PLANT_API_PORT", 9444));
            AddControlRow(fields, "HTTP-Trigger-Port", plantPort);
            ConfigureNumber(rtspPort, 1, 65535, IntEnv("YBB_EDGE_RTSP_PORT", 8554));
            AddControlRow(fields, "RTSP-Port", rtspPort);
            AddTextRow(fields, "state_root", "Datenverzeichnis", Env("YBB_EDGE_DATA_ROOT", @"C:\ProgramData\YBB\Edge"));
            AddTextRow(fields, "install_root", "Installationsverzeichnis", @"C:\Program Files\YBB Edge");
            AddTextRow(fields, "camera_adapters", "Kameraadapter (Komma)", Env("YBB_EDGE_CAMERA_ADAPTERS", "rtsp,usb_opencv"));
            AddTextRow(fields, "release_key", "Öffentlicher Release-Schlüssel (optional)", "");

            allowPlcWrites.Text = "SPS-Schreiben aus Pipeline erlauben";
            allowPlcWrites.AutoSize = true;
            allowPlcWrites.Checked = String.Equals(
                Env("YBB_ALLOW_PLC_WRITES", "false"), "true", StringComparison.OrdinalIgnoreCase);
            AddControlRow(fields, "SPS-Sicherheitsgate", allowPlcWrites);
            applyBaseProvisioning.Text = "Hostname, statische IP, DNS und Energieprofil setzen";
            applyBaseProvisioning.AutoSize = true;
            applyBaseProvisioning.Checked = true;
            AddControlRow(fields, "Windows-Erstprovisionierung", applyBaseProvisioning);

            var buttons = new FlowLayoutPanel
            {
                Dock = DockStyle.Bottom,
                Height = 52,
                Padding = new Padding(12),
                FlowDirection = FlowDirection.RightToLeft
            };
            installButton.Text = "Installieren";
            installButton.AutoSize = true;
            installButton.Click += InstallClicked;
            closeButton.Text = "Schließen";
            closeButton.AutoSize = true;
            closeButton.Click += delegate { Close(); };
            var keyButton = new Button { Text = "Release-Schlüssel auswählen", AutoSize = true };
            keyButton.Click += SelectReleaseKey;
            buttons.Controls.Add(installButton);
            buttons.Controls.Add(closeButton);
            buttons.Controls.Add(keyButton);

            log.Dock = DockStyle.Fill;
            log.ReadOnly = true;
            log.BackColor = Color.FromArgb(25, 25, 25);
            log.ForeColor = Color.Gainsboro;
            split.Panel1.Controls.Add(fields);
            split.Panel1.Controls.Add(buttons);
            split.Panel2.Controls.Add(log);
            Controls.Add(split);
            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (!busy) return;
                args.Cancel = true;
                MessageBox.Show(this, "Die laufende Installation muss zuerst abgeschlossen werden.",
                    "Installation läuft", MessageBoxButtons.OK, MessageBoxIcon.Information);
            };
            Append("Bundle: " + bundleRoot);
            Append("Alle Geräteeinstellungen werden durch diesen Installer erzeugt und validiert.");
        }

        private void ValidateBundlePlatform()
        {
            var profile = ReadJson(Path.Combine(bundleRoot, "platform-profile.json"));
            if (!String.Equals(Value(profile, "platform", ""), "windows_11", StringComparison.Ordinal))
                throw new InvalidDataException("Diese Installer-EXE benötigt ein Windows-11-Bundle.");
            foreach (var required in new[]
            {
                "SHA256SUMS", "SIGNATURE_POLICY", "python-runtime.zip", "winsw.exe",
                "cuda-runtime-installer.exe", @"deployment\edge\Install-WindowsOffline.ps1"
            })
            {
                if (!File.Exists(Path.Combine(bundleRoot, required)))
                    throw new FileNotFoundException("Bundle-Inhalt fehlt: " + required);
            }
        }

        private void ConfigureInterfaces()
        {
            interfaceAlias.DropDownStyle = ComboBoxStyle.DropDown;
            interfaceAlias.Dock = DockStyle.Fill;
            foreach (var adapter in NetworkInterface.GetAllNetworkInterfaces()
                .Where(item => item.NetworkInterfaceType != NetworkInterfaceType.Loopback)
                .OrderBy(item => item.Name))
                interfaceAlias.Items.Add(adapter.Name);
            var configured = Provision("windows_interface_alias", "Ethernet");
            interfaceAlias.Text = configured;
        }

        private static void ConfigureNumber(NumericUpDown control, int minimum, int maximum, int value)
        {
            control.Minimum = minimum;
            control.Maximum = maximum;
            control.Value = Math.Max(minimum, Math.Min(maximum, value));
            control.Dock = DockStyle.Left;
            control.Width = 140;
        }

        private void AddTextRow(TableLayoutPanel panel, string key, string label, string value)
        {
            var text = new TextBox { Text = value, Dock = DockStyle.Fill };
            textFields[key] = text;
            AddControlRow(panel, label, text);
        }

        private static void AddControlRow(TableLayoutPanel panel, string label, Control control)
        {
            var row = panel.RowCount++;
            panel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            var caption = new Label
            {
                Text = label,
                AutoSize = true,
                Anchor = AnchorStyles.Left,
                Padding = new Padding(0, 7, 8, 7)
            };
            control.Margin = new Padding(3, 5, 3, 5);
            panel.Controls.Add(caption, 0, row);
            panel.Controls.Add(control, 1, row);
        }

        private void SelectReleaseKey(object sender, EventArgs e)
        {
            using (var dialog = new OpenFileDialog
            {
                Filter = "PEM-Schlüssel (*.pem)|*.pem|Alle Dateien (*.*)|*.*"
            })
            {
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    textFields["release_key"].Text = dialog.FileName;
            }
        }

        private void InstallClicked(object sender, EventArgs e)
        {
            try
            {
                ValidateForm();
                if (applyBaseProvisioning.Checked)
                {
                    var answer = MessageBox.Show(
                        this,
                        "Die Windows-Erstprovisionierung setzt Hostname und statische IP. " +
                        "Eine bestehende Remote-Verbindung kann dadurch getrennt werden. Fortfahren?",
                        "YBB Edge Erstinstallation",
                        MessageBoxButtons.YesNo,
                        MessageBoxIcon.Warning);
                    if (answer != DialogResult.Yes) return;
                }
                var installSettings = CaptureSettings();
                SetBusy(true);
                Task.Factory.StartNew(() => InstallWorker(installSettings)).ContinueWith(task =>
                {
                    BeginInvoke((Action)(() =>
                    {
                        SetBusy(false);
                        if (task.IsFaulted)
                        {
                            var error = task.Exception == null ? "Unbekannter Fehler" :
                                task.Exception.GetBaseException().Message;
                            Append("FEHLER: " + error);
                            MessageBox.Show(this, error, "Installation fehlgeschlagen",
                                MessageBoxButtons.OK, MessageBoxIcon.Error);
                        }
                        else
                        {
                            Append("Installation erfolgreich abgeschlossen.");
                            MessageBox.Show(this,
                                "YBB Edge wurde installiert. Falls CUDA einen Neustart angefordert hat, " +
                                "Windows jetzt neu starten.",
                                "Installation abgeschlossen", MessageBoxButtons.OK,
                                MessageBoxIcon.Information);
                        }
                    }));
                });
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, ex.Message, "Ungültige Eingabe",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private Dictionary<string, string> CaptureSettings()
        {
            var settings = textFields.ToDictionary(
                item => item.Key, item => item.Value.Text.Trim(), StringComparer.OrdinalIgnoreCase);
            settings["interface_alias"] = interfaceAlias.Text.Trim();
            settings["prefix_length"] = Convert.ToString((int)prefixLength.Value);
            settings["control_port"] = Convert.ToString((int)controlPort.Value);
            settings["plant_port"] = Convert.ToString((int)plantPort.Value);
            settings["rtsp_port"] = Convert.ToString((int)rtspPort.Value);
            settings["allow_plc_writes"] = allowPlcWrites.Checked ? "true" : "false";
            settings["apply_base"] = applyBaseProvisioning.Checked ? "true" : "false";
            return settings;
        }

        private void InstallWorker(Dictionary<string, string> settings)
        {
            var generatedConfig = Path.Combine(
                Path.GetTempPath(), "ybb-edge-device-" + Guid.NewGuid().ToString("N") + ".env");
            try
            {
                File.WriteAllText(
                    generatedConfig, BuildEnvironment(settings), new UTF8Encoding(false));
                var verificationArguments = new List<string>
                {
                    "-BundleRoot", bundleRoot,
                    "-VerifyOnly"
                };
                if (!String.IsNullOrWhiteSpace(settings["release_key"]))
                {
                    verificationArguments.Add("-TrustedReleasePublicKey");
                    verificationArguments.Add(settings["release_key"]);
                }
                RunPowerShell(
                    Path.Combine(bundleRoot, @"deployment\edge\Install-WindowsOffline.ps1"),
                    verificationArguments);
                if (settings["apply_base"] == "true")
                {
                    var baseArguments = new List<string>
                    {
                        "-Hostname", settings["hostname"],
                        "-InterfaceAlias", settings["interface_alias"],
                        "-Address", settings["device_ipv4"],
                        "-PrefixLength", settings["prefix_length"],
                        "-Gateway", settings["gateway"],
                        "-DnsServersCsv", settings["dns"],
                        "-ServerIp", settings["server_ipv4"],
                        "-StateRoot", settings["state_root"]
                    };
                    var releaseKey = settings["release_key"];
                    if (!String.IsNullOrWhiteSpace(releaseKey))
                    {
                        baseArguments.Add("-ReleasePublicKey");
                        baseArguments.Add(releaseKey);
                    }
                    RunPowerShell(
                        Path.Combine(bundleRoot, @"deployment\edge\provisioning\Apply-WindowsBase.ps1"),
                        baseArguments);
                }
                var installArguments = new List<string>
                {
                    "-BundleRoot", bundleRoot,
                    "-InstallRoot", settings["install_root"],
                    "-StateRoot", settings["state_root"],
                    "-DeviceConfigPath", generatedConfig
                };
                if (!String.IsNullOrWhiteSpace(settings["release_key"]))
                {
                    installArguments.Add("-TrustedReleasePublicKey");
                    installArguments.Add(settings["release_key"]);
                }
                RunPowerShell(
                    Path.Combine(bundleRoot, @"deployment\edge\Install-WindowsOffline.ps1"),
                    installArguments);
            }
            finally
            {
                try { File.Delete(generatedConfig); } catch { }
            }
        }

        private void RunPowerShell(string script, IList<string> arguments)
        {
            var powershell = Path.Combine(
                Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
            var command = new List<string> { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script };
            command.AddRange(arguments);
            var process = new Process();
            process.StartInfo = new ProcessStartInfo
            {
                FileName = powershell,
                Arguments = String.Join(" ", command.Select(QuoteArgument)),
                WorkingDirectory = bundleRoot,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true
            };
            process.OutputDataReceived += delegate(object o, DataReceivedEventArgs a)
            {
                if (a.Data != null) BeginInvoke((Action)(() => Append(a.Data)));
            };
            process.ErrorDataReceived += delegate(object o, DataReceivedEventArgs a)
            {
                if (a.Data != null) BeginInvoke((Action)(() => Append("FEHLER: " + a.Data)));
            };
            Append("Starte: " + Path.GetFileName(script));
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            var exitCode = process.ExitCode;
            process.Dispose();
            if (exitCode != 0)
                throw new InvalidOperationException(
                    Path.GetFileName(script) + " wurde mit Exit-Code " + exitCode + " beendet.");
        }

        private string BuildEnvironment(Dictionary<string, string> settings)
        {
            var values = new SortedDictionary<string, string>(environment, StringComparer.Ordinal);
            values["YBB_EDGE_HOSTNAME"] = settings["hostname"];
            values["YBB_EDGE_MANAGEMENT_IP"] = settings["device_ipv4"];
            values["YBB_EDGE_SERVER_IP"] = settings["server_ipv4"];
            values["YBB_EDGE_PLANT_ALLOWED_IPS"] = NormalizeList(settings["plant_ips"]);
            values["YBB_EDGE_CONTROL_PORT"] = settings["control_port"];
            values["YBB_EDGE_PLANT_API_PORT"] = settings["plant_port"];
            values["YBB_EDGE_RTSP_PORT"] = settings["rtsp_port"];
            values["YBB_EDGE_DATA_ROOT"] = settings["state_root"];
            values["YBB_EDGE_CAMERA_ADAPTERS"] = NormalizeList(settings["camera_adapters"]);
            values["YBB_ALLOW_PLC_WRITES"] = settings["allow_plc_writes"];
            var output = new StringBuilder();
            foreach (var item in values)
                output.Append(item.Key).Append('=').Append(item.Value).Append('\n');
            return output.ToString();
        }

        private void ValidateForm()
        {
            if (!Regex.IsMatch(Field("hostname"), @"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$"))
                throw new InvalidDataException("Der Gerätename ist ungültig.");
            ValidateIpv4(Field("device_ipv4"), "Edge-Geräte-IP");
            ValidateIpv4(Field("gateway"), "Gateway");
            ValidateIpv4(Field("server_ipv4"), "Orchestrierungsserver-IP");
            ValidateIpv4List(Field("dns"), "DNS-Server", true);
            ValidateIpv4List(Field("plant_ips"), "HTTP-Trigger-Clients", false);
            if (String.IsNullOrWhiteSpace(interfaceAlias.Text) && applyBaseProvisioning.Checked)
                throw new InvalidDataException("Ein Windows-Netzwerkadapter ist erforderlich.");
            if (!Path.IsPathRooted(Field("state_root")) || !Path.IsPathRooted(Field("install_root")))
                throw new InvalidDataException("Installations- und Datenverzeichnis müssen absolute Pfade sein.");
            if (String.IsNullOrWhiteSpace(Field("camera_adapters")))
                throw new InvalidDataException("Mindestens ein Kameraadapter ist erforderlich.");
            foreach (var value in textFields.Values.Select(item => item.Text).Concat(new[] { interfaceAlias.Text }))
            {
                if (value.IndexOfAny(new[] { '\r', '\n', '\0', '"' }) >= 0)
                    throw new InvalidDataException("Eingaben dürfen keine Steuerzeichen oder Anführungszeichen enthalten.");
            }
            var releaseKey = Field("release_key");
            var policy = File.ReadAllText(Path.Combine(bundleRoot, "SIGNATURE_POLICY")).Trim();
            var pinnedKey = @"C:\ProgramData\YBB\Trust\release-signing-public.pem";
            if ((policy == "signed" || policy == "required") &&
                !File.Exists(releaseKey) && !File.Exists(pinnedKey))
                throw new FileNotFoundException(
                    "Für dieses signierte Bundle muss der öffentliche Release-Schlüssel ausgewählt werden.");
        }

        private static void ValidateIpv4(string value, string label)
        {
            IPAddress parsed;
            if (!IPAddress.TryParse(value, out parsed) || parsed.AddressFamily != AddressFamily.InterNetwork)
                throw new InvalidDataException(label + " ist keine gültige IPv4-Adresse.");
        }

        private static void ValidateIpv4List(string value, string label, bool required)
        {
            var entries = value.Split(',').Select(item => item.Trim())
                .Where(item => item.Length > 0).ToArray();
            if (required && entries.Length == 0)
                throw new InvalidDataException(label + " darf nicht leer sein.");
            foreach (var entry in entries) ValidateIpv4(entry, label);
        }

        private static string NormalizeList(string value)
        {
            return String.Join(",", value.Split(',').Select(item => item.Trim())
                .Where(item => item.Length > 0));
        }

        private void SetBusy(bool busy)
        {
            this.busy = busy;
            installButton.Enabled = !busy;
            closeButton.Enabled = !busy;
            UseWaitCursor = busy;
        }

        private string Field(string key) { return textFields[key].Text.Trim(); }
        private string Env(string key, string fallback)
        {
            return environment.ContainsKey(key) ? environment[key] : fallback;
        }
        private int IntEnv(string key, int fallback)
        {
            int value;
            return Int32.TryParse(Env(key, ""), out value) ? value : fallback;
        }
        private string Provision(string key, string fallback) { return Value(provisioning, key, fallback); }
        private int Number(string key, int fallback)
        {
            int value;
            return Int32.TryParse(Provision(key, ""), out value) ? value : fallback;
        }
        private void Append(string value)
        {
            if (InvokeRequired) { BeginInvoke((Action)(() => Append(value))); return; }
            log.AppendText(DateTime.Now.ToString("HH:mm:ss") + "  " + value + Environment.NewLine);
        }

        private static Dictionary<string, string> ReadEnvironment(string path)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var raw in File.ReadAllLines(path, Encoding.UTF8))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                var separator = line.IndexOf('=');
                if (separator < 1) throw new InvalidDataException("Ungültige device.env-Zeile.");
                result[line.Substring(0, separator)] = line.Substring(separator + 1);
            }
            return result;
        }

        private static Dictionary<string, object> ReadJson(string path)
        {
            var result = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(path))
                as Dictionary<string, object>;
            if (result == null) throw new InvalidDataException("JSON-Objekt erwartet: " + path);
            return result;
        }

        private static string Value(Dictionary<string, object> map, string key, string fallback)
        {
            return map.ContainsKey(key) && map[key] != null ? Convert.ToString(map[key]) : fallback;
        }

        private static string QuoteArgument(string value)
        {
            if (value.IndexOf('"') >= 0) throw new InvalidDataException("Ungültiges Anführungszeichen.");
            var trailingBackslashes = value.Reverse().TakeWhile(character => character == '\\').Count();
            return "\"" + value + new String('\\', trailingBackslashes) + "\"";
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            string temporaryBundleRoot = null;
            try
            {
                var identity = WindowsIdentity.GetCurrent();
                var principal = new WindowsPrincipal(identity);
                if (!principal.IsInRole(WindowsBuiltInRole.Administrator))
                {
                    Process.Start(new ProcessStartInfo(Application.ExecutablePath)
                    {
                        UseShellExecute = true,
                        Verb = "runas",
                        WorkingDirectory = AppDomain.CurrentDomain.BaseDirectory
                    });
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                var root = FindBundleRoot(AppDomain.CurrentDomain.BaseDirectory);
                if (root == null)
                {
                    var archive = SelectBundleArchive();
                    if (archive == null) return;
                    temporaryBundleRoot = ExtractBundleArchive(archive);
                    root = temporaryBundleRoot;
                }
                Application.Run(new InstallerForm(root));
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message, "YBB Edge Windows Installer",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                if (temporaryBundleRoot != null)
                {
                    try { Directory.Delete(temporaryBundleRoot, true); } catch { }
                }
            }
        }

        private static string SelectBundleArchive()
        {
            var directory = AppDomain.CurrentDomain.BaseDirectory;
            var candidates = Directory.GetFiles(
                directory, "ybb-edge-windows_11-*.zip", SearchOption.TopDirectoryOnly);
            if (candidates.Length == 1) return candidates[0];
            using (var dialog = new OpenFileDialog
            {
                Title = "YBB Edge Windows-Bundle auswählen",
                Filter = "YBB Windows-Bundle (ybb-edge-windows_11-*.zip)|ybb-edge-windows_11-*.zip|ZIP-Archive (*.zip)|*.zip"
            })
            {
                return dialog.ShowDialog() == DialogResult.OK ? dialog.FileName : null;
            }
        }

        private static string ExtractBundleArchive(string archivePath)
        {
            var destination = Path.Combine(
                Path.GetTempPath(), "ybb-edge-installer-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(destination);
            var destinationPrefix = Path.GetFullPath(destination)
                .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Cursor.Current = Cursors.WaitCursor;
            try
            {
                using (var archive = ZipFile.OpenRead(archivePath))
                {
                    foreach (var entry in archive.Entries)
                    {
                        var relative = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                        var target = Path.GetFullPath(Path.Combine(destination, relative));
                        if (!target.StartsWith(destinationPrefix, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidDataException(
                                "Unsicherer Pfad im Bundle-Archiv: " + entry.FullName);
                        if (String.IsNullOrEmpty(entry.Name))
                        {
                            Directory.CreateDirectory(target);
                            continue;
                        }
                        var parent = Path.GetDirectoryName(target);
                        if (!String.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);
                        using (var input = entry.Open())
                        using (var output = new FileStream(
                            target, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                            8 * 1024 * 1024, FileOptions.SequentialScan))
                            input.CopyTo(output, 8 * 1024 * 1024);
                    }
                }
                if (FindBundleRoot(destination) == null)
                    throw new InvalidDataException(
                        "Das ausgewählte Archiv ist kein vollständiges YBB Windows-Bundle.");
                return destination;
            }
            catch
            {
                try { Directory.Delete(destination, true); } catch { }
                throw;
            }
            finally
            {
                Cursor.Current = Cursors.Default;
            }
        }

        private static string FindBundleRoot(string start)
        {
            var directory = new DirectoryInfo(start);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "SHA256SUMS")) &&
                    File.Exists(Path.Combine(directory.FullName, "platform-profile.json")))
                    return directory.FullName;
                directory = directory.Parent;
            }
            return null;
        }
    }
}
