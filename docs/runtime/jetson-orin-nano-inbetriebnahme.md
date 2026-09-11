# Inbetriebnahme eines Jetson Orin Nano als YBB-Edge-Gerät

Stand: 10. September 2026

Dieses Runbook beschreibt den vollständig durchgeführten und abgenommenen Weg vom dauerhaften
Release-Schlüsselpaar über das signierte Offline-Bundle bis zur laufenden und in der Plattform
registrierten Edge-Runtime. Es verwendet die konkreten Werte der ersten Inbetriebnahme, ist aber
so aufgebaut, dass es für weitere Jetson-Geräte wiederverwendet werden kann.

Die Netzwerkkonfiguration des Jetsons war bereits korrekt eingerichtet. Deshalb wird bewusst nur
der direkte Runtime-Installer verwendet. `apply-jetson-base.sh` und die Basisprovisionierung werden
nicht ausgeführt; vorhandene IP-, Gateway-, DNS- und NetworkManager-Einstellungen bleiben
unverändert.

## 1. Abgenommene Zielkonfiguration

| Einstellung | Wert |
|---|---|
| Plattform | NVIDIA Jetson Orin Nano |
| Edge-IP | `172.23.27.245` |
| Orchestrierungsserver | `172.23.27.242` |
| Anlagen-VLAN | `172.23.27.0/24` |
| Edge-Control-Port | `9443/tcp` |
| HTTP-Trigger-Port | `9444/tcp` |
| RTSP-Port | `8554/tcp` |
| Runtime-Hostname | `ybb-edge-orin-nano-01` |
| Linux-/SSH-Benutzer | `peter` |
| Runtime-Datenverzeichnis | `/var/lib/ybb-edge` |
| Vorgesehenes NVMe-Gerät | `/dev/nvme0n1p1` |
| USB-Kameraadapter | `usb_opencv` |
| SPS-Schreibgate | freigegeben (`YBB_ALLOW_PLC_WRITES=true`) |
| Erstes abgenommenes Release | `1.0.2` |

Das zugehörige Plattformprofil erwartet CPython 3.12 auf AArch64 sowie den im Repository
festgelegten Jetson-/CUDA-/TensorRT-/DeepStream-Stack. Maßgeblich ist
`deployment/edge/profiles/jetson-orin-nano.json`.

## 2. Voraussetzungen prüfen

### 2.1 Build-Rechner

Auf dem Windows-Build-Rechner werden benötigt:

- das Repository unter `C:\KI\YBB_Inferenzserver`;
- CPython 3.12 mit `pip`;
- die vorbereitete Build-Umgebung `.tmp\edge312-venv`;
- Git for Windows einschließlich OpenSSL;
- Internetzugriff ausschließlich für `prepare-wheelhouse`;
- SSH/SCP oder ein kontrollierter Datenträger für die Übertragung zum Jetson.

In PowerShell prüfen:

```powershell
Set-Location 'C:\KI\YBB_Inferenzserver'

& '.\.tmp\edge312-venv\Scripts\python.exe' --version
& 'C:\Program Files\Git\usr\bin\openssl.exe' version
Test-Path '.\scripts\release\build_edge_offline_bundle.py'
```

### 2.2 Jetson

Auf dem Jetson prüfen:

```bash
ip -brief address
ip route
python3 --version
openssl version
lsblk -f
```

Die feste Adresse `172.23.27.245/24` muss bereits auf der richtigen Schnittstelle aktiv sein. Der
Server `172.23.27.242` und der Jetson müssen sich gegenseitig erreichen können.

Vor produktiven Workloads außerdem sicherstellen, dass das persistente Datenverzeichnis auf der
vorgesehenen NVMe liegt:

```bash
findmnt -T /var/lib/ybb-edge -o TARGET,SOURCE,FSTYPE,SIZE,AVAIL,USE%
df -h /var/lib/ybb-edge
```

Der Provisioner formatiert oder partitioniert kein Laufwerk. Falls der Mount noch nicht korrekt
ist, wird er gemäß `docs/runtime/edge-orchestration-operations-guide.md` vorbereitet, bevor Modelle
oder größere Artefakte ausgerollt werden.

## 3. Dauerhaftes Release-Schlüsselpaar erzeugen

Dieser Schritt wird pro Vertrauensdomäne einmal durchgeführt. Dasselbe Schlüsselpaar wird für
weitere Releases wiederverwendet, solange keine geplante Schlüsselrotation erfolgt.

Der private Schlüssel liegt außerhalb des Repositorys. Auf dem abgenommenen Build-Rechner wurde
folgender Speicherort verwendet:

```text
C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-private.pem
C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-public.pem
```

PowerShell:

```powershell
$openssl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$keyDir = Join-Path $env:USERPROFILE 'YBB-ReleaseKeys\production-v1'
$privateKey = Join-Path $keyDir 'release-private.pem'
$publicKey = Join-Path $keyDir 'release-public.pem'

New-Item -ItemType Directory -Path $keyDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $openssl)) {
    throw 'OpenSSL wurde nicht gefunden.'
}
if (-not (Test-Path -LiteralPath $keyDir)) {
    throw 'Das Release-Key-Verzeichnis wurde nicht angelegt.'
}
```

Verschlüsselten privaten RSA-Schlüssel erzeugen:

```powershell
& $openssl genpkey -algorithm RSA -aes-256-cbc -pkeyopt 'rsa_keygen_bits:3072' -out $privateKey
```

OpenSSL fragt die PEM-Passphrase zweimal ab. Diese Passphrase wird nicht in Konfigurationsdateien,
Skripten, Tickets oder Chatnachrichten gespeichert.

Öffentlichen Schlüssel ableiten:

```powershell
& $openssl pkey -in $privateKey -pubout -out $publicKey
```

Schlüsselpaar prüfen und den öffentlichen Fingerprint dokumentieren:

```powershell
& $openssl pkey -in $privateKey -check -noout
Get-Item -LiteralPath $privateKey, $publicKey
Get-FileHash -Algorithm SHA256 -LiteralPath $publicKey
```

Erwartete Schlüsselprüfung:

```text
Key is valid
```

Der bei dieser Inbetriebnahme verwendete SHA-256-Wert des öffentlichen PEM-Schlüssels lautet:

