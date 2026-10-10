# Gemeinsame Edge-Runtime und zentrale Softwareupdates

Der reguläre Ablauf liefert signierte Edge-Pakete inzwischen direkt mit dem
zentralen Update und importiert sie auf dem Offline-Server automatisch:
[Edge-Updates mit dem zentralen Offline-Update](../operations/central-offline-edge-updates.md).
Der nachfolgend beschriebene separate Export und Upload bleibt zusätzlich verfügbar.

Für den geführten UI-Ablauf einschließlich RSA-Schlüsselverwaltung siehe
[Updates, Installation und RSA-Vertrauen](../operations/deployment-workspace.md).
Die Windows-Deployment-UI integriert den bestehenden Edge-Builder; die Web-App
wählt passende Pakete zum zentralen Codestand automatisch pro Gerät vor.

Für FPS, Verarbeitungszeiten und Engine-Wiederverwendung siehe
[Edge-Leistung und persistente TensorRT-Engines](edge-runtime-performance.md).

Stand: 6. September 2026. Implementierter Softwarepfad; Hardwareabnahme auf den
Zielgeräten steht aus. Die bestehende Windows-/WSL2-Plattform bleibt die Zentrale.

## Was sich bei neuen Nodes ändert

Neue Workloads verwenden dieselbe `cv-runtime`-Registry, Handler, Payloads und
Pipeline-Engine wie der Server. Zustand bleibt im vorhandenen
`RuntimeExecutionContext.state` und wird je Graph seriell verarbeitet. Temporale
Inferenz verwendet auch auf dem Edge die gemeinsamen Feature-, Scaler-, Winkel-
und Konfidenzverträge. Bestehende E1-Manifeste bleiben im bisherigen Executor
ausführbar; neue Releases kennzeichnen `executor=cv_runtime_v1`.

Ein neuer portabler Registry-Node benötigt damit keine zweite Edge-Implementierung.
Sein Python-Code und seine Abhängigkeiten müssen dennoch auf das Gerät gelangen:
dafür gibt es signierte Runtime-Pakete. Ein reines Pipeline- oder Konfigurationsupdate
verteilt keinen ausführbaren Node-Code. Der Server und das Edge vergleichen den
SHA-256-Digest des gesamten `cv-runtime`-Codes sowie Node-Vertragsversionen vor der
Workload-Aktivierung. Ein anderer Codestand blockiert das neue Workload mit einem
Hinweis auf das notwendige Runtime-Update. Laufende Workloads werden dadurch nicht
ungefragt ersetzt.

Nodes für synchronisierte Kameraaufnahme, zentrale Snapshots/Testfotos und Capture
werden ohne lokale Adapter nicht als portable Edge-Fähigkeit veröffentlicht. Weitere
Nodes können in der Registry `compatibility_constraints.edge_supported=false` setzen.
Neue Kamera-SDKs, Betriebssystemtreiber, Python-Basisversionen oder CUDA/TensorRT-
Basiswechsel bleiben Plattformwartung. Beliebiger zukünftiger Code ist ohne passende
Abhängigkeiten und Hardware nicht automatisch ausführbar. Im gemeinsamen
Edge-Executor bleiben SPS-Schreibzugriffe ausdrücklich deaktiviert.

## Einmalige Einrichtung

Neue Offline-Bundles enthalten `cv-edge` und den stabilen
`deployment/edge/edge-runtime-launcher.py`. Die vorhandenen Windows- und
Jetson-Installer richten den bestehenden Agent-Dienst auf diesen Launcher ein.
Bestehende Installationen erhalten diesen Bootstrap einmal über denselben Installer
mit unverändertem Installations- und Datenverzeichnis. Ohne explizite neue
Gerätekonfiguration bleibt die bestehende `device.env` erhalten. Keine OS-, Kamera-
oder Daten-Neuinstallation ist nötig. Dieses einmalige Bootstrap-Update in einem
Wartungsfenster durchführen; es ist selbst noch kein Software-Slot-Rollout.

Der externe öffentliche RSA-Release-Schlüssel muss bereits vertrauenswürdig
provisioniert sein. Ein Paket kann seinen eigenen Vertrauensanker nicht setzen:

| Ort | Standardpfad |
|---|---|
| Zentrale in WSL2 | `/srv/cv-platform/config/edge-release-public.pem` |
| Windows-Edge | `C:/ProgramData/YBB/Trust/release-signing-public.pem` |
| Jetson-Edge | `/etc/ybb-edge/release-signing-public.pem` |

