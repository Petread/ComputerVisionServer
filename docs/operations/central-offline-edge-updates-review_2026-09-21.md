# Edge-Updates über die Offline-Zentrale

Stand: 21. September 2026. Prüfung des aktuellen Arbeitsstands; der nachfolgend
empfohlene integrierte Export und Import wurde in diesem Auftrag nicht implementiert.

## Ergebnis

Die Integration wurde anschließend im Folgeauftrag umgesetzt. Den aktuellen
Betriebsablauf und die Verifikation beschreibt
[Edge-Updates mit dem zentralen Offline-Update](central-offline-edge-updates.md).
Die folgenden Befunde dokumentieren den Stand vor dieser Umsetzung.

**Ja: Die Zentrale kann Edge-Updates ohne Internet über das vorhandene Frontend
verteilen. Der Übertragungs-, Installations- und Rollbackpfad existiert bereits.**
Heute muss zuvor ein signiertes Runtime-ZIP separat am Entwicklungsrechner gebaut
und im Frontend importiert werden. Der zentrale Offline-Updater übernimmt diese
Bereitstellung noch nicht automatisch.

Empfohlen wird, beim Export des zentralen Updates auf dem Entwicklungsrechner auch
die passenden Edge-Runtime-Pakete zu erstellen und mitzuliefern. Nach dem zentralen
Update stehen sie automatisch im vorhandenen Paketkatalog zur Verfügung. Der
Benutzer startet den Rollout auf ausgewählte Geräte im Frontend. Dafür brauchen
weder Zentrale noch Edge Internet; die Zentrale muss die Geräte im lokalen Netz
erreichen können.

Ein tatsächlicher Neubau aus Quellcode auf der Offline-Zentrale wäre mit zusätzlichen
lokalen Build-Abhängigkeiten, vollständigen Ziel-Wheelhouses und einem privaten
Signierschlüssel möglich. Der aktuelle Builder garantiert diesen Offlinebetrieb
jedoch nicht. Für den gewünschten Bedienablauf bietet dieser zusätzliche Buildort
keinen notwendigen Vorteil.

## Was bereits vorhanden ist

| Teil des Ablaufs | Aktueller Stand |
|---|---|
| Signierte Edge-Runtime-Pakete erstellen | Bestehender Exporter; separate Pakete für die unterstützten Zielprofile |
| Pakete in der Zentrale prüfen und speichern | Bestehender Upload prüft RSA-Signatur, Manifest und Wheel-Prüfsummen |
| Geräte im Frontend aktualisieren | Einzel- und Mehrfachauswahl, Kompatibilitätsprüfung, Fortschritt und Software-Rollback vorhanden |
| Pakete im lokalen Netz übertragen | Bestehender Client überträgt Dateien in Blöcken und setzt unterbrochene Übertragungen fort |
| Edge ohne Internet installieren | Launcher installiert lokale Wheels mit `pip install --no-index --no-deps` |
| Kandidaten aktivieren und bei Fehler zurückwechseln | Bestehende Software-Slots, Abhängigkeits- und Startprüfungen sowie Recovery |
| Edge-Pakete im zentralen Update gezielt mitliefern | Noch nicht integriert |
| Mitgelieferte Pakete automatisch registrieren | Noch nicht integriert |
| Ein eindeutiges Edge-Sollpaket pro zentralem Release festlegen | Noch nicht vorhanden; Auswahl beruht bisher auf Runtime-Digest und Plattformkompatibilität |

Der vorhandene Kanal-/Pipeline-Deploymentpfad erstellt Workload-Manifeste und
verteilt Modelle und Konfiguration. Das ersetzt kein Softwarepaket mit geändertem
Agent- oder Node-Code. Die Prüfung `admission.runtime.code` bleibt deshalb relevant.

## Warum der zentrale Quellcode allein nicht genügt

`deployment/export-windows-update.ps1` erstellt `app.tar.gz` mit dem Repositoryinhalt.
Damit gelangen grundsätzlich auch die Edge-Quellen und Exportskripte auf den
Server. Das Bundle-Manifest und die Liste der gezielt ausgelieferten Dateien
enthalten jedoch keine Edge-Runtime-ZIPs oder verbindliche Zielpaket-Zuordnung.
Ein zufällig im Quellbaum liegendes Edge-Vollbundle wäre kein Ersatz für diese
versionierte Integration.

Das zentrale Python-Wheelbundle wird für CPython 3.11 und Linux x86_64 vorbereitet.
Für ein Jetson werden dagegen passende ARM64-Wheels und die tatsächlich
installierte Python-/CUDA-/TensorRT-Basis benötigt. Windows-Edges benötigen
Windows-Wheels. Maßgeblich ist das freigegebene Profil des jeweiligen Geräts bzw.
seines vorhandenen Basisbundles. Die aktuell hinterlegten Edge-Profile verwenden
CPython 3.12; diese Prüfung bestätigt keine bestimmte Basisversion am realen Gerät.

