# Optionale Konfiguration fuer den Docker-freien Windows/WSL2 Offline-Installer.
# Der normale Build braucht keine Aenderung an dieser Datei:
#   .\deployment\build-windows-installer.ps1

# Eindeutiger Name der Anwendung. Wird fuer Bundle-, Installations- und Log-Verzeichnisse genutzt.
$AppName = "ybb-inferenzserver"

# Version des Release-Artefakts. "auto" erzeugt pro Export eine Timestamp/Git-Version.
$AppVersion = "auto"

# Optionale Edge-Runtime-Pakete im zentralen Update (Entwicklungsrechner).
# Je installiertem Zielprofil ein vorhandenes Basisbundle; keine Neuinstallation des Edge.
# Die UI speichert diese Auswahl. Die Passphrase wird ausschliesslich ueber stdin uebergeben.
$EdgeRuntimeBundles = @()
$EdgeSigningKeyPath = ""
$EdgeOpenSslExecutable = "openssl"
$EdgePackageIndexUrl = "https://pypi.org/simple"
$EdgeAdditionalWheelDirectory = ""

# Name der WSL2-Distribution auf dem Zielgeraet.
$DistroName = "YBB-Inferenzserver"

# Windows-Basisverzeichnis auf dem Zielgeraet. Darunter entstehen incoming, deployed, failed, logs usw.
$ServerBaseDir = "C:\YBB_Inferenzserver"

# Oeffentliche Adresse fuer Weboberflaeche, CORS und Label Studio, ohne Schema.
# Standard ist lokaler Zugriff per Windows-Portproxy. Fuer LAN-Zugriff kann hier
# die Ziel-PC-IP gesetzt werden, z.B. "192.168.1.50".
$AppHostName = "127.0.0.1"

# Ziel-WSL-Netzwerkmodus. "mirrored" ist fuer Basler/GigE-Vision-Kameras der
# performanteste Pfad, weil WSL die Windows-Netzwerkinterfaces direkt sieht und
# Windows-Dienste wie MediaMTX aus WSL per 127.0.0.1 erreichbar sind.
# "unchanged" laesst die globale .wslconfig auf dem Zielrechner unveraendert.
$WslNetworkingMode = "mirrored"
$WslDnsTunneling = $true
$WslFirewall = $true
$WslAutoProxy = $false
$WslHostAddressLoopback = $true
# Fuer GigE-Vision-Discovery/UDP-Stream-Verkehr im Mirrored Mode wird auf dem
# Ein-Distro-Zielrechner die Hyper-V-Firewall fuer WSL eingehend geoeffnet.
# Zulaessig: "Allow", "Block", "Unchanged".
$WslHyperVFirewallDefaultInboundAction = "Allow"
$WslRestartAllowed = $true

# Interne Builder-Distribution auf dem Entwicklungsrechner.
# Wird vom One-Command-Build automatisch angelegt, wenn sie fehlt.
$ManagedRuntimeDistroName = "YBB-Inferenzserver-Builder"
$RuntimeDistroInstallRoot = ".runtime\wsl\YBB-Inferenzserver-Builder"
$AutoResetRuntimeDistro = $true

# Quelle der vorbereiteten WSL2-Runtime fuer direkte Export-Aufrufe.
# Der normale build-windows-installer.ps1 setzt RuntimeDistroName automatisch aus ManagedRuntimeDistroName.
$RuntimeDistroName = ""

# Variante B: vorhandenes rootfs.tar verwenden. Wenn gesetzt, hat RuntimeTarPath Vorrang vor RuntimeDistroName.
$RuntimeTarPath = ""

# Runtime-Vorbereitung: Systempakete/Python-Umgebungen in der internen Builder-WSL aktualisieren.
$PrepareRuntimeInstallAptPackages = $true
$PrepareRuntimeInstallTrainingRuntime = $true

# Optional: Triton Server Archiv, das nach /opt/tritonserver entpackt wird.
# Wenn leer und PrepareRuntimeSkipTritonCheck=false ist, erzeugt der Export das
# Archiv automatisch aus TritonServerImage und cached es unter TritonServerArchiveCacheDir.
# Der Installer backt es in die vorbereitete WSL-Runtime ein; ein Update-Bundle
# liefert es als runtime/tritonserver.tar.gz mit und ruestet bestehende Zielgeraete nach.
$TritonServerArchivePath = ""
$PrepareRuntimeSkipTritonCheck = $true
$TritonServerImage = "nvcr.io/nvidia/tritonserver:24.08-py3"
$TritonServerArchiveCacheDir = ".runtime\triton"

# MediaMTX ist der offene RTSP/WebRTC-Fanout-Server fuer Livestreams.
# Wenn leer und PrepareRuntimeSkipMediaMtxCheck=false ist, laedt der Export das
# Windows-amd64 ZIP-Archiv von GitHub Releases und cached es unter MediaMtxArchiveCacheDir.
$MediaMtxArchivePath = ""
$PrepareRuntimeSkipMediaMtxCheck = $false
$MediaMtxVersion = "1.19.0"
$MediaMtxArchiveCacheDir = ".runtime\mediamtx"
$MediaMtxDownloadUrl = ""
$StreamingRtspPort = 8554
$StreamingWebRtcPort = 8889
$StreamingWebRtcIceTcpPort = 8189
$StreamingTurnTcpPort = 3478
$StreamAuthEnabled = $false

# Industrial PLC writes are blocked by the runtime environment unless this is
# explicitly enabled. Keep false for dry-run/validation-only deployments.
$PlcWritesAllowed = $false

# GStreamer ist fuer H.264-Stream-Publishing in MediaMTX erforderlich.
# Wenn leer und PrepareRuntimeSkipGStreamerCheck=false ist, erzeugt der Export
# aus der Builder-WSL ein Debian-Paketbundle und cached es unter GStreamerDebBundleCacheDir.
$GStreamerDebBundlePath = ""
$PrepareRuntimeSkipGStreamerCheck = $false
$GStreamerDebBundleCacheDir = ".runtime\apt\gstreamer"
$GStreamerDebBundleDistroName = ""
$GStreamerDebPackages = @(
    "coturn",
    "ffmpeg",
    "python3-gi",
    "python3-gst-1.0",
    "gir1.2-gstreamer-1.0",
    "gir1.2-gst-rtsp-server-1.0",
    "gstreamer1.0-rtsp",
    "gstreamer1.0-tools",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav"
)

# Groesse fuer geteilte grosse Dateien im Bundle. 1GB ist USB-/PowerShell-freundlich.
$BundlePartSizeBytes = 1GB

# Frontend vor dem Export bauen.
$BuildFrontend = $true

# Build-Time URLs fuer das Admin-Frontend.
# Relative Pfade funktionieren mit lokalem Portproxy und PC-IP:PORT.
$FrontendAdminApiUrl = ""
$FrontendRuntimeAdminApiUrl = "/runtime-api"
$FrontendResultServiceUrl = "/runtime-results"
$FrontendTriggerExecutorUrl = "/runtime-trigger"
$FrontendInferenceGatewayRtUrl = "/inference-rt"
$FrontendInferenceGatewayBatchUrl = "/inference-batch"
$FrontendAdminWsUrl = ""
$FrontendBasePath = "/frontend/"
