# Basler-Anlauf und Wiederverbindung – 15.09.2026

## Ausgangsbefund

Die Kamera `a2A1920-51gcBAS`, Seriennummer `41949247`, antwortet unter
`172.23.27.243`. Die direkte Registerabfrage ergab CCP `0` und einen
Heartbeat-Timeout von 3.000 ms. Pylon findet sie mit
`AnnounceRemoteDevice("172.23.27.243")`, während die allgemeine Suche bei den
fehlgeschlagenen Aufnahmeversuchen keine Geräte lieferte. Das belegt die
gezielte Erreichbarkeit und fehlende Steuerungsbelegung zum Abfragezeitpunkt,
noch keine erfolgreiche Bildübertragung über diesen Suchpfad.

Das Dienstjournal zeigt rund 115 Sekunden Aufnahme mit 1.148 übernommenen
Bildern bis zum Runtime-Wechsel um 10:29:45. Danach schlägt ein neuer
Aufnahmeprozess bei der Geräteerkennung fehl. Der ursprüngliche Auslöser der
unzuverlässigen allgemeinen Suche und eine mögliche Änderung durch das Update
sind damit nicht abschließend erklärt.

## Implementierte Änderungen

- `realtime_executor.sources` verwendet weiterhin zuerst die vorhandene
  allgemeine Gerätesuche. Fehlt die ausgewählte Kamera oder schlägt diese Suche
  fehl, wird ausschließlich die explizit konfigurierte `ip_address` mit Pylon
  gezielt abgefragt. RTSP-Host, Server-IP und Seriennummer werden nicht in eine
  Kamera-IP umgedeutet. Ungültige IPv4-Adressen sowie Multicast-, Null- und
  Limited-Broadcast-Adressen werden vor SDK-Zugriff abgelehnt.
- Die Antwort wird vor `CreateDevice`/`Open` durch die bestehende Geräteauswahl
  geprüft. Bei konfigurierter Seriennummer muss diese exakt übereinstimmen.
  Eine andere Kamera an derselben IP ist ein Konfigurationsfehler und wird
  nicht geöffnet. Die bisherige Priorität der Seriennummer bei allgemeiner
  Suche bleibt erhalten; die IP ist ein Fallback, keine neue Identität.
- Eine vorübergehend fehlende Kamera oder erfolglose IP-Abfrage führt zu
  `BaslerDeviceUnavailableError`, nicht mehr zum dauerhaft verriegelnden
  Konfigurationsfehler. Der bestehende Quellen-Worker versucht nach
  `reconnect_seconds` erneut zu verbinden (Default fünf Sekunden, mindestens
  0,25 Sekunden). Die Wartezeit ist durch das Stop-Event unterbrechbar. Es gibt
  keine enge Warteschleife und keinen zusätzlichen Aufnahmeprozess pro Versuch.
- Fehler und Wiederverbindungszähler werden über die vorhandene SourceHealth
  gemeldet. Logs unterscheiden allgemeine und gezielte Suche. Nach erfolgreichem
  Öffnen wird der letzte Quellenfehler gelöscht. Der bisherige Auto-Modus mit
  Aravis-Fallback bleibt für seine bisherigen Aufrufer kompatibel;
  `camera-media` verwendet weiterhin direktes Pylon.
- Das Kameraobjekt wird bereits vor `Open()` im Quellenobjekt gehalten, damit
  auch eine teilweise erfolgreiche, anschließend fehlgeschlagene Öffnung den
  vorhandenen Cleanup-Pfad erreicht. Eine bereits gestoppte Quelle beginnt
  keinen neuen Öffnungsversuch.
- Änderungen der Pipeline-Bildansicht starten nur deren abgeleitete
  Bildverarbeitung/Publisher neu. Die rohe Kameraaufnahme wird dafür nicht mehr
  ersetzt. Änderungen der eigentlichen Quellenkonfiguration starten die
  Aufnahme weiterhin neu.
