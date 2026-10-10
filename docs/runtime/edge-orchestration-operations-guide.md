# Edge-Orchestrierung – Installations-, Bedienungs- und Betriebshandbuch

Erweiterung vom 6. September 2026: [Gemeinsame Runtime, zentrale Softwareupdates
und die neue Geräteübersicht](./edge-runtime-software-updates.md). Dort sind der
einmalige Launcher-Bootstrap, Runtime-Paketexport und Software-Rollback beschrieben.

Stand: 22. Juli 2026  
Gültigkeit: Softwarestand M0 bis M5 plus E1 Productive Edge Execution und E2 Provisioning

## 1. Zweck und Freigabestatus

Dieses Handbuch beschreibt die Inbetriebnahme und Bedienung der zentralen
YBB-Plattform sowie zweier beispielhafter Edge-Geräte:

- zentraler Orchestrierungsserver auf Windows 11 mit der vorbereiteten
  WSL2-Runtime;
- Windows-11-Edge-PC mit der IP `192.168.50.31`;
- NVIDIA Jetson Orin Nano mit NVMe und der IP `192.168.50.21`.

Die Beispiele verwenden ein isoliertes Maschinen-VLAN `192.168.50.0/24` und
die feste Serveradresse `192.168.50.10`.

> **Wichtiger Freigabehinweis:** M0 bis M5 stellen den Control-Plane-Pfad,
> Geräteverwaltung, wiederaufnehmbare Artefaktübertragung, lokale A/B-Slots,
> Ergebnis-Replay, bedarfsgesteuerte JPEG-Vorschau, Diagnose und manuellen
> Rollback bereit. E1 ergänzt Graphausführung, lokale TensorRT-Inferenz,
> Task-Pre-/Postprocessing, Golden-Output-Vergleich, reale Result-Payloads,
> optionale Overlays und die begrenzte SPS-/Trigger-API auf Port `9444`.
> Der Softwarepfad ist implementiert, jedoch noch nicht auf realer Zielhardware
> abgenommen. E2 ergänzt native Offline-Bundle-GUI, Pflichtintegrität,
> Plattformprovisionierung, reale Inventarisierung und Kameraadapter. Port `8554`, physische
> Kamera-/Treiberfreigaben und die Hardwareabnahme des implementierten SPS-Schreibknotens bleiben offen. Eine produktive CV- oder SPS-Freigabe darf daher erst nach
> erfolgreicher Hardware-in-the-Loop-Abnahme erfolgen. Maßgeblich ist
> [SPEC_GAPS_Edge_Orchestration.md](./SPEC_GAPS_Edge_Orchestration.md).

Was mit dem heutigen Stand vollständig geprüft werden kann:

- feste IP registrieren und ausschließlich innerhalb des erlaubten CIDR
  ansprechen;
- Identity, Health, Capabilities und Runtime-Zustand abfragen;
- unveränderliche Releases und Artefakte übertragen und per SHA-256 prüfen;
- Deployment vorbereiten, aktivieren, stoppen und auf den vorherigen Slot
  zurückrollen;
- lokale Quellaufnahme und Frame-Sampling für die implementierten Quellen
  testen;
- freigegebene Graph-Nodes mit lokalem TensorRT ausführen und echte
  Result-Envelopes erzeugen;
- idempotente Trigger auf 9444 mit Body-, Rate-, Parallelitäts- und
  Timeoutgrenzen ausführen;
- Ergebnisse bei Netzunterbrechung puffern, erneut abholen und deduplizieren;
- Live-JPEGs nur bei aktivem zentralem Betrachter übertragen;
- Fleet-, Geräte-, Speicher- und Kanaldiagnosen bedienen.

## 2. Architektur und Vertrauensmodell

Der Browser spricht ausschließlich mit dem zentralen Server. Der Server startet
alle Verwaltungsverbindungen zum Edge-Gerät über HTTP. Das Edge-Gerät benötigt
für den Betrieb weder Internet noch DNS noch eine ausgehende
Managementverbindung.

```text
Browser/Operator
       |
       | HTTPS 443
       v
Windows-11-Orchestrierungsserver / WSL2-Runtime
       |
       | HTTP 9443, ausschließlich serverinitiiert
       +------------------------+
       |                        |
       v                        v
Jetson Orin Nano           Windows-11-Edge
192.168.50.21              192.168.50.31
       |                        |
       +---- Kameras/SPS im Maschinen-VLAN
             SPS -> Edge TCP 9444, nur explizite Allowlist
```

Die feste IPv4-Adresse ist die Geräteidentität. Es gibt keine Edge-Zertifikate,
API-Schlüssel oder Enrollment-Tokens. Daraus folgen verbindlich:

1. Das Maschinen-VLAN ist technisch isoliert.
2. Switch-ACLs erlauben nur die ausdrücklich benötigten Kommunikationspaare.
3. Port `9443/TCP` ist am Edge ausschließlich von `192.168.50.10` erreichbar.
4. Port `9444/TCP` ist ausschließlich von explizit freigegebenen
   SPS-/Trigger-IP-Adressen erreichbar. Der Server erhält nur dann Zugriff, wenn seine IP ebenfalls
   in dieser Liste steht.
5. Unbenutzte Ports bleiben geschlossen.
6. Secrets, Manifeste und Bilder sind zwischen Server und Edge nicht
   transportverschlüsselt. Das VLAN ist Teil der Sicherheitsgrenze.
7. Die Uhren von Server und Edge müssen über eine erlaubte lokale Zeitquelle
   synchronisiert werden.

Ein Gerätetausch unter derselben IP bleibt dasselbe logische Gerät. Nach dem
Tausch sind Identity, Capability-Snapshot und alle Deployments erneut zu
prüfen. Eine IP-Änderung ist dagegen keine normale Bearbeitung: Kanäle vom alten
Datensatz lösen, Gerät archivieren und die neue IP als neues Gerät registrieren.

## 3. Referenznetz und Firewallregeln

### 3.1 Adressplan

| Rolle | Beispieladresse | Anmerkung |
|---|---:|---|
| Orchestrierungsserver, Maschinen-NIC | `192.168.50.10/24` | feste Adresse, kein Standardgateway auf dieser NIC |
| Jetson Orin Nano | `192.168.50.21/24` | feste Edge-Geräte-IP |
| Windows-11-Edge | `192.168.50.31/24` | feste Edge-Geräte-IP |
| SPS | `192.168.50.40/24` | HTTP-Trigger und optionales S7-Schreibziel, nach HIL-Freigabe |
| Kamera | `192.168.50.50/24` | Beispiel für RTSP/GigE |
| erlaubtes Edge-CIDR | `192.168.50.0/24` | wird bei der Registrierung gespeichert |

Die Administrationsseite des Servers darf über eine zweite, getrennte NIC
erreichbar sein. Auf der Maschinen-NIC sollte kein Default-Gateway und kein
öffentlicher DNS-Server konfiguriert werden.

### 3.2 ACL-Matrix

| Quelle | Ziel | Port | Status/Zweck |
|---|---|---:|---|
| Server `192.168.50.10` | jedes registrierte Edge | TCP `9443` | Edge-Control, Artefakte, Resultate, Vorschau |
| Wartungsstation | Edge | TCP `22` bzw. RDP | nur zeitlich begrenzt zur Inbetriebnahme |
| Edge | lokale Kamera | gerätespezifisch | z. B. RTSP oder GigE Vision |
| Edge | lokale NTP-Quelle | UDP `123` | Zeitsynchronisation, falls vorhanden |
| SPS `192.168.50.40` | Edge | TCP `9444` | E1-Trigger und Last-Result; erst nach HIL freigeben |
| Edge | SPS `192.168.50.40` | TCP `102` | nur für einen freigegebenen Siemens-SPS-Schreibknoten |
| zentraler Media-Relay | Edge | TCP `8554` | reserviert, heute geschlossen lassen |
| Edge | Internet | alle | sperren |
| Edge | anderes Edge | alle | sperren |

