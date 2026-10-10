# Edge-Updates mit dem zentralen Offline-Update

Stand: 21. September 2026. Der Entwicklungsrechner baut und signiert die Pakete;
die Offline-Zentrale übernimmt und verteilt sie. Ein Edge-Neuaufbau bleibt Aufgabe
des vorhandenen Edge-Installers am Entwicklungsrechner.

## Bedienung

1. `deployment/YBB-Deployment-UI.exe` am Entwicklungsrechner öffnen und
   **Zentrale: Paket → Anwendungsupdate** wählen.
2. Einmal **Edge-Profile und Signierschlüssel einrichten / ändern** öffnen.
   Für jedes vorhandene Geräteprofil das passende bisherige Edge-Basisbundle
   (`.tar.gz`, `.zip` oder entpackter Ordner) hinterlegen. Die Liste enthält einen
   Pfad pro Zeile. Ein vorhandenes Profil samt Schlüsselpfad kann aus dem
   **Edge**-Bereich übernommen werden. Der Export verwendet das Profil aus dem
   gewählten Bundle, einschließlich Python-, CUDA- und TensorRT-Basis.
3. Den vorhandenen privaten RSA-Schlüssel auswählen. Bei einem verschlüsselten
   Schlüssel vor dem Export die Passphrase eingeben. Die UI speichert Profile und
   Pfade, aber keine Passphrase. **Edge-Runtime-Pakete im Update mitliefern** bleibt
   aktiviert. Ohne Edge-Geräte kann diese Option ausdrücklich deaktiviert werden.
4. **Update exportieren** erstellt ein zentrales Update-ZIP einschließlich der
   signierten Edge-Pakete. Die Vorbereitung erfolgt am Entwicklungsrechner und
   kann für Build-Abhängigkeiten oder fehlende Ziel-Wheels Internet benötigen.
5. Das zentrale ZIP über den bestehenden Offline-Installationsweg am Server
   einspielen. Der Updater prüft die mitgelieferten Edge-Pakete und übernimmt sie
   automatisch in den vorhandenen persistenten Katalog. Dafür wird kein Internet
   und kein privater Schlüssel auf dem Server benötigt.
6. Im Server-Frontend die **Edge-Geräte** öffnen, Geräte mit Updatebedarf auswählen
   und **Runtime aktualisieren** ausführen. Die Ansicht zeigt aktives Paket,
   vorgeschlagenes Paket, Fortschritt und Fehler. Anschließend Status abrufen und
   den Kanal-Preflight erneut ausführen.

Der öffentliche RSA-Schlüssel muss auf Zentrale und Edge bereits zum verwendeten
privaten Schlüssel passen. Die bestehende Schlüssel-UI bleibt unter
[Updates, Installation und RSA-Vertrauen](deployment-workspace.md) beschrieben.
Ein Paket ersetzt seinen eigenen Vertrauensanker nicht. Die Passphrase wird durch
die beteiligten Prozesse ausschließlich über Standardeingabe weitergereicht.

Die einmaligen Edge-Profileinstellungen sind eingeklappt. Die Optionen zum
Überspringen von MediaMTX, APT, Training-Runtime, GStreamer und Triton steuern
weiterhin die zentrale Plattformbasis; sie schalten die Edge-Pakete nicht ab.
Das Profil **Erstinstallation / GPU-Basis mitliefern** verwendet weiterhin den
bestehenden zentralen Installerexport. Die automatische Edge-Paketbeilage ist
Bestandteil des zentralen **Updateexports**.

Jetson-Updates enthalten zusätzlich UFW und seine Ubuntu-Abhängigkeiten für Noble/ARM64.
Die APT-Überspringen-Option für den zentralen Server schaltet diese Edge-Beilage nicht ab.
Download und Prüfung erfolgen am Entwicklungsrechner; Zentrale und Jetson benötigen dafür
keinen Internetzugang. Der aktuelle Edge-Offline-Installer richtet einmalig den privilegierten
Systempaket-Helfer und Launcher-Protokoll 2 ein. Anschließend installiert **Runtime aktualisieren**
fehlendes UFW automatisch aus der signierten Beilage. Für ältere Geräte zeigt die Paketauswahl
den erforderlichen einmaligen Installer-Schritt an. Details zu Firewall und Bestandsschutz:
[Edge-Betriebseinstellungen](edge-device-settings.md).

## Paketprüfung und Versionsauswahl

Das zentrale Bundle enthält `edge-runtime/targets.json` sowie signierte
`edge-runtime/<SHA256>.zip`-Dateien. Diese Dateien stehen auch in `manifest.sha256`.
Die Zuordnung enthält das zentrale Release, den Runtime-Digest und die exakten
Paketprüfsummen je Plattformprofil.

Der Export baut die internen Edge-Wheels aus den aktuellen Quellen. Deren
Python-Inhalt muss dem bereits erzeugten `app.tar.gz` entsprechen. Die gemeinsame
Registry wird einschließlich importierter Node-Fabriken ausgewertet. Wheel-Metadaten,
Versionsbedingungen und plattformspezifische Abhängigkeiten werden mit dem
bestehenden Dependency-Checker geprüft. Fehlende Abhängigkeiten werden am
Entwicklungsrechner nachgeladen; eine weiterhin unvollständige Zusammenstellung
bricht den Export ab.