```text
FE34B39C19850C5EDEE47699B3F5D1B3EB98C9AFBB9A94D16C2D4D9B3028A1DE
```

Sicherheitsregeln:

- `release-private.pem` bleibt ausschließlich in der geschützten Buildumgebung.
- Der private Schlüssel wird nie in das Repository, ein Bundle oder auf ein Edge-Gerät kopiert.
- Privater Schlüssel und Passphrase werden getrennt und gesichert aufbewahrt.
- `release-public.pem` ist der verteilbare Trust-Anker für Server und Edge-Geräte.
- Ein Austausch des Schlüssels ist eine geplante Schlüsselrotation und kein gewöhnliches Update.

## 4. Orin-Nano-Build konfigurieren

Die konkrete CLI-Konfiguration liegt auf dem Build-Rechner unter:

```text
dist/.generated/edge-jetson-orin-nano-172.23.27.245.json
```

Für das abgenommene Release `1.0.2` enthält sie folgende Werte:

```json
{
  "target_platform": "jetson_orin_nano",
  "release_version": "1.0.2",
  "source_date_epoch": 1788998400,
  "output_directory": "dist/edge",
  "wheelhouse_directory": "dist/wheelhouse/jetson_orin_nano-1.0.2",
  "package_index_url": "https://pypi.org/simple",
  "additional_wheel_directory": "",
  "build_internal_wheels": false,
  "python_executable": "C:/KI/YBB_Inferenzserver/.tmp/edge312-venv/Scripts/python.exe",
  "runtime_distributions": "edge-agent",
  "python_runtime_archive": "",
  "winsw_executable": "",
  "windows_installer_executable": "dist/edge-bundle-builder/YBB-Edge-Windows-Installer.exe",
  "device_hostname": "ybb-edge-orin-nano-01",
  "device_ipv4": "172.23.27.245",
  "prefix_length": 24,
  "gateway_ipv4": "",
  "dns_servers": "",
  "timezone": "Europe/Vienna",
  "server_ipv4": "172.23.27.242",
  "plant_allowed_ipv4s": "",
  "control_port": 9443,
  "plant_api_port": 9444,
  "rtsp_port": 8554,
  "allow_plc_writes": true,
  "state_root": "/var/lib/ybb-edge",
  "artifact_quota_bytes": 34359738368,
  "outbox_quota_bytes": 2147483648,
  "minimum_disk_free_bytes": 2147483648,
  "engine_builder_mode": "trtexec",
  "trtexec_path": "/usr/src/tensorrt/bin/trtexec",
  "camera_adapters": "rtsp,usb_opencv,gstreamer,aravis_gige,basler_pylon,flir_spinnaker",
  "nvme_device": "/dev/nvme0n1p1",
  "windows_interface_alias": "Ethernet",
  "require_signature": true,
  "signing_private_key": "C:/Users/straspe/YBB-ReleaseKeys/production-v1/release-private.pem",
  "signing_public_key": "C:/Users/straspe/YBB-ReleaseKeys/production-v1/release-public.pem",
  "openssl_executable": "C:/Program Files/Git/usr/bin/openssl.exe"
}
```

Leere Gateway- und DNS-Werte sind in diesem Fall beabsichtigt, weil die bestehende
Netzwerkkonfiguration nicht provisioniert wird. Das direkte Installationsskript benötigt diese
Werte nicht.

`allow_plc_writes=true` öffnet das globale Runtime-Gate für explizit konfigurierte SPS-Schreibnodes.
Es erzeugt allein noch keinen Schreibzugriff. `plant_allowed_ipv4s` steuert davon getrennt die
Quell-IP-Freigabe für eingehende HTTP-Trigger auf Port `9444`.

## 5. AArch64-Wheelhouse vorbereiten

Für jedes Release wird ein neues, leeres Wheelhouse-Verzeichnis verwendet. Nur dieser Schritt
benötigt Internetzugriff:

```powershell
Set-Location 'C:\KI\YBB_Inferenzserver'

& '.\.tmp\edge312-venv\Scripts\python.exe' `
  '.\scripts\release\build_edge_offline_bundle.py' `
  prepare-wheelhouse `
  --config '.\dist\.generated\edge-jetson-orin-nano-172.23.27.245.json'
```

Erwartetes Ergebnis:

```json
{"status":"prepared","wheelhouse":"C:\\KI\\YBB_Inferenzserver\\dist\\wheelhouse\\jetson_orin_nano-1.0.2"}
```

Der Builder erzeugt die internen YBB-Wheels und lädt alle kompatiblen CPython-3.12-/AArch64-Wheels.
Linux-spezifische, auf einem Windows-Buildhost sonst nicht automatisch ausgewählte Marker-
Abhängigkeiten werden explizit berücksichtigt. Für Jetson muss insbesondere ein AArch64-`uvloop`-
Wheel vorhanden sein.

Kontrolle:

```powershell
$wheelhouse = '.\dist\wheelhouse\jetson_orin_nano-1.0.2'

Get-ChildItem -LiteralPath $wheelhouse -Filter 'uvloop-*.whl'
Get-ChildItem -LiteralPath $wheelhouse -Filter 'opencv_python_headless-*.whl'

$lock = Get-Content -Raw -LiteralPath "$wheelhouse\wheelhouse-source.lock.json" |
  ConvertFrom-Json
$lock.platform
$lock.python_tag
$lock.python_abi
$lock.wheels.Count
```

Für Release `1.0.2` wurden 36 Wheels gesperrt. Enthalten sind unter anderem:

```text
uvloop-0.22.1-cp312-cp312-manylinux...aarch64.whl
opencv_python_headless-4.14.0.94-cp37-abi3-manylinux_2_28_aarch64.whl
```

## 6. Builder prüfen

Vor dem Release den fokussierten Bundle-Builder-Test ausführen:

```powershell
$testTemp = ".tmp/pytest-edge-bundle-$((Get-Date).ToString('yyyyMMddHHmmss'))"

& '.\.tmp\edge312-venv\Scripts\python.exe' -m pytest `
  'tests/unit/backend/test_edge_e2_bundle_builder.py' `
  -q `
  -p no:cacheprovider `
  --basetemp $testTemp
