# Offline-Deployment auf Windows

Edge-Runtime-Updates können beim zentralen Updateexport mitgeliefert und auf dem
Offline-Server automatisch für den Frontend-Rollout bereitgestellt werden:
[Bedienung und Paketprüfung](../docs/operations/central-offline-edge-updates.md).

Dieses Verzeichnis enthaelt zwei Deployment-Schienen:

1. **Docker-frei fuer Zielgeraete**: Windows-Installer/Updater mit vorbereiteter WSL2-Runtime. Das ist der empfohlene Weg fuer diese Anwendung, wenn das Zielgeraet keine Internetverbindung hat und persistente Daten erhalten bleiben sollen.
2. **Docker-Bundle**: die bisherige generische Docker-Image-Schiene weiter unten. Sie bleibt erhalten, wird fuer die neue Zielgeraete-Installation aber nicht benoetigt.

## Docker-freier Windows/WSL2 Offline-Installer

### Empfohlener Ablauf mit Windows-UI

Dieser Abschnitt ist der normale Weg fuer deinen aktuellen Zielzustand:

- auf dem Entwicklungsrechner weiterentwickeln
- daraus einen Installer oder spaeter ein Update-ZIP bauen
- das ZIP per USB-Stick oder Netzwerkfreigabe auf den Offline-Zielrechner kopieren
- auf dem Zielrechner den bestehenden Offline-Installationsweg verwenden
- Zugriff immer per IP-Adresse, ohne DNS- oder Hosts-Aenderung und ohne Sonderport

#### 0. UI auf dem Entwicklungsrechner bauen/starten

Die UI-EXE ist ein kleiner Wrapper um die PowerShell-Skripte. Nach Aenderungen
an der UI oder an den Deployment-Skripten sollte sie neu gebaut werden:

```powershell
cd C:\KI\YBB_Inferenzserver
Set-ExecutionPolicy -Scope Process Bypass
.\deployment\build-windows-ui-exe.ps1
.\deployment\YBB-Deployment-UI.exe
```

Die gemeinsame UI laeuft auf dem Entwicklungsrechner mit Internetzugang und
beginnt mit einer Aufgabenuebersicht:

- `Zentrale: Paket`: Installer oder Update mit erklaerten Profilen erstellen
- `Edge`: bestehenden Bundle-Builder im selben Fenster verwenden; Runtime-Updates und RSA-Schluessel verwalten
- `Modelle`: Modellpakete herunterladen und fuer den Offline-Server exportieren

Im Repository sind lokale Serveraktionen ausgeblendet und deaktiviert. Die fertigen
Pakete werden uebertragen und am Ziel ueber den bestehenden Installationsweg
eingespielt. Es gibt keine Ferninstallation durch diese UI. Die lokalen
Zielsystem-Funktionen bleiben fuer bestehende Offlinepakete verfuegbar.

Der [Ablauf fuer gemeinsame Updates und RSA-Vertrauen](../docs/operations/deployment-workspace.md)
beschreibt den normalen Weg ohne manuelle PowerShell-Kommandos. Die folgenden
aelteren Bezeichnungen `Installation` und `Update` beschreiben weiterhin die
lokalen Zielsystem-Abläufe. `Export` entspricht `Zentrale: Paket` auf dem
Entwicklungsrechner. Einzelne Export-Checkboxen liegen jetzt unter
`Erweiterte Einstellungen` und sind im Profil `Benutzerdefiniert` veraenderbar.

Das Profil `Anwendungsupdate` verwendet die installierte Serverbasis weiter:
MediaMTX, APT, Training-Runtime, GStreamer und Triton werden standardmaessig
uebersprungen; `PLC-Schreiben erlauben` ist aktiv. APT-/Training-Vorbereitung
findet nur beim Installerbau statt. Anwendung, Frontend und das Python-Wheel-Paket
bleiben im Update enthalten. Bei neuen Systemabhaengigkeiten oder fehlenden
Komponenten unter `Benutzerdefiniert` die betreffenden Skip-Optionen deaktivieren.

Unten in der UI bleiben Status und Lauf-Log sichtbar. Wenn ein Vorgang laeuft,
schliesst sich das Fenster nicht automatisch. Die angezeigte `Lauf-Logdatei`
kann fuer Diagnosezwecke direkt geoeffnet werden.

#### 1. Erst-Installer auf dem Entwicklungsrechner exportieren

In der UI den Reiter `Export` verwenden.

Empfohlene Felder fuer den ersten Installer:

```text
Repo-Pfad:          C:\KI\YBB_Inferenzserver
Config optional:   leer lassen, wenn keine eigene windows-offline.config.ps1 genutzt wird
App-Name:          ybb-inferenzserver
Version:           auto
Ziel-Basisordner:  C:\YBB_Inferenzserver
Distro:            YBB-Inferenzserver
IP/Host:           127.0.0.1 oder die spaetere IP des Zielrechners
Port:              443
Frontend bauen:    aktiviert
Triton-Check ueberspringen: aktiviert, solange kein Triton-Archiv mitgeliefert wird
```

Dann `Installer exportieren` klicken.

Der erste Lauf kann lange dauern, weil die interne Builder-WSL und die
Python-/System-Runtime vorbereitet werden. Danach kann fuer weitere Installer-
Exporte `Runtime-Vorbereitung ueberspringen` aktiviert werden, solange sich an
nativen Runtime-Abhaengigkeiten nichts geaendert hat.

Nach Erfolg liegt das Installer-ZIP hier:

```text
C:\KI\YBB_Inferenzserver\dist\ybb-inferenzserver-<version>-windows-installer.zip
```

Das ZIP kann sehr gross sein, weil es die vorbereitete WSL2-Runtime enthaelt.

#### 2. Installer-ZIP auf den Offline-Zielrechner kopieren

Beispiel per USB-Stick oder Netzwerkfreigabe:

```text
D:\incoming\ybb-inferenzserver-<version>-windows-installer.zip
```

Auf dem Zielrechner das ZIP in ein temporaeres Installationsverzeichnis
entpacken:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
New-Item -ItemType Directory -Path C:\YBB_Installer -Force
Expand-Archive "D:\incoming\ybb-inferenzserver-<version>-windows-installer.zip" -DestinationPath C:\YBB_Installer -Force
```

#### 3. Erstinstallation auf dem Offline-Zielrechner ausfuehren

PowerShell oder Explorer idealerweise als Administrator starten. Administrator-
rechte sind fuer den Windows-Portproxy und die Firewallregel wichtig; ohne
Administratorrechte kann die Installation laufen, der IP-Zugriff muss dann
spaeter nachgezogen werden.

Variante mit UI:

```powershell
cd C:\YBB_Installer
.\YBB-Deployment-UI.exe
```

Im Reiter `Installation`:

```text
Bundle-ZIP optional: leer lassen, wenn die UI aus dem entpackten Installer gestartet wurde
Installationsbasis: C:\YBB_Inferenzserver
Distro:             YBB-Inferenzserver
IP/Host:            IP-Adresse des Zielrechners, z.B. 192.168.1.50
Port:               443
Windows-Portproxy einrichten: aktiviert
Hosts-Datei nicht aendern:    aktiviert
```

Dann `Installieren` klicken.

Variante ohne UI:

```powershell
cd C:\YBB_Installer
.\install-windows-offline.ps1 -PublicAddress "192.168.1.50"
```

Wenn der Zielrechner nur lokal genutzt wird, kann statt der LAN-IP auch
`127.0.0.1` verwendet werden.

Nach der Installation liegt die Anwendung standardmaessig hier:

```text
C:\YBB_Inferenzserver\ybb-inferenzserver
```

#### 4. Anwendung oeffnen

Lokal auf dem Zielrechner:

```text
https://127.0.0.1/
https://127.0.0.1/frontend/
```

Von einem anderen Rechner im selben Netzwerk:

```text
https://<Zielrechner-IP>/
https://<Zielrechner-IP>/frontend/
https://<Zielrechner-IP>/label-studio/
```

Es wird kein interner DNS-/Hosts-Name verwendet. Der Zugriff laeuft ueber
Windows-Portproxy auf die WSL2-Distribution. Wenn der Zugriff nicht funktioniert
oder die Ziel-IP gewechselt hat, die UI auf dem Zielrechner starten und im
Reiter `Installation` auf `Portproxy setzen` klicken. Alternativ:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\updater\configure-windows-portproxy.ps1 -DistroName "YBB-Inferenzserver" -ListenPort 443 -ConnectPort 443
```

Das initiale Admin-Passwort liegt im Zielsystem in der WSL-Distribution:

```powershell
wsl -d YBB-Inferenzserver -u root -- cat /srv/cv-platform/secrets/bootstrap_admin_initial_password.txt
```

#### 5. Spaetere Updates exportieren und offline einspielen

Auf dem Entwicklungsrechner die UI starten und im Reiter `Export` auf
`Update exportieren` klicken. Dabei werden nur Anwendungscode, Frontend,
Migrationsskripte und der Windows-Updater gepackt; die grosse WSL-Runtime wird
nicht erneut exportiert.

Wichtig: Im Reiter `Export` fuer Updates dieselbe Zieladresse eintragen, unter
der die Anwendung spaeter erreichbar sein soll, also z.B.:

```text
IP/Host: 172.23.27.242
Port:    443
```

Diese Adresse wird auch fuer Label Studio verwendet. So erzeugt Label Studio
Links auf `https://<Zielrechner-IP>/label-studio/` und faellt nicht auf
`https://localhost/label-studio/` zurueck.

Das Ergebnis liegt unter:

```text
C:\KI\YBB_Inferenzserver\dist\ybb-inferenzserver-<version>-windows-update.zip
```

