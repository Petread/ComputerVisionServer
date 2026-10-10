# Edge-Betriebseinstellungen aus der Admin-Web-App

Unter **Edge-Geräte → Gerät öffnen → Einstellungen** liest die Zentrale den aktuellen
Gerätestand. **Einstellungen bearbeiten → Speichern und am Edge anwenden** überträgt
die Änderung über die vorhandene Edge-Control-Verbindung. Internetzugang ist dafür
weder am Server noch am Edge erforderlich.

## Bearbeitbare Werte

| Einstellung | Umgebungsvariable | Standard / Grenzen |
| --- | --- | --- |
| Erlaubte HTTP-Trigger-Clients | `YBB_EDGE_PLANT_ALLOWED_IPS` | Leere Liste: keine entfernten Clients; maximal 128 einzelne IPv4-Adressen |
| Trigger-Zeitlimit | `YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS` | 2500 ms; 100–120000 |
| Gleichzeitige Trigger | `YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS` | 4; 1–32 |
| Trigger pro Sekunde und Client | `YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND` | 20; 1–1000 |
| Maximale Trigger-Anfrage | `YBB_EDGE_PLANT_MAX_REQUEST_BYTES` | 65536 Bytes; 1024–1048576 |
| Vorschauqualität | `YBB_EDGE_PREVIEW_JPEG_QUALITY` | 70; 10–95 |
| Maximales Vorschaubild | `YBB_EDGE_PREVIEW_MAX_BYTES` | 1048576 Bytes; 65536–8388608 |
| TensorRT-Bauzeitlimit | `YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS` | 900 s; 60–7200 |

Die Trigger-Clients sind die SPS beziehungsweise Geräte, die HTTP-Aufrufe **zum Edge**
senden. CIDR-Netze, Hostnamen, Ports, IPv6 und Multicast sind in dieser Liste nicht zulässig.
Eine leere Liste entfernt die verwalteten Firewall-Freigaben. Die lokale Loopback-Diagnose
bleibt erreichbar. Das erlaubte Edge-Zielnetz in der zentralen Geräteverwaltung ist eine andere
Einstellung. Die bestehenden Felder für Serveradresse und Ports beschreiben die Verbindung;
sie konfigurieren weiterhin nicht die Netzwerkschnittstellen des Edge.

Weitere Umgebungsvariablen können gezielt in den gemeinsamen Vertrag aufgenommen werden.
Es gibt keinen freien Editor für Prozessumgebung, ausführbare Programme, Signierschlüssel,
Identität oder SPS-Schreibrechte. Das API liefert ausschließlich die oben genannten Werte.

## Einmalige Voraussetzung für bestehende Geräte

Die neue Agent-Version enthält die Einstellungsendpunkte. Der aktuelle **Edge-Offline-Installer**
installiert zusätzlich einen begrenzten OS-Helfer mit den erforderlichen Firewall- und
Dienstrechten. Bei einem vorhandenen Jetson muss dieser Installer einmalig mit Administratorrechten
ausgeführt werden; Konfiguration und persistente Edge-Daten bleiben erhalten. Das Offlinepaket
wird weiterhin auf dem Entwicklungsrechner erstellt. Ein reines Runtime-Wheel-Update kann
diese privilegierte Einrichtung nicht nachholen.

Linux verwendet `ybb-edge-settings.timer` und einen Root-Oneshot-Dienst. Der normale
Agent bleibt unter `ybb-edge`, mit `NoNewPrivileges` und schreibgeschütztem `/etc`.
Neu gebaute Jetson-Pakete enthalten UFW samt Abhängigkeiten für **Ubuntu 24.04 Noble / ARM64**.
Der Offline-Installer installiert fehlendes UFW aus diesem lokalen Paketverzeichnis und richtet
die Edge-Portregeln ein. Ubuntu-Version und Architektur werden vor der Installation geprüft.
Bereits installierte Abhängigkeiten bleiben erhalten, soweit sie die Anforderungen erfüllen;
es gibt kein allgemeines Betriebssystem-Upgrade und keine Paketentfernung.

Tegra-Kernel können trotz installiertem `iptables` die Module `nft_limit` und `nft_log`
nicht enthalten (beobachtet bei `6.8.12-1021-tegra`). Der Installer prüft vor der
UFW-Konfiguration den Backend- und Modulstand. Während seiner eigenen, noch nicht
abgeschlossenen UFW-Ersteinrichtung kann er auf die mit `iptables` gelieferten
Legacy-Werkzeuge wechseln: vorher werden Legacy-Module geladen und IPv4-/IPv6-Limit-
und Loggingregeln mit `iptables-legacy-restore --test` ohne Übernahme geprüft.
Teilweise geladene UFW-Regeln werden noch über das alte Backend entfernt; danach
werden beide Alternativen gemeinsam umgestellt und die gespeicherten Freigaben eingerichtet.
Die Konfigurationsdateien bleiben erhalten. Ein fehlgeschlagener Alternativenwechsel stellt
die bisherigen Auswahlwerte und ihren Auto-/Manuell-Modus wieder her.

