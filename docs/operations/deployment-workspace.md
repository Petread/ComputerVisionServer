# Updates, Installation und RSA-Vertrauen

Stand: 21. September 2026. Gemeinsamer Einstieg ist die bestehende
`YBB-Deployment-UI.exe` mit dem aktualisierten `windows-deployment-ui.ps1`.
Der Edge-Arbeitsbereich verwendet den bestehenden nativen Bundle-Builder direkt
im selben Fenster. Die bekannten Skripte und der separate Edge-Builder bleiben nutzbar.

Die gemeinsame UI wird in diesem Betrieb ausschließlich auf dem Entwicklungsrechner
mit Internetzugang verwendet. Sie erstellt Offlinepakete und verwaltet die
Signaturschlüssel. Installation und Updates auf dem Server verwenden weiterhin den
bestehenden Offline-Installationsweg; die UI steuert keine Ferninstallation.
Im Repository zeigt sie deshalb nur **Übersicht**, **Zentrale: Paket**, **Edge** und
**Modelle**. Lokale Serverinstallation, Portproxy und das Schreiben eines zentralen
WSL-Vertrauensankers sind in dieser Ansicht deaktiviert.

## Normaler Updateablauf

1. **Entwicklungsrechner → Zentrale: Paket:** Profil **Anwendungsupdate (empfohlen)**
   wählen. Weboberfläche und Anwendung werden beim Export gebaut;
   das Python-Wheel-Paket für Service- und Deployment-Abhängigkeiten bleibt enthalten.
   MediaMTX, GStreamer und Triton werden vom installierten Server weiterverwendet.
   **PLC-Schreiben erlauben** ist in diesem Profil standardmäßig aktiv.
2. Im selben Export einmal die passenden Edge-Basisbundles und den privaten
   Release-Schlüssel unter **Edge-Profile und Signierschlüssel einrichten / ändern**
   hinterlegen. **Edge-Runtime-Pakete im Update mitliefern** bleibt aktiv; bei
   verschlüsseltem Schlüssel die Passphrase eingeben. Der Export baut und signiert
   die Edge-Pakete gemeinsam mit dem zentralen Update. **Update exportieren** starten.
3. Zentrales Update-ZIP auf den Zielserver übertragen und über den bestehenden
   Offline-Updater beziehungsweise den vorhandenen `incoming`-Ordner einspielen.
   Die gemeinsame Builder-UI bleibt auf dem Entwicklungsrechner.
4. **Admin-Web-App → Edge-Orchestrierung:** Die mitgelieferten Pakete sind bereits
   geprüft und importiert. Die automatische Auswahl verwendet das exakte
   Sollpaket des zentralen Releases, auch bei reinen Agent-Updates. Geräte auswählen
   und aktualisieren. Fehlende Pakete oder unpassende Plattformen werden angezeigt.
5. Nach erfolgreichem Runtime-Wechsel **Status abrufen**, dann den Kanal-Preflight
   erneut ausführen und das geprüfte Kanal-Release ausrollen.

Beide Pakete müssen aus demselben Runtime-Quellstand entstehen. Änderungen nur an
der Weboberfläche erzwingen durch diese Prüfung kein Edge-Update. Der bestehende
Vergleich des gesamten Runtime-Codes bleibt aktiv; er wird nicht abgeschwächt.
Details zu Bereitstellung und Rückwechsel stehen unter
[Edge-Updates mit dem zentralen Offline-Update](central-offline-edge-updates.md).
Der Neuaufbau eines Edge-Geräts bleibt beim vorhandenen Edge-Installer am
Entwicklungsrechner. Der separate Runtime-Export und manuelle Paketimport bleiben
für bestehende Abläufe verfügbar.
Ein manuell gewähltes älteres Paket kann kompatibel sein, beseitigt aber die
Preflight-Sperre für den aktuellen zentralen Runtime-Stand nicht. Die UI zeigt dies an.

Der Preflight zeigt bei `admission.runtime.code` jetzt einen direkten Weg zum
Runtime-Update. Ein Paketimport startet noch keinen Rollout. Während des
Runtime-Wechsels pausieren die Kanäle. Daten, Kalibrierungen und Konfiguration bleiben
im bestehenden persistenten Speicher. Software-Rollback liegt unter **Wiederherstellung**.