Wenn `Triton-Check ueberspringen` in der UI deaktiviert ist, enthaelt dieses
Update-ZIP zusaetzlich `runtime/tritonserver.tar.gz`. Wenn in
`deployment\windows-offline.config.ps1` kein `$TritonServerArchivePath` gesetzt
ist, erzeugt der Export das Archiv automatisch aus dem Docker-Image
`$TritonServerImage`, standardmaessig
`nvcr.io/nvidia/tritonserver:24.08-py3`, und cached es unter
`.runtime\triton`. Beim Einspielen wird Triton in der bestehenden
WSL-Distribution nach `/opt/tritonserver` installiert und die systemd-Units
`ybb-triton-rt.service` und `ybb-triton-batch.service` werden angelegt.
Wenn das Feld `Config optional` in der UI leer bleibt, verwendet die UI die
Repo-Default-Config `deployment\windows-offline.config.ps1`. Der Triton-Pfad
kann dort optional gesetzt werden; wenn er leer bleibt, wird das Docker-Image
als Quelle verwendet.

Livestreams nutzen genau einen Produktionspfad: Kamera-Worker, Shared-Memory-
Ring, NVENC, interner RTSP-Ingest zu MediaMTX und WHEP/WebRTC ueber TURN/TCP.
In Windows/WSL-Offline-Installationen laeuft MediaMTX direkt auf Windows, damit
der interne RTSP-Ingest, WHEP und ICE nicht ueber `netsh portproxy` zur privaten
WSL-Adresse muessen. Coturn laeuft in WSL und stellt den Browsertransport auf
TCP 3478 bereit. HLS, MJPEG und CPU-Encoding sind deaktiviert.
Fuer Offline-Installationen und Updates kann in
`deployment\windows-offline.config.ps1` ein vorbereitetes MediaMTX-Windows-ZIP
gesetzt werden:

```powershell
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
$PlcWritesAllowed = $false
$GStreamerDebBundlePath = ""
$PrepareRuntimeSkipGStreamerCheck = $false
$GStreamerDebBundleCacheDir = ".runtime\apt\gstreamer"
```

Wenn `MediaMtxArchivePath` leer ist und `PrepareRuntimeSkipMediaMtxCheck=false`
bleibt, laedt der Export das passende Windows-amd64 Release-ZIP automatisch
nach `.runtime\mediamtx` und verwendet den Cache fuer Installer und Updates.
Das Export-Skript packt es dann als `runtime/mediamtx.zip` ein, schreibt die
SHA256-Pruefsumme in `windows-bundle-manifest.json` und nimmt es in
`manifest.sha256` auf. Der Updater installiert daraus eine Windows-Autostart-
Aufgabe `YBB-Inferenzserver MediaMTX`, schreibt `config\mediamtx.yml`, entfernt
alte MediaMTX-Portproxy-Regeln und oeffnet die Windows-Firewall fuer TCP
`3478`, `8554`, `8889`, `8189` und `9997` sowie UDP `8189` fuer das lokale
Relay zu MediaMTX.
Die MediaMTX-API auf `9997` wird dabei nicht anonym freigeschaltet: der
Installer erzeugt interne Credentials unter `secrets\mediamtx_api_*` sowie ein
TURN-Shared-Secret, schreibt `config\mediamtx-client.env` und uebergibt diese
an WSL. MediaMTX erzeugt daraus kurzlebige Browser-Credentials; das Shared
Secret wird nie an den Browser gesendet.
Wenn ein Update-Bundle MediaMTX bewusst ueberspringt, startet der Updater ein
bereits vorhandenes Windows-MediaMTX und prueft dessen API/WHEP-Ports. Ist kein
vorhandenes MediaMTX installiert oder startbar, bricht das Update ab statt einen
erfolgreichen Zustand ohne Livestream-Fanout zu melden.
`wsl/apply-release.sh` deaktiviert den alten `ybb-mediamtx.service`, setzt die
Stream-Defaults in `/etc/ybb-inferenzserver/runtime.env` auf den Windows-Host
und fuehrt einen verpflichtenden NVENC/RTSP-Publish/Read-Smoke-Test aus.
Stream-Autorisierung ist bewusst standardmaessig deaktiviert; RTSP/WebRTC-URLs
sind damit im LAN offen, bis explizite Stream-Credentials implementiert und
abgesichert werden. Setzt ein Bundle heute `StreamAuthEnabled=true`, blockiert
der Offline-Planer bzw. `apply-release.sh` die Anwendung statt scheinbar
geschuetzte, aber offene Streams zu installieren.

In `YBB-Deployment-UI.exe` gibt es im Export-Tab eine Checkbox
`MediaMTX ueberspringen` und ein Feld `MediaMTX-Archiv`. Wenn die Checkbox aus
bleibt, wird ein gesetzter Archivpfad verwendet oder das Archiv automatisch nach
`.runtime\mediamtx` geladen und mitgebuendelt. Wenn die Checkbox gesetzt ist,
wird MediaMTX nicht in Installer/Update aufgenommen, auch bei einem gespeicherten
Archivpfad in der UI oder Basis-Konfiguration. Im Profil `Anwendungsupdate` ist
die Checkbox standardmaessig gesetzt und vorhandenes MediaMTX wird weitergenutzt.

GStreamer wird ebenfalls ueber den bestehenden Offline-Updater mitgefuehrt.
Wenn `PrepareRuntimeSkipGStreamerCheck=false` bleibt und
`GStreamerDebBundlePath` leer ist, erzeugt der Export aus der Builder-WSL ein
Debian-Paketbundle unter `.runtime\apt\gstreamer` und packt es als
`runtime/gstreamer-debs.tar.gz` in Installer/Update. `apply-release.sh`
installiert daraus die benoetigten GStreamer-Tools und Plugins offline, bevor
der MediaMTX-Publish/Read-Smoke-Test und der Runtime-Start laufen. In der
Deployment-UI gibt es dazu die Checkbox `GStreamer ueberspringen`. Bei einer
vollstaendigen bestehenden Installation ist sie im Profil `Anwendungsupdate`
gesetzt. Fuer Erstinstallation, fehlende Plugins oder neue Systemabhaengigkeiten
muss das Paket mitgeliefert werden. Das Bundle muss insbesondere
`gstreamer1.0-rtsp` enthalten, weil der Produktionspublisher und der
MediaMTX-Smoke-Test das Element `rtspclientsink` verwenden. Fuer GPU-Encoding
braucht der GStreamer-Pfad ausserdem den modernen CUDA-Encoder
`nvcudah264enc` und `cudaupload`; wenn `nvcudah264enc`, `cudaupload` oder
`cudaconvert` fehlen oder der reale
Low-Latency-NVENC-Smoke-Test scheitert, wird der Stream als fehlgeschlagen
markiert und das Update bricht ab. Es gibt weder einen CPU-Encoder noch einen
alternativen Videopfad. Einige GStreamer-Versionen melden `WARNING: erroneous
pipeline` trotz Exitcode `0`; dieser stderr-Inhalt wird beim Smoke-Test als
Fehler gewertet, damit solche Profile nicht in Produktion gehen.
Der installierte `ybb-python-service` setzt vor dem Start der Runtime-Services
zusaetzlich WSL-/CUDA-/Python-NVIDIA-Library-Pfade im `LD_LIBRARY_PATH`, damit
`nvcudah264enc` die NVIDIA-Encode-Libraries finden kann. Der Legacy-Encoder
`nvh264enc` wird bewusst nicht verwendet, weil NVIDIA-Treiber ab Version 590
dessen alte NVENC-Presets nicht mehr unterstuetzen.

Runtime-Python-Abhaengigkeiten, die nachtraeglich in die Service- oder
Training-/Deployment-Venv muessen, werden ueber `runtime/python-wheels.tar.gz`
mitgefuehrt. Das ist fuer `python-snap7` erforderlich, wenn der
`Write to Siemens SPS` Runtime Node per Offline-Update ausgeliefert wird, und
fuer Deployment-Packaging-Abhaengigkeiten wie `mmdeploy`,
`tensorrt-cu12-bindings`, `onnx` und `onnxruntime`, wenn ein Modell erst beim
Deploy aus ONNX in ein TensorRT-Serving-Paket gebaut wird. Das
TensorRT-Binding ist das Python-Modul, das MMDeploy beim Engine-Build erwartet;
die nativen `libnvinfer`/`libnvonnxparser` Libraries kommen weiterhin aus dem
Triton-Archiv bzw. der vorbereiteten Runtime. Wenn kein Archiv ueber
`PythonWheelBundlePath` oder
`.runtime\python-wheels\python-wheels.tar.gz` vorhanden ist, laden die
Export-Skripte auf dem Entwicklungsrechner automatisch die Linux-Wheels fuer
die Service-Requirements (`requirements.txt`) und die
Deployment-Requirements (`deployment-requirements.txt`) per `pip download`
herunter und packen sie in das Update. Der Zielrechner bleibt offline:
`apply-release.sh` installiert das Bundle vor dem Neustart der Services mit
`pip --no-index` in die jeweilige Venv; falls eine Venv kein `pip` enthaelt,
verwendet das Skript `uv pip --python` oder bootstrapped `pip` ueber
`ensurepip`. Danach prueft das Update die Deployment-Packaging-Imports, damit
fehlende Offline-Runtime-Wheels vor dem naechsten Deploy sichtbar werden.

Der Siemens-SPS-Schreibknoten bleibt zusaetzlich ueber die Runtime-Umgebung
gesperrt. Fuer echte PLC-Schreibzugriffe muss im Export/Installer bewusst
`$PlcWritesAllowed = $true` gesetzt werden; in der Deployment-UI entspricht das
der Checkbox `PLC-Schreiben erlauben`. Der Updater schreibt daraus
`CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=true` in
`/etc/ybb-inferenzserver/runtime.env`. Ohne diese Freigabe melden konfigurierte
SPS-Schreibzeilen `environment_blocked`. Node und Mapping muessen trotzdem
explizit aktiviert sein und `dry_run` muss ausgeschaltet werden.

Dieses Update-ZIP auf den Offline-Zielrechner kopieren, am einfachsten nach:

```text
C:\YBB_Inferenzserver\ybb-inferenzserver\incoming
```