ICMP kann für die Inbetriebnahme zwischen Server und Edge erlaubt und später
nach Anlagenrichtlinie eingeschränkt werden. Entscheidend ist der erfolgreiche
TCP-Test auf `9443`.

## 4. Vorbereitung des zentralen Windows-11-Servers

### 4.1 Voraussetzungen

- Windows 11 Pro/Enterprise, aktueller freigegebener Patchstand;
- im UEFI aktivierte Hardwarevirtualisierung;
- WSL2 auf dem Zielrechner;
- genügend NVMe-Speicher für Daten, Modelle, PostgreSQL, Label Studio und
  MLflow;
- bei lokalem Training/Serving ein zum vorbereiteten Bundle passender
  NVIDIA-Treiber;
- lokale Administratorrechte für Erstinstallation, Portweiterleitung,
  Firewall und Autostart;
- ein separater Entwicklungs-/Buildrechner mit Internetzugang zum Erstellen
  des Offline-Installer-ZIPs.

WSL2 kann in einer administrativen PowerShell vorbereitet werden:

```powershell
wsl --install
wsl --status
```

Danach Windows neu starten. Auf einem vollständig offline aufgesetzten
Zielrechner müssen WSL-Komponenten und Kernel vor der Netzisolation durch die
IT bereitgestellt werden; der YBB-Installer lädt sie nicht nach.

### 4.2 Feste Maschinen-IP setzen

Diese Befehle an der lokalen Konsole als Administrator ausführen. Den echten
Adapteralias zuerst prüfen; eine falsche Änderung kann die Remoteverbindung
unterbrechen.

```powershell
Get-NetAdapter
Get-NetIPConfiguration -InterfaceAlias "Ethernet Maschine"
New-NetIPAddress -InterfaceAlias "Ethernet Maschine" `
  -IPAddress 192.168.50.10 -PrefixLength 24 -AddressFamily IPv4
```

Auf der isolierten Maschinen-NIC werden weder Gateway noch DNS gesetzt. Eine
zweite Administrations-NIC behält ihre eigene Konfiguration. Danach prüfen:

```powershell
Get-NetIPAddress -InterfaceAlias "Ethernet Maschine" -AddressFamily IPv4
Test-NetConnection 192.168.50.21 -Port 9443
```

Der zweite Test ist erst nach Installation des Jetson-Agenten erfolgreich.

### 4.3 Offline-Serverbundle bauen

Auf dem Entwicklungsrechner im Repository:

```powershell
cd C:\KI\YBB_Inferenzserver
Set-ExecutionPolicy -Scope Process Bypass
.\deployment\build-windows-ui-exe.ps1
.\deployment\YBB-Deployment-UI.exe
```

Im Reiter `Export` folgende Richtwerte verwenden:

```text
Repo-Pfad:          C:\KI\YBB_Inferenzserver
App-Name:           ybb-inferenzserver
Ziel-Basisordner:   C:\YBB_Inferenzserver
Distro:             YBB-Inferenzserver
IP/Host:            spätere Administrations-IP des Servers
Port:               443
Frontend bauen:     aktiviert
```

Triton, MediaMTX, GStreamer und Python-Wheel-Bundles dürfen nur übersprungen
werden, wenn sie auf dem Ziel bereits in kompatibler Version vorhanden sind.
Für eine vollständige Erstinstallation werden sie mitgebündelt. Der Export
erzeugt unter `dist` ein großes ZIP:

```text
dist\ybb-inferenzserver-<version>-windows-installer.zip
```

Die detaillierten Exportoptionen und GPU-/Kameraabhängigkeiten sind in
[deployment/README.md](../../deployment/README.md) beschrieben.

### 4.4 Offline-Server installieren

Installer-ZIP per freigegebener Netzwerkablage oder Datenträger auf den Server
kopieren und in einer administrativen PowerShell ausführen:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
New-Item -ItemType Directory -Path C:\YBB_Installer -Force
Expand-Archive `
  "D:\incoming\ybb-inferenzserver-<version>-windows-installer.zip" `
  -DestinationPath C:\YBB_Installer -Force
cd C:\YBB_Installer
.\install-windows-offline.ps1 -PublicAddress "<Administrations-IP-des-Servers>"
```

Der Installer importiert die vorbereitete WSL2-Runtime, legt Secrets und
Datenbanken an, führt Migrationen aus, installiert die Dienste und richtet den
HTTPS-Zugriff ein. Die Maschinen-IP `192.168.50.10` muss nicht die
Browseradresse sein.

Status und initiales Passwort prüfen:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\status-ybb-inferenzserver.cmd
wsl --list --verbose
wsl -d YBB-Inferenzserver -u root -- `
  cat /srv/cv-platform/secrets/bootstrap_admin_initial_password.txt
curl.exe -k -I https://127.0.0.1/
```

Anmeldung:

```text
URL:      https://<Administrations-IP-des-Servers>/
Benutzer: admin
Passwort: Inhalt der bootstrap_admin_initial_password.txt
```

Das initiale Passwort nach der ersten Anmeldung unter `Access & Security`
ändern und die Klartextdatei gemäß Betriebsrichtlinie sichern oder entfernen.

### 4.5 Erreichbarkeit aus der WSL-Runtime prüfen

Nicht nur Windows, sondern der in WSL laufende `runtime-admin-api` muss jedes
Edge erreichen. Nach Installation eines Edge-Geräts prüfen:

```powershell
wsl -d YBB-Inferenzserver -u root -- `
  curl -fsS http://192.168.50.21:9443/edge/v1/identity
wsl -d YBB-Inferenzserver -u root -- `
  curl -fsS http://192.168.50.31:9443/edge/v1/identity
```

Schlägt nur der WSL-Test fehl, WSL-Netzwerkmodus, Windows-Firewall, Routing und
VLAN-Zuordnung prüfen. Ein Browser-Test vom Operator-PC ersetzt diesen Test
nicht.

## 5. Gemeinsames Edge-Offline-Bundle

### 5.1 Verzeichnisaufbau

Beide Edge-Installer erwarten ein bereits gebautes, zur Zielarchitektur
passendes Bundle:

```text
edge-bundle/
  deployment/edge/
    Install-WindowsOffline.ps1
    install-jetson-offline.sh
    configure-ufw.sh
    ybb-edge-agent.service
    ybb-edge-watchdog.service
    ybb-edge-watchdog-recover.service
    ybb-edge-watchdog.timer
    edge-watchdog.py
  wheelhouse/
    edge_agent-*.whl
    cv_contracts-*.whl
    cv_status-*.whl
    ... alle transitiven Python-Wheels
  device.env
  platform-profile.json
  provisioning.json
  release-manifest.json
  runtime-requirements.txt
  wheelhouse.lock.json
  SIGNATURE_POLICY
  SHA256SUMS
  python-runtime.zip        # nur Windows
  winsw.exe                 # nur Windows
```

Das Wheelhouse wird auf einer vertrauenswürdigen Buildmaschine für exakt eine
Zielarchitektur und Python-ABI vorbereitet. Der E2-Builder selbst arbeitet
danach ohne Netz und erzeugt aus denselben Inputs byteidentische Archive.
Windows-x86_64- und Jetson-aarch64-Wheelhouses dürfen nicht vermischt werden.

Die native Windows-Anwendung wird mit
`deployment\edge\bundle-builder\Build-NativeBundleBuilder.ps1` kompiliert und
als `YBB-Edge-Bundle-Builder.exe` gestartet. Zielplattform und sämtliche
Release-, Netzwerk-, Runtime-, Kamera-, Provisioning- und Signaturparameter
werden dort eingestellt. Details stehen im E2-Follow-up.

Für einen Entwicklungs-/Abnahmestand kann auf der jeweils kompatiblen
Staging-Maschine folgender Referenzablauf verwendet werden:

```text
1. Plattform und lokales Wheelhouse in der nativen GUI wählen.
2. FastAPI, Uvicorn, Pydantic und alle transitiven Abhängigkeiten in dasselbe
   wheelhouse laden.
3. Auf der Buildmaschine CPython 3.12 mit `pip` verwenden. Für Windows lädt der bestätigte
   Vorbereitungsschritt die fest versionierte `python-runtime.zip` und WinSW automatisch und
   prüft beide Quellen per SHA-256; freigegebene Hersteller-Kamera-Wheels bleiben optionale
   lokale Eingaben.
4. `Signatur verpflichtend` für Produktionsreleases aktivieren.
5. `Pfade prüfen` und `Bundle bauen` ausführen.
6. Archiv-SHA und Signatur in der Releasefreigabe dokumentieren.
7. Bundle auf einer sauberen Offline-VM beziehungsweise Testhardware installieren.
```

Der `edge-agent` deklariert OpenCV aktuell nicht als Paketabhängigkeit. Auf
Jetson wird üblicherweise das JetPack-Systempaket über
`--system-site-packages` genutzt. Unter Windows muss OpenCV nach der
Agentinstallation aus demselben Wheelhouse installiert werden, wenn eine
Kamera-/Dateiquelle getestet werden soll.

## 6. Beispiel A – Windows 11 als Edge-Gerät

### 6.1 Voraussetzungen

- Windows 11 x86_64, aktueller freigegebener Patchstand;
- feste Ethernetverbindung zum Maschinen-VLAN;
- die im E2-Bundle enthaltene Python-Runtime, passend zum Wheelhouse;
- `winsw.exe` im Bundle;
- für GPU-Builds ein freigegebener NVIDIA-Treiber sowie lokales TensorRT mit
  `trtexec.exe`;
- für Quellaufnahme ein kompatibles OpenCV im Agent-Venv;
- lokale Administratorrechte.

Mit `YBB_EDGE_CAPABILITY_INVENTORY=probe` meldet der Agent reale CUDA-/TensorRT-,
GPU-, Treiber-, RAM- und Kamera-Providerdaten. Nicht ermittelbare Werte werden
als Warnung statt als erfolgreich inventarisierter Default gemeldet.

### 6.2 Feste IP konfigurieren

An der lokalen Konsole eine administrative PowerShell öffnen:

```powershell
Get-NetAdapter
Get-NetIPConfiguration -InterfaceAlias "Ethernet Maschine"
New-NetIPAddress -InterfaceAlias "Ethernet Maschine" `
  -IPAddress 192.168.50.31 -PrefixLength 24 -AddressFamily IPv4
Get-NetIPAddress -InterfaceAlias "Ethernet Maschine" -AddressFamily IPv4
ping.exe 192.168.50.10
```

`New-NetIPAddress` deaktiviert DHCP auf dem betroffenen Interface. Auf dieser
isolierten NIC keinen Default-Gateway und keinen öffentlichen DNS-Server
eintragen. Vorher sicherstellen, dass `192.168.50.31` nicht bereits verwendet
wird.

### 6.3 Geräte-Konfiguration erstellen

`deployment\edge\device.windows.env.example` als `device.env` in das
Windows-Bundle kopieren und anpassen:

```dotenv
YBB_EDGE_MANAGEMENT_IP=192.168.50.31
YBB_EDGE_SERVER_IP=192.168.50.10
YBB_EDGE_CONTROL_PORT=9443
YBB_EDGE_PLANT_API_PORT=9444
YBB_EDGE_PLANT_ALLOWED_IPS=192.168.50.40
YBB_ALLOW_PLC_WRITES=false
YBB_EDGE_PLANT_MAX_REQUEST_BYTES=65536
YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS=4
YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS=2500
YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND=20
YBB_EDGE_RTSP_PORT=8554
YBB_EDGE_HOSTNAME=ybb-edge-win11-01
YBB_EDGE_PLATFORM=windows_11
YBB_EDGE_ARCHITECTURE=x86_64
YBB_EDGE_OS_VERSION=Windows 11
YBB_EDGE_JETPACK_VERSION=
YBB_EDGE_L4T_VERSION=
YBB_EDGE_CUDA_VERSION=<tatsächlich-installierte-Version>
YBB_EDGE_TENSORRT_VERSION=<tatsächlich-installierte-Version>
YBB_EDGE_DEEPSTREAM_VERSION=
YBB_EDGE_GPU_MODEL=<tatsächliches-GPU-Modell>
YBB_EDGE_DATA_ROOT=C:\ProgramData\YBB\Edge
YBB_EDGE_ARTIFACT_QUOTA_BYTES=34359738368
YBB_EDGE_OUTBOX_QUOTA_BYTES=67108864
YBB_EDGE_OUTBOX_MAX_ITEMS=10000
YBB_EDGE_MINIMUM_DISK_FREE_BYTES=2147483648
YBB_EDGE_ENGINE_BUILDER_MODE=trtexec
YBB_EDGE_TRTEXEC_PATH=C:\Program Files\NVIDIA Corporation\TensorRT\bin\trtexec.exe
YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS=900
YBB_EDGE_PREVIEW_JPEG_QUALITY=70
YBB_EDGE_PREVIEW_MAX_BYTES=1048576
```

Ist TensorRT für einen reinen Control-Plane-Test noch nicht installiert,
`YBB_EDGE_ENGINE_BUILDER_MODE=simulated` verwenden. Dieser Modus ist nicht für
eine Produktionsfreigabe zulässig.

### 6.4 Agent offline installieren

Bundle beispielsweise nach `C:\YBB\edge-bundle` kopieren. Dann als
Administrator:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\YBB\edge-bundle
.\deployment\edge\Install-WindowsOffline.ps1 `
  -BundleRoot C:\YBB\edge-bundle
```

Der Installer:

- prüft verpflichtend `SHA256SUMS` und die konfigurierte Signaturpolicy;
- entpackt die enthaltene Python-Runtime und installiert `runtime-requirements.txt` offline;
- installiert den Agent offline in `C:\Program Files\YBB Edge`;
- legt den Zustand unter `C:\ProgramData\YBB\Edge` ab;
- registriert `YBBEdgeAgent` als automatischen WinSW-Dienst;
- erlaubt eingehend TCP `9443` ausschließlich von `192.168.50.10`;
- erlaubt TCP `9444` ausschließlich von `YBB_EDGE_PLANT_ALLOWED_IPS`.

Die Regel setzt voraus, dass das aktive Windows-Firewallprofil eingehende
Verbindungen standardmäßig blockiert und keine breitere Bestandsregel denselben
Port erlaubt. Das ist zusätzlich von einem nicht erlaubten VLAN-Teilnehmer zu
testen.

Für OpenCV-Quelltests anschließend aus dem vorhandenen Wheelhouse installieren:

```powershell
& "C:\Program Files\YBB Edge\venv\Scripts\python.exe" -m pip install `
  --no-index --find-links C:\YBB\edge-bundle\wheelhouse `
  --disable-pip-version-check opencv-python-headless
