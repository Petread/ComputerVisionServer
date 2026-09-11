# E2 Follow-up: Provisioning und Plattformen

- Stand: 2026-07-22
- Paket: E2 – Provisioning und Plattformen
- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`
- Vorgänger: M0–M5 und E1

## 1. Ziel und Ergebnis

E2 schließt den softwareseitigen Pfad zwischen einem freigegebenen Release-Input und einem
offline installierbaren Edge-Gerät. Geliefert sind:

- reproduzierbarer Offline-Bundle-Builder mit CI-Gate;
- native Windows-GUIs für Bundle-Build und Geräteerstinstallation sowie eine lokale
  Jetson-Installationsoberfläche;
- verpflichtende SHA-256-Prüfung und vorbereitete/erzwingbare Release-Signaturen;
- versionierte Plattform- und Golden-Image-Profile für Orin Nano, AGX Orin und Windows 11;
- sichere Basisprovisionierung nach dem OS-Flash;
- reale Capability-Inventarisierung;
- Edge-lokale Kameraadapter samt lokaler Admission und automatisierten Tests.

Die Software ist vollständig implementiert. Eine Freigabe konkreter Kamera-, Treiber- und
Hardwarekombinationen erfolgt erst nach dem E2-HIL-Gate.

## 2. Lieferumfang

| Baustein | Umsetzung | Status |
|---|---|---|
| Build-Kern | `scripts/release/build_edge_offline_bundle.py` | fertig |
| Native GUIs | C# WinForms für Bundle-Build und Windows-Installation; Tk/whiptail für Jetson | fertig |
| CI | Windows-GUI-Build, Reproduzierbarkeits-, Contract- und Adaptertests | fertig |
| Integrität | Manifest, Wheel-Lock, Pflicht-SHA-256, Signature-Policy | fertig |
| Plattformprofile | Orin Nano, AGX Orin, Windows 11/NVIDIA | fertig |
| Golden Images | validierbare Manifeste und Post-Flash-Provisioner | fertig |
| Capabilities | reale Betriebssystem-/NVIDIA-/Kamera-Inventarisierung | fertig |
| Kameras | OpenCV, GStreamer, Aravis, Pylon, Spinnaker | softwareseitig fertig |
| Hardwareabnahme | Plattform-/Kameramatrix | ausstehend |

## 3. Native Offline-Bundle-Builder-GUI

Die Anwendung ist in
`deployment/edge/bundle-builder/YBB.EdgeBundleBuilder.cs` implementiert. Das Fenster und die
gesamte Parametrierung sind kompiliertes WinForms; es wird kein PowerShell-Formular gestartet.
Der identische deterministische Build-Kern wird aus der Anwendung und aus CI verwendet, damit
GUI-Builds keinen zweiten, abweichenden Releasepfad erzeugen.

Build der beiden nativen Windows-Anwendungen:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deployment\edge\bundle-builder\Build-NativeBundleBuilder.ps1 `
  -OutputDirectory .\dist\edge-bundle-builder