### Was das Profil Anwendungsupdate überspringt

**MediaMTX**, **APT**, **Training-Runtime** und **GStreamer überspringen** sind
standardmäßig ausgewählt, ebenso Triton. Voraussetzung ist eine bereits vollständig
installierte und mit dem neuen Anwendungsstand kompatible Serverbasis.
Der Updateexport ruft die APT-/Training-Vorbereitung der Builder-WSL ohnehin nicht
auf; diese beiden Optionen wirken nur beim Installerbau. Die bestehende
Training-Umgebung wird weitergenutzt; notwendige Python-Pakete für den Deploymentpfad
werden weiterhin aus dem Wheel-Paket aktualisiert.

Ohne MediaMTX-Archiv startet und prüft der Updater das installierte Windows-MediaMTX.
Ohne GStreamer-Paketbundle müssen unter anderem GStreamer, TURN und die benötigten
Plugins bereits vorhanden sein. Die vorhandenen Prüfungen am Ziel bleiben aktiv.
Bei fehlenden Komponenten, neuen Systemabhängigkeiten oder einem geplanten
Versionswechsel unter **Benutzerdefiniert** die betroffenen Skip-Optionen deaktivieren
und das entsprechende Paket mitliefern. Für einen neuen Server das Installerprofil nutzen.

In der UI haben Skip-Optionen Vorrang vor Archivpfaden aus der Basis-Konfiguration
oder dem MediaMTX-Dateifeld. So fügt ein gespeicherter Archivpfad die übersprungene
Komponente nicht unbemerkt wieder hinzu.

## RSA-Schlüssel ohne PowerShell-Kommandos

### Vorhandenen Jetson weiterverwenden

Unter **Edge → RSA-Schlüssel verwalten** den bisherigen privaten RSA-Schlüssel
auswählen, gegebenenfalls seine Passphrase eingeben und einen neuen Dateinamen für
den öffentlichen Export wählen. **Prüfen und übernehmen** leitet den öffentlichen
Schlüssel ab und zeigt seinen SHA-256-Fingerabdruck über die kanonische SPKI-DER-Darstellung.
Ein vorhandener privater Schlüssel wird dabei nicht verändert.

Auf Zentrale und Jetson muss der öffentliche Schlüssel zum Signaturschlüssel passen.
Ist er dort schon richtig hinterlegt, ist kein erneutes Provisionieren erforderlich.
Aus einem öffentlichen Schlüssel kann kein privater Signaturschlüssel gewonnen werden.

### Neue Vertrauenskette

**Neues Schlüsselpaar erzeugen** aktivieren, zwei neue PEM-Dateipfade wählen und
eine Passphrase mit mindestens zwölf Zeichen zweimal eingeben. Es entsteht ein
RSA-Schlüssel mit 3072 Bit und verschlüsselter privater PEM-Datei. Bestehende Dateien
werden nicht überschrieben. Auch vollständige Edge-Bundles unterstützen diese
verschlüsselten Schlüssel; vor Freigabe wird ihre Signatur mit dem öffentlichen
Schlüssel geprüft.

Private PEM und Passphrase bleiben in der Buildumgebung. Passphrasen werden über
eine private Prozesseingabe übergeben, nicht als Kommandozeilenargument, Umgebungsvariable
oder Konfigurationsdatei gespeichert. Die UI merkt ausschließlich Dateipfade und
Arbeitsoptionen. Eine Sicherung der privaten PEM und der Passphrase muss separat
aufbewahrt werden; der öffentliche Export reicht zur Wiederherstellung nicht aus.

### Öffentlichen Schlüssel auf den Zielen hinterlegen

Erzeugen und Exportieren der RSA-Schlüssel erfolgen unter **Edge** auf dem
Entwicklungsrechner. Der öffentliche Export wird separat auf die Zielsysteme
übertragen und dort über deren vorhandenen Provisionierungsweg hinterlegt.
Die folgenden lokalen Zielsystem-Funktionen bleiben für bestehende Offlinepakete
erhalten; sie sind keine Funktionen zum Fernzugriff vom Entwicklungsrechner:

- **Windows-Zentrale → RSA-Vertrauen:** WSL-Distribution und öffentliche PEM wählen,
  **Schlüssel prüfen**, Fingerabdruck vergleichen und **In Zentrale hinterlegen**.
  Ziel ist `/srv/cv-platform/config/edge-release-public.pem`. Bei einem Wechsel
  erscheint eine Bestätigung mit altem und neuem Fingerabdruck; die bisherige öffentliche
  Datei wird gesichert. Zwischen Prüfung und Übernahme veränderte Schlüssel werden abgewiesen.
- **Jetson → vorhandener Installer:** Öffentliche PEM wählen und **Nur RSA-Vertrauen
  hinterlegen**. Fingerabdruck bestätigen. Diese Aktion ändert ausschließlich
  `/etc/ybb-edge/release-signing-public.pem`, legt bei einem Wechsel eine Sicherung an
  und installiert weder Runtime noch Netzwerk neu. Die bestehende Rechteerhöhung
  über `pkexec` beziehungsweise `sudo` wird weiterverwendet.
- **Windows-Edge:** Die bereits vorhandene öffentliche Schlüsselauswahl im nativen
  Edge-Installer verwenden.

Bei eigenen `YBB_EDGE_TRUSTED_RELEASE_KEY`-Dienstpfaden muss der öffentliche Schlüssel
am tatsächlich konfigurierten Pfad liegen. Die zentrale UI bedient den oben genannten
Standardpfad. Ein Paket setzt niemals selbst seinen Vertrauensanker. Bereits
importierte Pakete werden durch einen Schlüsselwechsel nicht automatisch entfernt;
ein Edge prüft sie beim Rollout gegen seinen dann gültigen Schlüssel.

## Installation und gespeicherte Einstellungen

**Zentrale: Paket → Erstinstallation / GPU-Basis mitliefern** ist für einen neuen
Server oder eine bewusst erneuerte GPU-Basis vorgesehen. Ohne vorhandenes
Triton-Archiv kann der bestehende Builder dafür Docker auf dem Entwicklungsrechner
verwenden. Das Produktionsziel bleibt Windows/WSL2.

Sonderoptionen liegen unter **Erweiterte Einstellungen**. Nur **Benutzerdefiniert**
gibt die einzelnen Export-Checkboxen frei. Das Standardprofil setzt die Optionen
konsistent zurück. Im Profil **Anwendungsupdate** ist PLC-Schreiben ausdrücklich
freigegeben. Im Erstinstallationsprofil bleibt es aus; unter **Benutzerdefiniert**
ist es einstellbar und wird mit den übrigen Optionen gespeichert.

Nicht geheime Einstellungen liegen unter
`%LOCALAPPDATA%\YBB\Deployment\central-ui.json` und `edge-builder.json`.
Letztes Edge-Basis-Build, Schlüsselpfade sowie Netzwerk- und Runtime-Profile werden
wiederverwendet. Update-ZIP und Release-Version werden nicht automatisch als nächster
zentraler Auftrag wiederholt. Paketbau und Schlüsselverwaltung für private Schlüssel
finden im vorhandenen Repository auf dem Entwicklungsrechner statt;
fertige Edge-Pakete werden auf dem Zielserver über die bestehende Web-App verteilt.

## Verifikation

Lokal geprüft werden native Builds, Initialisierung und Layout der WinForms-Oberfläche,
Exportprofile, Frontend-Unit-Tests, TypeScript und Produktionsbuild sowie
Schlüsselerzeugung, verschlüsselte Signatur, Schutz bestehender Schlüssel,
Fingerabdrücke, Schlüsselwechsel und isoliertes Hinterlegen am Jetson.
Die tatsächliche Installation in WSL2 und ein Update-/Rollback-Zyklus mit laufenden
Kameras bleiben Abnahmen auf dem separaten Zielserver beziehungsweise Edge-Gerät.

Bekannte Abweichung im vorhandenen Arbeitsstand: Der bestehende Pakettest
`test_signed_package_rejects_tampering_and_undeclared_files` findet fünf zusätzlich
registrierte Nodes aus `bark_registry.py`, die der bisherige statische Manifestexport
nicht erfasst. Derselbe Fehler wurde mit dem unveränderten Exporter aus `HEAD`
reproduziert. Diese separat laufende Runtime-Erweiterung wird hier nicht verändert;
ihre Manifestabdeckung muss vor dem Ausrollen von Paketen mit diesen Nodes geklärt werden.