```

### 6.5 Windows-Edge prüfen

```powershell
Get-Service YBBEdgeAgent
Get-NetFirewallRule -DisplayName "YBB Edge Control"
Invoke-RestMethod http://192.168.50.31:9443/edge/v1/identity
Invoke-RestMethod http://192.168.50.31:9443/edge/v1/health
Invoke-RestMethod http://192.168.50.31:9443/edge/v1/capabilities
Invoke-RestMethod http://192.168.50.31:9443/edge/v1/runtime
Invoke-RestMethod http://192.168.50.31:9444/edge-plant/v1/health
```

Vom Server aus:

```powershell
Test-NetConnection 192.168.50.31 -Port 9443
Test-NetConnection 192.168.50.31 -Port 9444
curl.exe -s http://192.168.50.31:9443/edge/v1/identity
```

Von einem nicht erlaubten Rechner müssen Port `9443` und `9444` blockiert sein.

## 7. Beispiel B – Jetson Orin Nano als Edge-Gerät

### 7.1 Hardware und Basisimage

Referenzgerät:

- Jetson Orin Nano Developer Kit;
- NVMe mit mindestens 64 GB, produktiv größer nach Artefakt-/Outboxbedarf;
- kabelgebundenes Ethernet;
- JetPack `7.2`, Jetson Linux/L4T `39.2`, CUDA `13.2`, TensorRT `10.16`
  und optional DeepStream `9.1` als Projekt-Zielprofil.

Die Versionskombination vor jeder Imagefreigabe gegen die offizielle NVIDIA-
Kompatibilitätsmatrix prüfen. Engines sind an Plattform, Architektur,
TensorRT-Version und GPU gebunden und werden nicht zwischen Windows und Jetson
geteilt.

### 7.2 JetPack auf NVMe installieren

Empfohlener Produktionsweg:

1. NVMe spannungsfrei in den Jetson einbauen.
2. Jetson per Recovery-USB mit einer von NVIDIA SDK Manager unterstützten
   Staging-Maschine verbinden.
3. Gerät in Force-Recovery versetzen.
4. Im SDK Manager `Jetson`, das erkannte Orin-Nano-Ziel und JetPack `7.2`
   wählen.
5. Als Zielmedium NVMe wählen und Jetson OS plus benötigte SDK-Komponenten
   installieren.
6. DeepStream `9.1` nur auswählen, wenn es für die geplante Pipeline gebraucht
   wird.
7. Nach dem Flash den Ubuntu-Ersteinrichtungsdialog lokal abschließen.
8. SDK-Manager-Logs und die verwendete Image-/Bundle-Version archivieren.

Der Flash-/Staging-Rechner benötigt in dieser Phase Internet. Das spätere
Anlagen-Edge bleibt offline. NVIDIA beschreibt alternativ ein vorbereitetes
Offline-SDK-Manager-Repository; auch dieses muss vorab auf einer verbundenen
Staging-Maschine erstellt werden.

### 7.3 Basisinstallation prüfen

Auf dem Jetson:

```bash
cat /etc/nv_tegra_release
uname -m
dpkg-query -W nvidia-jetpack nvidia-l4t-core tensorrt
/usr/src/tensorrt/bin/trtexec --version
gst-inspect-1.0 --version
python3 -c 'import cv2; print(cv2.__version__)'
lsblk
df -h /
```

Erwartet werden `aarch64`, der freigegebene L4T-/JetPack-Stand, ein
funktionsfähiges `trtexec` und ausreichend freier NVMe-Speicher. Versionswerte
nicht aus der Beispieldatei übernehmen, sondern aus diesen Prüfungen ableiten.

### 7.4 Hostname und feste IP konfigurieren

Zuerst den echten NetworkManager-Verbindungsnamen ermitteln. Die Änderung lokal
ausführen, weil die SSH-Verbindung abbricht:

```bash
sudo hostnamectl set-hostname ybb-edge-orin-01
nmcli connection show
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual \
  ipv4.addresses 192.168.50.21/24 \
  ipv4.gateway "" \
  ipv4.dns ""
sudo nmcli connection up "Wired connection 1"
ip -4 address show
ping -c 2 192.168.50.10
```

Falls eine lokale NTP-Quelle existiert, diese vor der Abnahme konfigurieren und
mit `timedatectl status` prüfen. Ohne verlässliche Zeitbasis sind Diagnose- und
Ergebniszeitstempel nicht belastbar.

### 7.5 Geräte-Konfiguration erstellen

`deployment/edge/device.env.example` als `device.env` in das aarch64-Bundle
kopieren. Die gemessenen Versionswerte eintragen:

```dotenv
YBB_EDGE_MANAGEMENT_IP=192.168.50.21
YBB_EDGE_SERVER_IP=192.168.50.10
YBB_EDGE_CONTROL_PORT=9443
YBB_EDGE_PLANT_API_PORT=9444
YBB_EDGE_PLANT_ALLOWED_IPS=192.168.50.40
YBB_ALLOW_PLC_WRITES=false
YBB_EDGE_PLANT_MAX_REQUEST_BYTES=65536
YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS=4
YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS=2500
YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND=20
YBB_EDGE_RTSP_PORT=8554
YBB_EDGE_HOSTNAME=ybb-edge-orin-01
YBB_EDGE_PLATFORM=jetson_orin_nano
YBB_EDGE_ARCHITECTURE=aarch64
YBB_EDGE_OS_VERSION=Jetson Linux 39.2
YBB_EDGE_JETPACK_VERSION=7.2
YBB_EDGE_L4T_VERSION=39.2
YBB_EDGE_CUDA_VERSION=13.2
YBB_EDGE_TENSORRT_VERSION=10.16
YBB_EDGE_DEEPSTREAM_VERSION=9.1
YBB_EDGE_GPU_MODEL=Jetson Orin Nano
YBB_EDGE_DATA_ROOT=/var/lib/ybb-edge
YBB_EDGE_ARTIFACT_QUOTA_BYTES=34359738368
YBB_EDGE_OUTBOX_QUOTA_BYTES=67108864
YBB_EDGE_OUTBOX_MAX_ITEMS=10000
YBB_EDGE_MINIMUM_DISK_FREE_BYTES=2147483648
YBB_EDGE_ENGINE_BUILDER_MODE=trtexec
YBB_EDGE_TRTEXEC_PATH=/usr/src/tensorrt/bin/trtexec
YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS=900
YBB_EDGE_PREVIEW_JPEG_QUALITY=70
YBB_EDGE_PREVIEW_MAX_BYTES=1048576
```

### 7.6 Agent offline installieren

Das aarch64-Bundle per temporär freigegebenem SCP, Netzwerkfreigabe oder
Datenträger nach `/opt/ybb-edge-bundle` kopieren:

```bash
cd /opt/ybb-edge-bundle
sudo bash deployment/edge/install-jetson-offline.sh /opt/ybb-edge-bundle
```

Für lokale USB-/CSI-Quellen benötigt der Servicebenutzer gegebenenfalls Zugriff
auf die Video-Geräte. Danach den Dienst neu starten:

```bash
sudo usermod -aG video ybb-edge
sudo systemctl restart ybb-edge-agent.service
```

Firewall erst anwenden, nachdem der Wartungszugang verifiziert wurde:

```bash
sudo bash deployment/edge/configure-ufw.sh /etc/ybb-edge/device.env
sudo ufw status numbered
sudo ufw enable
sudo ufw status verbose
```

Der Helper legt die Control-Regel `9443` nur für den Server und die Plant-Regel
`9444` nur für `YBB_EDGE_PLANT_ALLOWED_IPS` an. SSH-Wartung muss, falls benötigt,
vorher explizit aus dem Wartungsnetz erlaubt werden. Port `8554` bleibt
geschlossen, solange kein freigegebener Media-Dienst vorhanden ist. Die
9444-Regel erst nach erfolgreicher SPS-/VLAN-HIL aktivieren.

### 7.7 Jetson-Agent prüfen

```bash
sudo systemctl --no-pager --full status ybb-edge-agent.service
sudo systemctl --no-pager --full status ybb-edge-watchdog.timer
sudo journalctl -u ybb-edge-agent.service -n 100 --no-pager
curl -fsS http://192.168.50.21:9443/edge/v1/identity
curl -fsS http://192.168.50.21:9443/edge/v1/health
curl -fsS http://192.168.50.21:9443/edge/v1/capabilities
curl -fsS http://192.168.50.21:9443/edge/v1/runtime
curl -fsS http://192.168.50.21:9444/edge-plant/v1/health
```

Der Watchdog prüft den Agenten alle 30 Sekunden und startet ihn nach drei
Fehlversuchen neu. Vom Server anschließend:

```powershell
Test-NetConnection 192.168.50.21 -Port 9443
curl.exe -s http://192.168.50.21:9443/edge/v1/identity
```

## 8. Gerät im Orchestrierungsserver registrieren

1. Als `platform_admin`, `ml_engineer` oder `operator` anmelden.
2. In der Navigation `Edge Devices` öffnen.
3. `Gerät registrieren` wählen.
4. Für den Jetson folgende Werte eintragen:

   ```text
   Name:                 Jetson Orin Nano Anlage 1
   Technischer Ort:      Anlage 1 / Station 20
   Edge-Geräte-IP:                         192.168.50.21
   Orchestrierungsserver-IP:                192.168.50.10
   Erlaubtes Edge-Zielnetz (CIDR):          192.168.50.21/32
   Edge-Control-Port (Server -> Edge):      9443
   HTTP-Trigger-Port am Edge (SPS -> Edge): 9444
   RTSP-Port:                               8554
   ```

5. `Registrieren` wählen.
6. Gerät öffnen und `Status abrufen` ausführen.
7. Identity, Boot-ID, Plattform, Architektur, CUDA, TensorRT, GPU, freien
   Speicher und Runtime-Slots mit der lokalen Inventur vergleichen.

Für Windows Edge identisch vorgehen, aber IP `192.168.50.31`, Name und
Plattformwerte des Windows-PCs verwenden.

Das erlaubte Edge-Zielnetz muss die registrierte Edge-Geräte-IP enthalten. Für ein einzelnes
geroutet erreichbares Gerät ist `<Edge-IP>/32` die engste Freigabe. Die
Orchestrierungsserver-IP darf in einem anderen Netz liegen; Routing und Firewall müssen die
Verbindung zwischen beiden Adressen ermöglichen.

Die Edge-Geräte-IP wird im API-Vertrag aus Kompatibilitätsgründen weiterhin als
`management_ip` bezeichnet und ist nach der Registrierung unveränderlich. Das schützt vor
unbeabsichtigter Zielumleitung. Der Server lehnt Loopback, Link-Local,
Multicast, Broadcast, Adressen außerhalb des erlaubten CIDR und freie URLs ab.

### 8.1 Statusbedeutung

| Anzeige | Bedeutung | Operatoraktion |
|---|---|---|
| `Online` | letzter Poll erfolgreich | keine |
| `Eingeschränkt` | Agent antwortet, meldet aber ein Problem | Diagnose öffnen |
| `Veraltet` | letzte Beobachtung ist zu alt | Netzwerk und Agent prüfen |
| `Offline` | wiederholte Verbindung fehlgeschlagen | Strom, VLAN, Firewall, Dienst prüfen |
| `Unbekannt` | noch kein erfolgreicher Poll | `Status abrufen` ausführen |

`Erreichbarkeit` und `gemeldete Gesundheit` sind getrennt: Bei Serverausfall
läuft der letzte aktive Edge-Slot weiter, während dessen zentraler Status
zunächst veraltet und später offline wird.

## 9. Projekt-, Modell- und Pipeline-Vorbereitung

Der Bedienablauf auf dem Server bleibt für zentrale und Edge-Kanäle gleich:

1. Unter `Dataset Snapshots` Daten hochladen oder erfassen.
2. Unter `Label Studio Projects` Projekt zuordnen und Bilder labeln.
3. Einen unveränderlichen, validen Snapshot erzeugen.
4. Modell/Template wählen und Training starten.
5. Training, Metriken, Lineage und Promotion-Gates prüfen.
6. Modellversion für den vorgesehenen Einsatz freigeben.
7. Unter `Runtime Pipelines` eine Pipelineversion erzeugen, validieren und
   veröffentlichen.

Nur veröffentlichte, kompatible Pipelineversionen dürfen einem Kanal zugewiesen
werden. Der Edge-Preflight prüft unter anderem Platzierung, Capabilities,
Artefakte, Speicher und konkurrierende Workflows.

> Bis zum Abschluss von `GAP-EDGE-001` und `GAP-EDGE-002` beweist ein
> erfolgreicher Rollout nur den Orchestrierungs-, Transfer-, Build- und
> Aktivierungspfad. Er beweist keine echte Modellinferenz auf dem Edge.

## 10. Edge-Kanal anlegen und ausrollen

### 10.1 Kanal anlegen

1. `Channels` öffnen und einen Kanal anlegen.
2. Name und technischen Ort setzen.
3. Quelle wählen. Der Agent besitzt intern folgende Laufzeitadapter:

   - RTSP;
   - lokale Datei;
   - USB-/OpenCV-Gerät;
   - GStreamer-String.

   Der zentrale M3-Workload-Builder bildet derzeit RTSP, Datei/Snapshot und
   GigE-Bridges über einen vorhandenen GStreamer- oder RTSP-Pfad ab. USB ist
   trotz Agent-Adapter noch kein durchgängiger UI-zu-Edge-Pfad. Eine verwaltete
   Testdatei wird außerdem nicht automatisch als Kanalquellenartefakt auf das
   Edge kopiert; sie funktioniert nur, wenn derselbe Pfad lokal vorhanden ist.

4. Quellenadresse so angeben, wie sie **vom Edge-Gerät** erreichbar ist. Für
   die erste End-to-End-Abnahme wird eine RTSP-Testkamera empfohlen, zum
   Beispiel `rtsp://192.168.50.50/stream1`.
