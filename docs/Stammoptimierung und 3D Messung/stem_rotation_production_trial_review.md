# Abschlussreview der Stamm-Eindrehoptimierung vor dem Maschinenversuch

Stand: 2026-08-21  
Reviewbasis: `stammoptimierung_spezifikation.md`, vorhandener Integrationsplan sowie der
implementierte Kamera-, Mess-, Geometrie-, Optimierer-, Runtime-, HMI-, History- und
Replaypfad.

## Gesamturteil

**Bedingtes GO für einen beobachteten Shadow-Maschinenversuch.** Die geprüfte Funktion gibt
weiterhin nur eine Empfehlung aus (`shadow_mode=true`, `plc_writes_suppressed=true`); die
Produktionsvorlage enthält keinen SPS-Schreib- oder Lese-Node. Für eine automatische Drehung
oder SPS-Freigabe gilt ausdrücklich **NO-GO**.

Vor dem ersten Shadow-Lauf müssen auf dem Zielserver die Kalibrierungsbindung, die physische
Drehrichtung, reale Modellqualität, mechanische Grenzen, Entscheidungszeit und persistiertes
Replay anhand der Checkliste am Ende dieses Dokuments bestätigt werden. Im Repository liegt
noch kein freigegebener realer Referenzstamm; synthetische Abnahme ersetzt diese Bestätigung
nicht.

Nach den im Review vorgenommenen Korrekturen bleibt im fachlichen Shadow-Pfad kein bekannter
`FAIL` offen. Die offenen Punkte sind reale Inbetriebnahme- und Repository-Baseline-Warnungen.

Der erste Installationsversuch mit Bundle `2026.08.21-073331-g85e96f62b02a` scheiterte vor
der Release-Aktivierung an R-06. Die Korrektur ist im Repository enthalten; das bedingte GO
gilt erst wieder, wenn ein neu gebautes Bundle die Migration auf dem Zielserver erfolgreich
durchlaufen hat. Das fehlerhafte Bundle darf nicht erneut verwendet werden.

## Status A–J

| Bereich | Status | Ergebnis |
|---|---|---|
| A. Fachliche Trennung | **PASS** | Geometriekern und Optimierer enthalten keine Brett-, Produkt- oder Einschnittplanlogik. Der UI-Hauptwert heißt Geometriegewinn. Genau ein revisionsbehafteter Priority-Control-Snapshot ist aktiv. |
| B. Koordinaten/Winkel | **WARN** | Codekonvention und UI sind nach Korrektur konsistent: `x` längs, `q=(y,z)`, Rotation um `+x`, intern Radiant, außen Grad. Die reale Maschinenrichtung ist noch am Zielsystem zu verifizieren. |
| C. Mathematik | **PASS** | Ellipsenmatrix, gemeinsame Rotation von Zentrum und `phi`, stabile Schnittquadratik, symmetrischer Span, globales Minimum, 180°-Suchraum, Tie-Break und Mindestgewinn entsprechen der Spezifikation. |
| D. Datenqualität | **WARN** | Ungültige Scheiben, begrenzte Interpolation, zyklisches `phi`, Engstellen-Schutz und Quality-Gates sind umgesetzt. Anlagenbezogene Plausibilitäts-/Residualgrenzen und die monokulare Projektionsannahme sind noch mit realen Wiederholmessungen zu validieren. |
| E. Runtime/Priority/SPS | **PASS** | FRONTEND funktioniert ohne SPS; Revision, Modell-Hash und Session verhindern stale Results. Ein Moduswechsel optimiert dasselbe eingefrorene Modell neu. PLC bleibt eindeutig fail-closed und ist nicht angebunden. |
| F. UI | **PASS** | Delta/Gain sind prominent, absolute Scores nur in Diagnose, Achse/Quelle/Status/limitierende Scheiben sind sichtbar. Hauptpfeil und kanonischer Querschnitt verwenden nun die jeweils korrekte Richtungsdefinition. |
| G. Reproduzierbarkeit | **WARN** | History enthält Konfiguration, Scorekurve, Roh-/Endscheiben, aktuelle Pose und Kalibrierungsbindung; identisches Replay ist deterministisch. Es fehlt weiterhin ein versionierter realer Referenzstamm. |
| H. Tests | **WARN** | Alle 244 stammbezogenen Backend- und alle 139 Frontend-Tests sind grün. Die globale Python-Suite besitzt unabhängige bestehende Collection-/Plattform-Baselinefehler; Details stehen unter „Verifikation“. |
| I. Performance | **WARN** | Der komplette Offline-Runtime-/Optimizer-/HMI-Pfad benötigt lokal median 66–481 ms für 31–241 Scheiben. Eine Zielserver-SLA und Messung unter realer Inferenzlast fehlen. |
| J. Dokumentation | **PASS** | Datenfluss, Priority-Quelle, Einheiten, Winkelkonvention, Safety, Replay, Simulator, Startwerte, Grenzen und diese Maschinencheckliste sind dokumentiert. |