Dann auf dem Zielrechner eine der beiden Varianten starten:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\YBB-Deployment-UI.exe
```

Im Reiter `Update` kann das Feld `Update-ZIP optional` leer bleiben, wenn das
ZIP im Ordner `incoming` liegt. Der Updater nimmt dann automatisch das neueste
`*-windows-update.zip`.

Wenn die Ziel-IP korrigiert oder nachgezogen werden soll, im Reiter `Update`
zusaetzlich setzen:

```text
IP/Host optional: <Zielrechner-IP>
Port:             443
```

Oder ohne UI:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\update-ybb-inferenzserver.cmd
```

Wenn ein vorhandener alter Updater sofort mit `Die Variable "Host" kann nicht
ueberschrieben werden` abbricht, wurde noch der alte installierte
`update-windows-offline.ps1` gestartet. Fuer diesen einmaligen Bootstrap:

1. Das frisch exportierte `*-windows-update.zip` auf dem Zielrechner in einen
   temporaeren Ordner entpacken, z.B. `C:\YBB_Update`.
2. `C:\YBB_Update\YBB-Deployment-UI.exe` starten, also die UI aus dem
   entpackten Update-Bundle, nicht die installierte UI.
3. Im Reiter `Update`:

```text
App-Verzeichnis:     C:\YBB_Inferenzserver\ybb-inferenzserver
Update-ZIP optional: leer lassen
Distro optional:     YBB-Inferenzserver
IP/Host optional:    172.23.27.242
Port:                443
```

4. `Update einspielen` klicken.

Nach diesem erfolgreichen Bootstrap ist der installierte Updater aktualisiert.
Spaetere Updates koennen wieder normal ueber das installierte
`YBB-Deployment-UI.exe` und den Ordner `incoming` eingespielt werden.

Das Update kann gestartet werden, waehrend die Anwendung laeuft. Der Updater
stoppt die betroffenen Services kurz, spielt das neue Release ein, fuehrt
Migrationen aus, aktualisiert den installierten Updater selbst und startet die
Services wieder. Persistente Daten unter `/srv/cv-platform` bleiben erhalten.

Triton nach einem Installer oder Update auf dem Zielgeraet pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- test -x /opt/tritonserver/bin/tritonserver
wsl -d YBB-Inferenzserver -u root -- /opt/tritonserver/bin/tritonserver --version
wsl -d YBB-Inferenzserver -u root -- systemctl status ybb-triton-rt.service ybb-triton-batch.service --no-pager
wsl -d YBB-Inferenzserver -u root -- curl -sS http://127.0.0.1:8000/v2/health/ready
```

### Zielbild

Der Entwicklungsrechner bleibt unveraendert. Aus dem aktuellen Arbeitsstand wird ein ZIP-Bundle exportiert. Dieses Bundle wird auf das Zielgeraet kopiert und dort per UI oder PowerShell installiert bzw. als Update eingespielt.

Auf dem Zielgeraet entsteht:

```text
C:\YBB_Inferenzserver\<AppName>\
  wsl\                 # importierte WSL2-Distribution
  incoming\            # Update-ZIPs koennen hier abgelegt werden
  updater\             # dauerhaft installierter Windows-Updater
  logs\                # Windows-seitige Install-/Update-Logs
  deployed\            # erfolgreich verwendete Bundles, optional
  failed\              # fehlgeschlagene Bundles, optional
  update-ybb-inferenzserver.cmd
  start-ybb-inferenzserver.cmd
  status-ybb-inferenzserver.cmd
```

Innerhalb der WSL2-Distribution wird strikt getrennt:

```text
/opt/ybb-inferenzserver/releases/<Version>   # versionierter Anwendungscode
/opt/ybb-inferenzserver/current              # Symlink auf aktives Release
/srv/cv-platform                             # persistente Daten, Secrets, DB-Daten, Uploads, Modelle
/srv/cv-platform/control/pretrained-checkpoints
                                             # lokaler Cache fuer Transfer-Learning-Gewichte
