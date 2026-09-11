# Runtime-Integration der geometrischen Stamm-Eindrehoptimierung

Stand: 2026-08-21

Eine kurze Schritt-für-Schritt-Anleitung für die Konfiguration auf dem Zielserver steht in
[`stem_rotation_pipeline_configuration.md`](stem_rotation_pipeline_configuration.md).

## Geltungsbereich

Die geometrische Eindrehoptimierung ist in die bestehende Runtime-, Channel-, Result-History-
und Admin-Web-Struktur integriert. Sie bleibt eine reine Shadow-Empfehlung. Es gibt aktuell
keine SPS-Abhängigkeit und keinen SPS-Schreibzugriff dieser Funktion.

Verbindlich gilt:

- `x`: Stamm-Längsachse/Förderrichtung,
- `y`: horizontal/quer, links-rechts,
- `z`: vertikal, oben-unten,
- Stammscheiben liegen in der `y-z`-Ebene,
- positive und negative Drehwinkel beziehen sich auf die versionierte positive Rotation um
  Maschinen-`x` und den im Messmodell gespeicherten Betrachtungsstandpunkt.

## Prioritätsquelle

Jeder Runtime-Channel besitzt genau einen persistenten, revisionsbehafteten Control-Snapshot:

```json
{
  "schema_version": "runtime.stem-rotation-priority-control.v1",
  "priority_source": "FRONTEND",
  "priority_mode": "NONE",
  "optimization_enable": true,
  "revision": 0,
  "updated_at": null,
  "updated_by": null
}
```

Der Default ist `FRONTEND`; der Modus `NONE` ist fail-closed. Das Admin-Web setzt den Modus
über den vorhandenen Endpunkt `PUT /api/v1/runtime/channels/{channel_id}` mit
`stem_rotation_priority_mode`. Änderungen erhöhen serverseitig die Revision und erzeugen wie
andere Channel-Änderungen einen Audit-Eintrag. Der Control-Snapshot gehört ausdrücklich nicht
zum Runtime-Binding und löst daher keinen Channel-Neustart aus.

`analysis.stem_rotation_priority_adapter` bildet den Snapshot auf
`stem.rotation-priority.v1` ab:

- `VORSCHNITT_KRITISCH` -> `VERTICAL_Z`,
- `NACHSCHNITT_KRITISCH` -> `HORIZONTAL_Y`,
- `NONE`, deaktivierte Optimierung oder ungültige Daten -> keine gültige Priorität.

Die vorbereitete Klasse `PlcPriorityModeProvider` ist nur die fachliche Adaptergrenze. Wird
`priority_source=PLC` gewählt, bleibt die Ausgabe mit
`PLC_PRIORITY_PROVIDER_NOT_CONNECTED` ungültig. Es findet kein Rückfall auf den Frontend-Wert
statt. Damit kann das Frontend einen aktiven SPS-Modus weder überschreiben noch vortäuschen.
Eine spätere SPS-Anbindung muss einen authentifizierten Leseweg für denselben fachlichen
Control-Vertrag bereitstellen; Geometriekern und Optimierer bleiben dabei unverändert.

## Runtime-Datenfluss

Die Vorlage `template-realtime-stem-rotation-optimization` verwendet den vorhandenen
kalibrierten Messpfad und ergänzt nur dessen Entscheidungsteil:

```text
Kamera -> Entzerrung -> kanonische Prozessansicht -> bestehende Segmentierung
       -> Active-Stem-Tracker -> horizontale Krümmung + kalibrierte Mantelphase
       -> analysis.stem_slice_model --------------------+
                                                       |
Channel-Control -> analysis.stem_rotation_priority_adapter
                                                       |
                                                       v
                                  analysis.stem_rotation_optimizer
                                                       |
                                                       v
                                            output.result / History
```