Beim Edge-Exporter sind zwei Fälle zu unterscheiden:

- **Ohne `--refresh-runtime`:** Vorhandene Wheels und `platform-profile.json`
  werden verpackt und mit OpenSSL signiert. Dieser Schritt benötigt kein Internet,
  übernimmt aber genau den Code aus diesen Wheels. Ein altes Basisbundle liefert
  dadurch noch keinen neuen Edge-Code.
- **Mit `--refresh-runtime`:** Interne Wheels werden aus den aktuellen Quellen
  neu gebaut. `_refresh_internal_wheels()` aktiviert Build-Isolation; die
  Build-Backends können dadurch aus einem Paketindex bezogen werden. Falls Pypylon
  fehlt, lädt `_ensure_basler_wheel()` es über einen HTTPS-Paketindex nach.
  `--find-links` allein deaktiviert diese Indexzugriffe nicht.

Ein zentraler Offline-Neubau müsste zusätzlich sämtliche Build-Backend-Abhängigkeiten
lokal erhalten, Indexzugriffe explizit abschalten und den vollständigen
Abhängigkeitsbestand je Zielprofil prüfen. Die bestehende Wheelhouse-Prüfung prüft
vor allem vorhandene Distributionsnamen; sie ersetzt keine vollständige Prüfung
aller Versionsbedingungen und transitiven Abhängigkeiten.

## Empfohlener integrierter Ablauf

1. **Ein Export am Entwicklungsrechner.** In der vorhandenen Deployment-UI werden
   die benötigten Edge-Zielprofile dauerhaft hinterlegt. Der zentrale Export baut
   aus demselben Quellstand die internen Edge-Wheels, ergänzt passende native
   Abhängigkeiten und signiert je Zielprofil das vorhandene Runtime-ZIP-Format.
   Bereits unverändert freigegebene Edge-Artefakte können wiederverwendet werden.
2. **Ein zentrales Offlinepaket.** Neben der zentralen Anwendung enthält es die
   signierten Edge-ZIPs und eine Zuordnung von zentralem Release und Zielprofil
   zum exakten Paket-SHA256. Dateiprüfsummen gehören in die bestehende
   Bundle-Verifikation. Fehlt ein ausdrücklich benötigtes Zielpaket, muss der
   Export dies als Fehler melden.
3. **Bestehender Installationsweg am Server.** Der Windows-/WSL2-Updater prüft die
   mitgelieferten Pakete und übernimmt sie in den bestehenden persistenten
   Softwarekatalog. Erst zum erfolgreich aktivierten zentralen Release wird dessen
   Sollpaket-Zuordnung angeboten. Importstatus und Fehler müssen sichtbar sein.
4. **Rollout im vorhandenen Frontend.** Pro Gerät erscheinen installierter Stand,
   passendes Sollpaket, Updatebedarf bzw. konkrete Blocker. Der Benutzer wählt
   Geräte und startet das Update. Die bestehenden Fleet-Aufträge liefern
   Fortschritt, Ergebnis und Rollback. Danach folgen Capability-Abgleich und
   erneuter Kanal-Preflight.

Der private RSA-Schlüssel bleibt dabei am Entwicklungsrechner. Zentrale und Edge
benötigen den dazu passenden, bereits vertrauenswürdig provisionierten öffentlichen
Schlüssel sowie OpenSSL. Das Updatepaket darf seinen eigenen Vertrauensanker nicht
ungeprüft ersetzen. Der Server muss für diesen Ablauf keine Passphrase speichern.

Die Edge-Pakete gehören als eigenständiger Bestandteil auch zum Profil
**Anwendungsupdate**. Das Überspringen zentraler MediaMTX-, APT-, Training-Runtime-
oder GStreamer-Pakete darf die Edge-Softwarebereitstellung nicht abschalten.
Treiber-, Betriebssystem-, Python-Basis- oder CUDA-Basiswechsel auf den Geräten
bleiben dagegen Aufgaben des vorhandenen Plattforminstallers.

## Konkrete Lücken vor einer Umsetzung

**Eindeutige Versionsauswahl auch für Agent-Updates.**
`software_overview()` ermittelt `requires_update` nur über den Digest von
`cv_runtime`. Änderungen allein an `edge_agent`, `cv_contracts` oder anderen Wheels
werden darüber nicht erkannt. `recommendedSoftwarePackage()` nimmt das erste
kompatible Paket mit passendem Runtime-Digest; der Katalog ist nach SHA-Dateinamen
sortiert, nicht nach Aktualität. Mehrere solcher Pakete können deshalb zur Auswahl
eines älteren Agent-Stands führen. Die neue Sollzuordnung muss den vollständigen
Paket-SHA verwenden und mit dem aktuellen Gerätestatus abgleichen. Der bestehende
Softwarestatus meldet den aktiven Slot bereits über `active_release`; die Übersicht
muss diesen Zustand auch unabhängig von älteren Rollout-Aufträgen berücksichtigen.