## Im Review gefundene und behobene FAIL-Punkte

### R-01 – Gültigkeitsquote im Evaluationsbereich

Der Runtime-Adapter entfernte ungültige `StemSliceGeometryPayload`-Stationen vor dem reinen
Optimierer. Dadurch konnte dessen `valid_slice_ratio` nur noch die verbliebenen gültigen
Scheiben sehen. Gleichzeitig berücksichtigte das frühere Runtime-Gate alle angeforderten
Stationen, auch wenn unzuverlässige Enden bewusst durch `x_eval_start_mm/x_eval_end_mm`
ausgeschlossen waren.

Korrektur:

- Die Runtime zählt nun gültige verarbeitete Stationen und alle Fit-Stationen innerhalb des
  konfigurierten Evaluationsbereichs.
- Ungültige Stationen im Bereich können nicht mehr durch Adapterfilterung verschwinden.
- Ausgeschlossene Endbereiche blockieren die Freigabe nicht mehr fälschlich.
- `valid_slice_count` und `evaluated_slice_count` im Resultat beziehen sich auf denselben
  Evaluationsbereich.

Regression: `test_valid_slice_ratio_is_checked_inside_the_configured_evaluation_range`.

### R-02 – Drehpfeile und positive Richtung

Der Querschnitt verwendete den SVG-Sweep entgegengesetzt zur kanonischen Matrixrotation.
Außerdem leitete das große Bediensymbol Uhr-/Gegenuhrzeigersinn nur aus dem Vorzeichen ab und
nicht aus der kalibrierten physischen Konvention.

Korrektur:

- Im festen Plot `y` rechts/`z` oben gilt jetzt korrekt: `R(+delta)` dreht `y` nach `z`, also
  gegen den Uhrzeigersinn. Der Pfeil verwendet dafür feste Viertelkreis-Endpunkte; nur ein
  SVG-Sweep-Flag umzuschalten wäre wegen des wechselnden Bogenmittelpunkts mehrdeutig.
- Bei 0° wird kein Drehpfeil gezeichnet.
- Der Bedienpfeil kombiniert das Vorzeichen mit `positive_rotation =
  CLOCKWISE|COUNTER_CLOCKWISE` am protokollierten `rotation_reference_view`.
- Text und Symbol benennen die physische Richtung zusätzlich; Farbe ist dafür nicht nötig.

Regression: Frontend-Test für positive/negative Winkel, beide Symbole und den kanonischen
Plot-Sign-Marker.

### R-03 – unvollständiger History-/Replay-Snapshot

Das Resultat war aus den verarbeiteten Scheiben deterministisch erneut optimierbar, enthielt
aber das Scheibenmodell vor dem Preprocessing und die aktuelle Rotations-/Kalibrierungsbindung
nicht vollständig.

Korrektur: `stem.rotation-optimization-replay.v1` führt rückwärtskompatibel zusätzlich
`raw_slices`, optionale `raw_observations`, `current_pose_phase_deg`, Blick-/Positivrichtung,
Trace-Generation, Prozess-/Phasenkalibrierungs-IDs und Modell-Reason-Codes. Ein
Preprocessing-Parametersweep verwendet vorhandene Rohscheiben und warnt nur bei älteren
Resultaten, die sie noch nicht enthalten.