`analysis.stem_slice_model` bleibt der einzige zustandsbehaftete Teil der neuen Kette. Der
Priority-Adapter und der Optimierer sind zustandslos. Der Optimierer akzeptiert nur ein gültiges,
am Entscheidungs-Gate eingefrorenes `stem.slice-model.v1`. Die Ausgabe
`stem.rotation-optimization.v1` bleibt getrennt vom bestehenden Vertrag
`stem.orientation.v3`.

## Zustandsmodell

Der synchrone Runtime-Resultvertrag führt die für Bedienung und Diagnose benötigten Zustände:

- `IDLE`: noch keine Messsession,
- `MEASURING`: Scheibenbeobachtungen werden gesammelt,
- `MODEL_BUILDING`: ein Modell liegt vor, ist aber noch nicht am Entscheidungs-Gate eingefroren,
- `OPTIMIZING`: reservierter Zustand für eine später asynchron ausgeführte Optimierung,
- `RESULT_READY`: die synchrone Optimierung ist abgeschlossen,
- `DONE`/`APPLIED`: reserviert für eine später ausdrücklich freigegebene Aktor-Rückmeldung,
- `INVALID`: fachlich kein gültiger Drehwert,
- `ERROR`: Konfiguration oder Optimiereraufruf ist fehlgeschlagen.

Die aktuelle Implementierung geht bei einem gültigen eingefrorenen Modell innerhalb eines
Pipeline-Laufs direkt von `MODEL_BUILDING` auf `RESULT_READY`. Es wird kein alter Drehwert in
einen neuen Resultvertrag übernommen.

### Ablauf eines Stammzyklus

1. Ein neues Active-Stem-/Session-Ereignis setzt den zustandsbehafteten Model-Builder zurück.
2. Während des natürlichen Einrollens in `process_zone=transfer` entstehen
   `MEASURING`-Ergebnisse ohne Drehwert. Kleine X-Drift wird materialfest registriert.
3. Bestätigungsframes der Längsbeschleunigung werden nicht mehr gesammelt. Beim bestätigten
   `longitudinal_conveyor` (oder früheren Freeze-Gate) wird das validierte Scheibenmodell
   eingefroren; im Längstransport findet keine Scheibenmessung statt.
4. Priority-Adapter und Optimierer verarbeiten Modell und aktuelle Control-Revision gemeinsam.
5. Erst danach enthält das Ergebnis `result_ready=true`. Nur bei gültigem Modell, gültiger
   Priorität und erfolgreicher Optimierung gilt zusätzlich `result_valid=true`.
6. Der Result-Output persistiert den semantischen Berechnungsstand einmalig unter einem Key aus
   Session, Modell-Hash, Modus und Control-Revision.

### Reiner Moduswechsel am selben Stamm

1. Das Frontend sendet nur den neuen `stem_rotation_priority_mode`.
2. Der Channel-Store erhöht die Revision atomar; ein Runtime-Neustart findet nicht statt.
3. Der Realtime-Executor übernimmt den neuen Snapshot bei seiner bestehenden Channel-Prüfung.
4. Beim nächsten normalen Pipeline-Takt gibt der zustandsbehaftete Builder sein unverändertes
   eingefrorenes `StemSliceModel` erneut aus; es werden keine neuen Messbeobachtungen gesammelt.
   Der zustandslose Optimierer verwendet dieses Modell und den neuen Modus.
5. `result_ready` wird nur im neu erzeugten Resultvertrag gesetzt. Der History-Key unterscheidet
   die Revisionen, sodass kein Ergebnis des alten Modus als neues Ergebnis erscheint.

## Konfiguration

Die vorhandene Model-Builder-Konfiguration enthält insbesondere
`slice_spacing_mm`, `smoothing_window_slices`, `maximum_interpolation_gap_mm`,
`minimum_valid_slice_ratio`, `minimum_model_quality`, `maximum_material_x_step_mm` und
`maximum_material_x_excursion_mm`. Der Optimizer-Node ergänzt:

- `x_eval_start_mm`, `x_eval_end_mm`,
- `coarse_angle_step_deg`, `fine_angle_step_deg`, `fine_search_half_width_deg`,
- `score_tolerance_mm`, `min_gain_mm`, `min_rotation_deg`,
- `min_valid_slice_ratio`, `min_valid_slice_count`, `min_model_quality`,
- `rotation_negative_limit_deg`, `rotation_positive_limit_deg`.

Alle auf eine Berechnung wirkenden Werte werden mit den mechanischen Grenzen in den
Replay-Snapshot und dessen `configuration_hash` übernommen.

## Resultat, Logging und Replay

`stem.rotation-optimization.v1` enthält mindestens:

- Quelle, Modus, Achse und Control-Revision,
- Session/Track, Modell- und Konfigurations-Hash,
- `result_ready`, `result_valid`, `should_rotate`,
- `rotation_delta_deg` und das fachliche Alias `delta_angle_deg`,
- `gain_mm`, aktuelle/optimale Diagnosescores,
- `limiting_x_current_mm`, `limiting_x_optimal_mm`,
- Modellqualität, Scheibenanzahlen, Reason-Codes und `angle -> score`-Kurve,
- Betrachtungsstandpunkt und positive Drehrichtung,
- den begrenzten Replay-Snapshot aus Roh- und finalen Scheiben, optionalen Rohbeobachtungen,
  Fitdiagnosen, aktueller Pose, Blick-/Positivrichtung, Kalibrierungsbindungen, Priorität,
  Konfiguration und mechanischen Grenzen.

Result-History klassifiziert fertige Einträge als `stem_rotation_optimization`. Die Pipeline ist
immer Shadow-Mode; deshalb werden semantisch neue Berechnungen auch ohne periodische
Frame-History einmalig gespeichert. Wiederholte Frames derselben Kombination aus Session,
Modell, Modus und Revision erzeugen keinen weiteren Berechnungseintrag.

Offline kann ein gespeicherter Resultvertrag statisch mit folgendem Werkzeug erneut gerechnet
werden:

```text
python scripts/dev/replay_stem_rotation_optimization.py result.json
```

Das Werkzeug akzeptiert außerdem eingefrorene `stem.slice-model.v1`-Modelle und
kalibrierte `stem.projection-observations.v1`-Messungen. Simulator, Monte-Carlo,
Parametersweep, Diagnoseexport, Referenzfälle und die reale Inbetriebnahmereihenfolge
sind in [stem_rotation_validation_and_commissioning.md](stem_rotation_validation_and_commissioning.md)
dokumentiert.

## Bedien- und Diagnosevisualisierung

Die bestehende Kanaldetailseite im Admin-Web enthält bei Pipelines mit
`analysis.stem_rotation_optimizer` die vollständige Bedienansicht. Eine neue Web-Anwendung oder
Frontend-Technologie wurde nicht eingeführt.

Der Optimizer ergänzt jedes Ergebnis um das strikt typisierte, read-only
`stem.rotation-hmi.v1`. Dieses ViewModel wird im Realtime-Executor über
`RuntimeLiveViewState.stem_rotation_view` transportiert und im gespeicherten Resultat für
History und Replay mitgeführt. Es enthält bereits:

- projizierte Stammkontur und Mittellinien entlang `x`,
- aktuelle und optimierte Ellipsenpunkte der beiden möglichen limitierenden Scheiben,
- symmetrisch nutzbare Mittelsehnen entlang der priorisierten Achse,
- `DeltaG_i = G_i(optimal) - G_i(current)` je Scheibe,
- Markierungen für `limiting_x_current_mm` und `limiting_x_optimal_mm`,
- die Diagnosekurve `angle -> score`, absolute Scores, Modell-/Fitqualität und Konfiguration.