`YBB_EDGE_TRUSTED_RELEASE_KEY` kann den Pfad in der jeweiligen Dienstumgebung
überschreiben. OpenSSL muss für Signaturprüfungen verfügbar sein;
`YBB_EDGE_OPENSSL_EXECUTABLE` erlaubt einen absoluten Programmpfad. Der private
Release-Schlüssel bleibt ausschließlich in der Buildumgebung. Nach dem Bootstrap
in der zentralen Geräteansicht **Status abrufen**; das Gerät meldet dann
`software_update_protocol=2` auf Jetson beziehungsweise `1` auf Windows, Codestand und
Node-Verträge. Jetson-Protokoll 2 umfasst die privilegierte Offline-Nachinstallation von UFW;
ältere Launcher benötigen dafür einmal den neu gebauten Offline-Installer. Die Zentrale
berücksichtigt den beobachteten Launcherstand auch bei einem noch älteren aktiven Runtime-Slot.
Der Root-Helfer verwendet den festen Vertrauensanker `/etc/ybb-edge/release-signing-public.pem`.

### Fehler `EdgeSoftwareRelease: system_packages – Extra inputs are not permitted`

Beim zentralen Export werden die internen Edge-Pakete neu gebaut. Trotzdem prüft auf
dem Jetson zunächst der installierte Launcher das Update mit den Bibliotheken seiner
Basisumgebung `/opt/ybb-edge/venv`. Die Bibliotheken im neuen Runtime-ZIP können einen
veralteten Prüfer nicht ersetzen, bevor dieser das Paket akzeptiert hat. Auch ein
aktueller aktiver Runtime-Slot aktualisiert die Launcher-Basis nicht automatisch.

Ein früherer Vollbundle-Build konnte neue Installer-Dateien mit älteren internen Wheels
aus dem Cache kombinieren. Der Builder prüft diese Übereinstimmung jetzt vor der
Bundle-Erzeugung. Zur Reparatur eines betroffenen Geräts in der Builder-UI am
Entwicklungsrechner **Online-Inhalte vorbereiten (Internet)** ausführen, anschließend
ein neues signiertes Jetson-Vollbundle bauen und dessen vorhandenen Installer einmal
mit `--config /etc/ybb-edge/device.env` ausführen. Die bestehende Gerätekonfiguration,
Workloads und Engine-Caches bleiben erhalten. Danach das bereits mit dem zentralen
Update gelieferte Runtime-Paket erneut über das Frontend ausrollen.

Read-only-Diagnose der tatsächlich vom Launcher verwendeten Bibliothek auf dem Jetson:

```bash
sudo /opt/ybb-edge/venv/bin/python -c "import cv_contracts.edge as e; print(e.__file__); print('system_packages bekannt:', 'system_packages' in e.EdgeSoftwareRelease.model_fields)"
sudo cat /var/lib/ybb-edge/software/status.json
```

`False` bestätigt den alten Release-Vertrag in der Launcher-Basis. Bei `True` den
Zeitpunkt und Auftrag des letzten Fehlers prüfen; ein erneutes Starten des Agent-Dienstes
lädt die aktualisierten Basisbibliotheken. Das signierte Manifest nicht bearbeiten und
`system_packages` nicht entfernen: Es beschreibt die offline mitgelieferten Systempakete.

## Paket bauen und verteilen

Zuerst mit dem bestehenden Offline-Builder ein vollständiges, zum Ziel passendes
Bundle aus dem gewünschten Repository-Stand erstellen und entpacken. Danach aus
diesem Bundle das kleinere Softwarepaket exportieren:

```powershell
python scripts/release/build_edge_runtime_update.py `
  --bundle C:\Releases\edge-windows-entpackt `
  --output C:\Releases\edge-runtime-2026-09-06.zip `
  --release-id edge-runtime-2026-09-06 `
  --signing-key C:\ReleaseKeys\release-private.pem
```

Für jede Plattform wird deren passendes Bundle verwendet. Das ZIP enthält
Release-Manifest, RSA-Signatur und das vollständige Wheelhouse. Jetson-Updates enthalten
zusätzlich `system-packages/` mit dem gepinnten lokalen Ubuntu-Repository für UFW und dessen
Abhängigkeiten. Fehlende Downloads erfolgen ausschließlich beim Export am Entwicklungsrechner.
Neue Python-/CUDA-Basisinstallationen bleiben Plattformwartung. Paket-Digest, Wheels, Runtime-Digest
und Node-Verträge stammen aus den tatsächlichen Buildartefakten. Die Signatur umfasst auch
die Prüfsummen der Systempakete. Der Root-Helfer installiert diese vor dem Runtime-Wechsel;
Konfiguration und TensorRT-Cache bleiben erhalten. Wiederholte Exporte
derselben Artefakte mit demselben Schlüssel sind byteidentisch.

Der USB/OpenCV-Kameraadapter ist Bestandteil des Edge-Agenten und gelangt damit sowohl über
ein neu gebautes Vollbundle als auch über ein daraus exportiertes Runtime-Update auf das
Gerät. Windows-Bundles und daraus erzeugte Updates enthalten `opencv-python-headless`; Jetson
verwendet das OpenCV seines freigegebenen Plattform-Images. Der passende UVC-/Betriebssystemtreiber
bleibt Plattformwartung. Nach dem Update muss der Capability-Abgleich `usb_opencv` melden,
bevor ein entsprechend konfigurierter Kanal ausgerollt werden kann.