- Beim Prozessstopp wird nach kooperativem Beenden und `terminate()` bei Bedarf
  `kill()` verwendet und das tatsächliche Ende geprüft. Bleibt der Prozess am
  Leben, wird ein Fehler gemeldet und der alte Handle behalten; es darf kein
  Ersatzprozess parallel dieselbe Kamera öffnen. Auch beim Entfernen eines
  Kanals wird der Handle erst nach erfolgreichem Stopp verworfen.

## Konfiguration und Abnahme auf dem Zielserver

Nach Installation des Updates die bestehende Basler-Quellenkonfiguration um
die Kamera-IP ergänzen; alle übrigen Kamera- und Kalibrierungswerte erhalten:

```json
{
  "serial_number": "41949247",
  "ip_address": "172.23.27.243"
}
```

Dies ist ein Ausschnitt zum Zusammenführen mit der bestehenden Konfiguration,
kein vollständiger Ersatz. Ohne `ip_address` greifen die verbesserten
Wiederholungsversuche, aber keine gezielte IP-Suche. Die IP wird weder im
Updater noch im Programm hart codiert. Keine Änderung an Bildrate, Gain,
Belichtung, Paketgröße oder Firewall ist für diesen Fix erforderlich.

1. Kanal starten und im Journal `Basler Pylon directed discovery succeeded`
   prüfen, sofern die allgemeine Suche weiterhin fehlschlägt. Anschließend
   müssen echte neue Frames und die eingestellten etwa zehn FPS ankommen.
2. In einem freigegebenen Testfenster die Kamera kurz unerreichbar machen und
   wieder bereitstellen. Der gleiche Worker muss selbständig erneut suchen
   und die Aufnahme wiederherstellen; ein manueller Kanalneustart darf dafür
   nicht erforderlich sein. Fehler und anschließende Frames gemeinsam prüfen.
3. Eine Pipeline-Bildansicht ändern. Der rohe Kamera-Aufnahmeprozess und seine
   Ringgeneration sollen dabei erhalten bleiben. Eine echte Kameraeinstellung
   ändern und dagegen den geordneten Prozesswechsel prüfen.
4. Mehrere Start-/Stoppwechsel und anschließend mindestens 15 Minuten Betrieb
   beobachten. Kein paralleler Zugriff, dauerhaftes `failed` nach nur einem
   Suchfehler oder Verlust des Rohstreams bei bloßer Ansichtsänderung.

Journal in PowerShell lesen:

```powershell
wsl -d YBB-Inferenzserver -u root -- journalctl -u ybb-camera-media.service --since=-15min --no-pager
```

## Lokale Verifikation und Grenzen

88 Tests in den folgenden Dateien bestanden. Nach den abschließenden Typ- und
Formatkorrekturen bestanden die 26 neuen Basler-/Prozesswechseltests erneut.
Die drei betroffenen Python-Dateien bestehen die Syntaxprüfung und weisen im
Ruff-Vergleich mit HEAD keine neuen Befunde auf; bestehende Befunde in den
Bestandsdateien wurden nicht als Teil dieses Auftrags bereinigt. Der neue Test
besteht Ruff vollständig. `git diff --check` für die geänderten Bestandsdateien
ist ohne Befund.

```text
tests/unit/backend/test_basler_startup_recovery.py
tests/unit/backend/test_camera_media_streaming.py
tests/unit/backend/test_realtime_live_path.py
```

Standardaufruf in der eingerichteten Backend-Testumgebung:

```powershell
python -m pytest -q tests/unit/backend/test_basler_startup_recovery.py tests/unit/backend/test_camera_media_streaming.py tests/unit/backend/test_realtime_live_path.py
```

Auf diesem Entwicklungsrechner wurde der vorhandene lokale Runner
`.test-tmp/run_postupdate_tests.py` mit den bereits vorbereiteten Abhängigkeiten
unter `.test-tmp/stem-deps` verwendet (Python 3.14, Windows). Pylon und die
Prozessfehler werden in den neuen Regressionstests simuliert. Die echte
Linux-/WSL-Prozessbeendigung und SDK-Bildübertragung bleiben Teil der obigen
Zielserver-Abnahme. Verträge und Schemas wurden in diesem Auftrag nicht geändert.