5. RTSP-Benutzername und Passwort in den getrennten Credential-Feldern und
   nicht in der URL pflegen. Der Server speichert die Referenzen getrennt und
   materialisiert die Zugangsdaten erst im übertragenen Manifest. Dieses
   Manifest läuft im gewählten Vertrauensmodell trotzdem unverschlüsselt über
   HTTP `9443`; die VLAN-Isolation ist deshalb zwingend.
6. `Max processing FPS` und `Sample every N` passend zur Hardware setzen.
7. veröffentlichte Pipelineversion auswählen.
8. `Ausführungsort = Edge Device` wählen.
9. Jetson oder Windows-Edge auswählen und speichern.

`GigE Vision (Aravis)` wird im Vertrag angeboten, besitzt im Edge-Agenten aber
noch keinen eigenen produktiv abgenommenen Adapter. Basler-/FLIR-spezifische
Pfade sind ebenfalls offen. Für die Abnahme zunächst RTSP oder einen bereits
auf dem Ziel validierten GStreamer-String verwenden.

### 10.2 Preflight und Rollout

1. `Edge Devices` öffnen und das zugewiesene Gerät wählen.
2. Im `Deployment-Assistent` den Kanal auswählen.
3. TensorRT-Präzision wählen:

   - `FP16`: Standard für Orin und übliche NVIDIA-GPUs;
   - `FP32`: höhere Genauigkeit, mehr Speicher/Rechenzeit;
   - `INT8`: nur mit validiertem Kalibrierungsweg verwenden.

4. `Preflight ausführen` wählen.
5. Jede Prüfung und deren Abhilfetext lesen. Nur bei `ready` fortfahren.
6. `Geprüftes Release ausrollen` wählen.
7. Den Fortschritt unter `Workload Releases und Deployments` beobachten.
8. Erwarteter Endzustand ist `running`; der vorherige aktive Slot wird
   `standby`.

Artefakte sind content-addressed. Vorhandene Blobs werden nicht erneut
übertragen. Unterbrochene Transfers werden am vom Edge bestätigten Byte-Offset
fortgesetzt. Erst vollständige Größe und passender SHA-256 erlauben die
atomare Übernahme.

## 11. Laufender Betrieb

### 11.1 Kanäle und Livebild

- Die Kanalübersicht zeigt Server- und Edge-Kanäle gemeinsam.
- `Edge · <Gerätename>` kennzeichnet die Platzierung.
- Ein Livebild wird vom Edge nur übertragen, solange mindestens ein Browser
  eine Viewer-Lease hält.
- Die Übersicht fordert ungefähr `0,5 FPS`, die Detailansicht bis zu `5 FPS`.
- Die aktuelle Edge-Vorschau ist JPEG-basiert. Bei einer Viewer-Lease mit
  `include_overlay=true` überträgt E1 die Primitiven und rendert Boxen,
  Polygone oder Labels optional bereits auf dem Edge.

Wird der Browser geschlossen, läuft die Lease aus und der Agent beendet die
Vorschauübertragung. Die lokale Kanalverarbeitung bleibt davon unabhängig.

### 11.2 Ergebnisse und Replay