```

Updates tauschen nur `/opt/ybb-inferenzserver/current` bzw. `releases/<Version>` aus, fuehren Alembic-Migrationen aus und starten Services neu. Daten unter `/srv/cv-platform` bleiben erhalten: PostgreSQL/Label Studio/MLflow-Daten, Kanaele, Uploads, Modelle, Runtime-Ergebnisse, Pretrained-Gewichte, Secrets und Konfiguration.

### Pretrained-Modellgewichte als Offline-Update

Transfer Learning erwartet die notwendigen `.pt`/`.pth` Dateien lokal unter:

```text
/srv/cv-platform/control/pretrained-checkpoints
```

Die Gewichte werden nicht in jedes normale Runtime-Update eingebettet. Stattdessen kann die `YBB-Deployment-UI.exe` im Tab `Modelle` auf dem Entwicklungs-PC ein eigenes Modellupdate erzeugen. Dort werden entweder alle oder nur die markierten Modellgewichte heruntergeladen und als ZIP erzeugt:

```text
dist\ybb-<Zeitstempel>-pretrained-models-update.zip
```

Dieses ZIP wird wie ein normales Offline-Update in das `incoming`-Verzeichnis des Zielrechners kopiert oder im UI direkt ausgewaehlt. Im Tab `Modelle` wird es mit `Modelle einspielen` installiert. Der Updater prueft die SHA256-Manifestdatei und kopiert die Gewichte in der WSL2-Distribution nach `/srv/cv-platform/control/pretrained-checkpoints`.

CLI-Alternative auf dem Entwicklungs-PC:

```powershell
cd C:\KI\YBB_Inferenzserver
.\deployment\export-pretrained-models-update.ps1 -All
```

Fuer eine gezielte Auswahl koennen Modell-IDs uebergeben werden:

```powershell
.\deployment\export-pretrained-models-update.ps1 -Models ultralytics-oriented-detection-yolo26-n,openmmlab-detection-yolov8-n
```

Wenn Gewichte bereits manuell vorliegen, kann statt Download ein Quellordner genutzt werden. Der Dateiname muss dem Katalog entsprechen, z.B. `yolo26n-obb.pt`:

```powershell
.\deployment\export-pretrained-models-update.ps1 -Models ultralytics-oriented-detection-yolo26-n -SourceDirectory C:\Temp\ybb-models
```

### CLI-Alternative: Installer exportieren

Wenn du ohne UI exportieren willst, laeuft der Installer-Export vom
Entwicklungsrechner aus mit genau diesem Befehl:

```powershell
cd C:\KI\YBB_Inferenzserver
.\deployment\build-windows-installer.ps1
```

Es muss keine Version eingetragen und kein Name fuer eine Base-Distribution vergeben werden. Das Skript erzeugt pro Export automatisch eine Version aus Zeitstempel und Git-Hash, legt bei Bedarf eine interne Ubuntu-24.04-Builder-WSL an, bereitet diese Runtime vor und baut danach das Installer-ZIP.

Die interne Builder-WSL heisst standardmaessig `YBB-Inferenzserver-Builder` und liegt unter `.runtime\wsl\YBB-Inferenzserver-Builder`. Dieser Name ist nur ein technisches Build-Detail; fuer normale Exporte muss er nicht gepflegt werden.

Beim ersten Lauf braucht der Entwicklungsrechner Internetzugang, weil Ubuntu 24.04 und die Runtime-Abhaengigkeiten installiert werden. Das Zielgeraet bleibt offline-faehig.

Das Ergebnis liegt unter:

```text
dist\<AppName>-<automatische-Version>-windows-installer.zip
```

### Was der Build-Befehl macht

Der Installer enthaelt eine exportierte WSL2-Distribution. Diese Distribution wird automatisch auf dem Entwicklungsrechner vorbereitet und enthaelt alle nativen Runtime-Abhaengigkeiten bereits, weil das Zielgeraet offline ist.

Mindestens erwartet:

- Ubuntu 24.04 LTS auf WSL2 mit aktivierbarem `systemd`
- `python` Runtime fuer die Plattform, bevorzugt `/opt/ybb-runtime/venv/bin/python`
- optional separate Trainings-Runtime, bevorzugt `/opt/ybb-runtime/training-venv/bin/python` oder `/opt/cv-platform-training-venv/bin/python`
- Python-Abhaengigkeiten der Plattform inklusive `alembic`, `uvicorn`, `argon2`, MLflow/Label Studio und Training-Runtime, soweit auf dem Zielgeraet benoetigt
- PostgreSQL, Redis, Caddy, OpenSSL, `tar`, `psql`
- optional Triton Server, bevorzugt `/opt/tritonserver/bin/tritonserver`, wenn reale Triton-Inferenz auf dem Zielgeraet direkt lauffaehig sein soll
- GPU/CUDA/TensorRT-Abhaengigkeiten fuer reale Trainings- und Serving-Pfade
- Legacy-Aravis/GStreamer-RTSP-Bridge fuer Diagnose und Edge-/Altinstallationen unter Ubuntu
  24.04 WSL. Dieser Pfad liest z.B. Teledyne FLIR Blackfly Kameras ueber
  Aravis ein und stellt sie ueber den Channel-Quelltyp `aravis_gige` bereit.
  Der neue Serverdienst `camera-media` lehnt diesen CPU-x264-Zwischenpfad ab;
  produktive Serverkanaele brauchen einen direkten Adapter oder eine externe
  RTSP-Quelle. Die Legacy-Bridge wird als
  `ybb-aravis-rtsp@<name>.service` im Hintergrund konfiguriert und ist ein
  Zwischenpfad fuer Kameras, die nicht ueber einen nativen Adapter angebunden
  werden.
- optional Teledyne Spinnaker SDK, inklusive GigE Vision Treiber und
  Spinnaker GenTL Producer, wenn `flir_blackfly` Kanaele fuer Teledyne FLIR
  Blackfly/Blackfly S GigE Kameras auf dem Zielgeraet verwendet werden. Der
  GenTL `.cti` Pfad muss in der WSL-Runtime ueber `GENICAM_GENTL64_PATH` oder
  pro Kanal ueber `cti_path`/`cti_paths` sichtbar sein.
- Basler Pylon Suite auf dem Zielgeraet, wenn `basler_pylon` Kanaele fuer
  Basler ace 2/GigE Kameras verwendet werden. Die Python-Anbindung `pypylon`
  wird ueber das Offline-Python-Wheel-Bundle des Installers/Updaters
  mitgeliefert. `camera-media` erzwingt fuer produktive Serverkanaele direkten
  Pylon-Zugriff. `transport_mode=auto` wird dort zu `pylon_direct`
  normalisiert; `aravis_rtsp_bridge` wird abgelehnt, weil diese Kette vor dem
  NVENC-Publisher einen zweiten CPU-Encoder einfuehren wuerde.

`GigE Vision (Aravis)` ist damit kein zulaessiges Profil fuer den neuen
Server-Livestream. Es bleibt nur fuer bestehende Diagnose-/Edge-Pfade erhalten.

Die Kamera muss von Aravis in der WSL-Runtime sichtbar sein; bei GigE Vision
in WSL2 ist auf Windows 11 `networkingMode=mirrored` meist der stabilere
Netzwerkmodus fuer Discovery und UDP-Stream-Verkehr.

Der Offline-Installer/Updater kann diesen Zielzustand automatisch setzen. Die
Bundle-Config enthaelt dafuer:

```powershell
$WslNetworkingMode = "mirrored" # mirrored | nat | unchanged
$WslDnsTunneling = $true
$WslFirewall = $true
$WslAutoProxy = $false
$WslHostAddressLoopback = $true
$WslHyperVFirewallDefaultInboundAction = "Allow"
$WslRestartAllowed = $true
```

Bei `mirrored` wird `%UserProfile%\.wslconfig` gezielt aktualisiert, vorher
unter `<InstallRoot>\<AppName>\backups` gesichert und WSL anschliessend per
`wsl --shutdown` neu gestartet. Das ist noetig, weil globale `.wslconfig`
Einstellungen erst nach einem vollstaendigen WSL-Neustart greifen. Auf dem
Zielgeraet ist das erwartete Setup eine einzelne WSL-Distro ohne Docker.
`WslHyperVFirewallDefaultInboundAction = "Allow"` oeffnet die Hyper-V-Firewall
fuer WSL eingehend, damit GigE-Vision-Discovery und dynamische UDP-Streams die
WSL-Runtime erreichen koennen. Wenn diese globale Aenderung auf einem anderen
Rechner nicht gewuenscht ist, `WslNetworkingMode = "unchanged"` und
`WslHyperVFirewallDefaultInboundAction = "Unchanged"` setzen.

Fuer eine Basler ace 2, z.B. `a2A1920-51gcBAS`, im Kanalformular `Basler
Pylon` waehlen und Seriennummer, `name:<UserDefinedName>`,
`model:a2A1920-51gcBAS`, `id:<DeviceID>` oder IP-Adresse eintragen. Exposure,
Gain, Kamera-FPS, Acquisition Mode, PixelFormat, Trigger und GigE-Paketparameter
werden im Channel gespeichert und beim Start als Pylon/GenICam-Nodes gesetzt
und direkt angewendet. `LatestImageOnly` ist der Standard-Grab-Mode fuer
niedrige Latenz. Breite, Hoehe, FPS, Pixelformat und Linkgeschwindigkeit muessen
gesetzt sein, damit die Linklast vor dem Start hart validiert werden kann.

Der Build-Befehl fuehrt diese Schritte aus:

- optionale Defaults aus `deployment\windows-offline.config.ps1` laden, falls die Datei existiert
- automatische Release-Version erzeugen
- interne Ubuntu-24.04-Builder-WSL anlegen, falls sie noch fehlt
- Systempakete, `uv`, Python 3.11, Plattform-Abhaengigkeiten, Label Studio, MLflow und optional die Trainings-Runtime installieren
- Admin-Frontend bauen
- Windows-UI-EXE bauen und in das Bundle legen
- Anwendungscode als `app.tar.gz` packen
- vorbereitete WSL-Runtime als `runtime.tar` exportieren und bei Bedarf splitten
- ZIP-Bundle mit Installer-, Update-, WSL-Skripten, Manifest und SHA256-Pruefsummen erzeugen

Ohne gesetztes `$TritonServerArchivePath` wird der Triton-Check standardmaessig uebersprungen. Damit kann ein Installer fuer Control-Plane, Datenbank, Label Studio, Frontend und Services gebaut werden, auch wenn Triton erst spaeter vorbereitet wird. Fuer reale Triton-Inferenz muss Triton auf dem Zielgeraet bzw. in der Runtime vorhanden sein.

Wenn Triton explizit nicht geprueft werden soll:

```powershell
.\deployment\build-windows-installer.ps1 -SkipTritonCheck
```

Wenn Triton mit ausgeliefert werden soll, `$PrepareRuntimeSkipTritonCheck = $false` verwenden oder in der UI `Triton-Check ueberspringen` deaktivieren. Ist `$TritonServerArchivePath` leer, erzeugt der Entwicklungsrechner das Archiv automatisch aus `$TritonServerImage`. Das braucht Docker und Internetzugang auf dem Entwicklungsrechner, aber nicht auf dem Zielgeraet.

Beispiel:

```powershell
$TritonServerArchivePath = ""
$PrepareRuntimeSkipTritonCheck = $false
$TritonServerImage = "nvcr.io/nvidia/tritonserver:24.08-py3"
$TritonServerArchiveCacheDir = ".runtime\triton"
```

Optional kann weiterhin ein vorhandenes Archiv manuell gesetzt werden:

```powershell
$TritonServerArchivePath = "D:\offline-runtime\tritonserver-24.08-py3-linux-x86_64.tar.gz"
```

Beim Installer wird dieses Archiv in die vorbereitete WSL-Runtime nach `/opt/tritonserver` installiert. Beim Update-Export wird dasselbe Archiv als `runtime/tritonserver.tar.gz` in das Update-Bundle gelegt; der Ziel-Updater installiert es vor dem Neustart der Services ebenfalls nach `/opt/tritonserver`. So kann Triton auch auf bereits installierten Offline-Zielgeraeten per normalem Update nachgeruestet werden.
Das automatisch erzeugte Archiv enthaelt zusaetzlich `tritonserver/external-libs` mit den nativen CUDA-/Triton-Libraries, die im Docker-Image ausserhalb von `/opt/tritonserver` liegen, darunter die TensorRT-Builder-Libraries wie `libnvinfer_builder_resource.so.*`, und `tritonserver/bin/trtexec` fuer Deployment-seitige TensorRT-Engine-Builds aus ONNX. Die systemd-Units starten Triton ueber `/usr/local/bin/ybb-triton-server`, damit diese Libraries per `LD_LIBRARY_PATH` gefunden werden; der Control-Worker nutzt denselben Runtime-Pfad beim Paketieren.

Wenn die interne Builder-WSL bereits vorbereitet ist und nur neu exportiert werden soll:

```powershell
.\deployment\build-windows-installer.ps1 -SkipRuntimePrepare
```

### Optionale Defaults

Eine Config ist nicht fuer jeden Export noetig. Sie ist nur fuer dauerhafte Projektwerte gedacht, z.B. Ziel-Installationsname, Ziel-PC-IP oder Triton-Archiv:

```powershell
Copy-Item .\deployment\windows-offline.config.example.ps1 .\deployment\windows-offline.config.ps1
```

Werte wie `$AppVersion` und `$RuntimeDistroName` muessen fuer normale Builds nicht gesetzt werden. `$AppVersion = "auto"` ist der Standard. Fuer Spezialfaelle kann weiterhin ein direkter Export ausgefuehrt werden:

```powershell
.\deployment\export-windows-installer.ps1
```

Dieses direkte Skript erwartet eine bereits vorhandene vorbereitete Builder-WSL und fuehrt keine Runtime-Vorbereitung aus.

Das Installer-Bundle enthaelt `runtime.tar` oder `runtime.tar.partNNN`, `app.tar.gz`, `install-windows-offline.ps1`, `update-windows-offline.ps1`, `apply-windows-update.ps1`, `YBB-Deployment-UI.exe`, WSL-seitige Install-/Release-Skripte, SHA256-Manifest und Bundle-Metadaten.
Bei der Erstinstallation wird zusaetzlich ein dauerhafter Zielrechner-Updater unter `<InstallRoot>\<AppName>\updater` installiert. Spaetere Update-Bundles aktualisieren diesen Updater mit, damit der Zielrechner nicht von einem alten Update-Skript abhaengt.

### CLI-Alternative: Erstinstallation auf dem Offline-Zielgeraet

Dieser Abschnitt beschreibt die PowerShell-Variante ohne UI. Das Zielgeraet
braucht keine Internetverbindung und kein Docker. WSL2 muss auf dem Zielgeraet
aber bereits verfuegbar sein. Am besten PowerShell als Administrator starten,
damit der Windows-Portproxy fuer IP-Zugriff automatisch gesetzt werden kann.

1. Installer-ZIP auf das Zielgeraet kopieren, z.B. per USB-Stick nach `D:\incoming`.

2. Auf dem Zielgeraet PowerShell oeffnen und den Installer entpacken:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
New-Item -ItemType Directory -Path C:\YBB_Installer -Force
Expand-Archive "D:\incoming\ybb-inferenzserver-<version>-windows-installer.zip" -DestinationPath C:\YBB_Installer -Force
```

3. Erstinstallation starten:

```powershell
cd C:\YBB_Installer
.\install-windows-offline.ps1
```

Wenn das Installer-ZIP an einem anderen Ort liegen bleiben soll, kann es auch explizit an das entpackte Installationsskript uebergeben werden:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\YBB_Installer
.\install-windows-offline.ps1 -BundlePath "D:\incoming\ybb-inferenzserver-<version>-windows-installer.zip"
```

Wenn ein anderer Installationspfad auf dem Zielgeraet gewuenscht ist:

```powershell
cd C:\YBB_Installer
.\install-windows-offline.ps1 -InstallRoot "C:\KI_Runtime"
```

Wenn der Zielrechner aus dem Netzwerk ueber eine feste IP erreichbar sein soll,
diese Adresse bei der Erstinstallation ohne `https://` angeben:

```powershell
cd C:\YBB_Installer
.\install-windows-offline.ps1 -PublicAddress "192.168.1.50"
```

Das Skript importiert die vorbereitete WSL2-Runtime nach `C:\YBB_Inferenzserver\ybb-inferenzserver\wsl`, aktiviert bei Bedarf `systemd`, erzeugt lokale Secrets, initialisiert PostgreSQL-Datenbanken, spielt das Release ein, fuehrt Migrationen aus und startet die systemd-Services.

Nach erfolgreicher Installation ist die Weboberflaeche lokal erreichbar unter:

```text
https://127.0.0.1/
https://127.0.0.1/frontend/
```

Von anderen Rechnern im selben Netzwerk:

```text
https://<PC-IP>/
https://<PC-IP>/frontend/
https://<PC-IP>/label-studio/
```

Auf dem Zielgeraet liegen danach diese einfachen Einstiegspunkte:

```text
C:\YBB_Inferenzserver\ybb-inferenzserver\YBB-Deployment-UI.exe
C:\YBB_Inferenzserver\ybb-inferenzserver\start-ybb-inferenzserver.cmd
C:\YBB_Inferenzserver\ybb-inferenzserver\status-ybb-inferenzserver.cmd
C:\YBB_Inferenzserver\ybb-inferenzserver\update-ybb-inferenzserver.cmd
```

Fuer normale Updates muss kein WSL-Skript direkt aufgerufen werden.

Der normale Weg ohne DNS- oder Hosts-Aenderung ist ein Windows-Portproxy auf
Standard-Port `443`, aehnlich wie frueher bei Docker-Port-Publishing. Damit ist
dieselbe Anwendung lokal unter `https://127.0.0.1/` und im LAN unter
`https://<PC-IP>/` erreichbar, also ohne sichtbaren Sonderport wie `:8443`.
Caddy in WSL bedient dabei HTTPS `:443`, `/frontend/`, `/label-studio/`,
`/api/` und die weiteren Plattformpfade. Port `80` wird von neueren Bundles
zusaetzlich als reine Weiterleitung auf HTTPS eingerichtet.
Wenn eine konkrete Ziel-PC-IP angegeben wird, kann zusaetzlich genau diese
LAN-IP freigegeben werden. Der Installer startet diese Einrichtung automatisch,
wenn er als Administrator laeuft. Manuell geht es in PowerShell als
Administrator im entpackten Installer- oder Update-Verzeichnis.
Ganz ohne Windows-Firewallfreigabe geht nur lokaler Zugriff. Sobald ein anderer
Rechner im LAN zugreifen soll, muss Windows den eingehenden Standard-Port
erlauben; neuere Bundles begrenzen diese Regel auf die konkrete Ziel-PC-IP.

Nur lokaler Zugriff:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\configure-windows-portproxy.ps1 -ListenPort 443 -ConnectPort 443
```

Lokaler Zugriff plus LAN-Zugriff ueber eine konkrete Ziel-PC-IP:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\configure-windows-portproxy.ps1 -ListenPort 443 -ConnectPort 443 -ListenAddress "172.23.27.242" -WildcardListenAddress
```

Das Skript startet die WSL-Services, ermittelt die aktuelle WSL-IP und setzt
`netsh interface portproxy` von Windows-Port `443` auf WSL-Port `443`. Ohne
`-ListenAddress` lauscht es nur lokal auf `127.0.0.1` und `::1`; dafuer ist
keine eingehende Windows-Firewallregel noetig. Mit `-ListenAddress` und
`-WildcardListenAddress` lauscht der Portproxy per `0.0.0.0`, die
Windows-Firewallregel wird aber auf die konkrete Ziel-PC-IP begrenzt. Das ist
fuer WSL2/LAN robuster als ein expliziter Portproxy-Bind direkt auf die LAN-IP.
Wenn `PublicAddress` ein DNS-/Hosts-Name statt einer IPv4 ist, setzt der
Installer automatisch diesen Wildcard-Listenmodus und begrenzt die Firewall auf
die erkannten Host-IP-Adressen.
Wenn `443 -> 443` verwendet wird, richtet das Skript ausserdem `80 -> 80` ein,
damit ein Browser-Aufruf ohne Schema von HTTP sauber auf HTTPS umgeleitet
werden kann. MediaMTX-Ports werden von neueren Bundles nicht mehr per
Portproxy eingerichtet; `install-windows-mediamtx.ps1` hostet MediaMTX direkt
auf Windows und setzt die passenden Firewall-Regeln. Die WSL-IP kann sich nach
einem Windows- oder WSL-Neustart aendern; dann das Portproxy-Skript fuer Web/
HTTPS erneut ausfuehren. Die MediaMTX-Erreichbarkeit bleibt davon unabhaengig.

Wenn das Bundle `WslNetworkingMode=mirrored` enthaelt, ueberspringt
`configure-windows-portproxy.ps1` den Web-Portproxy und entfernt alte
Portproxy-Regeln fuer die betroffenen Webports. Installer und Updater entfernen
diese Regeln im Mirrored Mode bereits vor dem WSL-Start, damit Caddy `:80` und
`:443` direkt in WSL binden kann. Dabei werden alle `netsh portproxy`-Tabellen
fuer IPv4/IPv6-Kombinationen bereinigt. Danach wird WSL heruntergefahren, der
Windows-Dienst `iphlpsvc` neu gestartet und die Windows-Portbelegung geprueft.
Wenn `iphlpsvc` die Webports danach weiterhin haelt, setzt der Updater den
Windows-Portproxy vollstaendig per `netsh interface portproxy reset` zurueck,
startet `iphlpsvc` erneut und prueft nochmal. Wenn `80` oder `443` danach
weiterhin von Windows belegt sind, bricht der Updater vor dem Caddy-Start mit
Prozess-/Dienstdiagnose ab. In diesem Modus nutzt Windows direkt die
gespiegelten WSL-Netzwerkschnittstellen; die WSL-Runtime spricht
Windows-MediaMTX intern ueber `127.0.0.1` an.

RTSP ist nur der interne Ingest zwischen `ybb-camera-media` und MediaMTX und
kein oeffentlicher Client-Endpunkt. Die Admin-UI nutzt ausschliesslich WHEP.
Der HTTPS-Reverse-Proxy-Pfad lautet:
`https://<Zielrechner-IP>/stream-webrtc/channels/<channel-slug>/live/whep`.
MediaMTX liefert dafuer einen TURN-Server auf TCP/3478 mit kurzlebigen
Credentials aus. Coturn relayed den Medienpfad lokal zum ICE-Listener von
MediaMTX auf 8189. Schlaegt dieser Pfad fehl, meldet die UI den konkreten
WHEP-/ICE-Fehler und verbindet
sich begrenzt erneut mit demselben Endpunkt; ein Protokollwechsel findet nicht
statt. Einzelbilder bleiben ausschliesslich Diagnose- und Kalibrierfunktion.

Das Portproxy-Skript richtet neben IPv4 `127.0.0.1` auch IPv6 `::1 -> WSL` ein. Bei bestehenden Installationen das Skript erneut als Administrator ausfuehren.

Neuere Versionen entfernen dabei auch alte Portproxy-Regeln fuer denselben
Listen-Port, wenn diese noch auf private WSL-Zieladressen zeigen und nicht mehr
zu den gewuenschten Listen-Adressen gehoeren. Das bereinigt alte Teststaende
wie z.B. virtuelle Adapteradressen, ohne andere Ports anzufassen.

Wenn HTTPS ueber einen Sonderport mit `Recv failure: Connection was reset`
abbricht, nicht weiter an `:8443` festhalten, sondern den Standard-Port `443`
verwenden. Dann ist die URL wieder Docker-aehnlich `https://<Ziel-PC-IP>/`.

Port `8080` ist nicht die Weboberflaeche, sondern die interne Admin-API hinter Caddy. Ein Aufruf von `http://127.0.0.1:8080/` kann deshalb korrekt mit `404 Not Found` antworten.

Das initiale Admin-Passwort wird bei der Erstinstallation in der WSL-Distribution abgelegt und kann so ausgelesen werden:

```powershell
wsl -d YBB-Inferenzserver -u root -- cat /srv/cv-platform/secrets/bootstrap_admin_initial_password.txt
```

Installationslogs liegen auf dem Zielgeraet unter:

```text
C:\YBB_Inferenzserver\ybb-inferenzserver\logs
```

Pruefung nach der Installation:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
wsl --list --verbose
.\start-ybb-inferenzserver.cmd
wsl -d YBB-Inferenzserver -u root -- systemctl --no-pager status caddy ybb-platform.target
wsl -d YBB-Inferenzserver -u root -- systemctl --no-pager list-units 'ybb-*' caddy --type=service --state=running,failed,activating
```

Wenn `https://127.0.0.1/` oder `https://<PC-IP>/` nicht erreichbar ist, zuerst auf Windows pruefen:

```powershell
netsh interface portproxy show v4tov4
Test-NetConnection 127.0.0.1 -Port 80
Test-NetConnection 127.0.0.1 -Port 443
curl.exe -k -I https://127.0.0.1/
```

Wenn die Installation mit Exit Code `0` abgeschlossen wurde, aber
`127.0.0.1:443` nicht antwortet, sind die WSL-Services und der Windows-
Portproxy getrennt zu pruefen:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\status-ybb-inferenzserver.cmd
wsl -d YBB-Inferenzserver -u root -- bash -lc "curl -I http://127.0.0.1/"
wsl -d YBB-Inferenzserver -u root -- bash -lc "curl -k -I https://127.0.0.1/"
```

Wenn der HTTPS-Aufruf innerhalb von WSL `HTTP 200` liefert, aber Windows-Port `443`
weiterhin nicht erreichbar ist, fehlt der Portproxy. Dann PowerShell als
Administrator starten und ausfuehren:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\updater\configure-windows-portproxy.ps1 -DistroName "YBB-Inferenzserver" -ListenPort 443 -ConnectPort 443
```