```

Bei der Abnahme waren alle zwölf fokussierten Tests erfolgreich.

## 7. Signiertes Offline-Bundle bauen

Der eigentliche Bundle-Build benötigt nach vorbereitetem Wheelhouse keinen Internetzugriff:

```powershell
Set-Location 'C:\KI\YBB_Inferenzserver'

& '.\.tmp\edge312-venv\Scripts\python.exe' `
  '.\scripts\release\build_edge_offline_bundle.py' `
  build `
  --config '.\dist\.generated\edge-jetson-orin-nano-172.23.27.245.json'
```

OpenSSL fragt lokal nach der Passphrase des privaten Release-Schlüssels. Der erfolgreiche Build
erzeugt:

```text
C:\KI\YBB_Inferenzserver\dist\edge\ybb-edge-jetson_orin_nano-1.0.2.tar.gz
```

Der abgenommene SHA-256-Wert dieses konkreten Artefakts lautet:

```text
F7094B9464E573FCF1EA01689218D78BB00D9B8E71D1B7FE9C66E62A328D80E9
```

Lokalen Hash anzeigen:

```powershell
$bundle = '.\dist\edge\ybb-edge-jetson_orin_nano-1.0.2.tar.gz'
(Get-FileHash -Algorithm SHA256 -LiteralPath $bundle).Hash
```

## 8. Bundle auf dem Build-Rechner verifizieren

Die Prüfung verwendet ausdrücklich den extern gespeicherten öffentlichen Schlüssel und nicht den
im Bundle mitgelieferten Schlüssel:

```powershell
$bundle = '.\dist\edge\ybb-edge-jetson_orin_nano-1.0.2.tar.gz'
$verifyDir = '.\.tmp\edge-bundle-verify-nano-1.0.2-signed'
$trustedKey = 'C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-public.pem'

if (Test-Path -LiteralPath $verifyDir) {
    throw "Prüfverzeichnis existiert bereits: $verifyDir"
}

New-Item -ItemType Directory -Path $verifyDir | Out-Null
tar -xf $bundle -C $verifyDir

& '.\.tmp\edge312-venv\Scripts\python.exe' `
  '.\scripts\release\build_edge_offline_bundle.py' `
  verify `
  --bundle-directory $verifyDir `
  --openssl 'C:\Program Files\Git\usr\bin\openssl.exe' `
  --trusted-public-key $trustedKey
```

Erwartete Ausgabe:

```text
Verified OK
```

Signaturrichtlinie und Jetson-Pflichtwheel prüfen:

```powershell
Get-Content -LiteralPath "$verifyDir\SIGNATURE_POLICY"
Get-ChildItem -LiteralPath "$verifyDir\wheelhouse" -Filter 'uvloop-*.whl'
Get-Content -LiteralPath "$verifyDir\release-signing-key.json"
```

`SIGNATURE_POLICY` muss `required` enthalten. Der Fingerprint in
`release-signing-key.json` muss dem extern dokumentierten Public-Key-Hash entsprechen.

## 9. Bundle auf den Jetson übertragen

Auf dem Windows-Build-Rechner:

```powershell
Set-Location 'C:\KI\YBB_Inferenzserver'

$jetsonUser = 'peter'
$edgeIp = '172.23.27.245'

scp '.\dist\edge\ybb-edge-jetson_orin_nano-1.0.2.tar.gz' `
  "${jetsonUser}@${edgeIp}:~/"

scp 'C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-public.pem' `
  "${jetsonUser}@${edgeIp}:~/"

ssh "${jetsonUser}@${edgeIp}"
```

Der Public Key wird nur bei der ersten Inbetriebnahme oder bei einer geplanten Schlüsselrotation
übertragen. Der private Schlüssel verlässt den Build-Rechner nicht.

## 10. Übertragenes Artefakt auf dem Jetson prüfen

Auf dem Jetson:

```bash
cd ~

echo 'f7094b9464e573fcf1ea01689218d78bb00d9b8e71d1b7fe9c66e62a328d80e9  ybb-edge-jetson_orin_nano-1.0.2.tar.gz' \
  | sha256sum --check

sha256sum ~/release-public.pem
```

Der Public-Key-Hash muss lauten:

```text
fe34b39c19850c5edee47699b3f5d1b3eb98c9afbb9a94d16c2d4d9b3028a1de
```

## 11. Öffentlichen Trust-Anker pinnen

Der Installer vertraut absichtlich nicht automatisch dem Public Key aus einem austauschbaren
Bundle. Der zuvor kontrolliert übertragene Schlüssel wird deshalb separat gepinnt:

```bash
sudo install -d -m 0755 /etc/ybb-edge
sudo install -o root -g root -m 0644 \
  ~/release-public.pem \
  /etc/ybb-edge/release-signing-public.pem

sudo sha256sum /etc/ybb-edge/release-signing-public.pem
```

Auch hier muss der dokumentierte Public-Key-Hash erscheinen.

## 12. Bundle entpacken

```bash
sudo mkdir -p /opt/ybb-edge-bundle-1.0.2
sudo tar -xzf ~/ybb-edge-jetson_orin_nano-1.0.2.tar.gz \
  -C /opt/ybb-edge-bundle-1.0.2

cd /opt/ybb-edge-bundle-1.0.2
```

Das Bundle wird versionsbezogen entpackt. So bleiben frühere Installationsquellen für Diagnose und
Rollback nachvollziehbar.

## 13. Signaturprüfung auf dem Jetson

Vor jeder Installation wird zunächst ausschließlich geprüft:

```bash
sudo bash deployment/edge/install-jetson-offline.sh . --verify-only
```

Erwarteter Abschluss:

```text
Verified OK
Bundle checksums and release signature policy verified.
```

Damit sind sämtliche Dateien aus `SHA256SUMS` sowie dessen RSA-SHA256-Signatur gegen den gepinnten
Trust-Anker geprüft.

## 14. Runtime ohne Netzwerkänderung installieren

Da das Netzwerk bereits fertig eingerichtet ist, wird direkt der Offline-Runtime-Installer
aufgerufen:

```bash
cd /opt/ybb-edge-bundle-1.0.2
sudo bash deployment/edge/install-jetson-offline.sh .
```

Dieser Aufruf führt keine Basisprovisionierung aus. Insbesondere werden NetworkManager, statische
IP, Gateway und DNS nicht geändert.

Der Installer:

1. prüft Prüfsummen und Release-Signatur erneut;
2. legt den Systembenutzer `ybb-edge` an, nimmt ihn in vorhandene `video`-/`render`-Gruppen auf
   und erzeugt die benötigten Verzeichnisse;
3. validiert und installiert `/etc/ybb-edge/device.env`;
4. erzeugt `/opt/ybb-edge/venv` mit Zugriff auf freigegebene Systempakete;
5. installiert alle Python-Abhängigkeiten ausschließlich aus dem signierten Wheelhouse;
6. installiert Edge-Agent, Runtime-Launcher und Watchdog;
7. aktiviert und startet die systemd-Units.

Erwartete Abschlussmeldung:

```text
YBB Edge Runtime installed fully offline.
```

## 15. Dienste und Logs prüfen

```bash
sudo systemctl status ybb-edge-agent.service --no-pager -l
sudo systemctl status ybb-edge-watchdog.timer --no-pager -l