Die zuvor protokollierten `resource_tracker`-KeyErrors sind durch diesen Fix
nicht allgemein behoben. Die Shared-Memory-Eigentümerverwaltung bleibt eine
separate offene Baustelle. Weniger unnötige Kamera-Neustarts ersetzen deren
Korrektur nicht. Ebenso ist die Weitergabe jedes Kamerafehlers bis in alle
Runtime-/UI-Statusansichten hier nicht neu implementiert.

In diesem Auftrag wurden keine Servereinstellungen verändert, kein Updatepaket
erstellt und kein Deployment ausgeführt. Bereits vorhandene fremde Änderungen
bleiben erhalten.

## Nachtrag: fehlendes Frontend-Feld und erneuter Ausfall

Beim erneuten lesenden Abruf am 15.09.2026 gegen 17:43 MESZ enthielt die
Basler-Quelle weiterhin `serial_number=41949247`, aber keine `ip_address`.
Der separate Streamstatus stand auf `recovering` mit
`Basler Pylon discovery unavailable: No Basler Pylon devices were discovered.`
Damit ist die neue Fehlerbehandlung im Betrieb sichtbar, die gezielte IP-Suche
war mangels Adresse jedoch nicht nutzbar. Die Inferenz war separat gestoppt;
der Kamerastream blieb angefordert. Diese Zustände dürfen nicht verwechselt
werden. Die Ursache des ursprünglichen Abbruchs wurde dadurch nicht bewiesen.

Die erste Umsetzung enthielt kein separates Frontend-Eingabefeld für die
Kamera-IP neben der Seriennummer. Diese Bedienlücke wurde jetzt geschlossen:

- Beim Anlegen und Bearbeiten eines Basler-Kanals erscheint `Kamera-IP (optional)`.
- Eine gespeicherte IP wird geladen und mit der bestehenden Seriennummer
  gespeichert. Andere Kameraeinstellungen bleiben im vorhandenen Formularpfad.
- Ungültige IPv4-Adressen verhindern Speichern/Anlegen. Ein leeres Feld bleibt
  zulässig für Seriennummer-/USB-Konfigurationen. Bestehende IP-only-Quellen
  bleiben kompatibel. Bei Seriennummernquellen kann ein alter IP-Fallback
  gezielt gelöscht werden.
- Gemeinsame Feld- und Konfigurationsfunktionen liegen in
  `frontend/apps/admin-web/src/features/channels/basler-camera-address.tsx`.

46 Frontendtests (15 neue Adress-/Formularprüfungen und 31 vorhandene
Kanal-Detailtests), `tsc --noEmit` und `vite build` bestanden. Vite meldet die
bekannte Warnung zur Größe des Hauptbundles. Keine Vertrags-/Schemaänderung.

Der Versuch, ausschließlich die bestätigte Kamera-IP über den vorhandenen
Kanal-Update-Endpunkt einzutragen, wurde vom Server mit HTTP 401 abgewiesen.
Der bereitgestellte API-Schlüssel erlaubt diesen Schreibaufruf nicht. Die IP
wurde durch diesen Versuch nicht gespeichert; kein Kanalstart wurde ausgelöst.

Für die bereits installierte Frontend-Version: Im Bearbeitungsdialog des
bestehenden Kanals im Feld `Source address` die IP `172.23.27.243` eintragen und
speichern. Der geprüfte Bestands-Code führt die daraus erzeugte `ip_address`
mit der geladenen Quellenkonfiguration zusammen; deren Seriennummer bleibt
erhalten. Da die Anzeige die Seriennummer bevorzugt, kann nach erneutem Öffnen
wieder `41949247` im Feld stehen. Beide gespeicherten Werte anschließend per API
prüfen. Dies gilt für das Bearbeiten des bestehenden Kanals; beim Neuanlegen
mit nur einer IP wird keine Seriennummer ergänzt.

Das separate IP-Feld ist lokal gebaut, aber noch nicht auf dem Zielserver
installiert. Der Wiederanlauf mit gespeicherter IP bleibt ausstehend.
