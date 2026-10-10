using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace Ybb.Edge.BundleBuilder
{
    internal enum TargetPlatform
    {
        [Description("NVIDIA Jetson Orin Nano")] JetsonOrinNano,
        [Description("NVIDIA Jetson AGX Orin")] JetsonAgxOrin,
        [Description("Windows 11 / NVIDIA x86-64")] Windows11
    }

    internal sealed class BundleOptions
    {
        [Category("Release"), DisplayName("Zielplattform")]
        public TargetPlatform TargetPlatform { get; set; }

        [Category("Release"), DisplayName("Release-Version")]
        public string ReleaseVersion { get; set; }

        [Category("Release"), DisplayName("SOURCE_DATE_EPOCH")]
        public long SourceDateEpoch { get; set; }

        [Category("Release"), DisplayName("Ausgabeverzeichnis")]
        public string OutputDirectory { get; set; }

        [Category("Inhalte"), DisplayName("Wheelhouse-Verzeichnis")]
        public string WheelhouseDirectory { get; set; }

        [Category("Wheelhouse-Vorbereitung"), DisplayName("HTTPS-Paketquelle")]
        public string PackageIndexUrl { get; set; }

        [Category("Wheelhouse-Vorbereitung"), DisplayName("Zusätzliche Hersteller-Wheels")]
        public string AdditionalWheelDirectory { get; set; }

        [Category("Inhalte"), DisplayName("Interne Wheels im Offline-Build erneut bauen")]
        public bool BuildInternalWheels { get; set; }

        [Category("Inhalte"), DisplayName("Python für Build")]
        public string PythonExecutable { get; set; }

        [Category("Inhalte"), DisplayName("Zu installierende Distributionen (Komma)")]
        public string RuntimeDistributions { get; set; }

        [Category("Windows-Inhalte"), DisplayName("Python-Runtime ZIP")]
        public string PythonRuntimeArchive { get; set; }

        [Category("Windows-Inhalte"), DisplayName("WinSW EXE")]
        public string WinSwExecutable { get; set; }

        [Category("Windows-Inhalte"), DisplayName("Native Installer EXE")]
        public string WindowsInstallerExecutable { get; set; }

        [Category("Gerät"), DisplayName("Hostname")]
        public string DeviceHostname { get; set; }

        [Category("Gerät"), DisplayName("Edge-Geräte-IP (feste IPv4)")]
        [Description("IP-Adresse des Edge-Geräts selbst. Der Orchestrierungsserver erreicht darüber die Edge-Control-API.")]
        public string DeviceIpv4 { get; set; }

        [Category("Gerät"), DisplayName("Präfixlänge")]
        public int PrefixLength { get; set; }

        [Category("Gerät"), DisplayName("Gateway-IPv4")]
        public string GatewayIpv4 { get; set; }

        [Category("Gerät"), DisplayName("DNS-Server (Komma)")]
        public string DnsServers { get; set; }

        [Category("Gerät"), DisplayName("Zeitzone")]
        public string TimeZone { get; set; }

        [Category("Netzwerk"), DisplayName("Orchestrierungsserver-IP (feste IPv4)")]
        [Description("LAN-/VLAN-IP des zentralen KI- und Orchestrierungsservers. Nur diese Quell-IP darf auf den Edge-Control-Port zugreifen.")]
        public string ServerIpv4 { get; set; }

        [Category("Netzwerk"), DisplayName("Erlaubte HTTP-Trigger-Clients (SPS-IP, Komma)")]
        [Description("Optionale feste IP-Adressen der SPSen, die das Edge-Gerät per HTTP triggern dürfen. Die Orchestrierungsserver-IP nur eintragen, wenn auch der Server triggern darf.")]
        public string PlantAllowedIpv4s { get; set; }

        [Category("Netzwerk"), DisplayName("Edge-Control-Port (Server -> Edge)")]
        public int ControlPort { get; set; }

        [Category("Netzwerk"), DisplayName("HTTP-Trigger-Port am Edge (SPS -> Edge)")]
        [Description("Eingehender HTTP-Port am Edge-Gerät für SPS-Trigger, zum Beispiel Siemens LHTTP. Standard: TCP 9444.")]
        public int PlantApiPort { get; set; }

        [Category("Netzwerk"), DisplayName("Edge-RTSP-Port (Relay -> Edge)")]
        public int RtspPort { get; set; }

        [Category("SPS-Ausgabe"), DisplayName("SPS-Schreiben aus Pipeline erlauben")]
        [ReadOnly(true)]
        [Description("Globales Sicherheits-Gate. Erst zusammen mit einem aktivierten Siemens-SPS-Schreibknoten und deaktiviertem Dry-Run darf die Edge-Runtime auf SPS-IP und DB-/Byte-/Bit-Adresse schreiben.")]
        public bool AllowPlcWrites { get; set; }

        [Category("Runtime"), DisplayName("State Root")]
        public string StateRoot { get; set; }

        [Category("Runtime"), DisplayName("Artefakt-Quota (Bytes)")]
        public long ArtifactQuotaBytes { get; set; }

        [Category("Runtime"), DisplayName("Outbox-Quota (Bytes)")]
        public long OutboxQuotaBytes { get; set; }

        [Category("Runtime"), DisplayName("Mindestfreispeicher (Bytes)")]
        public long MinimumDiskFreeBytes { get; set; }

        [Category("Runtime"), DisplayName("Engine Builder")]
        public string EngineBuilderMode { get; set; }

        [Category("Runtime"), DisplayName("trtexec-Pfad (Altmodus)")]
        public string TrtExecPath { get; set; }

        [Category("Kameras"), DisplayName("Freigegebene Adapter (Komma)")]
        public string CameraAdapters { get; set; }

        [Category("Provisionierung"), DisplayName("NVMe-Gerät (Jetson)")]
        public string NvmeDevice { get; set; }

        [Category("Provisionierung"), DisplayName("Windows Netzwerkadapter")]
        public string WindowsInterfaceAlias { get; set; }

        [Category("Signatur"), DisplayName("Signatur verpflichtend")]
        public bool RequireSignature { get; set; }

        [Category("Signatur"), DisplayName("Privater RSA-Schlüssel")]
        public string SigningPrivateKey { get; set; }

        [Category("Signatur"), DisplayName("Öffentlicher RSA-Schlüssel")]
        public string SigningPublicKey { get; set; }

        [Category("Signatur"), DisplayName("OpenSSL")]
        public string OpenSslExecutable { get; set; }

        public BundleOptions()
        {
            TargetPlatform = TargetPlatform.JetsonOrinNano;
            ReleaseVersion = "1.0.0"; SourceDateEpoch = 1704067200;
            OutputDirectory = @"dist\edge"; WheelhouseDirectory = @"dist\wheelhouse\jetson_orin_nano";
            PackageIndexUrl = "https://pypi.org/simple"; AdditionalWheelDirectory = "";
            BuildInternalWheels = false; PythonExecutable = "python"; RuntimeDistributions = "edge-agent";
            PythonRuntimeArchive = ""; WinSwExecutable = "";
            WindowsInstallerExecutable = @"dist\edge-bundle-builder\YBB-Edge-Windows-Installer.exe";
            DeviceHostname = "ybb-edge-01"; DeviceIpv4 = "192.168.50.21";
            PrefixLength = 24; GatewayIpv4 = "192.168.50.1"; DnsServers = "192.168.50.1";
            TimeZone = "Europe/Vienna"; ServerIpv4 = "192.168.50.10";
            PlantAllowedIpv4s = ""; ControlPort = 9443; PlantApiPort = 9444; RtspPort = 8554;
            AllowPlcWrites = false;
            StateRoot = "/var/lib/ybb-edge"; ArtifactQuotaBytes = 34359738368;
            OutboxQuotaBytes = 67108864; MinimumDiskFreeBytes = 2147483648;
            EngineBuilderMode = "trtexec"; TrtExecPath = "trtexec";
            CameraAdapters = "rtsp,usb_opencv,gstreamer,aravis_gige,basler_pylon,flir_spinnaker";
            NvmeDevice = "/dev/nvme0n1p1"; WindowsInterfaceAlias = "Ethernet";
            RequireSignature = false; SigningPrivateKey = ""; SigningPublicKey = ""; OpenSslExecutable = "openssl";
        }

        public Dictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object>
            {
                { "target_platform", TargetPlatform == TargetPlatform.JetsonOrinNano ? "jetson_orin_nano" : TargetPlatform == TargetPlatform.JetsonAgxOrin ? "jetson_agx_orin" : "windows_11" },
                { "release_version", ReleaseVersion }, { "source_date_epoch", SourceDateEpoch },
                { "output_directory", OutputDirectory }, { "wheelhouse_directory", WheelhouseDirectory },
                { "package_index_url", PackageIndexUrl }, { "additional_wheel_directory", AdditionalWheelDirectory },
                { "build_internal_wheels", BuildInternalWheels }, { "python_executable", PythonExecutable },
                { "runtime_distributions", RuntimeDistributions },
                { "python_runtime_archive", PythonRuntimeArchive }, { "winsw_executable", WinSwExecutable },
                { "windows_installer_executable", WindowsInstallerExecutable },
                { "device_hostname", DeviceHostname }, { "device_ipv4", DeviceIpv4 },
                { "prefix_length", PrefixLength }, { "gateway_ipv4", GatewayIpv4 },
                { "dns_servers", DnsServers }, { "timezone", TimeZone }, { "server_ipv4", ServerIpv4 },
                { "plant_allowed_ipv4s", PlantAllowedIpv4s }, { "control_port", ControlPort },
                { "plant_api_port", PlantApiPort }, { "rtsp_port", RtspPort },
                { "allow_plc_writes", AllowPlcWrites }, { "state_root", StateRoot },
                { "artifact_quota_bytes", ArtifactQuotaBytes }, { "outbox_quota_bytes", OutboxQuotaBytes },
                { "minimum_disk_free_bytes", MinimumDiskFreeBytes }, { "engine_builder_mode", EngineBuilderMode },
                { "trtexec_path", TrtExecPath }, { "camera_adapters", CameraAdapters },
                { "nvme_device", NvmeDevice }, { "windows_interface_alias", WindowsInterfaceAlias },
                { "require_signature", RequireSignature }, { "signing_private_key", SigningPrivateKey },
                { "signing_public_key", SigningPublicKey }, { "openssl_executable", OpenSslExecutable }
            };
        }
    }

    internal sealed class RuntimeUpdateDialog : Form
    {
        private readonly TextBox source = new TextBox();
        private readonly TextBox release = new TextBox();
        private readonly TextBox output = new TextBox();
        private readonly TextBox signingKey = new TextBox();
        private readonly TextBox signingPassphrase = new TextBox { UseSystemPasswordChar = true };
        public string BundlePath { get { return Path.GetFullPath(source.Text.Trim()); } }
        public string ReleaseId { get { return release.Text.Trim(); } }
        public string OutputPath { get { return Path.GetFullPath(output.Text.Trim()); } }
        public string SigningKeyPath { get { return Path.GetFullPath(signingKey.Text.Trim()); } }
        public string TakeSigningPassphrase()
        {
            var value = signingPassphrase.Text;
            signingPassphrase.Clear();
            return value;
        }

        public RuntimeUpdateDialog(string outputDirectory, string defaultSigningKey, string previousBundle)
        {
            Text = "Runtime-Update erstellen";
            Width = 900; Height = 465; MinimumSize = new Size(760, 465);
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = false; MaximizeBox = false;
            release.Text = "edge-runtime-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
            output.Text = Path.Combine(outputDirectory, release.Text + ".zip");
            signingKey.Text = defaultSigningKey;
            source.Text = previousBundle;
            Font = new Font("Segoe UI", 10);

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), ColumnCount = 3, RowCount = 7 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
            var description = new Label {
                AutoSize = true, Dock = DockStyle.Fill,
                Text = "Wählen Sie ein fertiges Edge-Build als Archiv oder entpackten Ordner. Die Zielplattform wird aus diesem Build übernommen.\n" +
                    "Die aktuelle Runtime aus dem Repository wird neu gepackt und signiert. Dafür kann Internet benötigt werden.\n" +
                    "Das Update wird anschließend in der zentralen Edge-Orchestrierung importiert und ausgerollt."
            };
            layout.Controls.Add(description, 0, 0); layout.SetColumnSpan(description, 3);
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 100));
            AddField(layout, 1, "Vorhandenes Build", source);
            AddField(layout, 2, "Neue Release-ID", release);
            AddField(layout, 3, "Update-ZIP", output);
            AddField(layout, 4, "Privater RSA-Schlüssel", signingKey);
            AddField(layout, 5, "Schlüssel-Passphrase", signingPassphrase);
            layout.Controls.Add(new Label { Text = "Leer bei unverschlüsseltem Schlüssel. Wird nicht gespeichert.", AutoSize = true, Dock = DockStyle.Fill }, 2, 5);
            var sourceButtons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            sourceButtons.Controls.Add(Button("Archiv …", delegate {
                using (var dialog = new OpenFileDialog { Title = "Fertiges Edge-Build auswählen", Filter = "Edge-Bundles (*.tar.gz;*.tgz;*.zip)|*.tar.gz;*.tgz;*.zip" })
                    if (dialog.ShowDialog(this) == DialogResult.OK) source.Text = dialog.FileName;
            }));
            sourceButtons.Controls.Add(Button("Ordner …", delegate {
                using (var dialog = new FolderBrowserDialog { Description = "Entpacktes Build mit platform-profile.json und wheelhouse auswählen", ShowNewFolderButton = false })
                    if (dialog.ShowDialog(this) == DialogResult.OK) source.Text = dialog.SelectedPath;
            }));
            layout.Controls.Add(sourceButtons, 2, 1);
            layout.Controls.Add(Button("Speichern unter …", delegate {
                using (var dialog = new SaveFileDialog { Filter = "Runtime-Update (*.zip)|*.zip", FileName = output.Text, DefaultExt = "zip", OverwritePrompt = false })
                    if (dialog.ShowDialog(this) == DialogResult.OK) output.Text = dialog.FileName;
            }), 2, 3);
            layout.Controls.Add(Button("Schlüssel wählen …", delegate {
                using (var dialog = new OpenFileDialog { Filter = "RSA-Schlüssel (*.pem;*.key)|*.pem;*.key|Alle Dateien (*.*)|*.*" })
                    if (dialog.ShowDialog(this) == DialogResult.OK) signingKey.Text = dialog.FileName;
            }), 2, 4);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.RightToLeft };
            var cancel = Button("Abbrechen", delegate { DialogResult = DialogResult.Cancel; });
            var create = Button("Runtime-Update erstellen", ValidateAndAccept);
            actions.Controls.Add(cancel); actions.Controls.Add(create);
            layout.Controls.Add(actions, 0, 6); layout.SetColumnSpan(actions, 3);
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            Controls.Add(layout); AcceptButton = create; CancelButton = cancel;
        }

        private static Button Button(string text, EventHandler handler)
        {
            var button = new Button { Text = text, AutoSize = true };
            button.Click += handler; return button;
        }

        private static void AddField(TableLayoutPanel layout, int row, string label, TextBox field)
        {
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 44));
            layout.Controls.Add(new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left }, 0, row);
            field.Dock = DockStyle.Fill; field.Margin = new Padding(3, 10, 3, 3);
            layout.Controls.Add(field, 1, row);
        }

        private void ValidateAndAccept(object sender, EventArgs e)
        {
            try
            {
                if (String.IsNullOrWhiteSpace(source.Text) || (!File.Exists(BundlePath) && !Directory.Exists(BundlePath)))
                    throw new InvalidDataException("Bitte ein vorhandenes Build auswählen.");
                if (Directory.Exists(BundlePath) && (!File.Exists(Path.Combine(BundlePath, "platform-profile.json")) || !Directory.Exists(Path.Combine(BundlePath, "wheelhouse"))))
                    throw new InvalidDataException("Der Build-Ordner muss platform-profile.json und wheelhouse enthalten.");
                if (String.IsNullOrWhiteSpace(ReleaseId)) throw new InvalidDataException("Bitte eine neue Release-ID angeben.");
                if (String.IsNullOrWhiteSpace(output.Text) || !OutputPath.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Bitte eine ZIP-Ausgabedatei angeben.");
                if (String.IsNullOrWhiteSpace(signingKey.Text) || !File.Exists(SigningKeyPath))
                    throw new InvalidDataException("Für Runtime-Updates ist ein vorhandener privater RSA-Schlüssel erforderlich.");
                if (signingPassphrase.Text.IndexOfAny(new[] { '\r', '\n', '\0' }) >= 0)
                    throw new InvalidDataException("Die Schlüssel-Passphrase darf keine Zeilenumbrüche enthalten.");
                if (String.Equals(OutputPath, BundlePath, StringComparison.OrdinalIgnoreCase) ||
                    String.Equals(OutputPath, SigningKeyPath, StringComparison.OrdinalIgnoreCase) ||
                    (Directory.Exists(BundlePath) && OutputPath.StartsWith(BundlePath.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidDataException("Die Ausgabedatei muss außerhalb des ausgewählten Builds liegen und darf den Schlüssel nicht ersetzen.");
                if (File.Exists(OutputPath)) throw new InvalidDataException("Die Ausgabedatei existiert bereits. Bitte für das neue Release einen neuen Dateinamen wählen.");
                DialogResult = DialogResult.OK;
            }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }
    }

    internal sealed class ReleaseKeyDialog : Form
    {
        private readonly CheckBox create = new CheckBox { Text = "Neues Schlüsselpaar erzeugen", AutoSize = true };
        private readonly TextBox privateKey = new TextBox();
        private readonly TextBox publicKey = new TextBox();
        private readonly TextBox passphrase = new TextBox { UseSystemPasswordChar = true };
        private readonly TextBox confirmation = new TextBox { UseSystemPasswordChar = true };
        public bool Generate { get { return create.Checked; } }
        public string PrivatePath { get { return Path.GetFullPath(privateKey.Text.Trim()); } }
        public string PublicPath { get { return Path.GetFullPath(publicKey.Text.Trim()); } }
        public string TakePassphrase() { var value = passphrase.Text; passphrase.Clear(); confirmation.Clear(); return value; }

        public ReleaseKeyDialog(string existingKey)
        {
            Text = "RSA-Schlüssel verwalten"; Width = 880; Height = 440;
            MinimumSize = new Size(760, 440); StartPosition = FormStartPosition.CenterParent;
            Font = new Font("Segoe UI", 10); privateKey.Text = existingKey;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), ColumnCount = 3 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 175));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 130));
            var info = new Label { AutoSize = true, Dock = DockStyle.Fill, Text =
                "Bestehender Jetson: den bisherigen privaten Release-Schlüssel auswählen.\n" +
                "Nur für eine neue Vertrauenskette ein neues Paar erzeugen. Neue private Schlüssel werden verschlüsselt.\n" +
                "Auf Zentrale und Edge gehört ausschließlich der öffentliche Schlüssel. Fingerabdrücke vergleichen." };
            layout.Controls.Add(info, 0, 0); layout.SetColumnSpan(info, 3);
            layout.Controls.Add(create, 0, 1); layout.SetColumnSpan(create, 3);
            var fields = new[] { privateKey, publicKey, passphrase, confirmation };
            var labels = new[] { "Privater Schlüssel", "Öffentlicher Export", "Passphrase", "Passphrase wiederholen" };
            for (var i = 0; i < fields.Length; i++)
            {
                layout.Controls.Add(new Label { Text = labels[i], AutoSize = true, Anchor = AnchorStyles.Left }, 0, i + 2);
                fields[i].Dock = DockStyle.Fill; fields[i].Margin = new Padding(3, 12, 3, 8);
                layout.Controls.Add(fields[i], 1, i + 2);
            }
            confirmation.Enabled = false;
            create.CheckedChanged += delegate { confirmation.Enabled = create.Checked; };
            var browsePrivate = new Button { Text = "Auswählen …", AutoSize = true };
            browsePrivate.Click += delegate {
                using (FileDialog dialog = create.Checked ? (FileDialog)new SaveFileDialog() : new OpenFileDialog())
                {
                    dialog.Filter = "RSA-Schlüssel (*.pem;*.key)|*.pem;*.key";
                    if (dialog.ShowDialog(this) == DialogResult.OK) privateKey.Text = dialog.FileName;
                }
            };
            var browsePublic = new Button { Text = "Speichern …", AutoSize = true };
            browsePublic.Click += delegate {
                using (var dialog = new SaveFileDialog { Filter = "Öffentlicher Schlüssel (*.pem)|*.pem", FileName = "release-signing-public.pem" })
                    if (dialog.ShowDialog(this) == DialogResult.OK) publicKey.Text = dialog.FileName;
            };
            layout.Controls.Add(browsePrivate, 2, 2); layout.Controls.Add(browsePublic, 2, 3);
            var accept = new Button { Text = "Prüfen und übernehmen", AutoSize = true };
            accept.Click += delegate {
                try {
                    if (String.IsNullOrWhiteSpace(privateKey.Text) || String.IsNullOrWhiteSpace(publicKey.Text))
                        throw new InvalidDataException("Bitte beide Dateipfade auswählen.");
                    if (PrivatePath == PublicPath || File.Exists(PublicPath) || (Generate && File.Exists(PrivatePath)))
                        throw new InvalidDataException("Bestehende Schlüssel werden nicht überschrieben. Einen neuen Exportnamen wählen.");
                    if (!Generate && !File.Exists(PrivatePath)) throw new FileNotFoundException("Privater Schlüssel fehlt.");
                    if (passphrase.Text.IndexOfAny(new[] { '\r', '\n', '\0' }) >= 0)
                        throw new InvalidDataException("Passphrase darf keinen Zeilenumbruch enthalten.");
                    if (Generate && (passphrase.Text.Length < 12 || passphrase.Text != confirmation.Text))
                        throw new InvalidDataException("Mindestens 12 Zeichen verwenden und identisch wiederholen.");
                    DialogResult = DialogResult.OK;
                } catch (Exception ex) { MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); }
            };
            var cancel = new Button { Text = "Abbrechen", DialogResult = DialogResult.Cancel, AutoSize = true };
            var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill };
            actions.Controls.Add(accept); actions.Controls.Add(cancel);
            layout.Controls.Add(actions, 0, 6); layout.SetColumnSpan(actions, 3);
            Controls.Add(layout); AcceptButton = accept; CancelButton = cancel;
        }
    }

    public sealed class MainForm : Form
    {
        private readonly PropertyGrid propertyGrid = new PropertyGrid();
        private readonly RichTextBox log = new RichTextBox();
        private readonly Button buildButton = new Button();
        private readonly Button prepareButton = new Button();
        private readonly Button runtimeUpdateButton = new Button();
        private readonly FlowLayoutPanel toolbar = new FlowLayoutPanel();
        private BundleOptions options = new BundleOptions();
        private string repositoryRoot;
        private string previousBundle = "";
        private readonly TabControl tasks = new TabControl();
        private readonly Label profileSummary = new Label();
        private Process activeProcess;
        public bool IsBusy { get { return activeProcess != null; } }
        private string ProfilePath { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "YBB", "Deployment", "edge-builder.json"); } }

        public MainForm() : this(null) { }
        public MainForm(string root)
        {
            Text = "YBB · Edge-Updates und Installation";
            Width = 1050; Height = 780; MinimumSize = new Size(820, 600);
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10); BackColor = Color.FromArgb(244, 247, 250);
            repositoryRoot = root ?? FindRepositoryRoot(AppDomain.CurrentDomain.BaseDirectory) ?? Environment.CurrentDirectory;
            try {
                if (File.Exists(ProfilePath)) {
                    var saved = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(ProfilePath)) as Dictionary<string, object>;
                    if (saved != null) { Apply(saved); if (saved.ContainsKey("previous_bundle")) previousBundle = Convert.ToString(saved["previous_bundle"]); }
                }
            } catch (Exception) { Append("Gespeichertes Profil konnte nicht gelesen werden. Standardwerte werden verwendet."); }

            toolbar.Dock = DockStyle.Top; toolbar.AutoSize = true; toolbar.Padding = new Padding(6);
            toolbar.Controls.Add(MakeButton("Profil zurücksetzen", delegate { options = new BundleOptions(); previousBundle = ""; propertyGrid.SelectedObject = options; RefreshProfile(); }));
            toolbar.Controls.Add(MakeButton("Konfiguration laden", LoadConfiguration));
            toolbar.Controls.Add(MakeButton("Konfiguration speichern", SaveConfiguration));
            toolbar.Controls.Add(MakeButton("RSA-Schlüssel verwalten …", ManageKeys));
            prepareButton.Text = "Online-Inhalte vorbereiten (Internet)"; prepareButton.AutoSize = true; prepareButton.Click += PrepareWheelhouse;
            buildButton.Text = "Bundle bauen"; buildButton.AutoSize = true; buildButton.Click += BuildBundle;
            runtimeUpdateButton.Text = "Runtime-Update erstellen …"; runtimeUpdateButton.AutoSize = true;
            runtimeUpdateButton.Click += BuildRuntimeUpdate;
            toolbar.Controls.Add(MakeButton("Ausgabe öffnen", OpenOutput));

            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal, Size = new Size(980, 650), SplitterDistance = 465 };
            propertyGrid.Dock = DockStyle.Fill; propertyGrid.SelectedObject = options; propertyGrid.HelpVisible = true;
            propertyGrid.PropertyValueChanged += PlatformChanged;
            log.Dock = DockStyle.Fill; log.ReadOnly = true; log.BackColor = Color.FromArgb(25, 25, 25); log.ForeColor = Color.Gainsboro;
            tasks.Dock = DockStyle.Fill;
            var update = new TabPage("Runtime aktualisieren") { Padding = new Padding(20), AutoScroll = true };
            var install = new TabPage("Erstinstallation") { Padding = new Padding(20), AutoScroll = true };
            var advanced = new TabPage("Expertenoptionen") { Padding = new Padding(10) };
            var updateLayout = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoScroll = true };
            updateLayout.Controls.Add(Heading("Bestehendes Edge-Gerät aktualisieren"));
            updateLayout.Controls.Add(Description("1   Einmalig den bisherigen RSA-Schlüssel übernehmen oder ein Schlüsselpaar erzeugen.\n" +
                "2   Aus einem vorhandenen Edge-Build ein signiertes Runtime-Update erstellen.\n" +
                "3   ZIP in der Admin-Web-App unter Edge-Orchestrierung importieren und ausrollen.\n\n" +
                "Gerätekonfiguration, Daten und Kalibrierungen bleiben erhalten. Die Kanäle pausieren beim Runtime-Wechsel."));
            profileSummary.AutoSize = true; profileSummary.Margin = new Padding(0, 16, 0, 16);
            updateLayout.Controls.Add(profileSummary);
            runtimeUpdateButton.BackColor = Color.FromArgb(24, 103, 111); runtimeUpdateButton.ForeColor = Color.White;
            runtimeUpdateButton.FlatStyle = FlatStyle.Flat; runtimeUpdateButton.Padding = new Padding(12, 8, 12, 8);
            updateLayout.Controls.Add(runtimeUpdateButton); update.Controls.Add(updateLayout);
            var installLayout = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoScroll = true };
            installLayout.Controls.Add(Heading("Neues Gerät vorbereiten"));
            installLayout.Controls.Add(Description("Zielplattform und Netzwerk festlegen, Inhalte vorbereiten und Offline-Installer bauen.\n" +
                "Für einen bereits eingerichteten Jetson genügt normalerweise ein Runtime-Update."));
            var platform = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 450 };
            platform.Items.AddRange(new object[] { "Jetson Orin Nano", "Jetson AGX Orin", "Windows 11 / NVIDIA" });
            platform.SelectedIndex = (int)options.TargetPlatform;
            var refreshingPlatform = false;
            platform.SelectedIndexChanged += delegate { if (!refreshingPlatform) { options.TargetPlatform = (TargetPlatform)platform.SelectedIndex; ApplyPlatformDefaults(); RefreshProfile(); } };
            tasks.SelectedIndexChanged += delegate { refreshingPlatform = true; platform.SelectedIndex = (int)options.TargetPlatform; refreshingPlatform = false; };
            installLayout.Controls.Add(platform);
            AddBasicField(installLayout, "Hostname", "DeviceHostname");
            AddBasicField(installLayout, "Edge-Geräte-IP", "DeviceIpv4");
            AddBasicField(installLayout, "Orchestrierungsserver-IP", "ServerIpv4");
            AddBasicField(installLayout, "Ausgabeverzeichnis", "OutputDirectory");
            var buildActions = new FlowLayoutPanel { AutoSize = true };
            buildActions.Controls.Add(prepareButton); buildActions.Controls.Add(buildButton);
            installLayout.Controls.Add(buildActions); install.Controls.Add(installLayout);
            advanced.Controls.Add(propertyGrid);
            tasks.TabPages.Add(update); tasks.TabPages.Add(install); tasks.TabPages.Add(advanced);
            split.Panel1.Controls.Add(tasks); split.Panel2.Controls.Add(log);
            Controls.Add(split); Controls.Add(toolbar);
            RefreshProfile();
            FormClosing += delegate(object sender, FormClosingEventArgs e) {
                if (IsBusy) { e.Cancel = true; MessageBox.Show(this, "Bitte den laufenden Vorgang zuerst abschließen lassen."); }
                else SaveProfile();
            };
            Append("Repository: " + repositoryRoot);
        }

        private static Label Heading(string text) { return new Label { Text = text, AutoSize = true, Font = new Font("Segoe UI", 16, FontStyle.Bold), Margin = new Padding(0, 0, 0, 14) }; }
        private static Label Description(string text) { return new Label { Text = text, AutoSize = true, MaximumSize = new Size(800, 0), Margin = new Padding(0, 0, 0, 16) }; }
        private void AddBasicField(FlowLayoutPanel parent, string label, string property)
        {
            var row = new FlowLayoutPanel { AutoSize = true };
            row.Controls.Add(new Label { Text = label, Width = 210 });
            var field = new TextBox { Width = 420 };
            field.Text = Convert.ToString(typeof(BundleOptions).GetProperty(property).GetValue(options, null));
            field.TextChanged += delegate { typeof(BundleOptions).GetProperty(property).SetValue(options, field.Text, null); propertyGrid.Refresh(); };
            tasks.SelectedIndexChanged += delegate { field.Text = Convert.ToString(typeof(BundleOptions).GetProperty(property).GetValue(options, null)); };
            row.Controls.Add(field); parent.Controls.Add(row);
        }
        private void RefreshProfile() { profileSummary.Text = "Signaturschlüssel: " + (String.IsNullOrWhiteSpace(options.SigningPrivateKey) ? "Noch nicht eingerichtet · RSA-Schlüssel verwalten öffnen" : options.SigningPrivateKey) + "\nBasis-Build: " + (String.IsNullOrWhiteSpace(previousBundle) ? "Wird beim ersten Update ausgewählt" : previousBundle); }
        private void SaveProfile()
        {
            try {
                Directory.CreateDirectory(Path.GetDirectoryName(ProfilePath));
                var map = options.ToDictionary(); map["previous_bundle"] = previousBundle;
                File.WriteAllText(ProfilePath, new JavaScriptSerializer().Serialize(map), new UTF8Encoding(false));
            } catch (Exception ex) { Append("Profil konnte nicht gespeichert werden: " + ex.Message); }
        }
        private void ManageKeys(object sender, EventArgs e)
        {
            try {
                using (var dialog = new ReleaseKeyDialog(options.SigningPrivateKey)) {
                    if (dialog.ShowDialog(this) != DialogResult.OK) return;
                    var privatePath = dialog.PrivatePath; var publicPath = dialog.PublicPath;
                    var script = Path.Combine(repositoryRoot, "scripts", "release", "build_edge_runtime_update.py");
                    RunProcess(Quote(script) + " --key-action " + (dialog.Generate ? "create" : "export") +
                        " --signing-key " + Quote(privatePath) + " --output " + Quote(publicPath) +
                        " --openssl " + Quote(options.OpenSslExecutable) + " --signing-passphrase-stdin",
                        "RSA-Schlüssel", "RSA-Schlüssel wird geprüft …", "Schlüssel geprüft. Öffentlichen Export auf Zentrale und Edge verwenden; SHA-256-Fingerabdrücke vergleichen.",
                        null, dialog.TakePassphrase(), delegate {
                            options.SigningPrivateKey = privatePath; options.SigningPublicKey = publicPath;
                            options.RequireSignature = true; propertyGrid.Refresh(); RefreshProfile(); SaveProfile();
                        });
                }
            } catch (Exception ex) { SetActionsEnabled(true); ShowError(ex); }
        }

        private Button MakeButton(string text, EventHandler handler)
        {
            var button = new Button { Text = text, AutoSize = true };
            button.Click += handler; return button;
        }

        private void LoadConfiguration(object sender, EventArgs e)
        {
            using (var dialog = new OpenFileDialog { Filter = "JSON-Konfiguration (*.json)|*.json" })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                try
                {
                    var map = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(dialog.FileName)) as Dictionary<string, object>;
                    if (map == null) throw new InvalidDataException("JSON-Objekt erwartet.");
                    Apply(map); propertyGrid.Refresh(); RefreshProfile(); SaveProfile(); Append("Geladen: " + dialog.FileName);
                }
                catch (Exception ex) { ShowError(ex); }
            }
        }

        private void SaveConfiguration(object sender, EventArgs e)
        {
            using (var dialog = new SaveFileDialog { Filter = "JSON-Konfiguration (*.json)|*.json", FileName = "edge-bundle.json" })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                File.WriteAllText(dialog.FileName, new JavaScriptSerializer().Serialize(options.ToDictionary()), new UTF8Encoding(false));
                Append("Gespeichert: " + dialog.FileName);
            }
        }

        private void ValidateConfiguration(object sender, EventArgs e)
        {
            try { ValidateOptions(); Append("Konfiguration und lokale Eingabepfade sind plausibel."); }
            catch (Exception ex) { ShowError(ex); }
        }

        private void ValidateOptions()
        {
            var wheelhouse = Absolute(options.WheelhouseDirectory);
            if (!Directory.Exists(wheelhouse))
                throw new DirectoryNotFoundException("Wheelhouse fehlt: " + wheelhouse + Environment.NewLine + "Klicken Sie zuerst auf 'Online-Inhalte vorbereiten (Internet)' oder wählen Sie ein vorhandenes Plattform-Wheelhouse.");
            if (!Directory.GetFiles(wheelhouse, "*.whl", SearchOption.TopDirectoryOnly).Any())
                throw new InvalidDataException("Das Wheelhouse enthält keine Wheels: " + wheelhouse + Environment.NewLine + "Bereiten Sie ein neues Wheelhouse vor oder wählen Sie ein vollständiges Wheelhouse.");
            if (options.TargetPlatform == TargetPlatform.Windows11)
            {
                EnsureWindowsPayloadDefaults();
                if (!File.Exists(Absolute(options.PythonRuntimeArchive))) throw new FileNotFoundException("Python-Runtime ZIP fehlt. Klicken Sie zuerst auf 'Online-Inhalte vorbereiten (Internet)'.");
                if (!File.Exists(Absolute(options.WinSwExecutable))) throw new FileNotFoundException("WinSW fehlt. Klicken Sie zuerst auf 'Online-Inhalte vorbereiten (Internet)'.");
                if (!File.Exists(Absolute(options.WindowsInstallerExecutable))) throw new FileNotFoundException("Native Windows-Installer-EXE fehlt. Führen Sie Build-NativeBundleBuilder.ps1 aus.");
                if (!File.Exists(Absolute(@"dist\edge-inputs\windows_11\cuda_13.2.1_windows.exe"))) throw new FileNotFoundException("CUDA-13.2-Offline-Installer fehlt. Klicken Sie zuerst auf 'Online-Inhalte vorbereiten (Internet)'.");
                var wheelNames = Directory.GetFiles(wheelhouse, "*.whl", SearchOption.TopDirectoryOnly).Select(Path.GetFileName).ToArray();
                if (!wheelNames.Any(name => name.StartsWith("tensorrt_cu13-", StringComparison.OrdinalIgnoreCase)) ||
                    !wheelNames.Any(name => name.StartsWith("tensorrt_cu13_bindings-", StringComparison.OrdinalIgnoreCase)) ||
                    !wheelNames.Any(name => name.StartsWith("tensorrt_cu13_libs-", StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidDataException("Das Windows-Wheelhouse enthält nicht alle TensorRT-Komponenten. Klicken Sie zuerst auf 'Online-Inhalte vorbereiten (Internet)'.");
            }
            if (options.RequireSignature && (!File.Exists(Absolute(options.SigningPrivateKey)) || !File.Exists(Absolute(options.SigningPublicKey))))
                throw new FileNotFoundException("Für die verpflichtende Signatur fehlen Schlüsseldateien.");
        }

        private void PlatformChanged(object sender, PropertyValueChangedEventArgs e)
        {
            if (e.ChangedItem == null || e.ChangedItem.PropertyDescriptor == null || e.ChangedItem.PropertyDescriptor.Name != "TargetPlatform") return;
            ApplyPlatformDefaults();
        }

        private void ApplyPlatformDefaults()
        {
            if (options.TargetPlatform == TargetPlatform.Windows11)
            {
                options.WheelhouseDirectory = @"dist\wheelhouse\windows_11";
                EnsureWindowsPayloadDefaults();
                options.StateRoot = @"C:\ProgramData\YBB\Edge";
                options.EngineBuilderMode = "tensorrt_python";
                options.TrtExecPath = "trtexec";
                options.CameraAdapters = "rtsp,usb_opencv,gstreamer,basler_pylon,flir_spinnaker";
                options.RuntimeDistributions = "edge-agent,opencv-python-headless";
            }
            else
            {
                options.WheelhouseDirectory = options.TargetPlatform == TargetPlatform.JetsonAgxOrin
                    ? @"dist\wheelhouse\jetson_agx_orin"
                    : @"dist\wheelhouse\jetson_orin_nano";
                options.StateRoot = "/var/lib/ybb-edge";
                options.EngineBuilderMode = "trtexec";
                options.TrtExecPath = "/usr/src/tensorrt/bin/trtexec";
                options.CameraAdapters = "rtsp,usb_opencv,gstreamer,aravis_gige,basler_pylon,flir_spinnaker";
                options.RuntimeDistributions = "edge-agent";
            }
            propertyGrid.Refresh();
        }

        private void PrepareWheelhouse(object sender, EventArgs e)
        {
            try
            {
                if (options.TargetPlatform == TargetPlatform.Windows11) EnsureWindowsPayloadDefaults();
                var script = BackendScript();
                var wheelhouse = Absolute(options.WheelhouseDirectory);
                Uri packageIndex;
                if (!Uri.TryCreate(options.PackageIndexUrl, UriKind.Absolute, out packageIndex) ||
                    packageIndex.Scheme != Uri.UriSchemeHttps || !String.IsNullOrEmpty(packageIndex.UserInfo))
                    throw new InvalidDataException("Die Paketquelle muss eine HTTPS-URL ohne eingebettete Zugangsdaten sein.");
                if (!String.IsNullOrWhiteSpace(options.AdditionalWheelDirectory) && !Directory.Exists(Absolute(options.AdditionalWheelDirectory)))
                    throw new DirectoryNotFoundException("Das zusätzliche Hersteller-Wheel-Verzeichnis fehlt: " + Absolute(options.AdditionalWheelDirectory));
                if (Directory.Exists(wheelhouse) && Directory.EnumerateFileSystemEntries(wheelhouse).Any() &&
                    !File.Exists(Path.Combine(wheelhouse, "wheelhouse-source.lock.json")))
                    throw new InvalidOperationException("Das Ziel-Wheelhouse ist nicht leer und besitzt keinen gültigen Source-Lock: " + wheelhouse + Environment.NewLine + "Wählen Sie für eine neue Freigabe ein leeres Verzeichnis. Bestehende Wheels werden nicht vermischt.");
                var answer = MessageBox.Show(
                    this,
                    "Jetzt werden die Online-Inhalte für " + PlatformDisplayName() + " vorbereitet." + Environment.NewLine +
                    (options.TargetPlatform == TargetPlatform.Windows11
                        ? "Neben Python-Runtime und WinSW werden CUDA 13.2 sowie TensorRT 10.16 vollständig geladen und per SHA-256 geprüft (mehr als 4 GB)." + Environment.NewLine
                        : "Die Python-Pakete werden aus der konfigurierten HTTPS-Paketquelle geladen." + Environment.NewLine) + Environment.NewLine +
                    "Der anschließende Bundle-Build verwendet nur die vorbereiteten lokalen Inhalte und benötigt kein Internet. Fortfahren?",
                    "Online-Inhalte vorbereiten",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Information);
                if (answer != DialogResult.Yes) return;
                RunBackend(script, "prepare-wheelhouse", "Online-Vorbereitung gestartet …", "Online-Inhalte erfolgreich vorbereitet.");
            }
            catch (Exception ex) { SetActionsEnabled(true); ShowError(ex); }
        }

        private void BuildBundle(object sender, EventArgs e)
        {
            try
            {
                ValidateOptions();
                string passphrase = "";
                if (!String.IsNullOrWhiteSpace(options.SigningPrivateKey)) {
                    using (var dialog = new Form { Text = "Bundle signieren", Width = 600, Height = 210, StartPosition = FormStartPosition.CenterParent, Font = Font }) {
                        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), FlowDirection = FlowDirection.TopDown };
                        layout.Controls.Add(new Label { Text = "Passphrase des privaten RSA-Schlüssels (leer bei unverschlüsseltem Schlüssel)", AutoSize = true });
                        var secret = new TextBox { UseSystemPasswordChar = true, Width = 530 };
                        layout.Controls.Add(secret);
                        var accept = new Button { Text = "Signiert bauen", AutoSize = true, DialogResult = DialogResult.OK };
                        layout.Controls.Add(accept); dialog.Controls.Add(layout); dialog.AcceptButton = accept;
                        if (dialog.ShowDialog(this) != DialogResult.OK) return;
                        passphrase = secret.Text; secret.Clear();
                    }
                }
                RunBackend(BackendScript(), "build", "Build gestartet …", "Bundle erfolgreich erstellt.", passphrase);
            }
            catch (Exception ex) { SetActionsEnabled(true); ShowError(ex); }
        }

        private string BackendScript()
        {
            var script = Path.Combine(repositoryRoot, "scripts", "release", "build_edge_offline_bundle.py");
            if (!File.Exists(script)) throw new FileNotFoundException("Bundle-Builder-Backend fehlt.", script);
            return script;
        }

        private void BuildRuntimeUpdate(object sender, EventArgs e)
        {
            try
            {
                var script = Path.Combine(repositoryRoot, "scripts", "release", "build_edge_runtime_update.py");
                if (!File.Exists(script)) throw new FileNotFoundException("Runtime-Update-Exporter fehlt. Bitte das Repository aktualisieren.", script);
                using (var dialog = new RuntimeUpdateDialog(Absolute(options.OutputDirectory),
                    String.IsNullOrWhiteSpace(options.SigningPrivateKey) ? "" : Absolute(options.SigningPrivateKey), previousBundle))
                {
                    if (dialog.ShowDialog(this) != DialogResult.OK) return;
                    previousBundle = dialog.BundlePath; options.SigningPrivateKey = dialog.SigningKeyPath;
                    RefreshProfile(); SaveProfile();
                    var arguments = Quote(script) + " --bundle " + Quote(dialog.BundlePath) +
                        " --output " + Quote(dialog.OutputPath) + " --release-id " + Quote(dialog.ReleaseId) +
                        " --signing-key " + Quote(dialog.SigningKeyPath) + " --refresh-runtime --signing-passphrase-stdin" +
                        " --openssl " + Quote(options.OpenSslExecutable) + " --package-index-url " + Quote(options.PackageIndexUrl);
                    if (!String.IsNullOrWhiteSpace(options.AdditionalWheelDirectory))
                        arguments += " --additional-wheel-directory " + Quote(Absolute(options.AdditionalWheelDirectory));
                    Append("Basis-Build: " + dialog.BundlePath);
                    RunProcess(arguments, "Runtime-Update", "Runtime-Update wird erstellt …",
                        "Runtime-Update erstellt: " + dialog.OutputPath + Environment.NewLine +
                        "In Edge-Orchestrierung über 'Runtime-Paket importieren' hochladen und 'Runtime aktualisieren' ausführen.", null, dialog.TakeSigningPassphrase());
                }
            }
            catch (Exception ex) { SetActionsEnabled(true); ShowError(ex); }
        }

        private void RunBackend(string script, string command, string startedMessage, string successMessage, string passphrase = null)
        {
            var temporary = Path.Combine(Path.GetTempPath(), "ybb-edge-bundle-" + Guid.NewGuid().ToString("N") + ".json");
            File.WriteAllText(temporary, new JavaScriptSerializer().Serialize(options.ToDictionary()), new UTF8Encoding(false));
            RunProcess(Quote(script) + " " + command + " --config " + Quote(temporary) +
                (command == "build" ? " --signing-passphrase-stdin" : ""), command, startedMessage, successMessage, temporary, passphrase);
        }

        private void RunProcess(string arguments, string command, string startedMessage, string successMessage, string temporary, string standardInput = null, Action onSuccess = null)
        {
            var process = new Process();
            process.StartInfo = new ProcessStartInfo
            {
                FileName = options.PythonExecutable,
                Arguments = arguments,
                WorkingDirectory = repositoryRoot,
                UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
                RedirectStandardInput = standardInput != null,
                CreateNoWindow = true
            };
            process.EnableRaisingEvents = true;
            process.StartInfo.EnvironmentVariables["PIP_DISABLE_PIP_VERSION_CHECK"] = "1";
            process.StartInfo.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            process.StartInfo.StandardOutputEncoding = Encoding.UTF8;
            process.StartInfo.StandardErrorEncoding = Encoding.UTF8;
            process.OutputDataReceived += delegate(object o, DataReceivedEventArgs a) { if (a.Data != null) BeginInvoke((Action)(() => Append(a.Data))); };
            process.ErrorDataReceived += delegate(object o, DataReceivedEventArgs a) { if (!String.IsNullOrWhiteSpace(a.Data)) BeginInvoke((Action)(() => Append(a.Data))); };
            process.Exited += delegate
            {
                process.WaitForExit(); // Drain asynchronous output before disposing the process.
                BeginInvoke((Action)(() =>
                {
                    var succeeded = process.ExitCode == 0;
                    activeProcess = null;
                    SetActionsEnabled(true);
                    if (succeeded && onSuccess != null) onSuccess();
                    Append(succeeded ? successMessage : command + " fehlgeschlagen (Exit " + process.ExitCode + ").");
                    process.Dispose();
                    try { if (temporary != null) File.Delete(temporary); } catch { }
                }));
            };
            try
            {
                SaveProfile(); activeProcess = process; SetActionsEnabled(false); Append(startedMessage);
                process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
                if (standardInput != null)
                {
                    // Pass the secret only through a private pipe, never argv, env or a file.
                    var inputBytes = Encoding.UTF8.GetBytes(standardInput + "\n");
                    standardInput = null;
                    try
                    {
                        process.StandardInput.BaseStream.Write(inputBytes, 0, inputBytes.Length);
                        process.StandardInput.Close();
                    }
                    finally { Array.Clear(inputBytes, 0, inputBytes.Length); }
                }
            }
            catch
            {
                activeProcess = null;
                process.Dispose();
                try { if (temporary != null) File.Delete(temporary); } catch { }
                throw;
            }
        }

        private void SetActionsEnabled(bool enabled)
        {
            buildButton.Enabled = enabled;
            prepareButton.Enabled = enabled;
            runtimeUpdateButton.Enabled = enabled;
            toolbar.Enabled = enabled;
            propertyGrid.Enabled = enabled;
            tasks.Enabled = enabled;
        }

        private string PlatformDisplayName()
        {
            if (options.TargetPlatform == TargetPlatform.JetsonAgxOrin) return "NVIDIA Jetson AGX Orin (aarch64 / Python 3.12)";
            if (options.TargetPlatform == TargetPlatform.Windows11) return "Windows 11 / NVIDIA (x86-64 / Python 3.12)";
            return "NVIDIA Jetson Orin Nano (aarch64 / Python 3.12)";
        }

        private void EnsureWindowsPayloadDefaults()
        {
            var runtimeDistributions = options.RuntimeDistributions
                .Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(value => value.Trim())
                .Where(value => value.Length > 0)
                .ToList();
            if (!runtimeDistributions.Any(value => value.Equals("opencv-python-headless", StringComparison.OrdinalIgnoreCase)))
            {
                runtimeDistributions.Add("opencv-python-headless");
                options.RuntimeDistributions = String.Join(",", runtimeDistributions);
            }
            if (String.IsNullOrWhiteSpace(options.PythonRuntimeArchive))
                options.PythonRuntimeArchive = @"dist\edge-inputs\windows_11\python-runtime.zip";
            if (String.IsNullOrWhiteSpace(options.WinSwExecutable))
                options.WinSwExecutable = @"dist\edge-inputs\windows_11\winsw.exe";
            if (String.IsNullOrWhiteSpace(options.WindowsInstallerExecutable))
                options.WindowsInstallerExecutable = @"dist\edge-bundle-builder\YBB-Edge-Windows-Installer.exe";
            propertyGrid.Refresh();
        }

        private void OpenOutput(object sender, EventArgs e)
        {
            var path = Absolute(options.OutputDirectory);
            Directory.CreateDirectory(path); Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }

        private string Absolute(string path) { return Path.IsPathRooted(path) ? path : Path.Combine(repositoryRoot, path); }
        private static string Quote(string value)
        {
            // Windows command-line quoting, including quotes and trailing backslashes.
            var quoted = new StringBuilder("\"");
            var backslashes = 0;
            foreach (var character in value)
            {
                if (character == '\\') { backslashes++; continue; }
                quoted.Append('\\', character == '"' ? backslashes * 2 + 1 : backslashes);
                quoted.Append(character); backslashes = 0;
            }
            quoted.Append('\\', backslashes * 2); quoted.Append('"');
            return quoted.ToString();
        }
        private void Append(string value) { log.AppendText(DateTime.Now.ToString("HH:mm:ss") + "  " + value + Environment.NewLine); }
        private void ShowError(Exception ex) { Append("FEHLER: " + ex.Message); MessageBox.Show(this, ex.Message, "YBB Edge Bundle Builder", MessageBoxButtons.OK, MessageBoxIcon.Error); }

        private static string FindRepositoryRoot(string start)
        {
            var directory = new DirectoryInfo(start);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "scripts", "release", "build_edge_offline_bundle.py"))) return directory.FullName;
                directory = directory.Parent;
            }
            return null;
        }

        private void Apply(Dictionary<string, object> map)
        {
            Func<string, string, string> str = (key, fallback) => map.ContainsKey(key) ? Convert.ToString(map[key]) : fallback;
            Func<string, long, long> lng = (key, fallback) => map.ContainsKey(key) ? Convert.ToInt64(map[key]) : fallback;
            Func<string, bool, bool> boolean = (key, fallback) => map.ContainsKey(key) ? Convert.ToBoolean(map[key]) : fallback;
            var target = str("target_platform", "jetson_orin_nano");
            options.TargetPlatform = target == "windows_11" ? TargetPlatform.Windows11 : target == "jetson_agx_orin" ? TargetPlatform.JetsonAgxOrin : TargetPlatform.JetsonOrinNano;
            options.ReleaseVersion = str("release_version", options.ReleaseVersion); options.SourceDateEpoch = lng("source_date_epoch", options.SourceDateEpoch);
            options.OutputDirectory = str("output_directory", options.OutputDirectory); options.WheelhouseDirectory = str("wheelhouse_directory", options.WheelhouseDirectory);
            options.PackageIndexUrl = str("package_index_url", options.PackageIndexUrl); options.AdditionalWheelDirectory = str("additional_wheel_directory", options.AdditionalWheelDirectory);
            options.BuildInternalWheels = boolean("build_internal_wheels", options.BuildInternalWheels); options.PythonExecutable = str("python_executable", options.PythonExecutable);
            options.RuntimeDistributions = str("runtime_distributions", options.RuntimeDistributions);
            options.PythonRuntimeArchive = str("python_runtime_archive", options.PythonRuntimeArchive); options.WinSwExecutable = str("winsw_executable", options.WinSwExecutable);
            options.WindowsInstallerExecutable = str("windows_installer_executable", options.WindowsInstallerExecutable);
            options.DeviceHostname = str("device_hostname", options.DeviceHostname); options.DeviceIpv4 = str("device_ipv4", options.DeviceIpv4);
            options.ServerIpv4 = str("server_ipv4", options.ServerIpv4); options.PlantAllowedIpv4s = str("plant_allowed_ipv4s", options.PlantAllowedIpv4s);
            options.AllowPlcWrites = false;
            options.PrefixLength = (int)lng("prefix_length", options.PrefixLength);
            options.GatewayIpv4 = str("gateway_ipv4", options.GatewayIpv4); options.DnsServers = str("dns_servers", options.DnsServers);
            options.TimeZone = str("timezone", options.TimeZone);
            options.ControlPort = (int)lng("control_port", options.ControlPort); options.PlantApiPort = (int)lng("plant_api_port", options.PlantApiPort); options.RtspPort = (int)lng("rtsp_port", options.RtspPort);
            options.ArtifactQuotaBytes = lng("artifact_quota_bytes", options.ArtifactQuotaBytes); options.OutboxQuotaBytes = lng("outbox_quota_bytes", options.OutboxQuotaBytes);
            options.MinimumDiskFreeBytes = lng("minimum_disk_free_bytes", options.MinimumDiskFreeBytes);
            options.EngineBuilderMode = str("engine_builder_mode", options.EngineBuilderMode); options.TrtExecPath = str("trtexec_path", options.TrtExecPath);
            options.NvmeDevice = str("nvme_device", options.NvmeDevice); options.WindowsInterfaceAlias = str("windows_interface_alias", options.WindowsInterfaceAlias);
            options.StateRoot = str("state_root", options.StateRoot); options.CameraAdapters = str("camera_adapters", options.CameraAdapters);
            options.RequireSignature = boolean("require_signature", options.RequireSignature); options.SigningPrivateKey = str("signing_private_key", options.SigningPrivateKey);
            options.SigningPublicKey = str("signing_public_key", options.SigningPublicKey); options.OpenSslExecutable = str("openssl_executable", options.OpenSslExecutable);
            if (options.TargetPlatform == TargetPlatform.Windows11) EnsureWindowsPayloadDefaults();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }
}