Der Edge überträgt nur ausdrücklich aktivierte Ergebnis-Historie: Im Kanal unter
**Konfiguration → Datenerfassung & Historie** die Historie aktivieren und im
**Result output** die gewünschten Felder auf das Ziel `history` projizieren.
Der Sampling-Abstand des Kanals gilt bereits auf dem Edge. Ohne Historie wird
kein Ausführungsbericht pro Bild in die Outbox geschrieben; HTTP-Antwort,
Live-Vorschau und Leistungsstatistiken funktionieren trotzdem.

Nur diese ausgewählten Ergebnisse schreibt der Edge mit `boot_id` und monotoner
`sequence` in den begrenzten Übertragungspuffer. Der Server:

1. liest ab der letzten bestätigten Sequenz;
2. persistiert das Ergebnis zentral;
3. dedupliziert nach Geräte-IP, Boot-ID und Sequenz;
4. bestätigt nur die höchste lückenlos gespeicherte Sequenz.

Bei Netzunterbrechung werden nicht bestätigte Ergebnisse erneut geliefert. Ein
Neustart erzeugt eine neue Boot-ID und damit eine neue Sequenzdomäne. Outbox-
Quoten und freien Speicher in der Geräteansicht überwachen; bei Speicherdruck
werden neue Schreibvorgänge abgelehnt, bestehende unbestätigte Ergebnisse bleiben
erhalten. Neue Installationen verwenden 64 MiB beziehungsweise 10.000 Ergebnisse
als Obergrenze. Vorhandene Werte in `device.env` bleiben beim Runtime-Update erhalten.
Nach der Bestätigung werden die lokalen Ergebnisse gelöscht. Die SQLite-Datei
kann ihre bisherige Größe behalten und freie Seiten wiederverwenden.

Details, Grenzen und Einführung: [Edge-Speicherverhalten](edge-result-storage.md).

### 11.3 Plant-/SPS-Trigger

Im Node **Result output → Edge HTTP-Antwort** lässt sich der Antwortumfang festlegen:

| Auswahl | HTTP-Antwort |
| --- | --- |
| Nur konfigurierte Ergebnisfelder | Direkt die Result projections mit Ziel `response`, z. B. `{"result":"LKW wartend"}`. Weitere Felder wie Score, Anzahl oder Messwerte werden in derselben Projektionstabelle hinzugefügt. |
| Ergebnisfelder + ausgewählte Metadaten | `{"data":{"result":"LKW wartend"},"meta":{"duration_ms":237.5}}`; ausschließlich die ausgewählten Metadaten werden ergänzt. |
| Vollständiger Ausführungsbericht | Der bisherige Bericht mit `execution_result`, Zwischenergebnissen und Laufzeiten; bleibt aus Kompatibilitätsgründen die Voreinstellung. |

Die Auswahl verkleinert den tatsächlichen HTTP-Body. Die Historie wird unabhängig
davon ausdrücklich aktiviert; Vorschau und Gerätestatistiken benötigen keine
Ergebnispersistierung. **Metadaten in der Historie**
steuert bei Result projections die Zuordnungsfelder der Historie, unabhängig von
der HTTP-Antwort. Mehrere Result-Output-Nodes einer Pipeline müssen dieselben
Edge-HTTP-Antworteinstellungen verwenden. Fehler bleiben strukturierte HTTP-Fehler;
auch im kompakten Modus wird eine fehlgeschlagene Ausführung nicht als erfolgreiches
Ergebnis zurückgegeben. `last-result` liefert weiterhin den Diagnosebericht.

Im Node **HTTP trigger → Edge HTTP-Trigger** ist **Idempotenzschlüssel am Edge
verlangen** standardmäßig ausgeschaltet. Ohne Schlüssel startet jeder PUT eine neue
Verarbeitung; ein leerer Body genügt. Ein freiwillig gesetzter `X-Idempotency-Key`
verhindert fünf Minuten lang doppelte Verarbeitung. Bei eingeschalteter Pflicht wird ein
Aufruf ohne Schlüssel vor der Ausführung mit HTTP 422 abgelehnt.

Die neuen Einstellungen betreffen den lokalen Edge-Endpunkt auf Port 9444. Dafür
Zentrale und Edge-Runtime aktualisieren, die Pipeline speichern/veröffentlichen
und die aktualisierte Pipeline am Edge aktivieren. Bereits gespeicherte Aufträge
behalten bei Wiederholung mit demselben Schlüssel ihre damaligen Ergebnisse und
das damalige Antwortformat innerhalb dieser fünf Minuten, auch nach einem Neustart
oder Pipeline-Update. Für neue Vorgänge neue Schlüssel verwenden. Nach Ablauf
kann derselbe Schlüssel wieder eine Verarbeitung auslösen. Ohne übertragene
Historie ist `result_sequence` im vollständigen Bericht beziehungsweise in den
ausgewählten Metadaten `null`.

Beispiel für **Nur konfigurierte Ergebnisfelder**, ohne Schlüsselpflicht:

```powershell
$uri = "http://172.23.27.245:9444/edge-plant/v1/channels/chn_d516a5a7b48f4f9cbb76e96a1f6b7109/trigger"
$result = Invoke-RestMethod -Method Put -Uri $uri -TimeoutSec 15
$result | ConvertTo-Json -Depth 10
# {"result":"LKW wartend"}
```

Die folgenden Beispiele verwenden das bisherige vollständige Antwortformat.

Der Kanal muss als Trigger-Pipeline veröffentlicht, dem Edge zugewiesen und
`running` sein. Der Edge hält den neuesten Kameraframe lokal. Jeder SPS-Zyklus
startet mit einem einfachen PUT ohne zusätzliche Header und ohne Body eine neue
Ausführung:

```powershell
$channelId = "<channel-id>"
$result = Invoke-RestMethod -Method Put `
  -Uri "http://192.168.50.21:9444/edge-plant/v1/channels/$channelId/trigger"
$result.execution_result.final_response
```

Ein optionaler `X-Idempotency-Key` schützt vor doppelter Ausführung, falls ein
Client denselben Auftrag nach einem verlorenen HTTP-Response wiederholt.
Ohne diesen Header ist jeder PUT ein neuer Trigger.

Beispiel mit optionalem Key und zusätzlicher Nutzlast von der freigegebenen
SPS-/Teststation:

```powershell
$channelId = "<channel-id>"
$headers = @{ "X-Idempotency-Key" = "line1-cycle-00004711" }
$body = @{
  correlation_id = "part-4711"
  payload = @{ part_id = "4711"; recipe = "A" }
  timeout_ms = 1000
  include_overlay = $false
} | ConvertTo-Json -Depth 5

$result = Invoke-RestMethod -Method Put `
  -Uri "http://192.168.50.21:9444/edge-plant/v1/channels/$channelId/trigger" `
  -Headers $headers -ContentType "application/json" -Body $body
$result.execution_result.final_response
```

Bei verlorenem Response innerhalb von fünf Minuten denselben Key mit identischem Body wiederholen. Der
Edge liefert das persistierte Ergebnis mit `duplicate=true`; ein anderer Body
mit demselben Key wird abgelehnt. Alternativ:

```powershell
Invoke-RestMethod `
  "http://192.168.50.21:9444/edge-plant/v1/channels/$channelId/last-result"
```

Ein HTTP `429` bedeutet Rate- oder Parallelitätsgrenze, `504` eine
überschrittene Antwortfrist. Ein bereits laufender GPU-Auftrag wird bei `504`
nicht unsicher abgebrochen. Die letzte Antwort bleibt kurzzeitig im RAM; mit
Idempotenzschlüssel bleibt sie für das Wiederholungsfenster auf dem Datenträger.
Eine Outbox entsteht nur bei ausdrücklich aktivierter Historie. Das SPS-Programm
muss Fehler und `retryable` auswerten
und darf bei Unsicherheit keine unkontrollierte Maschinenaktion auslösen.

### 11.4 SPS-Schreiben aus einer Edge-Pipeline