sudo systemctl is-enabled ybb-edge-agent.service
sudo systemctl is-active ybb-edge-agent.service
sudo systemctl is-enabled ybb-edge-watchdog.timer
sudo systemctl is-active ybb-edge-watchdog.timer
```

Agent-Log:

```bash
sudo journalctl -u ybb-edge-agent.service -n 100 --no-pager
```

Der Agent muss auf folgenden Adressen lauschen:

```text
http://172.23.27.245:9443  Edge-Control-API
http://172.23.27.245:9444  Anlagen-/HTTP-Trigger-API
```

## 16. Lokale Agent-Endpunkte prüfen

Auf dem Jetson:

```bash
curl -fsS http://172.23.27.245:9443/edge/v1/identity \
  | python3 -m json.tool

curl -fsS http://172.23.27.245:9443/edge/v1/health \
  | python3 -m json.tool

curl -fsS http://172.23.27.245:9443/edge/v1/capabilities \
  | python3 -m json.tool

curl -fsS http://172.23.27.245:9443/edge/v1/runtime \
  | python3 -m json.tool
```

Für ein neu installiertes, noch nicht belegtes Gerät sind folgende Werte korrekt:

- Health-State `ok`;
- Supervisor-State `idle`;
- keine aktiven Deployment-IDs;
- Slots `A` und `B` im Zustand `empty`;
- keine laufenden Workload-Prozesse;
- Result-Backlog `0`;
- Datenbank-Journalmodus `wal`;
- Speicherdruck `false`;
- Capability `usb_opencv` vorhanden und verfügbar.

Bei der Erstabnahme lief `ybb-edge-agent.service` aktiv, beide Ports waren gestartet und der Agent
meldete `Edge agent and durable runtime are ready.`

## 17. Erreichbarkeit vom Orchestrierungsserver prüfen

Auf dem Server `172.23.27.242` in PowerShell:

```powershell
Invoke-RestMethod 'http://172.23.27.245:9443/edge/v1/identity' |
  ConvertTo-Json -Depth 10

Invoke-RestMethod 'http://172.23.27.245:9443/edge/v1/health' |
  ConvertTo-Json -Depth 10

Invoke-RestMethod 'http://172.23.27.245:9443/edge/v1/capabilities' |
  ConvertTo-Json -Depth 10
```

Diese Prüfung belegt den tatsächlichen Pfad Server → Edge und nicht nur einen lokalen Zugriff auf
dem Jetson.

`configure-ufw.sh` wird erst nach separater Prüfung des SSH-/Wartungszugangs angewendet. Die
Firewall-Aktivierung ist nicht Bestandteil dieser Erstinstallation.

## 18. Edge-Gerät in der Plattform registrieren

In der bestehenden Admin-Web-App:

1. **Edge-Orchestrierung** öffnen.
2. **Gerät registrieren** wählen.
3. Folgende Werte eintragen.

| Feld | Wert |
|---|---|
| Name | `Jetson Orin Nano 01` |
| Technischer Ort | anlagenspezifisch oder leer |
| Edge-Geräte-IP | `172.23.27.245` |
| Orchestrierungsserver-IP | `172.23.27.242` |
| Anlagen-VLAN/CIDR | `172.23.27.0/24` |
| Edge-Control-Port | `9443` |
| HTTP-Trigger-Port am Edge | `9444` |
| RTSP-Port | `8554` |

4. **Registrieren** auswählen.
5. Das Gerät in der Liste öffnen.
6. **Status abrufen** ausführen.

Die Plattform prüft dabei die Edge-Control-Verbindung und übernimmt Health-, Runtime- und
Capability-Snapshots. Die Erstinbetriebnahme ist abgeschlossen, wenn das Gerät in der Liste
erscheint, erreichbar ist und `usb_opencv` als Fähigkeit meldet.

Bei der beschriebenen Erstabnahme wurde das Gerät erfolgreich registriert und anschließend in der
Edge-Geräteliste der Plattform angezeigt.

## 19. USB-Kamera vorbereiten und prüfen

Nach dem Anstecken einer UVC-kompatiblen USB-Kamera auf dem Jetson:

```bash
ls -l /dev/video*
ls -l /dev/v4l/by-id/ 2>/dev/null || true
command -v v4l2-ctl >/dev/null && v4l2-ctl --list-devices
```

Wenn verfügbar, wird ein stabiler Pfad unter `/dev/v4l/by-id/` gegenüber `/dev/video0` bevorzugt,
weil sich numerische Indizes nach Neustart oder Umstecken ändern können.

Der Installer nimmt `ybb-edge` automatisch in vorhandene `video`- und `render`-Gruppen auf.
Zugriff des Runtime-Benutzers prüfen:

```bash
id ybb-edge
sudo -u ybb-edge test -r /dev/video0 && echo 'Kamerazugriff lesbar'
```

Bei einer Installation mit einem älteren Bundle kann die Gruppenzuordnung einmalig nachgeholt
werden:

```bash
sudo usermod -aG video ybb-edge
getent group render >/dev/null && sudo usermod -aG render ybb-edge
sudo systemctl restart ybb-edge-agent.service
```

Direkten OpenCV-Lesetest aus der installierten Runtime durchführen:

```bash
sudo -u ybb-edge /opt/ybb-edge/venv/bin/python - <<'PY'
import cv2