Regression: Persistieren, Laden und Erhalt von Rohscheiben, aktueller Pose und
Kalibrierungs-Hash.

### R-04 – falsches Shadow-Metadatum in Result-History

Der Resultvertrag setzte korrekt `shadow_mode=true`, die Datenbankprojektion erkannte aber nur
`stem.orientation.v3` als Shadow-Ergebnis. Ein gespeichertes
`stem.rotation-optimization.v1` konnte deshalb im Runtime-Metadatum als nicht-Shadow erscheinen.

Korrektur: Die History-Erkennung akzeptiert nun beide versionierten Shadow-Schemata.

Regression: History-Typ und Shadow-Erkennung für ein fertiges Optimierungsergebnis.

### R-05 – veralteter Stateful-Vertragstest der Krümmungsmessung

Der horizontale Krümmungs-Node ist in der Registry korrekt `stateful`, weil seine optionale
Beobachtungsdrosselung einen track-lokalen Zeitstempel in `RuntimeExecutionContext.state`
führt. Der zugehörige Designer-/Registry-Test erwartete noch den früheren zustandslosen
Vertrag und scheiterte deshalb in der Gesamtsuite.

Korrektur: Die Test-Erwartung lautet nun `stateful=true`. Dadurch bleibt die Echtzeitpipeline
bei aktivierter Drosselung entsprechend der Repository-Regel serialisiert; produktiver Code
oder Messmathematik wurden dafür nicht geändert.

Regression: isolierter Registry-/Loader-Vertragstest und anschließend alle 244
stammbezogenen Backend-Unit-Tests.

### R-06 – ungültiger JSON-Server-Default in Migration 0036

Der erste Updateversuch erreichte Migration `20260820_0036`, scheiterte dort aber mit
`invalid input syntax for type json`. `sa.text(...)` interpretierte die JSON-Fragmente
`:true`, `:0` und `:null` als SQLAlchemy-Bindparameter und kompilierte sie im PostgreSQL-DDL
zu SQL-`NULL`, beispielsweise `"optimization_enable"NULL`.

Korrektur: Der vollständige JSON-Inhalt wird als literaler `server_default` an die Spalte
übergeben und nicht mehr als frei interpretierter SQL-Text. Ein Regressionstest migriert
einen bestehenden Kanal in einer temporären Datenbank und parst den gespeicherten Default;
ein zweiter Test kompiliert die echte Spalte mit dem PostgreSQL-Dialekt und parst das DDL-JSON.

Der Updater führt Migrationen vor `activate_release` aus. PostgreSQL meldete transaktionales
DDL; deshalb blieben Alembic-Revision und `current`-Release nach dem Fehler unverändert. Für
dieses Fehlerbild ist keine manuelle Datenbankänderung vorgesehen: neues Bundle bauen und
über den vorhandenen Updater erneut einspielen.

## Detailprüfung

### A. Fachliche Trennung

- `stem_rotation_geometry.py`, `stem_rotation_optimizer.py` und deren Domänenmodelle kennen
  ausschließlich verfügbare symmetrische Geometrie, Score und `gain_mm`.
- `VORSCHNITT_KRITISCH` wird nur auf `VERTICAL_Z`, `NACHSCHNITT_KRITISCH` nur auf
  `HORIZONTAL_Y` abgebildet.
- `RuntimeStemRotationPriorityControl` ist die eine persistente Quelle. Der
  `PriorityModeProvider` wählt exakt einen Provider; es gibt keinen Rückfall von PLC auf
  Frontend.
- Die HMI verwendet „Geometriegewinn“, „nutzbare Geometrie“, „priorisierte Richtung“ und
  „limitierende“ Position. Absolute Scores stehen nur in der Diagnose.

### B. Koordinaten und Winkel

- Domänenreihenfolge ist ausdrücklich `q=(y,z)`; Achsvektoren sind horizontal `(1,0)` und
  vertikal `(0,1)`.
- `R(delta)=[[cos,-sin],[sin,cos]]` rotiert Mittelpunkt und Ellipsenorientierung gemeinsam;
  Halbachsen bleiben unverändert.
