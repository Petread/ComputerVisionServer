# Speicher auf Edge-Geräten

Stand: 22. September 2026.

Die Edge-Runtime ist kein Bild- oder Ergebnisarchiv. Der reguläre Betrieb soll
ohne dauerhafte Speicherung jedes Frames funktionieren. Die vorhandene
Kanal-Historienregel und die Ziele des Result-Output-Nodes gelten deshalb bereits
vor einer lokalen Persistierung oder Übertragung.

| Daten | Verhalten |
| --- | --- |
| Kameraframes | Aktuelles Bild und für die Pipeline benötigter Zustand im RAM; keine automatische Fotoablage. |
| Vorschau | Letztes JPEG je Kanal im RAM; insgesamt höchstens 128 Einträge und 16 MiB serialisierte Nutzlast, Ablauf nach fünf Minuten ohne Erneuerung. |
| Leistungsstatistiken | Bestehendes begrenztes Messfenster im RAM; kein Ergebnisarchiv nötig. |
| Historie ausgeschaltet | Kein Ergebnis pro Frame in der lokalen Outbox und keine Ergebnisübertragung an die Zentrale. |
| Historie eingeschaltet | Nur die `history`-Projektionen des Result output; der Kanal-Sampling-Abstand wird vor dem Schreiben angewendet. Keine vollständigen Node-/Inferenzberichte, HTTP-only-Felder oder eingebetteten Bilder. |
| Noch nicht übertragene Historie | Vorhandene dauerhafte Outbox; bei neuen Installationen höchstens 64 MiB Nutzlast oder 10.000 Einträge. Bestätigte Einträge werden gelöscht. |
| HTTP-Trigger ohne Schlüssel | Sofortige Antwort; letzte Antwort nur im RAM. Cache insgesamt höchstens 128 Einträge/16 MiB serialisierte Nutzlast, fünf Minuten. Kein Triggerarchiv; nach Neustart nicht mehr verfügbar. |
| HTTP-Trigger mit Schlüssel | Wiederholungsantwort übersteht Neustarts für fünf Minuten. Höchstens 10.000 Einträge/64 MiB Nutzlast, maximal 512 KiB je Antwort. Neue Aufträge brauchen neue Schlüssel. |
| Übertragungsbestätigungen | Höchstens 4.096 Detailantworten; der bestätigte Sequenzstand pro Boot bleibt erhalten. |
| Gerätekonfiguration, Modelle, Engines, Software | Weiterhin persistent, damit Offlinebetrieb, Neustart und Rollback funktionieren. |

Die Byte-Limits betreffen serialisierte Nutzdaten, nicht die gesamte RAM- oder
SQLite-Dateigröße. SQLite benötigt zusätzlich Seiten und Indizes, verwendet
freigegebene Seiten wieder und muss eine einmal erreichte Dateigröße nicht
sofort verkleinern. Es wird weder pro Frame ein `VACUUM` ausgeführt noch auf
transaktionale Sicherheit verzichtet. Abgelaufene Triggerantworten werden beim
Zugriff, beim Agentstart und im laufenden Worker spätestens beim nächsten
minütlichen Bereinigungslauf entfernt.

Bei voller Outbox werden keine unbestätigten Ergebnisse überschrieben. Neue
Speicherungen melden einen Fehler, bis die Zentrale den Puffer geleert hat.
Neue Ergebnis- und Triggercache-Schreibvorgänge berücksichtigen zusätzlich die
konfigurierte freie Plattenreserve (standardmäßig 2 GiB). Bei vollem Triggercache
wird ein neuer Auftrag mit Schlüssel vor der Verarbeitung mit HTTP 507 abgelehnt;
bereits gespeicherte Schlüssel bleiben wiederholbar. Die Ausführung ohne Historie
und ohne Idempotenzschlüssel benötigt diese Ergebnispuffer nicht.

## Bedienung und Update

1. Zentrale und Edge-Runtime aktualisieren.
2. Unter **Kanal → Konfiguration → Datenerfassung & Historie** die Historie nur
   einschalten, wenn zentrale Aufzeichnungen gewünscht sind. Aufbewahrungsdauer,
   Sampling und maximale Datensätze gelten weiterhin für die zentrale Historie.
3. Im Result-Output-Node nur die tatsächlich gewünschten Felder nach `history`
   projizieren; `response` steuert unabhängig davon die HTTP-Antwort.
4. Kanal neu starten, damit das neue Workload-Paket die Historienregel enthält.

Alte Workload-Pakete ohne Historienregel aktivieren auf dem neuen Agent keine
automatische Übertragung. Bestehende unbestätigte Outbox-Einträge werden weiterhin
übertragen. Bereits lokal gesetzte `YBB_EDGE_OUTBOX_QUOTA_BYTES` und
`YBB_EDGE_OUTBOX_MAX_ITEMS` werden durch ein Runtime-Update nicht überschrieben.
Der frühere Standard war 2 GiB/250.000 Einträge. Für diese Änderung ist keine
erneute Betriebssysteminstallation auf dem Jetson nötig.

`result_sequence` ist jetzt `null`, wenn kein Historiendatensatz erzeugt wurde.
Die Idempotenz-Wiederholung ist nach fünf Minuten ausdrücklich nicht mehr
garantiert. Innerhalb des Fensters bleiben Antwortformat und Inhalt auch über
Pipelinewechsel erhalten. Bereits mit der älteren Nummerierung hinter einem
bestätigten Sequenzstand liegende Ergebnisse werden beim Agentstart mit neuen
Sequenznummern wieder übertragbar gemacht, statt sie still zu löschen.

## Grenzen der Absicherung

Diese Änderung begrenzt den laufenden Bild-/Ergebnisstrom und dessen Antwort-Caches.
Sie ist keine globale Bereinigung des Jetson-Dateisystems. Manuell kopierte
Installer in `Downloads`, alte entpackte Softwareversionen und alte TensorRT-
Enginevarianten werden dadurch nicht automatisch gelöscht. Artefaktuploads
haben ihre eigene bestehende Quote und Softwareinstallationen eine eigene
Platzprüfung; der Enginecache besitzt noch keine automatische Gesamtkapazitäts-
bereinigung. Eine pauschale Garantie, dass das gesamte Gerät nach beliebig vielen
Updates niemals voll wird, wäre deshalb falsch. Aktuelle Modelle, Engines und
Rollbackstände dürfen nicht ungeprüft entfernt werden.

## Lokale Verifikation

Unit-Tests prüfen ausgeschaltete Historie, projizierte Inhalte, Sampling,
kontinuierliche Sequenzen nach ACK, Wiederherstellung älterer Sequenzkollisionen,
Cache-Limits und Ablauf, Trigger ohne Plattenspeicherung, Neustart-Replay,
In-Memory-Vorschau, Plattenreserve und die Übernahme der Historienregel in
content-addressed Workload-Pakete. FPS und physische Datenträgerentwicklung müssen
auf dem Zielgerät beobachtet werden; lokal steht kein Jetson zur Verfügung.