device = "/dev/video0"
capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
try:
    if not capture.isOpened():
        raise SystemExit(f"Kamera konnte nicht geöffnet werden: {device}")
    ok, frame = capture.read()
    if not ok or frame is None:
        raise SystemExit("Kamera lieferte keinen Frame")
    print({"device": device, "width": int(frame.shape[1]), "height": int(frame.shape[0])})
finally:
    capture.release()
PY
```

Anschließend in der Plattform einen Kanal konfigurieren:

- Input-Quelle: **USB camera (OpenCV/UVC)** beziehungsweise `usb_opencv`;
- Gerätepfad: bevorzugt `/dev/v4l/by-id/<stabiler-name>`, alternativ `/dev/video0` oder Index `0`;
- Edge-Platzierung: das registrierte Orin-Nano-Gerät;
- Backend: standardmäßig `auto`, auf Jetson bei expliziter Konfiguration `v4l2`;
- gewünschte Bildgröße, FPS und Runtime-Sampling passend zur Kamera und Pipeline.

Vor dem Start den vorhandenen Kanal-Preflight ausführen. Danach Pipeline veröffentlichen und auf
das Edge-Gerät ausrollen.

## 20. SPS-Schreiben kontrolliert in Betrieb nehmen

Die Gerätekonfiguration enthält die ausdrücklich freigegebene globale Einstellung:

```bash
sudo grep '^YBB_ALLOW_PLC_WRITES=' /etc/ybb-edge/device.env
```

Erwartet:

```text
YBB_ALLOW_PLC_WRITES=true
```

Trotzdem schreibt die Runtime nur, wenn die ausgerollte Pipeline einen aktivierten SPS-Schreibnode
mit gültiger Zielkonfiguration enthält. Für die erste Funktionsprüfung:

1. Pipeline und SPS-Ziel vollständig konfigurieren.
2. Zuerst mit `dry_run=true` ausrollen.
3. Messwert, Datentyp, Byte-/Bitadresse, Endianness, Grenzwerte und Freigabebedingungen prüfen.
4. Diagnose- und Auditresultate kontrollieren.
5. Erst nach dieser Abnahme den konkreten Node auf `dry_run=false` stellen.

Das globale Gate ersetzt keine Pipeline-Freigabe. Ohne gültige Pipelinebedingungen und
ausreichende Ergebnisqualität erfolgt kein Schreibzugriff.

## 21. Folge-Releases und Runtime-Updates

### Basler nachrüsten, ohne den laufenden Jetson-Build neu aufzusetzen

Den laufenden Build fertigstellen lassen. Für Orin Nano und AGX Orin wird der vorhandene
`basler_pylon`-Adapter verwendet. Neue Edge-Pakete enthalten `pypylon`; die Zentrale überträgt
Geräteauswahl, ROI, Bildrate, Belichtung, Gain, GigE-Parameter und Triggerkonfiguration an ihn.
`auto` verwendet auf dem Edge den direkten Pylon-Zugriff. Eine ausdrücklich konfigurierte
`aravis_rtsp_bridge` gehört zum Server und wird im Edge-Preflight abgelehnt.

Aus dem **fertigen, entpackten Bundle** lässt sich mit dem aktuellen Repository-Stand ein
Runtime-Update erzeugen, ohne JetPack, CUDA, TensorRT oder das Betriebssystem neu zu bauen:

**Grafisch im Edge Builder:**

1. Die aktuelle `dist\edge-bundle-builder\YBB-Edge-Bundle-Builder.exe` starten. Falls noch die
   ältere Oberfläche installiert ist, den Builder mit `Build-NativeBundleBuilder.ps1` neu kompilieren
   (siehe [Builder bauen](../../deployment/edge/README.md#native-windows-anwendung)).
2. In den vorhandenen Einstellungen **Python für Build**, **OpenSSL** und gegebenenfalls die
   **HTTPS-Paketquelle** bzw. **Zusätzliche Hersteller-Wheels** prüfen.
3. **Runtime-Update erstellen …** anklicken. Im Dialog über **Archiv …** das fertige Jetson-Build
   (`.tar.gz`) oder Windows-Build (`.zip`) wählen. Über **Ordner …** kann stattdessen das entpackte
   Build mit `platform-profile.json` und `wheelhouse` ausgewählt werden.
4. Eine neue **Release-ID**, eine neue **Update-ZIP** außerhalb des Basis-Builds und den vorhandenen
   privaten RSA-Release-Schlüssel angeben. Der Schlüsselpfad aus der Builder-Konfiguration wird
   vorbelegt. Bei einem verschlüsselten Schlüssel die **Schlüssel-Passphrase** im verdeckten
   Eingabefeld eintragen; bei einem unverschlüsselten Schlüssel bleibt es leer. Für Runtime-Updates
   ist die Signatur immer erforderlich.
5. **Runtime-Update erstellen** ausführen und den Fortschritt im Builder-Protokoll verfolgen.
   Plattform und Basisabhängigkeiten stammen aus dem ausgewählten Build; die Runtime wird aus
   dem aktuellen Repository-Stand erzeugt. Die Zielplattform im Hauptfenster wird dafür nicht verwendet.
6. Die erzeugte ZIP anschließend in der zentralen **Edge-Orchestrierung** importieren und ausrollen.

OpenSSL wird vor dem Entpacken und Neubauen der Runtime gesucht. Bei der Standardeinstellung
`openssl` erkennt der Exporter unter Windows auch die Git-Installation ohne zusätzlichen
PATH-Eintrag. Falls die Erkennung fehlschlägt, unter **Signatur → OpenSSL** den vollständigen
Programmpfad eintragen, beispielsweise `C:\Program Files\Git\usr\bin\openssl.exe`.
Ein `WinError 2` beim Signieren bedeutet, dass das konfigurierte Programm nicht gefunden wurde.
Die pip-Meldung über eine neu verfügbare pip-Version ist dagegen nur ein Hinweis.

Der Exporter prüft den RSA-Schlüssel und die Passphrase ebenfalls vor dem Build. Die Passphrase
wird nur für diesen Auftrag über eine Eingabepipe übertragen und weder in der Konfiguration noch
in Prozessargumenten, Umgebungsvariablen, Protokollen oder im Updatepaket gespeichert. Im
Kommandozeilenweg erlaubt `--ask-signing-passphrase` eine verdeckte interaktive Eingabe.

Wenn bereits der Windows-Dateidialog beim Import meldet **„Sie verfügen nicht über die
Berechtigung, diese Datei zu öffnen“**, ist die ZIP noch nicht zur Zentrale hochgeladen worden.
Ältere Runtime-Exporte konnten die eingeschränkten Zugriffsrechte eines temporären Ordners
behalten. Der korrigierte Exporter übernimmt für neue ZIPs die Rechte des Ausgabeordners.
Für ein bereits erstelltes Paket lassen sich die geerbten Ordnerrechte wiederherstellen
(bei „Zugriff verweigert“ PowerShell als Administrator öffnen):

```powershell
icacls 'C:\Pfad\zum\edge-runtime-update.zip' /reset
```

Den Pfad durch die betroffene Runtime-ZIP ersetzen. Der Befehl ändert nur deren Zugriffsrechte;
Paketinhalt und Signatur bleiben erhalten. Danach dieselbe ZIP erneut zum Import auswählen.

Der Builder verwendet denselben Exporter wie der folgende optionale Kommandozeilenweg. Das
Basis-Build bleibt unverändert. Aus Archiven werden ausschließlich Plattformprofil und Wheels
in ein temporäres Verzeichnis gelesen; OS-Installer werden nicht entpackt.

```powershell
& '.\.tmp\edge312-venv\Scripts\python.exe' `
  '.\scripts\release\build_edge_runtime_update.py' `
  --bundle 'C:\Pfad\zum\entpackten\Jetson-Bundle' `
  --refresh-runtime `
  --output 'C:\Pfad\zum\edge-basler-runtime-update.zip' `
  --release-id 'edge-basler-2026-09-11' `
  --signing-key 'C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-private.pem'