- Kern und Fit verwenden `float`/NumPy `float64` und Radiant. Node-, HMI- und spätere
  SPS-Grenzen verwenden Grad.
- `delta=0` ist Pflichtkandidat. Der effektive Suchbereich ist höchstens 180° und bleibt in den
  signierten mechanischen Grenzen, die 0° enthalten müssen.
- Die Software kann die physische Bedeutung nur gegen das gespeicherte Blick-/Positivprofil
  prüfen. Ob dieses Profil der realen Dreheinrichtung entspricht, ist ein Maschinen-Gate.

### C. Mathematik

- Die Matrixexpansion entspricht `M=R(phi) diag(1/a²,1/b²) R(phi)^T`.
- `A`, `B`, `C`, skalierte Diskriminantentoleranz und numerisch stabile Wurzelbildung sind
  nachvollziehbar implementiert.
- Der Ursprung muss zwischen beiden Schnittpunkten liegen; sonst ist der Span null/ungültig.
- Der Stammscore ist das Minimum über den Evaluationsbereich. Das Diagnosequantil beeinflusst
  die Entscheidung nicht.
- Grob-/Feinsuche bewertet Grenzen und 0°. Innerhalb `score_tolerance_mm` gewinnt der kleinere
  absolute Weg; ein exakter Vorzeichengleichstand ist deterministisch versioniert.
- `gain_mm=optimal-current`; negativer Gain außerhalb der Fließkommatoleranz macht das Resultat
  ungültig. Mindestgain/-winkel setzen den ausgegebenen Stellwinkel auf 0°.

### D. Messung, Preprocessing und Modellqualität

- Der Model-Builder verwendet vorhandene metrische Krümmungs-/Breitenprofile und die
  kalibrierte Mantelphase; keine zweite Segmentierung oder Winkelspur wurde eingeführt.
- Mittelpunkt und Ellipsensupport werden deterministisch mit qualitätsgewichteter Huber-IRLS
  gefittet. Beobachtungszahl, Phasenstützen/-spanne, Kondition, Residuen, positive Definitheit
  und Halbachsen werden geprüft.
- Track-, Trace-, Kalibrierungs- oder Richtungswechsel setzen die Sammlung zurück. Während
  der natürlichen Rollphase wird die kleine signierte X-Drift über die vorhandene
  Mantelspur/Prozesshomographie materialfest registriert. Bestätigungsframes der
  Längsbeschleunigung werden verworfen; mit `longitudinal_conveyor` friert das Modell ein.
- Interpolation ist kurz und beidseitig begrenzt; es gibt keine Extrapolation. `phi` wird mit
  180°-Periodizität behandelt. Robuste Glättung erhält mindestens 75 % einer lokalen
  Engstellenabweichung; eine gut gemessene Engstelle wird nicht als Ausreißer verworfen.
- `model_quality`, Mindestanzahl und die korrigierte gültige Quote im Evaluationsbereich sind
  Freigabegates.

Offen ist keine Codekorrektur, sondern die fachliche Parametrierung: 5-mm-Fitresiduen,
25-%-Mindestqualität, 5–1000-mm-Halbachsen sowie Bewegungs-/Nachbarschaftsgrenzen sind
Repository-Startwerte und keine validierten Anlagenwerte. Die Referenzebenen-Homographie wird
für den Supportfit als orthographische metrische Projektion behandelt; Höhenperspektive,
Okklusion und eine nicht feste reale Drehachse bleiben reale Messgrenzen.

### E. Runtime, Prioritätsquelle und spätere SPS

- Ein neues Track-/Session-Ereignis verwirft den vorherigen Builder-State und dessen Resultat.
- Ein gültiger Winkel entsteht nur aus einem gültigen, eingefrorenen Modell mit kalibrierter
  Phase, eindeutiger Richtung, ausreichender Qualität und gültiger Priorität.
- Channel-Control-Änderungen erhöhen die Revision ohne Runtime-Neustart. Der eingefrorene
  Modell-Hash bleibt gleich; der zustandslose Optimierer berechnet den neuen Modus im nächsten
  Takt. HMI und History vergleichen Quelle, Modus und Revision.