```

Danach stehen `dist\edge-bundle-builder\YBB-Edge-Bundle-Builder.exe` und
`dist\edge-bundle-builder\YBB-Edge-Windows-Installer.exe` bereit. Die Installer-EXE wird in das
Windows-Bundle aufgenommen und zusätzlich als versionsgleiche Companion-EXE neben dem fertigen
ZIP ausgegeben. Sie kann das danebenliegende ZIP automatisch auswählen und temporär entpacken.

Einstellbar sind:

- Zielplattform, Releaseversion, reproduzierbarer Zeitstempel und Ausgabe;
- lokales Wheelhouse, interner Wheel-Build, Python-Buildruntime und explizite zu installierende
  Runtime-/Kamera-Distributionen;
- vollständige relocatable Windows-Python-Runtime mit `venv`/`pip`, WinSW, lokalem
  CUDA-Offlineinstaller und nativer TensorRT-Laufzeit;
- Hostname, statische IPv4, Präfix, Gateway, DNS, Zeitzone und Interface;
- feste Edge-Geräte-IP, feste Orchestrierungsserver-IP, Anlagen-CIDR und gerichtete Ports;
- optionale SPS-IP-Allowlist für eingehende HTTP-Trigger auf Edge-Port 9444;
- separates globales Sicherheits-Gate für ausgehende SPS-Schreibzugriffe aus der Pipeline;
- State Root, Artefakt-/Outbox-Quota und Mindestfreispeicher;
- Engine-Builder, optionaler `trtexec`-Altpfad und freigegebene Kameraadapter;
- NVMe-Zielangabe und Windows-Netzwerkadapter;
- Signaturpflicht, privater/öffentlicher Releaseschlüssel und OpenSSL.

Konfigurationen können als JSON geladen und gespeichert werden. `Pfade prüfen` validiert lokale
Pflichtinputs. Ein Plattformwechsel setzt passende State-/TensorRT-/Kamera-Defaults, ohne bereits
eingegebene IP-Adressen zu verändern.

Die Online-Inhalte besitzen einen eigenen, ausdrücklich bestätigten Vorbereitungsablauf. Die GUI setzt
pro Plattform ein getrenntes Ziel unter `dist\wheelhouse\<target>` und ruft
`prepare-wheelhouse` auf. Dieser einzige Schritt benötigt Internetzugang zur konfigurierten
HTTPS-Paketquelle. Er baut die internen Wheels, löst Abhängigkeiten für Architektur, Python-Tag
und ABI der Zielplattform auf, bindet optional freigegebene Hersteller-Wheels ein und schreibt
einen Source-Lock. Ein vorhandenes Ziel wird nur mit passendem, vollständig verifiziertem
Source-Lock wiederverwendet. Für Windows lädt derselbe Schritt zusätzlich die in
`deployment/edge/windows-payload-sources.json` fest versionierte Python-Runtime, WinSW, den
vollständigen CUDA-Offlineinstaller und die TensorRT-Windows-Distributionen, verifiziert Größen
und SHA-256-Prüfsummen und schreibt einen separaten Payload-Source-Lock.

## 4. Deterministischer Bundle-Build

Der Builder akzeptiert nur `jetson_orin_nano`, `jetson_agx_orin` oder `windows_11`. Hostname,
IPv4-Felder, Präfixe, Ports und Kamera-Allowlist werden validiert. Der Ablauf ist:

1. Plattformprofil laden und lokale Inputs prüfen.
2. Optional die beiden internen Wheels ohne Dependency-Auflösung bauen.
3. Wheelhouse auf alle erforderlichen Distributionen prüfen.
4. Wheel-ZIPs sortiert mit festen Metadaten normalisieren.
5. Installer, Provisioner, Profil, Geräte- und Provisioningkonfiguration stagen.
6. `wheelhouse.lock.json` und kanonisches `release-manifest.json` erzeugen.
7. Signaturpolicy und Public-Key-Fingerabdruck vorbereiten.
8. Verpflichtende `SHA256SUMS` über sämtliche Payload-Dateien erzeugen.
9. Optional `SHA256SUMS` per RSA-SHA-256 signieren und das reproduzierbare Archiv schreiben.

Der eigentliche Befehl `build` führt keinen `pip download` und keinen anderen Netzaufruf aus. Das
Wheelhouse wird zuvor über den expliziten GUI-/CLI-Schritt `prepare-wheelhouse` in einer Buildzone
mit Internetzugang für Plattform, Architektur und Python-ABI erzeugt und anschließend freigegeben.

CLI-Beispiel:

```powershell
python .\scripts\release\build_edge_offline_bundle.py build `
  --config .\deployment\edge\bundle-builder\example-config.json
```

Vorbereitung per CLI, funktional identisch zur GUI:

```powershell
python .\scripts\release\build_edge_offline_bundle.py prepare-wheelhouse `
  --config .\deployment\edge\bundle-builder\example-config.json