Ein funktionierendes nft-Backend, eine abgeschlossene Bestandsinstallation und fremde Regeln
(beispielsweise Docker-Regeln) werden nicht automatisch umgestellt. Dort meldet der Installer
den notwendigen lokalen Wartungsschritt. Fehlende Tegra-Kernelmodule werden nicht durch
generische Ubuntu-Kernelpakete ersetzt. Hintergrund zum Plattformproblem:
[JetPack-7.2-Erfahrungsbericht im NVIDIA-Forum](https://forums.developer.nvidia.com/t/headless-mode-on-nvidia-jetson-agx-orin-64gb-with-jetpack-7-2/374718).

Bei erstmalig installiertem UFW werden die Steuerverbindung der Zentrale und die konfigurierten
Trigger-Clients vor den Sperren für die beiden Edge-Ports freigegeben. Für andere eingehende
Verbindungen gilt weiterhin `allow`, damit beispielsweise bestehende SSH- und Kamerazugriffe
nicht durch eine neue globale Sperre blockiert werden. Vorhandene aktive UFW-Regeln und deren
Standardrichtlinie bleiben erhalten. Ein bereits installiertes, deaktiviertes UFW wird nicht
ungefragt aktiviert; dieser Sonderfall verlangt lokale Firewallwartung. Einstellungsänderungen
verwalten anschließend die markierten YBB-Trigger-Regeln und stellen Freigaben vor die Portsperre.

Der aktuelle Installer liefert auch die privilegierte Systempaket-Unterstützung und den
Launcher mit Updateprotokoll 2. Danach enthalten sowohl separat erstellte als auch mit der
Zentrale gelieferte Jetson-Runtime-Updates diese Ubuntu-Pakete. **Runtime aktualisieren** lässt
fehlendes UFW vom vorhandenen Root-Helfer offline installieren, bevor die Runtime umgeschaltet
wird. Der Helfer prüft die RSA-Signatur selbst mit dem bereits installierten öffentlichen
Schlüssel und nimmt nur die signierten Systempakete an. Ein normaler Software-Rollback setzt
den Runtime-Slot zurück; die eingerichtete Firewall und Betriebssystempakete bleiben bestehen.

Bereits erzeugte alte Archive erhalten diese Ergänzung nicht nachträglich. Für Geräte ohne
den neuen Root-Helfer muss einmal ein **neu gebauter** Offline-Installer angewendet werden;
das ist eine Aktualisierung der bestehenden Installation, kein Jetson-Neuaufbau.

Fehlendes oder inaktives UFW wird vom Einstellungsdienst auch ohne Änderungsauftrag geprüft
und im Frontend vor dem Bearbeiten angezeigt. Bereits angenommene Aufträge werden nochmals
vor dem Schreiben geprüft. Bei dieser fehlenden Voraussetzung bleiben `device.env`,
Firewallregeln und Agent unverändert. Nach Einrichtung der Firewall wird die Bearbeitung beim
nächsten Helferlauf wieder freigegeben; einen zuvor fehlgeschlagenen Auftrag erneut absenden.

Die Paketquellen, Versionen, Größen und SHA-256-Werte sind in
`deployment/edge/ubuntu-noble-arm64-ufw.lock.json` festgeschrieben. Nur der Entwicklungsrechner
lädt fehlende Dateien und prüft sie; der Cache liegt unter
`dist/edge-inputs/ubuntu-noble-arm64-ufw`. Der Edge verwendet eine isolierte APT-Dateiquelle
ohne öffentliche Paketquellen. Bei anderen Ubuntu-Versionen muss zuerst ein passendes,
geprüftes Systempaketprofil ergänzt werden.

Windows verwendet die geplante Aufgabe **YBB Edge Settings** unter SYSTEM. Sie ersetzt
die Regel **YBB Edge Plant Trigger**, aktualisiert die vorhandene WinSW-Konfiguration und
startet den bestehenden Dienst neu. Die Aufgabe prüft minütlich; Linux prüft etwa alle fünf Sekunden.

## Ablauf und Fehlerbehandlung

1. Die Oberfläche liest ausschließlich beobachtete Werte vom erreichbaren Gerät.
2. Eine Revision verhindert das Überschreiben inzwischen geänderter Einstellungen.
3. Der Agent validiert alle Werte und speichert einen dauerhaften, begrenzten Auftrag.
4. Der installer-eigene Helfer validiert denselben Vertrag nochmals, sichert die bisherigen
   Betriebswerte, passt Firewall und `device.env` an und startet den Agent neu.
5. Erst die neue Boot-ID mit passender Revision, erfolgreicher Gesundheitsprüfung und
   erreichbarem Trigger-Port bestätigt den Auftrag. Laufende Kanäle werden über die vorhandene
   Runtime-Wiederherstellung erneut gestartet; der TensorRT-Cache bleibt erhalten.

Fehler lösen die Wiederherstellung der bisherigen Betriebswerte und Firewall-Freigaben aus.
Eine unterbrochene Änderung wird anhand eines dauerhaften Journals beim nächsten Helferlauf
zurückgenommen. Scheitert auch die Wiederherstellung, bleibt der Fehler sichtbar und weitere
Einstellungsänderungen sind gesperrt. Software-Updates und Einstellungsaufträge werden am
Agent gegeneinander gesperrt. Die zentrale Auditspur erfasst die angeforderte Änderung.

Während eines Neustarts sind Kanäle und Trigger kurz unterbrochen. Netzwerkfehler werden
nicht als Erfolg ausgegeben. Ein offline oder deaktiviertes Gerät kann nicht bearbeitet werden;
bereits angenommene Aufträge werden auf dem Edge selbst zu Ende geführt. Die Einstellungen
bleiben bei Runtime-Updates und Software-Rollbacks im vorhandenen `device.env` erhalten.

## Verifikation

Unit-Tests prüfen Vertrag und Ablehnungen, Revision und Idempotenz, Annahme und Bestätigung,
Entfernen von Freigaben, Unterbrechung sowie Wiederherstellung nach Firewall-, Neustart- und
Bestätigungsfehlern. Frontend-Tests prüfen Gerätestand, Fortschritt, Offlinefall und fehlende
Installer-Unterstützung. Die abschließende Prüfung mit echter UFW/Windows-Firewall, SPS-Triggern
und laufenden Kanälen erfolgt auf dem Zielgerät; auf dem Entwicklungsrechner läuft keine Anlage.