Ein eingehender HTTP-Trigger und ein ausgehender SPS-Schreibzugriff sind getrennte Funktionen:

- `YBB_EDGE_PLANT_ALLOWED_IPS` erlaubt einer SPS den HTTP-Aufruf zum Edge auf Port 9444;
- der Pipelineknoten `Write to Siemens SPS` enthält SPS-IP, S7-Port, Rack, Slot sowie je Mapping
  DB-, Byte- und Bitadresse;
- der vollständige Knoten wird im versionierten Pipeline-Graph mit dem Kanal-Workload an das Edge
  übertragen und dort ausgeführt;
- `enabled=true`, `dry_run=false` und `YBB_ALLOW_PLC_WRITES=true` müssen gemeinsam gelten, bevor
  ein Hardware-Schreibzugriff stattfindet;
- für Echtzeitpipelines bleibt das konfigurierte Mindestintervall von mindestens 1000 ms aktiv.

Die SPS-IP aus der HTTP-Trigger-Allowlist wird nicht automatisch als Schreibziel verwendet. Damit
bleibt auch der Fall korrekt, in dem SPS A den Kanal triggert, das Ergebnis aber an SPS B
geschrieben wird. Vor Freigabe TCP 102 vom Edge zur konfigurierten SPS in der VLAN-ACL erlauben
und DB-/Byte-/Bitadresse im Dry-Run sowie mit einer Test-SPS prüfen.

### 11.5 Diagnose

Auf der Gerätedetailseite `Diagnose aktualisieren` wählen. Prüfen:

- Netz/Identity/Health/Capabilities;
- Supervisor und aktiver A/B-Slot;
- Artefakt- und Outboxbelegung;
- Ergebnis-Replay-Lag;
- Kanalstatus und P95-Latenz gegen das angezeigte SLO.

`healthy` ist unauffällig, `degraded` verlangt Untersuchung, `critical`
verlangt Eingriff. Die M5-Schwellen sind derzeit fest implementiert und noch
keine frei konfigurierbare Alarmrichtlinie. Die Fleet-Übersicht ist eine
Momentaufnahme, kein vollständiger Alert-Lifecycle.

### 11.6 Manueller Rollback

Ein Rollback ist verfügbar, wenn ein laufendes Deployment einen vorherigen
A/B-Slot besitzt:

1. Gerät öffnen.
2. Unter `Workload Releases und Deployments` das aktive Deployment suchen.
3. `Rollback` wählen und Sicherheitsabfrage bestätigen.
4. Warten, bis der vorherige Slot wieder aktiv ist.
5. Kanalzustand, Ergebnisfluss und Diagnose erneut prüfen.
6. Ursache und Deployment-ID im Betriebsprotokoll dokumentieren.

Rollback baut keine Artefakte neu. Fehlt ein vorheriger Slot, muss ein bekannt
gutes Release erneut ausgerollt werden.

### 11.6 Gerät deaktivieren, archivieren oder ersetzen

- `Deaktivieren` stoppt zentrale Reconciliation/Verwaltung für das Gerät,
  entfernt aber nicht automatisch einen lokal laufenden Slot.
- Vor Wartung den Kanal geordnet stoppen und den resultierenden Zustand prüfen.
- Archivieren ist erst möglich, wenn keine Kanäle mehr zugewiesen sind.
- Bei Gerätetausch mit gleicher IP: altes Gerät stoppen, Ersatz offline
  installieren, exakt dieselbe IP konfigurieren, `Status abrufen`, neue
  Identity-Generation/Capabilities prüfen und Release erneut ausrollen.
- Bei neuer IP: Kanäle umplatzieren, alten Datensatz archivieren, neue IP
  registrieren.

## 12. Updates und Wartung

### 12.1 Zentraler Server

Serverupdates werden als Offline-Update-ZIP exportiert und über die installierte
`YBB-Deployment-UI.exe` oder `update-ybb-inferenzserver.cmd` eingespielt. Die
persistenten Daten unter `/srv/cv-platform` bleiben erhalten. Vor jedem Update:

1. Backup und freien Speicher prüfen;
2. aktiven Release-/Kanalzustand dokumentieren;
3. Update-Prüfsummen verifizieren;
4. Wartungsfenster eröffnen;
5. nach dem Update Migrationen, Dienste, Edge-Reconciler, Result-Sync und
   Deployment-Controller prüfen.

### 12.2 Edge-Geräte

Ein zentral orchestriertes Edge-Agent-/OS-Fleet-Update ist noch nicht
implementiert (`GAP-EDGE-025`). Agent, JetPack, Windows, Treiber oder TensorRT
werden daher im Wartungsfenster gerätespezifisch aktualisiert. Danach immer:

1. Agent und Kameraaufnahmen stoppen;
2. lokalen Zustand sichern, ohne eine laufende SQLite-Datei zu kopieren;
3. geprüftes Offline-Bundle installieren;
4. Dienst starten und alle vier Probe-Endpunkte prüfen;
5. vom Server `Status abrufen`;
6. Capability-Änderungen prüfen;
7. TensorRT-Engines neu bauen und Release erneut validieren;
8. Replay, Livebild und Rollbackkandidat kontrollieren.

## 13. Hardware-in-the-Loop-Abnahme

Für jedes freizugebende Hardware-/Softwareprofil ist ein eigener HIL-Lauf
erforderlich. Die vorhandenen Reports stehen derzeit auf `pending_hardware`:

- [M0-HIL-Report](./edge-jetson-orin-nano-hil-report.md)
- [M2-HIL-Report](./edge-jetson-orin-nano-m2-hil-report.md)
- [M3-HIL-Report](./edge-jetson-orin-nano-m3-hil-report.md)
- [M4-HIL-Report](./edge-jetson-orin-nano-m4-hil-report.md)
- [M5-HIL-Report](./edge-jetson-orin-nano-m5-hil-report.md)

Auf dem Jetson kann die Inventur aus dem Repository ausgeführt werden:

```bash
python3 scripts/benchmark/edge_hil_probe.py \
  --management-ip 192.168.50.21 \
  --server-ip 192.168.50.10 \
  --output edge-orin-nano-hil.json
```

Der Probe verwendet aktuell bei der Speicherinventur noch den historischen
Pfad `/srv/ybb-edge`; der installierte Agent nutzt `/var/lib/ybb-edge`. Bis zur
Korrektur die Speicherwerte zusätzlich mit `df -h /var/lib/ybb-edge` erfassen.

Mindestumfang der Abnahme:

- 1080p/30-Quelle und konfiguriertes Sampling;
- fünf Minuten Warmup und 30 Minuten Messung;
- P50/P95/P99-Latenz, Source-/Processed-/Drop-FPS;
- CPU, RAM, GPU, GPU-Speicher und Datenträger;
- lokaler ONNX-zu-TensorRT-Build, Buildzeit und Enginegröße;
- Golden-Input-Vergleich mit echten Modelloutputs;
- Netzverlust bei weiterlaufender lokaler Aufgabe;
- Result-Replay ohne doppelte zentrale Persistenz;
- Agent-, Geräte- und Serverneustart;
- A/B-Rollback;
- Zugriffstest von erlaubter und nicht erlaubter Quell-IP.

Die aktuelle Integritätsprüfung des Agenten ersetzt keinen echten
Outputvergleich. Eine Produktionsfreigabe benötigt die noch offene
End-to-End-Inferenz und die ausgefüllten HIL-Reports.

## 14. Fehlerbehebung

### Gerät bleibt `Unbekannt` oder wird `Offline`

1. IP und Kabel/VLAN prüfen.
2. Vom Server `Test-NetConnection <IP> -Port 9443` ausführen.
3. Erreichbarkeit aus WSL prüfen.
4. Windows-Dienst bzw. `systemctl status ybb-edge-agent` prüfen.
5. Firewallregel muss die echte Server-Maschinen-IP erlauben.
6. Registriertes CIDR und IP vergleichen.
7. Gerätedetail öffnen und `Status abrufen` erneut ausführen.

### `address_not_allowed`, CIDR- oder Netzwerkfehler