- Der semantische History-Key enthält Session, Modell-Hash, Modus und Revision; wiederholte
  Frames duplizieren dasselbe Ergebnis nicht.
- `priority_source=PLC` führt derzeit absichtlich zu
  `PLC_PRIORITY_PROVIDER_NOT_CONNECTED`, `priority_mode=NONE` und keinem Winkel. Deshalb gibt es
  für diese Funktion noch keinen SPS-Handshake zu prüfen. Vor einer späteren PLC-Aktivierung
  müssen authentifizierter Read-Adapter, Datenalter/Disconnect, Restart, Handshake und Mapping
  separat abgenommen werden.

### F. HMI

- Hauptansicht: Quelle/Revision, Prioritätsumschalter, Achse, großer relativer Winkel,
  physische Richtung und `gain_mm`.
- Längsansicht: Mittellinie, Form-/Ovalitätsentwicklung und beide limitierenden Positionen.
- Querschnitt: Maschinenursprung, aktuelle/optimierte Ellipse mit unterschiedlichen
  Linienstilen, priorisierte Achse, Mittelsehnen und kanonischer Drehpfeil.
- Delta-Plot: übernimmt `DeltaG_i` unverändert aus dem Backend, besitzt Nulllinie und
  text-/stilunterschiedene positive und negative Werte.
- Diagnose: absolute Scores, Fit-/Messqualität, Scheibenanzahl, Scorekurve und Konfiguration.
- Nicht synchrone Prioritätsrevisionen verbergen die Empfehlung. Historie erscheint bei
  gestoppter Runtime nur mit expliziter Kennzeichnung.

Ein manueller visueller Abnahmelauf auf der realen Industrieanzeige bleibt nötig; auf diesem
Entwicklungs-PC wird die Anwendung entsprechend der Deploymentvorgabe nicht gestartet.

### G. Logging und Replay

- Persistiert werden Resultat, UTC-Zeit, Session/Track, Quelle/Modus/Revision, Software- und
  Geometrieversion, Modell-/Konfigurations-Hash, Roh-/Endmodell, Fitdiagnose,
  Rotations-/Kalibrierungsbindung, vollständige bewertete Scorekurve, Scores, Gain,
  limitierende Positionen, Qualität und Reason-Codes.
- Inline-Bilder werden nicht unkontrolliert in Result-History kopiert. Rohvideos bleiben im
  bestehenden Capture-/Datasetpfad.
- Replay ruft den produktiven Node und den produktiven HMI-ViewModel-Builder auf; es existiert
  keine zweite Geometrieformel.
- Gleicher Input, gleiche Version und gleiche Konfiguration liefern deterministische
  Entscheidung und Kurve. Versions-/Konfigurationsabweichungen werden gemeldet.

### H. Testabdeckung

Abgedeckt sind unter anderem:

- analytische Kreise/Ellipsen, Offsetzentrum, Engstelle, wechselnde limitierende Scheibe,
  Tangente, Ursprung außerhalb, 180°-Symmetrie;
- beide Prioritätsmodi, positive/negative optimale Winkel, Tie-Break, Gain-/Winkeltotzone,
  Grenzen, deterministische Grob-/Feinsuche;
- Kreis-/Ellipsen-/Mittelpunktfit, Rauschen, Ausreißer, unzureichende Winkelabdeckung,
  kurze/lange Lücken, Pixel-mm-/Kalibrierungsbindung;
- Trackreset, Transfer-only-Freeze, signierte Material-X-Registrierung, Moduswechsel ohne
  Neumessung, stale Revision, PLC
  fail-closed, History/Replay und Shadow-Metadaten;
- alle geforderten HMI-Zustände, Achsen, Richtungspfeile, keine gültige Anzeige bei Invalid,
  Backend-Ellipsen, limitierende Positionen und unveränderte `DeltaG_i`.

Es gibt noch keine echte Bild-/Messsequenz als versionierten Regressionstest und keine im
Projekt etablierte Snapshot-/Visual-Regression-Infrastruktur. Serverseitige Screenshots wurden
daher nicht erzeugt.