Die Paketidentität hängt vom Edge-Inhalt und Zielprofil ab. Unveränderte Edge-Wheels
können daher bei einem reinen zentralen Update dasselbe Paket ergeben. Ein geänderter
Agent wird dagegen auch bei unverändertem `cv_runtime`-Digest erkannt: maßgeblich
sind das freigegebene vollständige Paket und der beobachtete aktive Software-Slot.
Die Zentrale liest dafür das bestehende `/software`-Statusinterface der Geräte.
Der beobachtete Softwarestand wird im vorhandenen Fleet-Prüfprotokoll gespeichert.
Die Health- und Capability-Daten bleiben auch für ältere zentrale Releases lesbar.

Der Import kopiert Pakete zunächst in das Katalogdateisystem und prüft dort Signatur
und Inhalt. Dadurch funktioniert die Übernahme vom Windows-Mount nach WSL2, ohne
das ursprüngliche Offlinebundle zu verbrauchen. Wiederholte Imports sind möglich.
Der Import lädt dieselbe `runtime.env` wie die produktiven Dienste, während Code
und Konfiguration aus dem Release-Kandidaten stammen. Ein CLI-Import ohne
Produktionsumgebung wird abgewiesen, damit keine Dateien unter `.local/state`
statt im persistenten Produktionskatalog landen.
Die vollständige Sollzuordnung wird erst nach erfolgreicher Paketprüfung in den
Release-Kandidaten geschrieben. Der Import startet keinen Geräte-Rollout.

Ein zentrales Rollback verwendet die Zuordnung des wieder geladenen zentralen
Releases. Bereits importierte Pakete bleiben im persistenten Katalog erhalten.
Geräte werden dabei nicht ungefragt zurückgesetzt. Beim bewusst gestarteten
Softwarewechsel pausieren deren Kanäle; Konfiguration, Kalibrierungen, Modelle und
Engine-Cache bleiben außerhalb der Software-Slots erhalten.

Ältere zentrale Bundles ohne Zuordnung behalten den manuellen Import. Gibt es dabei
mehrere passende Pakete mit demselben Runtime-Digest, wird keine beliebige Datei
automatisch ausgewählt. Der manuelle Import und die abweichende Paketauswahl stehen
im Frontend unter einer eingeklappten Option weiterhin zur Verfügung.

Fehlen Launcher, passender öffentlicher Schlüssel oder passende Python-/CUDA-Basis,
muss die vorhandene Einrichtung korrigiert werden. Betriebssystem-, Treiber- und
Python-Basiswechsel sowie der Neuaufbau eines Edge-Geräts werden durch diese
Runtime-Pakete nicht durchgeführt.

## Fehlender Katalogeintrag und ausgegrauter Update-Button

Die Meldung über ein fehlendes mitgeliefertes Edge-Paket betrifft die exakte
Paketprüfsumme in der automatischen Zuordnung. Ein später separat gebautes Paket
hat eine andere Paketidentität und ersetzt diesen Eintrag nicht automatisch.

Bei einem bereits installierten zentralen Update können die ursprünglichen
`edge-runtime/*.zip` aus dessen Offline-ZIP über **Manueller Paketimport** erneut
importiert werden. Alle in der Zuordnung genannten Pakete müssen verfügbar sein.
Anschließend funktioniert die automatische Auswahl wieder. Ein Neuaufbau des
Jetson ist dafür nicht erforderlich.

Mit der korrigierten Updateverwaltung kann auch bei einer fehlerhaften automatischen
Zuordnung ein gültiges Paket ausdrücklich manuell ausgewählt und ausgerollt werden.
Die Anzeige lautet dann **Ausgewählt**. Die automatische Auswahl bleibt gesperrt,
bis die Zuordnung wieder vollständig ist. Ein fehlendes Paket-ZIP, ein unpassendes
Plattformprofil, ein offline/deaktiviertes Gerät oder ein laufender Softwareauftrag
sperrt weiterhin die jeweilige Installation. Signatur- und Kompatibilitätsprüfungen
werden unverändert durchgeführt.

## Verifikation

Am 21. September 2026 bestanden 80 Backend-/Windows-Tests und 8 Frontend-Tests.
TypeScript-Prüfung, produktiver Frontend-Build, Python-Lint, PowerShell- und
Bash-Syntaxprüfungen sowie der Windows-UI-Smoke-Test waren erfolgreich.
`deployment/YBB-Deployment-UI.exe` wurde neu gebaut.

Unit- und Build-Prüfungen laufen auf dem Entwicklungsrechner. Abgedeckt sind unter
anderem Signaturen, vollständige Node-Verträge, Importwiederholung, falsche und
fehlende Pakete, Codeabweichung gegenüber dem zentralen Archiv, Zielabhängigkeiten,
Agent-Updates mit gleichem Runtime-Digest und die Zuordnung bei zentralem Rollback.
Windows-Tests prüfen außerdem die UTF-8-Passphrase über die UI-Prozesskette und den
Ausschluss des Signierschlüssels und der Edge-Vollinstaller aus dem Anwendungsarchiv.

Die Abnahme mit gesperrtem Internet, realem zentralem Server, Jetson, Kamera und
einem vollständigen Update-/Rollback-Zyklus muss auf den Zielgeräten erfolgen.

## Geräteeinstellungen

Trigger-Client-IP-Adressen und weitere Betriebseinstellungen können nach Einrichtung des
aktuellen Edge-Installers direkt über die Admin-Web-App verwaltet werden.
Siehe [Edge-Betriebseinstellungen](edge-device-settings.md).