**Vollständige Node-Verträge im Export.**
Der Exporter liest derzeit `_node()`-Aufrufe ausschließlich aus
`cv_runtime/registry.py`. Die im Arbeitsstand zusätzlich eingebundenen fünf
Node-Verträge aus `bark_registry.py` fehlen im exportierten Manifest. Ein vorhandener
Test reproduziert diese Abweichung. Vor der automatischen Veröffentlichung muss
der Export die vollständige gemeinsame Registry korrekt abbilden.

**Import über die Windows-/WSL2-Dateisystemgrenze.**
`import_package()` verschiebt die übergebene Datei mit `os.replace()` in den
Katalog. Der HTTP-Upload legt seine temporäre Datei deshalb bereits dort an.
Ein Offlinebundle liegt typischerweise unter dem Windows-Mount `/mnt/...`;
ein direktes Verschieben in das WSL2-Datenverzeichnis kann an der Dateisystemgrenze
scheitern. Der Installer muss zunächst eine temporäre Kopie im Katalogdateisystem
anlegen und den vorhandenen Prüfer/Importer darauf anwenden. Das ursprüngliche
Offlinebundle bleibt erhalten.

Der Katalog liegt unter
`<storage.state_root>/runtime/edge-workloads/blobs/sha256/software`, auf dem
Standardserver also außerhalb des austauschbaren Anwendungsreleases. Import und
Wiederholung müssen idempotent sein; alte Pakete bleiben für Rückwechsel verfügbar.
Ein Rollback der Zentrale muss wieder deren eigene Sollzuordnung verwenden und
darf keinen ungefragten Edge-Rollout auslösen.

## Verifikation und Grenzen

Am Entwicklungsrechner ausgeführt:

- `test_edge_software_updates.py`: **58 bestanden, 1 fehlgeschlagen**. Der Fehler
  ist die oben beschriebene Abweichung um fünf Node-Verträge. Im betreffenden Test
  wurden vor dieser Assertion bereits Signaturprüfung und Manipulationsfälle
  durchlaufen; spätere Assertions dieses Tests wurden nicht mehr ausgeführt.
- `orchestration.test.tsx`: **3 bestanden**. Abgedeckt sind bestehende
  Paketauswahl, Rollout-Blocker und Darstellung der Bootstrap-Anforderung.
- `test_edge_software_orchestration.py`: Sammlung durch fehlendes `alembic` in
  der lokalen Testumgebung blockiert; die Datenbank-Orchestrierung wurde in diesem
  Auftrag daher nicht erneut per Test bestätigt.

Es fand kein Update eines Servers oder Edge-Geräts statt. Zur Abnahme der späteren
Integration gehört ein vollständiger Durchlauf mit gesperrtem Internet: Export
übertragen, Zentrale aktualisieren, automatische Katalogübernahme, Edge-Rollout,
Wiederaufnahme nach Netzwerkunterbrechung und Rollback. Kamera, vorhandene Daten
und TensorRT-Engine-Wiederverwendung müssen dabei auf dem Zielgerät geprüft werden.

## Beteiligte bestehende Implementierung

- [Zentraler Bundle-Exporter](../../deployment/export-windows-update.ps1):
  `New-AppArchive`, `$bundleManifest`, `$bundleFileNames`.
- [Zentraler WSL2-Updater](../../deployment/wsl/apply-release.sh):
  Release-Prüfung, Aktivierung und bestehender persistenter Datenpfad.
- [Edge-Runtime-Exporter](../../scripts/release/build_edge_runtime_update.py):
  `export_runtime_update`.
- [Edge-Bundle-Builder](../../scripts/release/build_edge_offline_bundle.py):
  `_build_internal_wheels`, `_refresh_internal_wheels`, `_ensure_basler_wheel`,
  `_validate_wheelhouse`.
- [Zentrale Softwareverwaltung](../../backend/services/runtime-admin-api/src/runtime_admin_api/edge_software.py):
  `import_package`, `software_overview`, `queue_rollout`, `process_software_command`.
- [Bestehende API](../../backend/services/runtime-admin-api/src/runtime_admin_api/api/edge_routes.py):
  Paketupload und Software-Aufträge.
- [Frontend-Orchestrierung](../../frontend/apps/admin-web/src/features/edge-devices/orchestration.tsx):
  Paketauswahl und Rollout.
- [Edge-Launcher](../../deployment/edge/edge-runtime-launcher.py):
  `prepare`, Abhängigkeitsprüfung, Aktivierung und Recovery.
- [Bisheriger Betriebsablauf](../runtime/edge-runtime-software-updates.md).