### I. Performance

Gemessen wurde der komplette produktive Priority-/Runtime-/Optimizer-/HMI-Pfad, 1° grob,
0,1° fein, ±90°, drei Wiederholungen:

| Länge | Raster | Scheiben | Median | Maximum |
|---:|---:|---:|---:|---:|
| 3 m | 25 mm | 121 | 245,6 ms | 247,0 ms |
| 3 m | 50 mm | 61 | 125,9 ms | 126,8 ms |
| 3 m | 100 mm | 31 | 66,1 ms | 66,4 ms |
| 6 m | 25 mm | 241 | 480,7 ms | 503,6 ms |
| 6 m | 50 mm | 121 | 240,7 ms | 242,1 ms |
| 6 m | 100 mm | 61 | 125,4 ms | 126,2 ms |

Es wurde kein Hotspot geändert: Die Werte sind für ein Offline-Shadow-Ergebnis plausibel, aber
ohne definierte Zeitreserve bis zum ersten Walzenpaar und ohne Zielserverlast nicht freigabefähig.

### J. Dokumentation

Ergänzende operative Dokumente:

- `stem_rotation_runtime_integration.md`: Quelle, Runtimezustände, Datenfluss, Konfiguration,
  Safety, HMI und Deploymentgrenze;
- `stem_slice_measurement.md`: Fit, Material-X, Kalibrierungsannahme und Messgrenzen;
- `stem_rotation_validation_and_commissioning.md`: reales Replay, Simulator, Diagnoseexport,
  Monte Carlo, Parametersweep, Performance und Startwerte.

## Verifikation auf dem Entwicklungsrechner

Entsprechend der Deploymentregel wurde keine Anwendung und kein Dienst gestartet. Geprüft
wurden ausschließlich statische Checks, Unit-/Contract-Tests, Frontend-Tests und der
Produktionsbuild:

| Prüfung | Ergebnis |
|---|---|
| Gesamte stammbezogene Backend-Suite (`test_stem_*`, `test_runtime_stem_*`, Kamera-Kalibrierung) | **PASS**, 244 Tests |
| Nach den Replay-/Runtime-Korrekturen fokussierte Audit-Suite | **PASS**, 34 Tests |
| Migration 0036 und Priority-Channel nach R-06 | **PASS**, 5 Tests |
| Admin-Web Vitest gesamt | **PASS**, 14 Dateien / 139 Tests |
| TypeScript `tsc --noEmit` | **PASS** |
| Admin-Web Produktionsbuild | **PASS**, Hinweis auf bestehenden ca. 1,1-MB-Chunk |
| ESLint gesamt | **PASS ohne Fehler**, 87 bestehende Warnungen |
| Ruff, geänderte Optimierungs-/Replay-Dateien | **PASS** |
| isolierter Mypy-Lauf der geänderten Optimierungs-/Replay-Module | **PASS** |
| Performancebenchmark, 31–241 Scheiben | **PASS**, Messwerte unter Abschnitt I |

Der unveränderte Aufruf der vollständigen Python-Suite brach zunächst bereits bei der
Collection ab: `test_runtime_pipeline_authoring_preview.py` importiert das nicht vorhandene
Symbol `_should_emit_mjpeg_snapshot` aus den Runtime-Admin-API-Routen. Mit ausschließlich
dieser Datei ausgeschlossen lief die Suite vollständig durch und ergab **1.192 PASS / 50
FAIL / 1 Warning** in 648,38 s. Einer dieser Fehler war R-05 und ist anschließend isoliert
korrigiert sowie in der kompletten 244-Test-Stammsuite bestätigt worden. Die übrigen Fehler
liegen in Acceptance/Deployment, Plattform-E2E, Access Management, Inference Gateway,
Realtime, Channel Management, Runtime Execution/Lifecycle, Domain Contracts, temporalen
Datasetverträgen und YOLO-Training. Sie wurden nicht durch diesen Auditauftrag verändert.