```

`--refresh-runtime` benötigt Internet auf dem Buildrechner für Python-Buildwerkzeuge und das
gegebenenfalls fehlende Pypylon-Wheel. Es baut die internen Python-Pakete aus diesem Checkout,
übernimmt die übrigen Wheels und das exakte Plattformprofil aus dem fertigen Bundle und arbeitet
in einem temporären Verzeichnis. Das Originalbundle bleibt unverändert. Ohne diese Option
exportiert der Befehl weiterhin ausschließlich den bereits im Bundle enthaltenen Codestand.
Für AGX Orin das eigene AGX-Bundle verwenden; die Plattformbindung bleibt erhalten.

Rollout-Reihenfolge:

1. Die Zentrale über den bestehenden Windows-/WSL2-Updatepfad auf denselben Repository-Stand
   aktualisieren. Ein Edge-Paket allein aktualisiert die serverseitige Preflight-Übersetzung nicht.
2. Den laufenden Jetson-Build beziehungsweise die Erstinstallation abschließen. Der installierte
   Launcher muss `software_update_protocol=1` melden. Fehlt er, einmal den bestehenden Edge-Installer
   mit einem aktuellen Bundle und denselben Installations-/Datenpfaden ausführen; kein Neuaufsetzen
   des Jetson-Systems. Siehe [Bootstrap und Rollback](edge-runtime-software-updates.md).
3. Runtime-Paket in **Edge-Orchestrierung** importieren und **Runtime aktualisieren** ausführen.
   Der Dienstwechsel unterbricht die Kameraaufnahme kurz; Modelle, Engine-Cache, Kalibrierungen
   und persistente Daten bleiben in den vorhandenen Datenverzeichnissen.
4. **Status abrufen**. `camera_adapters` muss `basler_pylon` enthalten. In
   `/etc/ybb-edge/device.env` muss `YBB_EDGE_CAMERA_ADAPTERS` den Adapter ebenfalls freigeben.
   Eine geänderte Freigabeliste erfordert einen Dienstneustart.
5. Im Kanal **Basler Pylon** auswählen, bevorzugt `serial_number` oder `ip_address` setzen und
   `transport_mode=pylon_direct` verwenden. Kameraadressen beziehen sich auf das Jetson-Netz.
   Bei mehreren Kameras ist eine eindeutige Auswahl erforderlich.
6. Preflight neu erstellen, Kanal deployen und Livebild sowie Inferenzergebnisse prüfen.

Basler liefert Pypylon-Binärpakete für Linux AArch64 inklusive Pylon-Runtime. Für USB3 müssen
zusätzlich die zum SDK gehörenden udev-Regeln auf dem Jetson installiert sein und dem Dienstkonto
`ybb-edge` Zugriff erlauben. Dafür die Linux-ARM64-Installation von Basler bzw. deren
`setup-usb.sh` verwenden; danach Kamera erneut verbinden. Diese Host-Einrichtung ist kein
Python-Runtime-Update und erfordert keinen neuen Jetson-Build. Bei GigE müssen Kamera und
Jetson-Schnittstelle erreichbar sein; Paketgröße und Linkbudget müssen zur Verbindung passen.
Quellen: [Pypylon-Installation](https://docs.baslerweb.com/pypylon-installation-and-environment-setup),
[Basler Linux-Installation](https://docs.baslerweb.com/software-installation-%28linux%29),
[USB-udev-Hinweis](https://github.com/basler/pypylon#known-issues).

Die Capability-Meldung bestätigt ein ladbares SDK, auch wenn noch keine Kamera angeschlossen ist.
Sie bestätigt noch keine erfolgreiche Aufnahme. Hardwareabnahme auf **beiden** Jetson-Modellen:
Geräteauswahl, Bildfarben/ROI/FPS, Inferenz, Trigger-Wartezeit, Wiederanlauf nach Kameratrennung
sowie Runtime-Update und Rollback mit aktivem Kanal prüfen. Diese Live-Tests sind auf dem
Entwicklungsrechner nicht möglich.

### Weitere Vollbundles

Für jedes neue Vollbundle:

1. `release_version` erhöhen; eine veröffentlichte Versionsnummer nicht mit anderem Inhalt erneut
   verwenden.
2. Ein neues, leeres Wheelhouse-Verzeichnis konfigurieren.
3. `prepare-wheelhouse` einmal online ausführen.
4. Builder-Tests ausführen.
5. Bundle mit demselben dauerhaften privaten Schlüssel signieren.
6. Bundle lokal gegen den externen Public Key verifizieren.
7. Auf dem Jetson `--verify-only` ausführen und anschließend installieren.

Solange derselbe Release-Schlüssel verwendet wird, bleibt dieser Trust-Anker unverändert:

```text
/etc/ybb-edge/release-signing-public.pem
```

Für kleinere zentral verteilte Runtime-Updates wird aus einem entpackten, plattformgerechten
Vollbundle ein signiertes Runtime-Paket erzeugt:

```powershell
& '.\.tmp\edge312-venv\Scripts\python.exe' `
  '.\scripts\release\build_edge_runtime_update.py' `
  --bundle 'C:\Pfad\zum\entpackten\Jetson-Bundle' `
  --output 'C:\Pfad\zum\edge-runtime-update.zip' `
  --release-id 'edge-runtime-<version>' `
  --signing-key 'C:\Users\straspe\YBB-ReleaseKeys\production-v1\release-private.pem'
```