1. `/edge-devices` öffnen und **Runtime-Paket importieren** wählen. Die Zentrale
   prüft Signatur, alle Wheel-Prüfsummen und die zulässigen ZIP-Pfade.
2. Das freigegebene Paket auswählen. Plattform, Architektur, Python, CUDA und
   TensorRT müssen zum gemeldeten Gerät passen; Abweichungen stehen direkt in der
   Tabelle. Vor dem Rollout den angezeigten Gerätestatus aktualisieren.
3. Ein Gerät oder mehrere passende Geräte auswählen und **Runtime aktualisieren**
   ausführen. Rollouts werden bewusst zentral ausgelöst; ein Import startet sie
   nicht automatisch. Je Gerät läuft höchstens ein Software-Auftrag.
4. Übertragung, Prüfung, Installation, Aktivierung und Ergebnis in der Tabelle
   verfolgen. Die bestehende Artefaktübertragung setzt nach Netzunterbrechungen am
   bestätigten Offset fort. Auftrag und Wiederholungen bleiben in der vorhandenen
   zentralen Fleet-Command-Tabelle nachvollziehbar.
5. Nach Erfolg und erneutem Capability-Abgleich die gewünschte Pipeline über den
   bestehenden Kanal-Preflight und Deploymentpfad aktivieren. Software-Rollback und
   Kanal-/Modell-Rollback sind eigenständige Aktionen.

Das Paketuploadlimit beträgt 8 GiB. Die Zentrale besitzt dafür einen eigenen
Request-Timeout von standardmäßig 3600 Sekunden. Software-Aufträge laufen spätestens
nach zwei Stunden mit einem sichtbaren Fehler aus; danach Gerätezustand prüfen.

Für das Nachrüsten des bestehenden Basler-Adapters kann der Export mit `--refresh-runtime`
ein fertiges Bundle wiederverwenden: aktuelle interne Python-Wheels bauen, fehlendes Pypylon-Wheel
für die Zielarchitektur ergänzen und ein neues signiertes Paket erzeugen. Das Originalbundle und
die Jetson-Systembasis werden nicht verändert. Dies ist ein Online-Schritt am Buildrechner;
Installation und Rollout am Edge bleiben offline-fähig. Server und Edge müssen denselben
Runtime-Stand verwenden. USB-Geräterechte bleiben einmalige Host-Einrichtung. Die konkrete
[Basler-Nachrüstung für Orin Nano und AGX Orin](jetson-orin-nano-inbetriebnahme.md#basler-nachrüsten-ohne-den-laufenden-jetson-build-neu-aufzusetzen)
beschreibt Voraussetzungen und Reihenfolge.

## Aktivierung, Daten und Rollback

Der Launcher installiert offline in
`YBB_EDGE_DATA_ROOT/software/releases/<Paket-SHA256>/venv`. Der bisherige Agent
arbeitet während Übertragung und Vorbereitung weiter. Vor dem Wechsel prüft der
Launcher Signatur, Plattform, freien Speicher, Paketabhängigkeiten, Handler-Imports
und den tatsächlich installierten Runtime-Digest. Erst danach beendet er den Agent
geordnet und startet den Kandidaten. Während dieses Wechsels pausieren die Kanäle.

Der neue Prozess muss den erwarteten Codestand, gesunden Agent-Status und laufende
aktive Workloads mehrfach bestätigen. Schlägt das fehl, startet der Launcher die
vorherige Runtime und prüft auch deren Wiederanlauf. Ein Prozessabbruch während der
unbestätigten Aktivierung führt beim Neustart zurück zum vorherigen Slot. Ein
manueller **Software-Rollback** verwendet denselben kontrollierten Wechsel.

Konfiguration, vorhandener Zustandsspeicher, Kalibrierungen, Modelle, Engine-Cache
und Ergebnis-Outbox liegen außerhalb der Software-Slots und bleiben erhalten.
Zustand im Arbeitsspeicher einer temporalen Sequenz wird beim Prozesswechsel neu
aufgebaut. Softwarepakete müssen die vorhandenen persistenten Datenformate
rückwärtskompatibel halten; destruktive Datenmigrationen gehören nicht in diesen
Updatepfad. Alte Slots werden nicht automatisch gelöscht.

Die lokale Verifikation deckt gemeinsame Graphausführung, temporale Kalibrierung,
Paketexport und Signaturprüfung, dauerhafte Aufträge, Aktivierungsfehler, Neustart-
Recovery und Frontend-Darstellung ab. Vor Produktion auf Windows und Jetson einen
echten Update-/Rollback-Zyklus mit Kamera, TensorRT, aktiven Kanälen und gepufferten
Ergebnissen abnehmen. Diese Abnahme wurde auf dem Entwicklungsrechner nicht ausgeführt.