Der repository-weite Ruff-Lauf ist ebenfalls keine grüne Baseline: **2.755 Meldungen**, davon
unter anderem 962 Zeilenlängen-, 648 Argumenttyp- und 124 Importsortierungsbefunde; zwei
`undefined-name`-Befunde sind enthalten. Ein transitiver Mypy-Lauf über den betroffenen
Importgraphen meldete 100 bestehende Fehler in 17 importierten Dateien, während der isolierte
Lauf der geänderten Module grün ist. Diese globalen Befunde verhindern eine pauschale
Repository-Freigabe, ändern aber nicht das Ergebnis der fokussierten Stammprüfung.

## Offene WARN-Punkte und konkrete Empfehlung

1. **Kein realer Referenzstamm im Repository.** Auf dem Zielserver zuerst mehrere bestehende
   reale Stämme als `stem.projection-observations.v1` und
   `stem.rotation-optimization.v1` sichern. Mindestens gerade/rund, gebogen, oval, konisch,
   Engstelle, schwierige Segmentierung und Grenzabdeckung wiederholt aufnehmen.
2. **Maschinenrichtung noch nicht bewiesen.** Vor jeder Nutzung des Vorzeichens eine sichere,
   kleine Referenzdrehung mit sichtbarer Markierung ausführen und Phase, `+x`,
   `rotation_reference_view`, `positive_rotation`, HMI-Symbol und reale Mechanik gemeinsam
   protokollieren.
3. **Startwerte sind nicht produktionsvalidiert.** Messstreuung aus Wiederholungen ermitteln;
   erst danach Residual-/Quality-Gates, `score_tolerance_mm`, `min_gain_mm`,
   `min_rotation_deg`, Raster/Glättung und Interpolationsgrenze freigeben.
4. **Zielserverzeit fehlt.** Latenz vom Entscheidungsframe bis zum gespeicherten Ergebnis unter
   realer Kamera-/Inferenzlast messen und gegen die Anlagenzeit bis zum ersten Walzenpaar
   abnehmen.
5. **Globale Repository-Baseline nicht vollständig grün.** Die bestehenden Preview-Import-,
   PNG-Fixture-, Realtime-Timing-/SQLite-, Ruff- und Mypy-Probleme separat bereinigen. Sie sind
   nicht durch den Stammoptimierungspfad verursacht, verhindern aber die Aussage „gesamtes
   Repository grün“.
6. **PLC ist nur Adaptergrenze.** Erst nach separater Spezifikation und Tests für Read-Health,
   Datenalter, Restart/Disconnect, Handshake, Enum-/Einheitenmapping und Source-Ownership von
   `FRONTEND` auf `PLC` umstellen. SPS-Schreiben bleibt bis zur ausdrücklichen Freigabe aus.

## Kompakter Maschinenversuchs-Check

Der Versuch darf nur als Shadow-Lauf beginnen. Ein nicht erfülltes Stop-Kriterium bedeutet:
keine Gradempfehlung fachlich verwenden, Ursache protokollieren, Replay sichern.

### Vor dem ersten Stamm

- [ ] **Update/Migration:** Update über den vorhandenen Updater installiert; Migration
  `20260820_0036` erfolgreich; Pipelineversion und Softwareversion protokolliert.
- [ ] **Shadow sicher:** Produktionsgraph enthält keinen `output.siemens_sps_write` für diese
  Funktion; Ergebnis zeigt `shadow_mode=true` und `plc_writes_suppressed=true`.
- [ ] **Prozesskalibrierung gültig:** aktiver Kalibrierungs-Hash passt zu Kamera, Auflösung,
  kanonischer Ansicht und Krümmungsprofil; Pixel-mm-Maß an einem bekannten Maß kontrolliert.
- [ ] **Drehwinkeltracking gültig:** kalibriert, gelockt, eindeutiger Track/Trace, keine
  ungeklärten Resets; Phasenabdeckung mindestens gemäß Konfiguration; Konfidenz ausreichend.
- [ ] **Richtung verifiziert:** sichere Referenzdrehung beweist Vorzeichen, Blickstandpunkt und
  Uhr-/Gegenuhrzeigersinn an Kamera, HMI und Maschine. Ergebnis mit Foto/Log dokumentiert.