Die UI skaliert diese Punkte ausschließlich in SVG-Koordinaten. Sie ruft weder Geometriekern
noch Optimierer auf und berechnet keine fachlichen Scores neu. Die Hauptkachel zeigt nur die
relative Korrekturdrehung und den Geometriegewinn. Absolute Scores sind auf die eingeklappte
Diagnoseansicht beschränkt. Aktuelle und optimierte Konturen, positive und negative lokale
Differenzen sowie beide limitierenden Positionen unterscheiden sich zusätzlich über Linienstil,
Symbole und Text; Farbe ist nicht das einzige Merkmal.

Ein Frontend-Moduswechsel wird anhand Quelle, Modus und Control-Revision mit dem ViewModel
abgeglichen. Bis die Runtime dasselbe eingefrorene Scheibenmodell mit der neuen Revision
berechnet hat, verschwindet die alte Drehempfehlung und die Ansicht meldet
`Prioritätswechsel erkannt`. Während einer laufenden Runtime wird nie auf ein altes
History-Ergebnis zurückgefallen. Bei gestoppter Runtime kann das letzte persistierte Ergebnis
angezeigt werden, ist aber deutlich als `Historisches Ergebnis` markiert.

Darstellungsgrenzen:

- Die Längsansicht und der Querschnitt sind schematische Diagnoseprojektionen, kein
  millimetergenau gerendertes 3D-Mesh.
- Die Sicht ist auf die vom Backend gelieferten gültigen und vorverarbeiteten Scheiben begrenzt;
  verworfene Rohbeobachtungen erscheinen nur als Fitdiagnose.
- Der Pfeil im kanonischen Querschnitt folgt immer `R(+delta)` im festen `y-z`-System
  (`+delta`: y nach z, gegen den Uhrzeigersinn bei y-rechts/z-oben). Der große Bedienpfeil wird
  dagegen aus Vorzeichen und der kalibrierten physischen Konvention
  `CLOCKWISE|COUNTER_CLOCKWISE` am versionierten Betrachtungsstandpunkt abgeleitet. Fehlt eine
  gültige Richtungsdefinition, zeigt die UI keinen Drehwert als Empfehlung.
- Es werden weder Produkt-/Brettkonturen noch Produktreserven dargestellt, weil die Funktion
  keinen Einschnittplan kennt.

## Safety- und Fallback-Pfade

Kein gültiger Drehwert wird ausgegeben bei:

- deaktivierter Optimierung,
- `NONE` oder ungültiger Priorität,
- gewählter, aber nicht angebundener PLC-Quelle,
- fehlender oder unzureichender Messung/Winkelabdeckung,
- nicht eingefrorenem, ungültigem oder qualitativ zu schwachem Modell,
- zu geringem gültigem Scheibenanteil,
- ungültigem Evaluationsbereich oder mechanischem Grenzkonflikt,
- numerischem/technischem Optimiererfehler.

Bei geringem Geometriegewinn oder zu kleinem Stellwinkel bleibt das Ergebnis fachlich gültig,
aber `should_rotate=false` und `rotation_delta_deg=0`. Bei ungültigen Ergebnissen sind beide
Winkelfelder `null`. Die neue Pipeline enthält keinen SPS-Node; zusätzlich kennzeichnet der
Resultvertrag `shadow_mode=true` und `plc_writes_suppressed=true`.

## Verifikation und Deployment

Die Anwendung ist auf diesem Entwicklungsrechner nicht lauffähig. Hier werden ausschließlich
statische Prüfungen sowie Unit- und Build-Tests ausgeführt, soweit die lokale Werkzeugkette sie
zulässt. Live-, Kamera-, SPS- und Ende-zu-Ende-Prüfungen erfolgen nach Einspielen über den
vorhandenen Updater auf dem separaten Zielserver. Die Migration
`20260820_0036_stem_rotation_priority_control.py` hält Priorität und Revision update- und
rollbackfähig in der bestehenden PostgreSQL-Datenbank.
