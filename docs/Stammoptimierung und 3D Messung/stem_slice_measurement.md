# Scheibenmodellbildung aus Kamera-Messprofilen

## Ergebnis

`analysis.stem_slice_model` erzeugt `stem.slice-model.v1` aus den bereits vorhandenen
Runtime-Ergebnissen:

- `StemHorizontalCurvaturePayload`: metrische Prozess-X/Y-Mittellinie und Breitenprofil,
- `StemSurfacePhasePayload`: kalibrierte, unwrapped Phase mit Blick- und
  Positivrichtungskonvention,
- `active_stem_tracking`: Trackbindung, Mess-/Freeze-/Decision-Gates und
  Bewegungsdiagnose.

Es gibt keine zusätzliche Segmentierung, Konturextraktion, Pixel-mm-Umrechnung oder
Drehwinkelverfolgung. Der Node ist zustandsbehaftet und wird im Realtime-Executor über
den bestehenden `RuntimeExecutionContext.state` ausgeführt. Kamera-, SPS- und
Frontend-Dienste bleiben unverändert.

## Koordinaten und Fit

Das Ergebnis verwendet ausschließlich das Fachkoordinatensystem `x` längs, `y`
horizontal und `z` vertikal. Die Scheibe liegt in der `y-z`-Ebene; positive Phase dreht
um `+x`. Für die Beobachtungsphase relativ zur aktuellen Modelllage gilt:

```text
theta = calibrated_phase - current_pose_phase
y_observed = offset0 + cy*cos(theta) - cz*sin(theta)
```

`offset0` ist der je Messstation konstante Kamera-/Maschinenversatz der projizierten
Mittellinie. Er wird als eigener Nuisance-Parameter gefittet und nicht in den mit dem
Stamm rotierenden Mittelpunktkomponenten `cy/cz` gespeichert. Dadurch erzeugt ein
statischer Bild- oder Kalibrierungsversatz keinen scheinbaren Krümmungsanteil.

Die Ellipse wird nicht aus verrauschten Einzel-Minima und -Maxima bestimmt. Mit der
sichtbaren Halbbreite `h=width/2`, `v=(cos(theta),-sin(theta))` und der symmetrischen
Supportmatrix `S` wird robust gelöst:

```text
h^2 = v^T * S * v
```

Beide linearen Systeme verwenden qualitätsgewichtete Huber-IRLS und 64-Bit-
Fließkommazahlen. Die Eigenwerte von `S` ergeben `a^2` und `b^2`; der Eigenvektor zum
größeren Eigenwert ergibt `phi`. Bei nahezu kreisförmigen Scheiben wird `phi=0`
deterministisch gesetzt und als nicht informativ markiert.

Harte Gültigkeitsprüfungen umfassen Beobachtungszahl, getrennte Phasenstützstellen,
Phasenspanne, Konditionszahl, positive Definitheit, Achsplausibilität sowie Mittelpunkt-
und Breitenresiduen. Nachbarplausibilität reduziert die Qualität, verwirft aber keine
gut gemessene lokale Engstelle. Das bestehende Preprocessing übernimmt regelmäßiges
50-mm-Raster, kurze Lücken, robuste räumliche Glättung und zyklisches `phi`-Unwrapping.

## Material-X, natürliche Rollphase und Messgrenzen

Die Produktion besitzt keine kommandierte Messdrehung. Der Stamm rollt beim Einwurf von
rechts durch den Transferbereich; genau diese vorhandene Mantelspur liefert die
unregelmäßig verteilten Phasenbeobachtungen. Der Builder erfindet weder Sollwinkel noch eine
zweite Drehwinkelspur.

`TRACKED_SIGNED_MATERIAL_X_V1` übernimmt zusätzlich die signierte Bildtranslation aus
derselben Mantelphasenverfolgung, projiziert sie mit der vorhandenen Prozesshomographie nach
Maschinen-X und registriert jedes Profil auf ein festes Materialraster:

```text
x_machine(frame) = material_origin_machine + x_material + signed_x_shift(frame)
```

Damit ist die während des Transfers erwartete kleine Längsdrift (im Testaufbau bis ungefähr
`+/-100 mm`) zulässig. Der Node:

1. sammelt ausschließlich bei bestätigter `process_zone=transfer`,
2. verwirft nur die plausiblen Bestätigungsframes einer beginnenden Längsbeschleunigung,
3. begrenzt registrierten Sprung und gesamte X-Auslenkung fail-closed,
4. schneidet die in allen Frames gemeinsam sichtbare **Material-X**-Länge,
5. friert das Modell bei bestätigtem `longitudinal_conveyor` sofort ein und
6. rekonstruiert weder verdeckte Stammenden noch lange Datenlücken.

Fehlt die metrische signierte Registrierung oder überschreitet sie die Plausibilitätsgrenzen,
bleibt das Modell ungültig. Die grobe Maskenzentrumsbewegung des Active-Stem-Trackers wird nur
für Transfer-/Längstransport-Zustand und Plausibilität benutzt, nicht als zweite Materialspur.

Der Tracker bestätigt den Übergang zweistufig. Zuerst klassifiziert er jeden kalibrierten
Bewegungsschritt. Anschließend bewertet er in einem begrenzten Zeitfenster die Zahl
X-dominanter Beobachtungen, den vorzeichenrichtigen Netto-X-Weg und dessen Anteil am gesamten
absoluten X-Weg. Bei kalibrierter Prozessgeometrie muss der Stamm-Mittelpunkt außerdem den
konfigurierten Längsförder-ROI erreicht haben. Eine Bewegung entgegen der Förderrichtung setzt
die vorangegangene Vorwärtsevidenz zurück. Erst die gemeinsame Bestätigung schaltet den
monotonen Prozesszustand von `transfer` auf `longitudinal_conveyor`.

Das Scheibenmodell verwendet dafür das explizite Flag
`movement.longitudinal_transition_pending`. Der ältere
`longitudinal_motion_streak` bleibt ausschließlich als Rückwärtskompatibilitäts-Fallback für
Tracker-Payloads vor `stem.active-track.v2` erhalten. Dadurch werden X-lastige Projektionseffekte
beim natürlichen Rollen nicht mehr pauschal als Messwerte verworfen.

Die vorhandene Prozesskalibrierung ist eine perspektivisch entzerrte Homographie in
einer festgelegten Referenzebene. Der Supportfit behandelt deren metrisches X/Y-Profil
als orthographische Projektion des rotierenden Querschnitts. Höhenabhängige
Perspektivfehler, Okklusionen sowie eine nicht feste reale Drehachse können mit einer
einzelnen Kamera nicht vollständig getrennt werden. Residual- und Qualitätsgrenzen
müssen daher mit realen Serveraufzeichnungen validiert werden. Insbesondere wird `cz`
nicht auf null gesetzt oder aus einem Einzelbild geraten.

## Lifecycle und Diagnose

Messprofile werden ab dem vorhandenen Session-Start-Gate nur im Transfer angenommen. Das
Curvature-Freeze-Gate oder der bestätigte Wechsel zum Längstransport beendet die Sammlung;
das bis dahin verwendbare Modell wird unmittelbar mit deterministischem `model_hash`
eingefroren. Im Längstransport werden keine Scheiben mehr ergänzt oder verändert. Track-,
Trace-, Kalibrierungs- oder Winkelkonventionswechsel setzen die Sammlung fail-closed zurück.

`StemSliceModelPayload` enthält getrennte Roh- und vorverarbeitete Scheiben,
Stationsdiagnosen, Modellqualität, Abdeckungs-/Lückenwerte, Kalibrierungsbindungen und
Reason Codes. Ungültige Stationen enthalten nur `x`, Status und Gründe, keine erfundenen
`cy`-, `cz`-, Achsen- oder Winkelwerte. Rohbeobachtungen sind begrenzt und werden nur mit
`include_raw_observations=true` ausgegeben.

## Offline-Replay

Das versionierte Eingabeformat `stem.projection-observations.v1` enthält ausschließlich
extrahierte metrische Beobachtungen. Ein synthetisches Beispiel liegt unter
`tests/fixtures/synthetic/stem-projection-observations.v1.json`.

```bash
PYTHONPATH=backend/packages/cv-runtime/src \
python scripts/dev/replay_stem_slice_model.py \
  tests/fixtures/synthetic/stem-projection-observations.v1.json \
  --output /tmp/stem-slice-model-result.json
```

Der Replaypfad ruft exakt denselben reinen Builder wie der Runtime-Node auf und benötigt
weder Kamera noch laufende Anwendung. Reale portable Messaufzeichnungen sind im
Repository weiterhin nicht vorhanden; Aufzeichnung und Live-Validierung erfolgen auf
dem Zielserver über die bestehenden Update-/Runtime-Pfade.