- [ ] **Mechanische Grenzen:** negative/positive Grenzen kommen aus der realen Mechanik,
  enthalten 0° und lassen keine verbotene Richtung zu.
- [ ] **Frontendquelle eindeutig:** `priority_source=FRONTEND`; der gewünschte Modus und die
  aktuelle Revision sind in Control, Live-View und späterem Resultat identisch.
- [ ] **Startparameter markiert:** verwendete Werte sind als Versuchsstand, nicht als
  Produktionsfreigabe, versioniert.
- [ ] **Logging aktiv:** Result-History speichert semantische Shadow-Ergebnisse; freier
  Speicher/Retention und Exportpfad sind geprüft.

### Pro Stamm

- [ ] Session-/Track-ID ist neu; kein alter Winkel bleibt während `MEASURING` sichtbar.
- [ ] Beobachtungen entstanden ausschließlich im Transfer; das Modell wurde spätestens beim
  bestätigten Längstransport eingefroren und danach nicht mehr verändert.
- [ ] Registrierte X-Auslenkung und Einzelschritte sind plausibel (erwartet im Testaufbau
  höchstens ungefähr `+/-100 mm`); keine Registrierungs-Ausreißercodes.
- [ ] `model_quality`, gültige Scheibenquote/-zahl, maximale Lücke, Phasenabdeckung und
  Fitresiduen erfüllen die Versuchsgrenzen.
- [ ] Priority-Quelle, Modus, Achse und Revision stimmen zusammen.
- [ ] `result_ready=true` und `result_valid=true`; andernfalls Winkel ignorieren.
- [ ] `gain_mm >= 0`; bei Low Gain/kleinem Winkel gilt `should_rotate=false` und 0° Ausgabe.
- [ ] Winkel liegt innerhalb der mechanischen Grenzen; Symbol/Text entsprechen der verifizierten
  realen Drehrichtung.
- [ ] Limitierende Region ist in Längsansicht, Querschnitt und Scorekurve plausibel; bei
  springender Einzelscheibe die Nachbarschaft beurteilen.
- [ ] Ergebnis liegt rechtzeitig vor dem ersten Vorzentrierungs-Walzenpaar vor.
- [ ] History-Eintrag enthält Roh-/Endscheiben, Pose/Kalibrierung, Konfiguration, Kurve und
  Resultat.

### Nach der Versuchsserie

- [ ] Mindestens ein Ergebnis mit unveränderter Konfiguration offline replayt; Entscheidung,
  Kurve und limitierende Position stimmen deterministisch überein.
- [ ] Wiederholmessungen desselben/vergleichbarer Stämme liefern Streuung für Winkel, Gain und
  limitierende Region.
- [ ] `min_gain_mm` und `score_tolerance_mm` liegen nachvollziehbar oberhalb relevanter
  Messstreuung beziehungsweise berücksichtigen Scoreplateaus.
- [ ] Problemfälle wurden als pseudonymisierte Diagnosepakete gesichert; mindestens ein realer
  Referenzfall wird nach Review versioniert.
- [ ] Erst nach gesonderter fachlicher Freigabe entscheiden, ob ein nächster Versuchsschritt
  weiterhin Shadow bleibt oder eine separat abgesicherte Aktoranbindung geplant wird.

## Harte Stop-Kriterien

- Kalibrierungs-, Track-, Trace- oder Richtungsbindung fehlt beziehungsweise wechselt.
- Uhr-/Gegenuhrzeigersinn oder `+x` ist nicht praktisch verifiziert.
- Modell-/Fitqualität, Scheibenquote oder Winkelabdeckung unterschreitet den Grenzwert.
- HMI-Control und Resultat haben unterschiedliche Quelle, Mode oder Revision.
- Gradwert erscheint bei `result_valid=false`, außerhalb der Grenzen oder bei aktivem neuen
  Messzyklus als alte Empfehlung.
- History-/Replay-Datensatz fehlt oder identisches Replay ist nicht deterministisch.
- Ergebnis kommt nicht mit nachgewiesener Zeitreserve vor dem ersten Walzenpaar.
- Unerwarteter SPS-Schreibpfad oder `plc_writes_suppressed != true` wird festgestellt.
