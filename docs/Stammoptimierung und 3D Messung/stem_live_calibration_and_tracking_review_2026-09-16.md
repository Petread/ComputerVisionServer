# Liveprüfung nach Update: Kalibrierung und wechselnde Bewegungsanzeige

Stand: 16.09.2026, Beobachtung etwa 14:05–14:08 MESZ. Ausschließlich lesende Serverabfragen; keine Änderung an Pipeline, Kalibrierung oder Runtime.

## Aktive Kalibrierung

Aktiv und veröffentlicht ist **v45**, `plv_cf8eb6f76a354aea8dfd79d924a164bb`, Kanal `chn_8db0e33874024d15b65f370efbbcc12c`. Die vollständige neueste Version stimmt mit der aktiven Versions-ID überein.

- Der Z-Endpunkt und `z_reference_length_mm=400` sind gespeichert.
- `canonical_view.config.calibration.spatial_calibration` ist `null` beziehungsweise fehlt im mitgeführten Liveprofil.
- Die Provenienz lautet weiterhin `MANUAL_REFERENCE_POINT_HOMOGRAPHY`.
- Der vorgeschaltete Node `undistort` verwendet `IDENTITY_PASSTHROUGH`, `accepted_observation_count=0`; im Liveframe steht `intrinsic_calibrated=false`.

Damit ist die räumliche Kalibrierung **noch nicht aktiv**. Eine gespeicherte Z-Strecke allein schaltet die räumliche Berechnung nicht ein. Erforderlich sind die gemessene Kameraintrinsik und die erfolgreiche räumliche Berechnung im bestehenden Assistenten. Der Name `calibrated_spatial_temporal_motion_x` in der Bewegungsdiagnostik bezeichnet die räumliche ROI-Prüfung plus zeitliche XY-Bewegungsevidenz und ist kein Nachweis einer XYZ-Kamerakalibrierung.

## Nachgewiesener Anzeigenfehler beim Längsauslauf

Der Betreiber meldet: zunächst Rotationsbeobachtung, kurz Längsbeobachtung, dann während des Längstransports wieder Rotationsbeobachtung. Ein konkreter Ablauf wurde mit Track-Metadaten, Kandidatengeometrie und Bildern nachvollzogen:

| Quellzeit MESZ | Frame | Befund |
|---|---:|---|
| 14:08:11,856 | 16875 | Aktiver Track `…0050` ist `longitudinal_conveyor`, Beschriftung `Longitudinal feed`. |
| 14:08:14,015 | 16940 | Derselbe Track bleibt längs; `decision_gate_reached=true`. Im Bild läuft der linke Stamm aus, rechts liegt der Nachfolger. |
| 14:08:14,269 | 16947 | Aktiver Track ist jetzt `…0051`, Rotationsbeobachtung des rechten Nachfolgers. Der auslaufende Stamm ist vorübergehend nicht als Kandidat eingeblendet. |
| 14:08:15,565 | 16986 | Der linke auslaufende Stamm erscheint wieder als **unselektierter** Kandidat `Rotation observation`; `…0051` bleibt auf dem rechten Nachfolger. |
| bis 14:08:18,056 | bis 17061 | Die kleiner werdende Kontur des auslaufenden Stamms behält diese falsche Kandidatenbeschriftung. |

Der letzte Längsframe hatte für den auslaufenden Stamm die normalisierte Boundingbox ungefähr `(0,4125; 0; 0,7125; 0,573)`. Beim erneuten Auftauchen als unselektierter Kandidat ist sie `(0,483; 0; 0,713; 0,417)` und schrumpft beim weiteren Längsauslauf. Die zeitlich benachbarten Bilder zeigen denselben auslaufenden Stamm und den wartenden Nachfolger.

Der lokale Code erklärt diese Folge:

1. `complete_at_decision_gate=true`: Der aktive Track endet, sobald seine führende Kontur das Entscheidungs-Gate erreicht. Er kann dabei noch sichtbar sein.
2. `_complete_active_track()` speichert den letzten Zustand, `_matches_completed_track()` blendet entsprechende Kandidaten zeitweise aus.
3. Die aktive Konfiguration begrenzt diesen Ausschluss auf **45 Quellframes**. Bei etwa 30 Kameraframes/s sind das ungefähr **1,5 Sekunden**. Der beobachtete Wiedereintritt bei Frame 16986 liegt 46 Quellframes nach dem Gate-Frame 16940.
4. Danach stuft `_tracker_candidates()` die Kontur wieder anhand ihrer ROI-Überlappung ein. Bei Überlappung mit dem Transferbereich startet sie als `transfer`.
5. `_tracker_candidate_with_active_zone()` erhält den bestätigten Prozesszustand ausschließlich für den ausgewählten aktiven Track. Der auslaufende, unselektierte Kandidat erhält diese Korrektur nicht. `_tracker_overlay()` beschriftet ihn deshalb erneut als `Rotation observation`.

**Der konkret nachgewiesene Rücksprung ist ein Verlust der Prozessrolle in der Kandidatenanzeige nach dem Track-Abschluss.** Er beweist keine erneute Rotation und keinen internen Zustandsrücksprung desselben aktiven Tracks. In der ersten Pollstichprobe gab es ebenfalls keinen beobachteten Wechsel `longitudinal_conveyor → transfer` innerhalb derselben Track-ID. Die Stichprobe ist kein vollständiger Mitschnitt aller Runtimeframes; andere Fehlerfälle sind damit nicht ausgeschlossen.

Eine fachlich passende Korrektur muss Messabschluss und Sichtbarkeit auseinanderhalten: Auslaufende Stämme behalten ihre bereits bestätigte Rolle, solange ihre Kontur noch zugeordnet werden kann. Wartende Nachfolger erhalten eine eigene Rolle. Eine längere Ausschlusszeit allein würde die Anzeige nur zeitweise verstecken und die Zuordnung nicht lösen. `Rotation observation` ist derzeit außerdem eine Prozessrollenbezeichnung, kein Beleg für eine aktuell gemessene Eigenrotation.

## Messbetrieb in der Stichprobe

Die erste Abfrage lief über 90 Sekunden, Quellzeit 14:05:12,379–14:06:42,047 MESZ: 300 Antworten, dedupliziert 253 Zustände mit Frame und ein Zustand ohne Frame. Beobachtet wurden 14 Track-IDs. Das ist keine unabhängig gezählte Anzahl physischer Stämme.

Von den 253 Zuständen mit Frame waren 247 dem Transfer und sechs dem Längsförderer zugeordnet. **Keiner enthielt ein gültiges Drehergebnis.** Hauptgründe waren `MODEL_NOT_FROZEN` (241), `INVALID_STEM_MODEL` (7) und `PHASE_GENERATION_MISMATCH` (5). Diese Zahlen beziehen sich auf abgefragte Zustände, nicht auf 253 unabhängige Messversuche.

Die Quelle war verbunden und ohne gemeldete Quellfehler. Die Pipeline verarbeitete bei zwei Statusabfragen etwa 6–7,5 fps aus rund 30 fps; der Kanal meldete deshalb `degraded` und etwa 75–80 % verworfene Frames. Diese Laufzeitbefunde sind zusätzliche Einschränkungen, erklären aber den oben nachgewiesenen Beschriftungswechsel nicht.

Weitere 45 Sekunden lief eine gezielte Beobachtung mit 117 unterschiedlichen Bildzuständen und gespeicherten Bildern an Track-/Zonenwechseln. Lokale Diagnoseartefakte: `.test-tmp/stem-spatial-live-20260916/`, insbesondere `active-version.json`, `poll.json`, `transitions-detailed.json` und `transition-16940.jpg` / `transition-16947.jpg`. Der API-Schlüssel wurde nicht in diese Artefakte geschrieben.

Der Befund ist dokumentiert; in diesem Liveprüfauftrag wurden keine Produktionsdateien geändert. Die nicht aktive Raumkalibrierung und der Verlust der Auslaufrolle sind getrennte Aufgaben. Das Aktivieren der Z-Kalibrierung allein repariert die Kandidatenanzeige nicht.
