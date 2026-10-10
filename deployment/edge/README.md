# YBB Edge – Offline-Bundle und Provisionierung

Der E2-Builder erzeugt vollständig offline installierbare, reproduzierbare Bundles für:

- NVIDIA Jetson Orin Nano (`aarch64`);
- NVIDIA Jetson AGX Orin (`aarch64`);
- Windows 11 mit NVIDIA GPU (`x86_64`).

## Native Windows-Anwendung

Bundle-Builder und Windows-Geräteinstaller sind kompilierte C#-WinForms-Anwendungen. Beide werden
auf Windows mit demselben Buildskript erzeugt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deployment\edge\bundle-builder\Build-NativeBundleBuilder.ps1 `
  -OutputDirectory .\dist\edge-bundle-builder
```

Das Ergebnis enthält `YBB-Edge-Bundle-Builder.exe` sowie
`YBB-Edge-Windows-Installer.exe`. Anschließend den Bundle Builder starten, Zielplattform wählen und alle Release-, Geräte-,
Netzwerk-, Runtime-, Kamera- und Signaturparameter setzen. Bei der ersten Verwendung ist das
plattformbezogene Wheelhouse noch nicht vorhanden:

1. `Wheelhouse-Verzeichnis` auf dem vorgeschlagenen plattformspezifischen Wert belassen oder ein
   neues, leeres Ziel angeben.
2. Optional eine interne HTTPS-Paketquelle und ein lokales Verzeichnis mit freigegebenen
   Hersteller-Wheels angeben.
3. Auf dem Entwicklungsrechner CPython 3.12 mit `pip` installieren und im Feld `Python für
   Build` dessen ausführbare Datei angeben.
4. `Online-Inhalte vorbereiten (Internet)` ausführen und den bewussten Online-Schritt bestätigen.
   Für Windows werden dabei zusätzlich die fest versionierte CPython-3.12-Runtime, WinSW, der
   vollständige lokale CUDA-Installer und die nativen TensorRT-Windows-Wheels geladen und gegen
   die in `windows-payload-sources.json` hinterlegten Größe- und SHA-256-Vorgaben geprüft. Dieser
   einmalige Download umfasst mehr als 4 GB. Bereits vollständig vorbereitete Payloads und
   Wheelhouses werden anhand ihrer Source-Locks verifiziert und wiederverwendet.
   Für Jetson werden UFW und seine Ubuntu-24.04-ARM64-Abhängigkeiten anhand von
   `ubuntu-noble-arm64-ufw.lock.json` vorbereitet (rund 30 MB). Der Offline-Build nimmt
   diese Dateien automatisch in jedes neue Jetson-Bundle auf.
5. Danach `Pfade prüfen` und `Bundle bauen` ausführen. Diese beiden Schritte benötigen keine
   Internetverbindung.

Bestehende Wheelhouses mit gültigem Source-Lock werden bei der Vorbereitung geprüft;
Hersteller- und Fremdpakete werden wiederverwendet, die internen Python-Pakete aus dem
aktuellen Repository neu gebaut und der Source-Lock aktualisiert. Nach Codeänderungen
deshalb vor einem neuen Vollbundle erneut **Online-Inhalte vorbereiten (Internet)** ausführen.
Ein neues leeres Wheelhouse ist dafür nicht erforderlich. `Interne Wheels im Offline-Build
erneut bauen` kann alternativ mit lokal verfügbaren Build-Abhängigkeiten verwendet werden.
Der Bundle-Build vergleicht den Python-Code aller sechs internen Wheels mit dem aktuellen
Repository und lehnt veraltete Pakete ab, auch wenn ihre Versionsnummer unverändert ist.
Damit können aktuelle Installer-Dateien nicht mehr unbemerkt mit alten Runtime-Verträgen
aus dem Cache kombiniert werden.
Die Beispielkonfiguration liegt unter `bundle-builder/example-config.json`.

### Runtime-Update aus einem vorhandenen Build erstellen

Der bestehende Builder bietet **Runtime-Update erstellen …**. Damit lässt sich ein fertiges
Edge-Bundle als `.tar.gz`/`.zip` oder als entpackter Ordner auswählen. Im Dialog eine neue
Release-ID, den Pfad der Update-ZIP und den privaten RSA-Release-Schlüssel angeben, anschließend
**Runtime-Update erstellen** starten. Ein vorhandener Schlüsselpfad aus der Builder-Konfiguration
wird vorbelegt. Die Ausgabe muss einen neuen Dateinamen außerhalb des Basis-Builds verwenden.
Verschlüsselte RSA-Schlüssel werden über das verdeckte Feld **Schlüssel-Passphrase** unterstützt.
Die Passphrase gilt nur für den aktuellen Auftrag und wird über eine Eingabepipe übertragen;
sie wird nicht mit der Konfiguration gespeichert. Schlüssel und Passphrase werden vor dem
Neubauen der Runtime geprüft. Bei unverschlüsselten Schlüsseln das Feld leer lassen.