Wenn lokaler Zugriff funktioniert und LAN-Zugriff erwuenscht ist, nur die
konkrete LAN-IP ergaenzen:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\updater\configure-windows-portproxy.ps1 -DistroName "YBB-Inferenzserver" -ListenPort 443 -ConnectPort 443 -ListenAddress "172.23.27.242" -WildcardListenAddress
```

Wenn die Startseite laedt, aber Browser-Requests wie `/api/v1/...` mit
`ERR_CONNECTION_RESET` abbrechen oder Label Studio nicht erreichbar ist, ist
Caddy erreichbar, aber mindestens ein interner Backend-Port fehlt. Dann direkt
in WSL pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- bash -lc 'ss -ltnup | grep -E ":80|:443|:8080|:8090|:8100|:5000"'
wsl -d YBB-Inferenzserver -u root -- bash -lc 'for p in 80 443 8080 8090 8100 5000; do if timeout 1 bash -lc "</dev/tcp/127.0.0.1/$p"; then echo "$p OK"; else echo "$p FEHLT"; fi; done'
```

Erwartet werden mindestens diese internen Ports:

```text
80    Caddy HTTP-Redirect
443   Caddy HTTPS
8080  Admin-API
8090  Runtime-API
8100  Label Studio
5000  MLflow
```

MediaMTX laeuft bei neuen Offline-Bundles direkt auf Windows. Diese Ports
werden deshalb auf Windows geprueft:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
& "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File .\install-windows-mediamtx.ps1 -Action Status -AppDir .
Get-NetTCPConnection -LocalPort 3478,8554,8888,8889,8189,9997 -State Listen
Get-NetUDPEndpoint -LocalPort 8189
wsl.exe -d YBB-Inferenzserver -u root -- systemctl status ybb-turnserver.service --no-pager
curl.exe -s http://127.0.0.1:9997/v3/paths/list
```

Wenn nur `443` offen ist, laufen zwar Reverse Proxy und Frontend, aber die
Backend-Services muessen neu gestartet bzw. deren Logs geprueft werden:

```powershell
wsl -d YBB-Inferenzserver -u root -- bash -lc 'systemctl restart ybb-secrets.service postgresql.service redis-server.service'
wsl -d YBB-Inferenzserver -u root -- bash -lc 'if [ -x /usr/local/bin/ybb-start-platform ]; then /usr/local/bin/ybb-start-platform; else systemctl restart ybb-admin-api.service ybb-runtime-admin-api.service ybb-label-studio.service ybb-mlflow.service caddy; fi'
Start-Sleep -Seconds 30
wsl -d YBB-Inferenzserver -u root -- bash -lc 'ss -ltnup | grep -E ":443|:8080|:8090|:8100|:5000"'
wsl -d YBB-Inferenzserver -u root -- journalctl -u ybb-admin-api.service -u ybb-runtime-admin-api.service -u ybb-label-studio.service -u ybb-mlflow.service -n 180 --no-pager -l
```

Neue Installer- und Update-Bundles pruefen diese Ports nach dem Start
automatisch. Dadurch meldet der Installer nicht mehr erfolgreich, wenn nur die
statische Startseite bereit ist.

Wenn `/label-studio/` mit `502` antwortet und `127.0.0.1:8100` in WSL fehlt,
ist Label Studio noch nicht startbereit oder beim Start haengen geblieben.
Neue Bundles deaktivieren dafuer auch den internen Label-Studio-Versioncheck,
damit Offline- oder Corporate-Netze keinen PyPI-Zugriff beim Start ausloesen.
Nach einem Update kann man den Patch so pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- bash -lc 'grep -n "LATEST_VERSION_CHECK" /opt/ybb-runtime/venv/lib/python*/site-packages/label_studio/core/settings/base.py'
wsl -d YBB-Inferenzserver -u root -- bash -lc 'systemctl restart ybb-label-studio.service; sleep 20; ss -ltnp | grep ":8100"'
curl.exe -k -I https://127.0.0.1/label-studio/
```

Wenn Label Studio unter `/label-studio/` mit `404` antwortet oder ohne Design
erscheint, liegt es meist an Prefix- oder Asset-URLs. Neue Bundles setzen Label
Studio auf die oeffentliche URL `https://<Zielrechner-IP>/label-studio`,
routen aber Label-Studios absolute Asset-Pfade `/static/*` und `/react-app/*`
automatisch zu Label Studio. `/label-studio/*` wird mit Prefix-Stripping
weitergeleitet, weil Django die Backend-Route ohne diesen Prefix aufloesen muss.
Auf bestehenden Installationen kann man den Zustand so pruefen:

```powershell
curl.exe -k https://127.0.0.1/runtime-api/health/ready
curl.exe -k -I https://127.0.0.1/static/css/main.css
curl.exe -k -I https://127.0.0.1/react-app/main.css
wsl -d YBB-Inferenzserver -u root -- grep -n "FRONTEND_HOSTNAME\|DOMAIN_FROM_REQUEST\|HOST=" /usr/local/bin/ybb-label-studio-service
wsl -d YBB-Inferenzserver -u root -- grep -n static /etc/caddy/Caddyfile
wsl -d YBB-Inferenzserver -u root -- grep -n react-app /etc/caddy/Caddyfile
wsl -d YBB-Inferenzserver -u root -- grep -n runtime-api /etc/caddy/Caddyfile
wsl -d YBB-Inferenzserver -u root -- grep -n label-studio /etc/caddy/Caddyfile
```

Bei `curl.exe -k -I .../runtime-api/health/ready` kann `405 Method Not Allowed`
erscheinen, weil dieser Health-Endpunkt keine `HEAD`-Requests annimmt. Fuer
Runtime-Health deshalb ohne `-I` pruefen.

Wenn im Browser-Netzwerklog solche `/static/...`- oder `/react-app/...`-
Requests fehlschlagen, ein neues Update-Bundle einspielen.

Wenn Label Studio bei automatisch gesammelten Capture-Bildern meldet
`There was an issue loading URL from $image value`, pruefen ob der Local-Files-
Pfad direkt erreichbar ist. Fuer neue Tasks wird der neutrale Pfad
`/data/local-files/?d=...` verwendet; `/label-studio/data/local-files/?d=...`
bleibt fuer bestehende Tasks als Kompatibilitaetsroute erhalten.

```powershell
curl.exe -k -I "https://127.0.0.1/data/local-files/?d=runtime/captures/<pfad-zum-bild>.jpg"
curl.exe -k -I "https://127.0.0.1/label-studio/data/local-files/?d=runtime/captures/<pfad-zum-bild>.jpg"
wsl -d YBB-Inferenzserver -u root -- bash -lc 'grep -n "LOCAL_FILES_URL_PREFIX" /etc/ybb-inferenzserver/runtime.env'
```

Beide `curl`-Aufrufe sollen `HTTP 200` liefern. Bei `404`, `403` oder
`text/html` statt Bilddaten ein neues Update-Bundle einspielen; der Installer
prueft diese Local-Files-Routen nach dem Update automatisch und setzt
bestehende Capture-Dateien unter `/srv/cv-platform/runtime/captures`
web-lesbar.

Wenn die Weboberflaeche erreichbar ist, der Plattformstatus aber fast alle
Dienste als `down` zeigt, obwohl `status-ybb-inferenzserver.cmd` laufende
systemd-Services meldet, prueft die Admin-API vermutlich noch Docker-DNS-Namen
statt WSL-lokaler Ports. Neue Update-Bundles setzen dafuer automatisch:

```text
CV_PLATFORM_SERVICE_HEALTH_HOST=127.0.0.1
CV_PLATFORM_SERVICE_HEALTH_EXCLUDE=operations-agent
```

Nach dem Update kann man direkt in WSL pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- bash -lc 'grep -n "CV_PLATFORM_SERVICE_HEALTH" /etc/ybb-inferenzserver/runtime.env'
wsl -d YBB-Inferenzserver -u root -- bash -lc 'curl -s http://127.0.0.1:8082/health/ready | head -c 120; echo'
wsl -d YBB-Inferenzserver -u root -- bash -lc 'curl -s http://127.0.0.1:8090/health/ready | head -c 120; echo'
```

Einige Inferenz-Gateway-Dienste koennen weiterhin `down` melden, solange kein
Triton-Server vorbereitet wurde. Das ist fuer Control-Plane, Admin-UI,
Label Studio und Updates nicht automatisch ein Blocker.

RTSP-Streams werden vom `ybb-realtime-executor` innerhalb der WSL-Runtime
geoeffnet. Wenn der Stream auf Windows laeuft, aber ein Channel in der
Anwendung auf `connecting` bleibt, zuerst aus der installierten Runtime testen:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\rtsp-probe-ybb-inferenzserver.cmd "rtsp://BENUTZER:PASSWORT@KAMERA-IP:554/pfad" 15
```

Der Probe-Befehl nutzt dieselbe Python/OpenCV-Runtime wie der Executor und
setzt standardmaessig RTSP-over-TCP. Wenn dieser Test fehlschlaegt, erreicht WSL
den Stream nicht korrekt oder Zugangsdaten/Stream-Pfad stimmen aus Sicht der
WSL-Runtime nicht. Wenn der Test erfolgreich ist, den Channel in der
Weboberflaeche neu starten und bei Bedarf die Executor-Logs pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- journalctl -u ybb-realtime-executor.service -n 120 --no-pager -l
```

Ein `403 Forbidden` von Caddy bei `https://127.0.0.1/` bedeutet meist, dass das Admin-Frontend im aktiven Release fehlt. Dann pruefen:

```powershell
wsl -d YBB-Inferenzserver -u root -- test -f /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist/index.html
$LASTEXITCODE
```

Exit Code `0` bedeutet: Frontend-Datei vorhanden. Exit Code `1` bedeutet: neues Update-Bundle exportieren und einspielen.