Der zentrale Server benötigt denselben öffentlichen Trust-Anker unter:

```text
/srv/cv-platform/config/edge-release-public.pem
```

Dieser Schlüssel muss einmalig auch in der **Zentrale** eingerichtet werden; die Installation
auf dem Jetson allein reicht nicht aus. Dazu `release-public.pem` aus dem verwendeten
Release-Schlüsselpaar auf den zentralen Windows-Server kopieren, beispielsweise als
`C:\Temp\edge-release-public.pem`. Der private Schlüssel bleibt auf dem Buildrechner.

Auf dem zentralen Windows-Server in PowerShell den öffentlichen Schlüssel in die vorhandene
WSL-Runtime übernehmen (gegebenenfalls den tatsächlich verwendeten Distributionsnamen einsetzen):

```powershell
wsl -d YBB-Inferenzserver -u root -- install -D -m 0644 /mnt/c/Temp/edge-release-public.pem /srv/cv-platform/config/edge-release-public.pem
wsl -d YBB-Inferenzserver -u root -- openssl pkey -pubin -in /srv/cv-platform/config/edge-release-public.pem -noout
wsl -d YBB-Inferenzserver -u root -- sha256sum /srv/cv-platform/config/edge-release-public.pem
```

Die SHA-256-Prüfsumme muss mit `Get-FileHash` der ursprünglichen öffentlichen PEM-Datei auf
dem Buildrechner übereinstimmen. Ein abweichender Speicherort kann über
`YBB_EDGE_TRUSTED_RELEASE_KEY` in der Dienstumgebung konfiguriert werden. Wird nur die Datei
am bereits konfigurierten Pfad installiert, ist kein Dienstneustart erforderlich:
Die Importprüfung liest sie bei jedem Versuch neu.

Bei einer fehlenden oder unlesbaren Schlüsseldatei meldet die Importprüfung den betroffenen
Pfad. Bei einem falschen Schlüssel oder einer beschädigten Signatur wird das Paket abgelehnt.
Das vorhandene Paket muss nach Einrichtung des passenden öffentlichen Schlüssels nicht neu
gebaut werden; **Paket prüfen** beziehungsweise den Import erneut ausführen.

Danach wird das Paket unter **Edge-Orchestrierung** importiert und über **Runtime aktualisieren**
gezielt auf kompatible Geräte ausgerollt. Ein Import allein startet keinen Rollout.

Bleibt **Runtime aktualisieren** ausgegraut, stehen die Kompatibilitätsgründe unter den
Geräteaktionen. Die CUDA-/TensorRT-Anforderungen gelten mit der Genauigkeit des signierten
Buildprofils: CUDA `13.2` akzeptiert beispielsweise die gemessene Version `13.2.2`,
TensorRT `10.16` die Version `10.16.2.10`. Explizit angegebene Patch-/Buildkomponenten müssen
übereinstimmen; `13.2.1` passt nicht zu `13.2.2`. Fehlende oder ungültige Versionsangaben
erfüllen eine geforderte Version nicht. Plattform, Architektur und Python-Basisversion
werden weiterhin geprüft. Diese Regel ändert keine installierten NVIDIA-Komponenten.

Ältere Zentralen verglichen die Versionszeichenfolgen vollständig und sperrten dadurch
auch passende Geräte mit genauer gemeldeten Patchversionen. Nach Aktualisierung der Zentrale
auf den korrigierten Stand die Orchestrierungsseite neu laden und **Status abrufen** ausführen.
Das bereits importierte Runtime-Paket bleibt verwendbar. Beim vorhandenen profilbasierten
Jetson-Installer enthält die Launcher-Konfiguration dieselben Versionsreihen wie das Paket;
die genaueren Messwerte stammen aus der Geräteinventarisierung.

### Kanal wartet auf die Engine-Vorbereitung

Meldet dagegen die Zentrale bereits `placement.edge_deployment_status="running"`, während
der Kanal weiterhin `desired_state="start_requested"`/`starting` zeigt und der
Edge-Deployment-Controller keinen aktiven Auftrag hat, kann ein alter Wiederholungsfehler
vorliegen: Eine erneute Anfrage mit demselben Idempotenzschlüssel setzte den Kanal zurück
auf den Anforderungsstatus, obwohl der Auftrag schon abgeschlossen war.

Die korrigierte Zentrale gibt bei solchen Wiederholungen den bestehenden Auftrag zurück,
ohne den Kanalstatus erneut zu verändern. Der Controller repariert bereits betroffene
Edge-Kanäle automatisch, wenn der letzte Steuerungsauftrag erfolgreich ist und genau zum
aktuell eingetragenen, laufenden Deployment desselben Kanals und Geräts gehört. Ein neuerer
Start-/Stoppauftrag oder ein fehlgeschlagenes Deployment wird dabei nicht übergangen.
Hierfür nur die Zentrale aktualisieren; weder Jetson-Neustart noch ein neues Runtime-Paket
oder ein zusätzlicher Kanalstart sind zur Statusreparatur nötig.

Controllerstatus im angemeldeten Browser:
`/runtime-api/api/v1/runtime/edge-deployment-controller`.