Die registrierte IP muss eine normale Hostadresse innerhalb des hinterlegten
IPv4-CIDR sein. Loopback, Link-Local, Multicast, Broadcast und andere Netze
werden absichtlich abgewiesen. Keine URL und keinen Hostnamen in das IP-Feld
eintragen.

### Artefakttransfer bricht ab

- freien Speicher und Artefaktquote prüfen;
- SHA-256 und erwartete Größe prüfen;
- identisches Release erneut starten; der Upload setzt am bestätigten Offset
  fort;
- bei reproduzierbarem Hashfehler das erzeugte Releasebundle auf dem Server
  verwerfen und neu bauen.

### Preflight meldet Capability-Konflikt

- `Status abrufen` und lokale Versionsinventur vergleichen;
- deklarierte `device.env`-Werte korrigieren;
- Agent neu starten;
- kein TensorRT-Engineartefakt einer anderen Plattform wiederverwenden;
- bei `INT8` den Kalibrierungsweg prüfen oder zunächst `FP16` verwenden.

### Deployment bleibt bei Build/Aktivierung hängen

- `trtexec`-Pfad und Ausführbarkeit lokal prüfen;
- Build-Timeout und freien Speicher prüfen;
- Agent-Log öffnen;
- im Simulatorbetrieb keinen Produktionsnachweis ableiten;
- fehlgeschlagene Aktivierung zurückrollen oder bekannt gutes Release erneut
  ausrollen.

### Keine Ergebnisse nach Netzunterbrechung

- `M4 Ergebnis- und Live-Synchronisation` auf laufenden Worker und Fehler
  prüfen;
- Gerätediagnose und `Result-Replay-Cursor` öffnen;
- Boot-ID-Wechsel berücksichtigen;
- Outboxbelegung und freien Speicher prüfen;
- Agent nicht zurücksetzen oder dessen State-Verzeichnis löschen, bevor
  unbestätigte Ergebnisse synchronisiert wurden.

### Kein Livebild

- Kanal muss `running` oder `degraded` sein;
- Kanalübersicht oder Detailansicht geöffnet lassen, damit eine Viewer-Lease
  besteht;
- Quelle vom Edge lokal prüfen;
- OpenCV/GStreamer und Geräteberechtigungen prüfen;
- Preview-Größenlimit und Agent-Log prüfen;
- eine fehlende RTSP-Ausgabe auf `8554` ist im aktuellen Stand erwartbar; die
  M4-Vorschau läuft als JPEG über `9443`.

### Windows-Edge-Dienst startet nicht

```powershell
Get-Service YBBEdgeAgent
& "C:\Program Files\YBB Edge\ybb-edge-service.exe" status
Get-Content "C:\Program Files\YBB Edge\device.env"
```

Pythonpfad, Wheel-Kompatibilität, WinSW-Konfiguration und Schreibrechte auf
`C:\ProgramData\YBB\Edge` prüfen. Capability-Werte in `device.env` enthalten
keine Anführungszeichen.

### Jetson-Agent startet nicht

```bash
sudo systemctl --no-pager --full status ybb-edge-agent.service
sudo journalctl -u ybb-edge-agent.service -b --no-pager
sudo -u ybb-edge /opt/ybb-edge/venv/bin/python -c \
  'import edge_agent, cv_contracts, cv_status; print("imports ok")'
sudo ss -ltnp | grep 9443
```

Wheel-Architektur, Pythonversion, `/etc/ybb-edge/device.env`, Zugriffsrechte auf
`/var/lib/ybb-edge` und den `trtexec`-Pfad prüfen.

## 15. Inbetriebnahme-Checkliste

### Server

- [ ] Windows/WSL2 installiert und gepatcht
- [ ] feste Maschinen-IP `192.168.50.10`
- [ ] Offline-Serverbundle und Prüfsummen archiviert
- [ ] Plattformdienste laufen
- [ ] initiales Admin-Passwort geändert
- [ ] WSL erreicht beide Edge-IP-Adressen auf `9443`

### Je Edge-Gerät

- [ ] Hardware-, OS-, Treiber- und Runtimeprofil dokumentiert
- [ ] feste, kollisionsfreie IP
- [ ] kein Internet-/Default-Gateway auf der Maschinen-NIC
- [ ] gerätespezifisches, geprüftes Offline-Bundle
- [ ] Agent startet automatisch
- [ ] Identity, Health, Capabilities und Runtime lokal erreichbar
- [ ] `9443` nur vom Server erreichbar
- [ ] `9444` nur von explizit freigegebenen SPS-/Testclient-IPs erreichbar; die
      Orchestrierungsserver-IP ist nur bei Bedarf als Triggerclient eingetragen
- [ ] Gerät registriert und erster Poll erfolgreich
- [ ] Capabilities gegen lokale Inventur geprüft
- [ ] Speicherquoten passend dimensioniert
- [ ] Kanalquelle lokal erreichbar
- [ ] Preflight erfolgreich
- [ ] Release übertragen und A/B-Aktivierung geprüft
- [ ] echter Pipeline-Graph und TensorRT-Engine geladen
- [ ] Golden Output innerhalb der freigegebenen Toleranz
- [ ] Plant-Trigger, Idempotenz, Timeout, Rate- und Parallelitätsgrenze geprüft
- [ ] Netzverlust und Replay geprüft
- [ ] Viewer-Lease und Live-JPEG geprüft
- [ ] Rollback geprüft
- [ ] HIL-Report ausgefüllt

### Produktionsfreigabe

- [ ] E1-HIL-Bericht auf `accepted`
- [ ] echte Modelloutputs gegen Golden Inputs verglichen
- [ ] SPS-Pfad nur bei implementierter und abgenommener Plant API freigegeben
- [ ] alle P0-HIL-Gates bestanden
- [ ] Backup-, Restore-, Update- und Ersatzgeräteprozess geprobt
- [ ] Betreiber, Rollen, Wartungsfenster und Eskalationsweg dokumentiert

## 16. Weiterführende Dokumente und offizielle Grundlagen

Projektintern:

- [M0 Follow-up-Plan](./edge-orchestration-follow-up-plan.md)
- [M1 Follow-up-Plan](./edge-orchestration-m1-follow-up-plan.md)
- [M2 Follow-up-Plan](./edge-orchestration-m2-follow-up-plan.md)
- [M3 Follow-up-Plan](./edge-orchestration-m3-follow-up-plan.md)
- [M4 Follow-up-Plan](./edge-orchestration-m4-follow-up-plan.md)
- [E1 Productive Edge Execution](./edge-orchestration-e1-follow-up-plan.md)
- [E1 Jetson HIL-Bericht](./edge-jetson-orin-nano-e1-hil-report.md)
- [M5 Follow-up-Plan](./edge-orchestration-m5-follow-up-plan.md)
- [Edge-Spezifikationslücken](./SPEC_GAPS_Edge_Orchestration.md)
- [Allgemeiner Operator Guide](../operations/operator-guide.md)
- [Windows-Offline-Deployment](../../deployment/README.md)
- [Edge-Offline-Installer](../../deployment/edge/README.md)

Herstellerdokumentation:

- [Microsoft: WSL installieren](https://learn.microsoft.com/windows/wsl/install)
- [Microsoft: New-NetIPAddress](https://learn.microsoft.com/powershell/module/nettcpip/new-netipaddress)
- [Microsoft: New-NetFirewallRule](https://learn.microsoft.com/powershell/module/netsecurity/new-netfirewallrule)
- [NVIDIA: SDK Manager – Jetson installieren](https://docs.nvidia.com/sdk-manager/install-with-sdkm-jetson/)
- [NVIDIA: Jetson Linux R39.2 – Flashing Support](https://docs.nvidia.com/jetson/archives/r39.2/DeveloperGuide/SD/FlashingSupport.html)
- [NVIDIA: Jetson Orin Nano – JetPack Setup](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/setup_jetpack.html)
- [NVIDIA: DeepStream Installation und Kompatibilität](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html)