Der Installer/Updater richtet eine Windows-Aufgabe `YBB-Inferenzserver Keepalive`
ein. Diese Aufgabe startet beim PC-Boot unter dem Windows-Benutzer, dem die
WSL-Distribution gehoert, und haelt die WSL-Dienste auch dann am Leben, wenn
kein Benutzer interaktiv angemeldet ist oder ein Kontroll-Login wieder
abgemeldet wird.

Status pruefen:

```powershell
$appDir = "C:\YBB_Inferenzserver\ybb-inferenzserver"
cd $appDir
.\status-autostart-ybb-inferenzserver.cmd
.\status-ybb-inferenzserver.cmd
```

Falls die Aufgabe durch Gruppenrichtlinien oder fehlende Rechte nicht angelegt
wurde, kann sie als Administrator erneut registriert werden:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\install-autostart-ybb-inferenzserver.cmd
.\start-autostart-ybb-inferenzserver.cmd
```

Die Aufgabe verwendet `LogonType: S4U`. Das bedeutet: kein interaktives Fenster,
kein gespeichertes Kennwort im Skript, Start beim Rechnerstart. Falls die IT
`Log on as batch job` fuer den Installationsbenutzer blockiert, muss diese
Berechtigung fuer den YBB-Betriebsbenutzer erlaubt werden.

Die fruehere Autostart-Ordner-Variante startet nur nach Benutzeranmeldung und
ist fuer einen Zielrechner, der ohne Login weiterlaufen soll, nicht ausreichend.

Task entfernen:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\remove-autostart-ybb-inferenzserver.cmd
```

Falls eine Erstinstallation vor `Installation erfolgreich abgeschlossen` abbricht, kann bereits eine unvollstaendige WSL-Distribution importiert worden sein. Bei einer echten Erstinstallation ohne Nutzdaten kann dieser Zwischenstand entfernt und danach derselbe Installer erneut gestartet werden:

```powershell
wsl --terminate YBB-Inferenzserver 2>$null
wsl --unregister YBB-Inferenzserver
Remove-Item -Recurse -Force "C:\YBB_Inferenzserver\ybb-inferenzserver\wsl" -ErrorAction SilentlyContinue
Remove-Item -Force "C:\YBB_Inferenzserver\ybb-inferenzserver\install.lock" -ErrorAction SilentlyContinue
```

### Update exportieren

Wenn auf dem Zielgeraet bereits Daten vorhanden sind, wird kein neuer Installer
exportiert, sondern ein Update-Bundle. In der UI ist das der Button
`Update exportieren` im Reiter `Export`.

Ohne UI geht es so:

```powershell
cd C:\KI\YBB_Inferenzserver
.\deployment\export-windows-update.ps1
```

Das Ergebnis liegt unter:

```text
dist\<AppName>-<automatische-Version>-windows-update.zip
```

### Update auf dem Offline-Zielgeraet einspielen

Empfohlener einfacher Ablauf:

1. Update-ZIP auf das Zielgeraet kopieren, z.B. nach:

```text
C:\YBB_Inferenzserver\ybb-inferenzserver\incoming
```