Wenn der Kanal `starting` und sein aktuelles Edge-Deployment `preparing` meldet, kann ein
TensorRT-Build laufen. Ein älteres Deployment desselben Kanals kann währenddessen weiterhin
`running` sein. Deshalb immer die Deployment-ID der Zentrale mit dem Jetson abgleichen.
Die zentrale Statusadresse beginnt beim WSL-Installer mit
`/runtime-api/api/v1/runtime/channels/<channel_id>/status`.

Auf dem Jetson zeigt `pgrep -af '[t]rtexec'`, ob ein Engine-Build-Prozess existiert. Die
Vorbereitung hat standardmäßig ein Build-Zeitlimit von 900 Sekunden. Wiederholte Neustartaufträge
während dieser Phase vermeiden. Ein laufender Prozess allein bestätigt noch keine erfolgreiche
Kamerabildverarbeitung.

Der korrigierte Edge-Agent verwendet fertige Engines mit identischem Build-Schlüssel auch über
Workload-Releases hinweg wieder. Der Schlüssel berücksichtigt Modellinhalt, Präzision,
Eingangsgrößen, Kalibrierung und den Plattform-/Builder-Namensraum. Die Engine wird in das neue
Release-Verzeichnis kopiert und vor der Aktivierung weiterhin geprüft. Dadurch erfordert eine
Änderung der übrigen Pipeline- oder Kameraeinstellungen keinen erneuten Build derselben Engine.

Bei einem erforderlichen `trtexec`-Build liegt dessen laufendes Protokoll neben der Engine als
`/var/lib/ybb-edge/engines/<workload_release_id>/<node_key>/<build_key>.build.log`.
Es kann mit `tail -f <Pfad>` verfolgt werden. Bei Fehler oder Zeitüberschreitung bleibt das
Protokoll erhalten; eine unvollständige `.tmp`-Engine wird entfernt. Diese Änderungen am
Edge-Agent werden mit einem neuen Runtime-Update ausgerollt; ein neuer Vollbuild ist nicht nötig.

### Abhängigkeitsprüfung bei vorhandenen Jetson-Installationen

Die Update-Umgebung verwendet `--system-site-packages`, damit die von JetPack bereitgestellten
NVIDIA-/GStreamer-Anbindungen sichtbar bleiben. Ein globales `pip check` erfasst dadurch auch
unbeteiligte Systempakete. Beispielsweise können `scipy 1.11.4` mit `numpy<1.28` und
`pynacl 1.5.0` mit fehlendem `cffi` ein Runtime-Update blockieren, obwohl keine dieser beiden
Bibliotheken von der Edge-Runtime benötigt wird.

Der Launcher prüft jetzt alle Pakete aus dem signierten Manifest und deren vollständige
Abhängigkeitskette einschließlich Extras, Plattformmarkern und Python-Version. Vorhandene
TensorRT-/PyGObject-Systemanbindungen werden ebenfalls geprüft. Fehlende oder inkompatible
Runtime-Abhängigkeiten verhindern weiterhin die Aktivierung. Systempakete werden dabei nicht
geändert. Die anschließenden Import- und Gesundheitsprüfungen bleiben bestehen.

Für einen bereits installierten Launcher mit globalem `pip check` kann das kleine
Wartungsarchiv `dist\edge\ybb-edge-dependency-fix.tar.gz` direkt auf den Jetson übertragen
werden. Auf dem Entwicklungsrechner in PowerShell:

```powershell
scp 'C:\KI\YBB_Inferenzserver\dist\edge\ybb-edge-dependency-fix.tar.gz' peter@172.23.27.245:~/
```

Auf dem Jetson:

```bash
tar -xzf ~/ybb-edge-dependency-fix.tar.gz -C ~
sudo python3 ~/ybb-edge-dependency-fix/repair-runtime-dependency-check.py
sudo systemctl restart ybb-edge-agent.service
```

Benutzername, IP und bei Bedarf `--install-root` anpassen. Das Reparaturskript sichert den
Launcher als `/opt/ybb-edge/edge-runtime-launcher.before-dependency-check.py` und ersetzt
ausschließlich dessen Aufruf der Abhängigkeitsprüfung. Es übernimmt keine neuen Imports aus
einer eventuell noch nicht installierten `cv-edge`-Version. Beim Dienstneustart pausiert die
Kanalverarbeitung kurz. Danach in der Zentrale **Status abrufen** und dasselbe Runtime-Paket
erneut über **Runtime aktualisieren** ausrollen. Ein neuer Vollbuild ist dafür nicht erforderlich.

## 22. Abschlusscheckliste

- [ ] Jetson-Softwareprofil entspricht `jetson-orin-nano.json`.
- [ ] Edge-IP `172.23.27.245/24` und Server-IP `172.23.27.242` sind erreichbar.
- [ ] `/var/lib/ybb-edge` liegt auf dem vorgesehenen persistenten Datenträger.
- [ ] Privater Release-Schlüssel ist verschlüsselt, gesichert und außerhalb des Repositorys.
- [ ] Public-Key-SHA-256 ist dokumentiert und auf dem Edge gepinnt.
- [ ] Wheelhouse enthält alle CPython-3.12-/AArch64-Wheels einschließlich `uvloop`.
- [ ] Bundle-Hash ist dokumentiert.
- [ ] Lokale und geräteseitige Signaturprüfung melden `Verified OK`.
- [ ] `ybb-edge-agent.service` läuft und ist aktiviert.
- [ ] `ybb-edge-watchdog.timer` läuft und ist aktiviert.
- [ ] Health-State ist `ok`, Backlog ist `0`, Speicherdruck ist `false`.
- [ ] Server kann `/identity`, `/health` und `/capabilities` auf Port `9443` abrufen.
- [ ] Gerät ist in der Plattform registriert und **Status abrufen** ist erfolgreich.
- [ ] Capability `usb_opencv` ist verfügbar.
- [ ] USB-Kamera ist für Benutzer `ybb-edge` lesbar und liefert einen OpenCV-Frame.
- [ ] SPS-Schreibgate entspricht der freigegebenen Betriebsphase.
- [ ] Erster SPS-Test erfolgt als `dry_run`, bevor reale Schreibzugriffe aktiviert werden.