Die Funktion übernimmt Plattformprofil und Basisabhängigkeiten aus dem ausgewählten Build und
ruft `build_edge_runtime_update.py --refresh-runtime` auf. Python, OpenSSL, HTTPS-Paketquelle und
zusätzliche Hersteller-Wheels stammen aus den bestehenden Builder-Einstellungen. Für das Bauen
der aktuellen Python-Pakete und das Ergänzen fehlender SDK-Wheels kann Internet erforderlich sein.
Fortschritt, Fehler und der fertige Ausgabepfad erscheinen im vorhandenen Protokoll.

Das Basis-Build wird nicht verändert; Python-/CUDA-/OS-Installer werden nicht neu gebaut. Das
fertige signierte Runtime-Paket wird anschließend in **Edge-Orchestrierung → Runtime-Paket
importieren** hochgeladen und mit **Runtime aktualisieren** auf passende Geräte ausgerollt.
Die [Jetson-Anleitung](../../docs/runtime/jetson-orin-nano-inbetriebnahme.md#basler-nachrüsten-ohne-den-laufenden-jetson-build-neu-aufzusetzen)
enthält die Voraussetzungen für Basler und bereits installierte Update-Launcher.

## IP-Adressen und Kommunikationsrichtungen

Die IP-Felder haben unterschiedliche Rollen und sind nicht austauschbar:

| Konfiguration | Beispiel | Bedeutung |
|---|---:|---|
| Edge-Geräte-IP | `192.168.50.21` | Feste IP des Edge-Geräts selbst |
| Orchestrierungsserver-IP | `192.168.50.10` | Feste VLAN-IP des zentralen Servers; darf auf Edge-Port 9443 zugreifen |
| Erlaubte HTTP-Trigger-Clients | `192.168.50.40` | Feste SPS-IP, die HTTP-Aufrufe an Edge-Port 9444 senden darf |
| SPS-Ziel im Pipelineknoten | `192.168.50.40:102` | Ziel, zu dem die Edge-Runtime Siemens-S7-Werte schreibt |

Die SPS-Trigger-Allowlist im Builder steuert nur eingehende HTTP-Aufrufe von der SPS zum Edge.
Sie konfiguriert keine SPS-Schreibadresse. SPS-IP, Port, Rack, Slot, DB, Byte und Bit für einen
Schreibzugriff liegen ausschließlich im Runtime-Pipelineknoten `Write to Siemens SPS` und werden
mit dem Kanal-Workload an das Edge-Gerät übertragen. Hardware-Schreiben erfolgt nur, wenn der
Knoten aktiviert, `dry_run=false` und zusätzlich das Builder-Gate
`SPS-Schreiben aus Pipeline erlauben=true` gesetzt ist.

Bleibt `Erlaubte HTTP-Trigger-Clients` leer, erhält kein entfernter Teilnehmer eine
Firewallfreigabe für Port 9444. Soll auch der Orchestrierungsserver einen Testtrigger senden
dürfen, muss seine IP explizit zusätzlich in diese Liste aufgenommen werden.

## Build-Regeln

Der Befehl `build` des Build-Kerns `scripts/release/build_edge_offline_bundle.py` lädt nichts aus
dem Internet. Er verwendet ausschließlich das gewählte lokale Wheelhouse und die vorbereiteten
Windows-Payloads. Der explizite Befehl `prepare-wheelhouse` darf in der vorbereitenden
Buildzone auch die gepinnten Ubuntu-Dateien für Jetson bereitstellen. Bei fehlenden oder
beschädigten Cache-Dateien fordert `build` diesen Vorbereitungsschritt an. Der Befehl lädt
selbst weiterhin nichts nach. `prepare-wheelhouse` darf außerdem in der vorbereitenden
Buildzone Pakete aus der konfigurierten HTTPS-Quelle beziehen; er lädt passend zur gewählten
Plattform, Architektur und Python-ABI und erzeugt `wheelhouse-source.lock.json`. Für Windows lädt
derselbe bestätigte Online-Schritt die in `windows-payload-sources.json` festgeschriebene
Python-Runtime, WinSW, den CUDA-Offlineinstaller und TensorRT, prüft Größe und SHA-256 und erzeugt
`windows-payload-source.lock.json`.
Jedes Bundle enthält:

```text
device.env
platform-profile.json
provisioning.json
release-manifest.json
wheelhouse.lock.json
SIGNATURE_POLICY
SHA256SUMS
deployment/edge/*
deployment/edge/provisioning/*
wheelhouse/*.whl
system-packages/*.deb          # nur Jetson: UFW und Abhängigkeiten für Noble/ARM64
system-packages/Packages       # lokales APT-Repository
system-packages/manifest.json  # Zielbasis und Prüfsummen
python-runtime.zip             # nur Windows, verpflichtend
winsw.exe                      # nur Windows, verpflichtend
cuda-runtime-installer.exe     # nur Windows, vollständiger lokaler CUDA-Installer
wheelhouse/tensorrt*.whl       # nur Windows, Python-API, Bindings und native TensorRT-Laufzeit
YBB-Edge-Windows-Installer.exe # nur Windows, native WinForms-Erstinstallation
deployment/edge/Install-JetsonOfflineUI.sh
deployment/edge/install-jetson-offline-ui.py
SHA256SUMS.sig                 # bei signed/required
release-signing-public.pem     # bei signed/required
release-signing-key.json       # Public-Key-Fingerprint
```

`SHA256SUMS` ist verpflichtend. Die Installer brechen bei fehlender oder fehlerhafter Prüfsumme
ab. Für Produktion ist in der GUI `Signatur verpflichtend=true` zu setzen; privater und
öffentlicher RSA-Schlüssel werden nur als Build-Inputs verwendet, der private Schlüssel wird nie
in das Bundle kopiert. Der Public Key wird bei der Golden-Image-Provisionierung als externer
Trust-Anker gepinnt; der Installer vertraut niemals einem austauschbaren Bundle-Key.

## Jetson

Die vollständige, praktisch abgenommene Inbetriebnahme eines Jetson Orin Nano mit bereits
konfiguriertem Netzwerk ist unter
[Jetson Orin Nano – Inbetriebnahme](../../docs/runtime/jetson-orin-nano-inbetriebnahme.md)
dokumentiert.

1. Golden-Image-Manifest unter `provisioning/golden-images/` wählen.
2. Jetson Linux/JetPack auf eine vorbereitete NVMe flashen.
3. State Root auf der NVMe mounten.
4. Das plattformspezifische `tar.gz`-Bundle entpacken und lokal starten:

   ```bash
   bash deployment/edge/Install-JetsonOfflineUI.sh
   ```

5. Die Oberfläche liest die signierten Bundle-Defaults, erfasst Hostname, NetworkManager-
   Verbindung, statische IP, DNS, Server, Ports, NVMe-State-Root, Kameras und SPS-Gate. Mit
   `pkexec` beziehungsweise `sudo` führt sie Basisprovisionierung und Offline-Installation aus.
6. Wenn Tk auf einem Desktop-Image verfügbar ist, erscheint eine grafische Oberfläche. Auf
   Headless-Images verwendet dasselbe Startskript die `whiptail`-Terminal-UI beziehungsweise als
   letzte Rückfallebene einen geführten Terminaldialog.
7. Neue Jetson-Bundles installieren fehlendes UFW offline und richten die Edge-Portregeln
   automatisch ein. Bestehendes, deaktiviertes UFW verlangt lokale Firewallwartung.
   Details und der einmalige Umstieg bestehender Geräte stehen unter
   [Edge-Betriebseinstellungen](../../docs/operations/edge-device-settings.md).

Der Provisioner formatiert oder partitioniert niemals einen Datenträger. TensorRT, CUDA und
DeepStream stammen aus dem freigegebenen Golden Image. Jetson nutzt dessen OpenCV; das
Windows-Bundle installiert `opencv-python-headless` aus seinem Offline-Wheelhouse.

## Windows 11

1. Golden-Image-Manifest anwenden. Ein vorhandener freigegebener NVIDIA-Treiber wird
   wiederverwendet; andernfalls kann der mitgelieferte vollständige CUDA-Installer den Treiber
   offline installieren.
2. Die neben dem ZIP erzeugte Datei
   `ybb-edge-windows_11-<version>-installer.exe` per Doppelklick starten. Liegt genau ein passendes
   Bundle-ZIP daneben, wird es automatisch verwendet; andernfalls öffnet die Anwendung einen
   Dateidialog. Sie entpackt das ZIP in ein temporäres Verzeichnis und fordert die
   Administratorfreigabe über UAC selbst an. Alternativ kann die gleichwertige
   `YBB-Edge-Windows-Installer.exe` aus einem bereits entpackten Bundle gestartet werden.
3. Hostname, Netzwerkadapter, statische IP, DNS, Server, Ports, Datenpfad, Kameras und SPS-Gate
   prüfen beziehungsweise anpassen und `Installieren` wählen.

Die native Oberfläche erzeugt die Gerätekonfiguration intern, validiert sie gegen das signierte
Plattformprofil, verifiziert vor jeder Systemänderung das vollständige Bundle und ruft
anschließend die vorhandenen Skripte `Apply-WindowsBase.ps1` und
`Install-WindowsOffline.ps1` auf. Eine `device.env` muss weder erstellt, kopiert noch editiert
werden. Der skriptbasierte Direktaufruf bleibt nur als Diagnose- und Automatisierungsweg erhalten.
Die Signaturprüfung bei der Windows-Installation verwendet Windows/.NET und benötigt kein
separat installiertes OpenSSL. Der öffentliche Release-Schlüssel muss weiterhin als externer
Trust-Anker auf dem Gerät vorhanden oder in der Installationsoberfläche ausgewählt sein.

Die enthaltene vollständige relocatable Python-Runtime muss `venv` und `pip` bereitstellen; das
reduzierte offizielle Embeddable-Paket ist nicht ausreichend. Sie wird lokal entpackt und `pip`
verwendet ausschließlich das Bundle-Wheelhouse. Der Installer installiert bei Bedarf CUDA aus
dem mitgelieferten lokalen Installer sowie TensorRT aus den mitgelieferten Wheels. Danach baut er
ein eingebettetes ONNX-Modell mit dem produktiven TensorRT-Python-Builder, deserialisiert die
Engine und führt eine echte GPU-Inferenz aus. Erst nach erfolgreichem Smoke-Test wird der Dienst
registriert; nur wenn eine CUDA-/Treiberinstallation ausdrücklich einen Neustart verlangt, wird
der Test für den nächsten Installationslauf vorgemerkt.

Pipeline-spezifische ONNX-Modelle werden nicht in einem unveränderlichen Basis-Bundle dupliziert.
Der bestehende Edge-Workload-Builder adressiert und überträgt beim Kanal-Deployment die konkret
aufgelösten ONNX-Dateien, Labelmaps, optionalen INT8-Kalibrier-Caches und Golden Inputs. Der Edge
Agent baut daraus lokal GPU-spezifische TensorRT-Engines. Ein separater Triton Inference Server
ist in diesem Ausführungspfad weder beteiligt noch erforderlich. WinSW registriert den Agenten als
automatischen Dienst. Die Firewall beschränkt 9443 auf die Orchestrierungsserver-IP und 9444 auf
die explizite HTTP-Trigger-Client-Allowlist.

## Capability- und Kamera-Freigabe

`YBB_EDGE_CAPABILITY_INVENTORY=probe` ist für produktive Geräte verpflichtend. Der Agent meldet
reale OS-/NVIDIA-/SDK-Werte, Warnungen und tatsächlich installierte Kameraadapter. Konfigurierbare
Adapter: RTSP, USB/OpenCV, GStreamer, Aravis GigE, Basler Pylon und FLIR Spinnaker. Die Auswahl
muss zusätzlich im Plattformprofil und in `YBB_EDGE_CAMERA_ADAPTERS` erlaubt sein.

Kanäle verwenden für USB/UVC-Kameras den Quellentyp `usb_opencv`. Unter Windows wird die
Kamera über `device_index` (zum Beispiel `0`) adressiert. Auf Jetson/Linux kann ebenfalls ein
Index oder vorzugsweise ein stabiler Pfad unter `/dev/v4l/by-id/` beziehungsweise
`/dev/v4l/by-path/` konfiguriert werden. Optional werden `backend` (`auto`, `v4l2`, `dshow`,
`msmf`), `width`, `height`, `fps`, `fourcc` und `buffer_size` in das Edge-Workload übernommen.
Der Agent aktiviert das Workload nur, wenn sein Capability-Inventar `usb_opencv` tatsächlich
meldet.

Die Softwaretests ersetzen keine Kamera-/Treiber-HIL. Maßgeblich sind
`docs/runtime/edge-orchestration-e2-follow-up-plan.md` und
`docs/runtime/edge-platform-e2-hil-report.md`.

## Zentrale Runtime-Updates

Die Installer richten jetzt den bestehenden Agent-Dienst auf den Runtime-Launcher
ein. Einrichtung, signierte Softwarepakete und Rollback sind im
[Updatehandbuch](../../docs/runtime/edge-runtime-software-updates.md) beschrieben.
Bestehende Gerätekonfigurationen bleiben ohne expliziten Ersatz erhalten.