2. Dauerhaften Updater starten:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\update-ybb-inferenzserver.cmd
```

Der Updater nimmt automatisch das neueste `*-windows-update.zip` aus `incoming`, prueft die Bundle-Pruefsummen, spielt das Release in WSL ein, aktualisiert den installierten Updater selbst und verschiebt das ZIP danach nach `deployed` oder bei Fehlern nach `failed`.

Alternativ kann ein Bundle explizit uebergeben werden:

```powershell
cd C:\YBB_Inferenzserver\ybb-inferenzserver
.\update-ybb-inferenzserver.cmd -BundlePath "D:\incoming\ybb-inferenzserver-<version>-windows-update.zip"
```

Der direkte Low-Level-Ablauf bleibt fuer Diagnosefaelle verfuegbar:

```powershell
Expand-Archive .\ybb-inferenzserver-1.2.4-windows-update.zip -DestinationPath .\update
Set-ExecutionPolicy -Scope Process Bypass
.\update\update-windows-offline.ps1
```

Das Update prueft SHA256-Pruefsummen, entpackt `app.tar.gz` in ein neues Release-Verzeichnis in der bestehenden WSL-Distribution, setzt `current` auf die neue Version, fuehrt Alembic-Migrationen aus und startet die Plattform-Services neu. `/srv/cv-platform` wird nicht geloescht oder ersetzt.

### Temporal-Sequence- und Stammausrichtungs-Kompatibilitaet

Die Temporal-Erweiterungen verwenden denselben Installer und dieselben beiden
Exportpfade:

```powershell
.\deployment\build-windows-installer.ps1
.\deployment\export-windows-update.ps1
```

`export-windows-installer.ps1` und `export-windows-update.ps1` brechen vor dem
Frontend-Build ab, wenn der zusammengehoerige Vertragsverbund unvollstaendig
ist. Geprueft werden insbesondere `TaskType.TEMPORAL_SEQUENCE`,
`CanonicalSequenceItem`, die Annotation-Schema-Registry mit
`stem_orientation_angle.v1`, Video-/Timeline-Konfiguration, idempotenter
Label-Studio-Import, Target Builder, Temporal-Snapshot, Runtime-Nodes,
TCN-/GRU-Trainer, Migration `20260803_0035`, OpenAPI-Dateien und die gemeinsam
genutzten Frontend-Schemas. Das Bundle dokumentiert diese Grenze als
`CompatibilityProfile=temporal-sequence.v1`.

`wsl/apply-release.sh` wiederholt die Vertragspruefung am entpackten
Release-Kandidaten vor Alembic-Migration und Aktivierung. Der Service-
Importtest instanziiert zusaetzlich die registrierte Label-Studio-Konfiguration,
den Runtime-Node-Katalog und den allgemeinen Temporal-Trainer. Damit wird kein
Projekt, Dataset, Kanal oder Modell angelegt und kein vorhandener Kanal
veraendert.

Nach dem Zielsystemstart prueft der vorhandene Reverse-Proxy-Smoke-Test neben
der Label-Studio-Seite und `/data/local-files/?d=...` auch einen HTTP-Byte-
Range-Request (`206 Partial Content`). Dieser Zugriff ist fuer abspielbare MP4-
Aufgaben und framegenaues Video-/Timeline-Labeling erforderlich. Die alte Route
`/label-studio/data/local-files/?d=...` bleibt fuer bestehende Bildaufgaben
erhalten.

Die generierten beziehungsweise gemeinsam gepflegten Vertragsartefakte werden
vor einem Export mit den Repository-Befehlen aktualisiert und geprueft:

```powershell
pnpm generate:openapi
pnpm build:admin-web
python -m pytest tests/unit/backend/test_windows_offline_deployment_scripts.py tests/unit/backend/test_stem_orientation_review_migration.py tests/unit/backend/test_runtime_result_service.py tests/unit/backend/test_temporal_sequence_datasets.py tests/unit/backend/test_label_project_provisioning.py tests/unit/backend/test_temporal_label_studio_import.py tests/unit/backend/test_temporal_snapshot_materialization.py tests/unit/backend/test_temporal_sequence_training.py tests/unit/backend/test_temporal_runtime_inference.py tests/unit/backend/test_temporal_sample_recorder.py
```

Auf einem reinen Entwicklungs-PC ohne ausfuehrbare Zielruntime bleiben der
Installer-/Updater-Apply, Label-Studio-Medienabruf, Alembic gegen PostgreSQL,
Triton und systemd-Zieltests offen. Sie werden erst beim Apply in der
vorbereiteten WSL2-Zielruntime ausgefuehrt; ein erfolgreicher Quelltest ersetzt
diese Zielpruefung nicht.

#### Persistenz und deaktivierter Ausgangszustand

Alle neuen Daten bleiben unter der bestehenden persistenten Root und damit
ausserhalb von `/opt/ybb-inferenzserver/releases`:

```text
/srv/cv-platform/runtime/captures/.temporal-spool       # kurzlebige, crash-feste Recorder-Spools
/srv/cv-platform/runtime/captures/temporal-samples     # MP4, Features, Rotationstraces, Manifeste
/srv/cv-platform/control/labelstudio/exports           # Label-Studio-Exporte
/srv/cv-platform/control/snapshots                     # immutable Bild- und Temporal-Snapshots
/srv/cv-platform/control/dvc/repo                      # DVC-Arbeitsbereich
/srv/cv-platform/control/dvc/remote                    # lokaler DVC-Remote
/srv/cv-platform/training                              # Trainingsworkspaces und Artefakte
/srv/cv-platform/serving/packages                     # versionierte Serving-Pakete, auch TCN/GRU
/srv/cv-platform/labelstudio                          # bestehende und neue Label-Studio-Projekte/Labels
/srv/cv-platform/runtime/results                      # vorhandene Result-Artefakte
PostgreSQL                                            # Datasets, Mappings, Result-History und Reviews
```

Intrinsische Kamerakalibrierungen werden als versionierte Node-Konfiguration in
der bestehenden kanalbezogenen Pipelineversion in PostgreSQL gespeichert; es
gibt dafuer keinen zweiten Dateispeicher. Rotationskalibrierungs-ID und -Version
liegen in den persistierten Sample-/Result-/Modellartefakten unter den oben
genannten Roots.

Die Installation aktiviert keine Stammausrichtung. Die Nodes erscheinen nur im
vorhandenen Katalog; ausgefuehrt werden sie erst, wenn eine Pipeline explizit
konfiguriert, validiert, publiziert und einem Kanal zugeordnet wurde. Temporal-
Modelle bleiben ohne manuelle Freigabe Shadow-Artefakte, und
`PlcWritesAllowed=false` bleibt der Default. Bestehende Bilddatasets,
Label-Studio-Projekte, Pipelineversionen und Trainingsjobs werden weder
umgeschrieben noch automatisch neu zugeordnet.

#### Update von v1-Ergebnissen und Rollbackgrenzen

`runtime.execution.result.v1` und vorhandene Quality-Decision-Payloads bleiben
im generischen Result-Reader unveraendert lesbar. `stem.orientation.v2` ist ein
zusaetzlicher Ergebnistyp; eine spaetere Verifikation ersetzt die eingefrorene
Vor-Einzug-Empfehlung nicht. Es gibt keine automatische Konvertierung alter
Quality-Ergebnisse in Winkelresultate, weil diese Information in v1 nicht
vorhanden ist.

Migration `20260803_0035` fuegt nur die append-only Reviewtabelle hinzu. Ihr
Downgrade entfernt die Tabelle absichtlich nicht, damit Bedienerreviews bei
einem Anwendungsrollback erhalten bleiben. Eine aeltere Anwendung ignoriert
diese zusaetzliche Tabelle; ein spaeteres Roll-forward verwendet sie erneut.
Temporal-Dataset-, Projekt- und Importmetadaten nutzen vorhandene Spalten und
versionierte JSON-Vertraege und benoetigen keine zweite Migration.

Die reale Rollbackgrenze des Windows-/WSL2-Pfads lautet:

- Vertrags-, Import- und Frontendpruefungen laufen vor Migration und Aktivierung;
  ein Fehler dort laesst den bisherigen `current`-Symlink aktiv.
- Alembic laeuft vor dem Umschalten auf das neue Release. Deshalb muss jede
  Migration mit der vorherigen Anwendung lesekompatibel oder explizit als
  nicht rueckrollbar dokumentiert sein. `20260803_0035` erfuellt dies additiv.
- Der WSL2-Updater besitzt derzeit keinen allgemeinen automatischen
  Schema-Downgrade. Nach bereits erfolgter Aktivierung ist ein Rueckwechsel des
  Anwendungssymlinks eine betreute Betriebsaktion; Datenbank- und Assetdaten
  werden dabei nicht geloescht.
- Vor jedem produktiven Update bleibt ein Backup von `/srv/cv-platform` und
  PostgreSQL erforderlich. Das Zurueckspielen eines Backups ist die Grenze fuer
  spaetere, nicht additiv rueckwaertskompatible Migrationen.

Die idempotente Upgrade-/Downgrade-Erhaltung der Reviewdaten, die v1-
Resultlesbarkeit, Bundle-Metadaten, Apply-Reihenfolge, Persistenzpfade und
Video-Byte-Range-Pruefung sind durch die oben genannten Unit-Tests abgedeckt.

### Wichtige Grenzen

- Das Zielgeraet braucht keine Internetverbindung, aber WSL2 muss auf Windows verfuegbar sein. Wenn Windows-Features fuer WSL2 noch nicht aktiviert sind, kann ein Neustart noetig sein.
- Die vorbereitete Runtime muss alle nativen Systempakete und Python-/GPU-Abhaengigkeiten enthalten. Das Update-Bundle enthaelt Anwendungscode und optional ein Triton-Server-Archiv, aber keine neue Linux-Systembasis.
- Wenn sich native Runtime-Abhaengigkeiten aendern, muss eine neue Runtime vorbereitet und ein neues Installer-Bundle exportiert werden. Fuer reine Code-/Frontend-/Migrationsupdates reicht das Update-Bundle.
- Datenbankmigrationen werden vor dem Neustart der Services ausgefuehrt. Wie bei jedem produktiven System sollten vor Updates Backups der persistenten Daten unter `/srv/cv-platform` erstellt werden.
- Die Skripte sind bewusst neben der bestehenden Docker-Schiene angelegt und veraendern die Entwicklungsstruktur nicht.

---

# Offline-Deployment fuer Docker-Anwendungen auf Windows

Dieses Setup erzeugt auf einem Windows-Laptop ein versioniertes Release-Bundle, das alle Dateien fuer den Windows-Server enthaelt. Der Server braucht keine Internetverbindung, keine Docker Registry und keinen Build-Prozess. Er laedt ausschliesslich die Docker Images aus dem Bundle.

## Grundidee

Auf dem Laptop wird das Docker Image gebaut, als `images.tar` exportiert und zusammen mit `compose.yaml`, `.env`, `VERSION`, `manifest.sha256` und `deploy-manifest.json` in ein ZIP-Bundle gepackt. Wenn `images.tar` fuer PowerShell-ZIP zu gross ist, teilt das Build-Skript die Datei automatisch in `images.tar.part001`, `images.tar.part002` usw. auf. Das Deploy-Skript setzt diese Teile auf dem Server wieder zu `images.tar` zusammen und prueft danach die SHA256-Pruefsumme.

Auf dem Server wird dieses Bundle manuell nach Windows kopiert, z.B. nach `C:\DockerApps\myapp\incoming`. Danach fuehrt man nur noch `deploy-release.ps1` aus. Das Skript entpackt das Bundle, prueft SHA256-Pruefsummen, laedt das Image per `docker image load` und startet die Anwendung mit `docker compose`.

Der Server soll nicht bauen und nicht pullen, damit Deployments reproduzierbar bleiben und auch ohne Internet funktionieren. `latest` wird bewusst nicht verwendet, weil dieser Tag nicht eindeutig ist: Was heute `latest` ist, kann morgen ein anderes Image sein. Jedes Release bekommt deshalb einen festen Versions-Tag wie `1.2.3`.

## Voraussetzungen pruefen

Auf Laptop und Server:

```powershell
docker version
docker compose version
```

Unter Windows muss Docker Desktop gestartet sein. Fuer Linux-Container muss Docker Desktop im Linux-Container-Modus laufen. PowerShell als Administrator kann je nach Docker-Installation, Pfaden und Berechtigungen noetig sein. Pfade mit Leerzeichen muessen sauber gequotet werden.

## Release auf dem Laptop bauen

1. Beispielkonfiguration kopieren:

```powershell
Copy-Item .\deployment\app.config.example.ps1 .\deployment\app.config.ps1
```

2. Werte in `deployment\app.config.ps1` anpassen.

3. Bundle bauen:

```powershell
.\deployment\build-release.ps1 -Config .\deployment\app.config.ps1
```

Das Ergebnis liegt unter:

```text
dist\<AppName>-<AppVersion>.bundle.zip
```

Das Bundle enthaelt die Image-Daten entweder direkt als `images.tar` oder bei grossen Images als mehrere `images.tar.partNNN`-Dateien. Der Server rekonstruiert daraus lokal `images.tar`, damit `docker image load` ohne Internet und ohne Registry funktioniert.

## Bundle auf den Server kopieren

Kopieren Sie die ZIP-Datei manuell, z.B. per USB-Stick, Netzwerkfreigabe, RDP-Dateikopie oder SCP. Ein typischer Zielpfad ist:

```text
C:\DockerApps\myapp\incoming\myapp-1.2.3.bundle.zip
```

## Deployment auf dem Server ausfuehren

```powershell
.\deployment\deploy-release.ps1 -BundlePath "C:\DockerApps\myapp\incoming\myapp-1.2.3.bundle.zip"
```

Das Skript legt pro App diese Struktur an:

```text
C:\DockerApps\<AppName>\incoming
C:\DockerApps\<AppName>\releases
C:\DockerApps\<AppName>\current
C:\DockerApps\<AppName>\deployed
C:\DockerApps\<AppName>\failed
C:\DockerApps\<AppName>\logs
```

Das konkrete Release wird nach `C:\DockerApps\<AppName>\releases\<AppVersion>` kopiert. `current` ist ein normales Verzeichnis mit einer Kopie der neuen Version; es wird keine Admin-Symlink- oder Junction-Funktion vorausgesetzt. Parallel laufende Deployments werden durch `C:\DockerApps\<AppName>\deploy.lock` verhindert.

Nach Erfolg wird das Bundle nach `deployed` verschoben. Bei Fehlern versucht das Skript, das Bundle nach `failed` zu verschieben und liefert einen Exit Code ungleich `0`. Logs erscheinen auf stdout und zusaetzlich unter `logs`, z.B. `deploy-2026-05-19-153000.log`.

## Rollback

Verfuegbare Releases anzeigen:

```powershell
.\deployment\rollback.ps1 -AppName "myapp" -List -ServerBaseDir "C:\DockerApps"
```

Auf eine konkrete Version zurueckrollen:

```powershell
.\deployment\rollback.ps1 -AppName "myapp" -Version "1.2.2" -ServerBaseDir "C:\DockerApps"
```

Rollback kopiert `releases\<Version>` nach `current`, laedt bei vorhandener `images.tar` das Image erneut und fuehrt danach `docker compose --env-file .env -f compose.yaml up -d --remove-orphans` aus.

## Wiederverwendung fuer mehrere Anwendungen

Jede Anwendung braucht nur eine eigene Config-Datei mit eigenem `AppName`, `AppVersion`, `DockerImageName`, Ports, Compose-Datei und Server-Basisverzeichnis. Die Skripte bleiben gleich. Wichtig ist, dass jede App einen eindeutigen `AppName` und passende Host-Ports nutzt.

## Persistente Daten und Migrationen

Die Beispiel-Compose-Datei verwendet `name: ${APP_NAME}` als Compose-Projektnamen. Dadurch bleiben Container, Netzwerke und Volumes auch dann pro Anwendung getrennt, wenn mehrere Apps jeweils aus einem Ordner namens `current` gestartet werden.

Die Beispiel-Compose-Datei verwendet ein Docker Volume:

```yaml
volumes:
  - app_data:/app/data
```

Docker Volumes bleiben beim Austausch von `current`, beim erneuten `docker compose up` und bei normalen Container-Neustarts erhalten. Das ist fuer persistente Daten gewollt.

Vorsicht bei Datenbankmigrationen: Ein neues Release kann Daten dauerhaft veraendern. Ein Rollback des Containers macht solche Migrationen nicht automatisch rueckgaengig. Fuer Datenbanken sollten Backups, getestete Migrationspfade und klare Vorwaerts-/Rueckwaertsstrategien definiert werden.