```

## 5. Checksummen und Release-Signatur

`install-jetson-offline.sh` und `Install-WindowsOffline.ps1` brechen ab, wenn `SHA256SUMS` oder
`SIGNATURE_POLICY` fehlen. Jeder Pfad muss innerhalb des Bundle-Roots liegen und jeder Hash
übereinstimmen.

Policies:

- `prepared`: verpflichtende Hashprüfung; Signaturinfrastruktur ist vorbereitet;
- `signed`: Signatur liegt vor und wird immer geprüft;
- `required`: wie `signed`, zusätzlich verbindliche Releasepolicy.

Für Produktion wird `require_signature=true` eingesetzt. Der private RSA-Schlüssel darf nicht im
Repository oder im Bundle landen. Er liegt im geschützten CI-/Release-Keystore. Der Public Key
wird während der vertrauenswürdigen Golden-Image-Provisionierung unter `/etc/ybb-edge/` oder
`C:\ProgramData\YBB\Trust\` gepinnt. Die Installer prüfen `SHA256SUMS.sig` ausschließlich gegen
diesen externen Trust-Anker; der zusätzliche Bundle-Key wird niemals als Vertrauensquelle
verwendet.

## 6. Golden Images und Basisprovisionierung

Versionierte Golden-Image-Manifeste liegen unter
`deployment/edge/provisioning/golden-images/`. Sie definieren Basissystem, NVIDIA-Stack, Python,
Storage und Validierungsprüfungen.

Jetson:

1. NVIDIA-Image gemäß freigegebenem Manifest per SDK Manager/initrd auf NVMe flashen.
2. NVMe als ext4 vorbereiten und am vorgesehenen State Root mounten.
3. `apply-jetson-base.sh` lokal als root mit expliziten Netzwerkparametern ausführen.
4. Offline-Bundle kopieren, Integrität prüfen und installieren.

Der Provisioner verweigert die Ausführung ohne `/etc/nv_tegra_release` und ohne bereits
gemounteten State Root. Er setzt Hostname, NetworkManager-Verbindung, Zeit, Servicekonto,
Verzeichnisse und NVIDIA-Leistungsprofil. Er partitioniert und formatiert niemals automatisch.

Windows:

1. Freigegebenes Windows-11-WIM installieren und das Bundle entpacken.
2. `YBB-Edge-Windows-Installer.exe` lokal starten und die UAC-Freigabe bestätigen.
3. Geräte- und Netzwerkwerte in der UI prüfen; die Anwendung provisioniert Windows, installiert
   den mitgelieferten NVIDIA-/CUDA-/TensorRT-Stack und registriert den Dienst.

Der Provisioner validiert Windows 11, Interface und IPv4-Werte und setzt statisches Netzwerk,
Zeit, State Root und High-Performance-Energieplan. Ein fehlender NVIDIA-Treiber wird anschließend
aus dem mitgelieferten CUDA-Offlineinstaller installiert.

Jetson startet nach dem Golden-Image-Flash `Install-JetsonOfflineUI.sh`. Die Tk-Oberfläche oder
der whiptail-/Terminal-Fallback erfasst dieselben Gerätewerte, ruft `apply-jetson-base.sh` und
`install-jetson-offline.sh` privilegiert auf und erzeugt die Runtimekonfiguration intern. Beide
Plattformen verwenden danach denselben streng validierten `device.env`-Vertrag; eine manuelle
Konfigurationsdatei ist kein Installationsschritt.

## 7. Capability-Inventarisierung

Mit `YBB_EDGE_CAPABILITY_INVENTORY=probe` liest der Agent reale Werte. Er nutzt unter anderem:

- `/etc/os-release`, `/etc/nv_tegra_release`, `dpkg-query` und Device Tree auf Jetson;
- `nvcc`, Python-Import von TensorRT, `trtexec` und `deepstream-app`;
- `nvidia-smi` auf Windows/x86-64;
- Betriebssystem-APIs für RAM und `disk_usage` für State Storage;
- Modul-/SDK- und Geräteerkennung für die Kameraadapter.

Die API liefert `inventory_source` als `probed`, `partial` oder `configured_fallback`, eine
strukturierte `camera_inventory` und `inventory_warnings`. Nur installierte, im Geräteprofil
freigegebene Adapter erscheinen in `camera_adapters` als tatsächlich verwendbar.

## 8. Kameraadapter und sichere Grenzen

| Adapter | Plattformen | Provider | Wesentliche Grenzen |
|---|---|---|---|
| `rtsp` | alle | OpenCV/FFmpeg | explizite URI, Supervisor-Restart |
| `usb_opencv` | alle | OpenCV/UVC | Deviceindex, 16–16384 px, 0,1–240 FPS |
| `gstreamer` | alle mit freigegebenem Build | OpenCV/GStreamer | explizite Pipeline |
| `aravis_gige` | Jetson, optional Linux | Aravis/GStreamer | sichere Kamera-ID, bounded Format/FPS |
| `basler_pylon` | alle mit SDK | pypylon | Serienauswahl, Exposure/Gain, Trigger, Timeout |
| `flir_spinnaker` | alle mit SDK | PySpin | Serienauswahl, Exposure/Gain, Timeout, BGR-Konvertierung |

Vendor-SDKs werden erst bei Verwendung importiert. Fehler bleiben am Kanalworker und lösen den
begrenzten Supervisor-Backoff aus. Der Agent prüft die Geräte-Allowlist nochmals lokal beim
Deployment-Prepare.

## 9. Tests und Gates

Automatisiert abgedeckt:

- byteidentische Archive für alle drei Plattformen;
- Pflichtinhalte, Wheel-Vollständigkeit und Windows-Zusatzpayload;
- positive Hashprüfung und Manipulationserkennung;
- Capability-Vertragsserialisierung und reale Probeauswertung;
- OpenCV-Aufnahmeparameter und Kamera-Registry;
- native C#-Kompilierung in Windows CI;
- Ruff, OpenAPI-Export und bestehende Edge-Regressionstests.

Software-Gate: erfüllt. Hardware-Gate: ausstehend gemäß
`docs/runtime/edge-platform-e2-hil-report.md`. Insbesondere werden echte NVIDIA-Stacks,
statische Netzwerke, Service-Recovery und jede freigegebene Kamera-/SDK-Kombination geprüft.

## 10. Rollback

- Ein Bundle ist unveränderlich; Rollback verwendet die vorherige freigegebene Bundleversion.
- Der Edge-Agent behält seinen bestehenden A/B-Workload-Rollback.
- Basisprovisionierung wird nicht über einen ungesicherten Netzwerk-Rollback zurückgenommen.
  Golden-Image-Rollback erfolgt durch Reimage aus dem freigegebenen Vorgängerimage.
- Bei fehlerhaftem Kameratreiber wird der vorherige Treiber-/SDK-Stand aus dem Golden-Image-
  Inventar wiederhergestellt und der Capability-Snapshot erneut geprüft.
