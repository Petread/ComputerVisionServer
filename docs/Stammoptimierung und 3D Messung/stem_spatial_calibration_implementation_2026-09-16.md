# Räumliche Kalibrierung mit der vorbereiteten Z-Achse

Stand: 16.09.2026. Umsetzung im bestehenden Kalibrierungsassistenten und Runtimepfad; noch nicht auf dem Anlagenserver erprobt.

## Was umgesetzt ist

Der bestehende Schritt für Prozesskoordinaten hat die Option **Use measured Z reference for spatial camera calibration** erhalten. Die bekannten Punkte O, X, Y und XY sowie der vermessene Z-Endpunkt bestimmen zusammen mit der gemessenen Kameraintrinsik eine räumliche Kamerapose. Gespeichert werden Rotation, Translation, Kameraposition in Anlagenkoordinaten, Projektionsmatrix und Fehlerdiagnostik. Die XY-Homographie wird bei dieser Option aus derselben räumlichen Pose abgeleitet. Damit widersprechen sich Ebenenprojektion und räumliche Projektion nicht.

Das bestehende Scheibenmodell und die Optimierung nutzbarer Geometrie bleiben das Zielsystem. Dieser Auftrag liefert die Kamerageometrie als Grundlage für die anschließende räumliche Bewegungs- und Formschätzung. Er ersetzt noch nicht deren bisherige Beobachtungsmodelle: Insbesondere wird der Hub eines Stamms durch diese Kalibrierung allein noch nicht gemessen.

## Einrichtung an der Anlage

1. Im vorhandenen vorgeschalteten Kamera-/Undistort-Knoten eine gemessene Schachbrett- oder ChArUco-Kalibrierung erstellen. Auflösung, Optik und Fokus müssen zur späteren Aufnahme passen. Das bestehende Durchreichprofil `IDENTITY_PASSTHROUGH` reicht für diese räumliche Berechnung nicht.
2. Im Koordinatenassistenten ein vollständiges, intrinsisch entzerrtes Bild einfrieren. Die Punkte werden im bestehenden Assistenten auf diesem Bild markiert; ein zugeschnittenes oder perspektivisch ausgerichtetes Modellbild ist kein Ersatz für dieses Referenzbild.
3. O, X, Y und die XY-Ecke eines tatsächlich vermessenen Rechtecks in der Referenzebene markieren. X liegt längs zum Stamm, Y quer dazu. X- und Y-Abstände in Millimetern eingeben.
4. Direkt am selben Ursprung O eine bekannte senkrechte Strecke bestimmen, beispielsweise mit einem lotrechten Maßstab. Den tatsächlichen Endpunkt im Bild als Z-Punkt markieren und die gemessene Strecke in Millimetern eingeben. Ein frei platzierter Richtungspfeil ist keine Höhenreferenz.
5. Positive Z-Richtung und Kameraseite passend wählen. Die räumliche Berechnung verwendet ein rechtshändiges XYZ-System. Der Z-Endpunkt hat darin die Koordinaten `(0, 0, Z_Referenzebene + Z_Strecke)`; bei positiv nach unten konfiguriertem Z müssen auch X/Y und Kameraseite zu dieser Achsenwahl passen.
6. Räumliche Option aktivieren, berechnen und das Ergebnis im bestehenden Pipeline-Konfigurationsablauf speichern. Angezeigt werden Kameraposition, räumlicher RMS-Fehler, größter Punktfehler und Z-Punktfehler. Nach einer Änderung der Kameraposition oder Intrinsik ist neu zu kalibrieren.

Ein rollender Referenzkörper ist dafür nicht erforderlich. Die Einrichtung benötigt aber vermessene feste Geometrie und eine belastbare Intrinsik. Zusätzliche, nicht zum Fit verwendete Kontrollmaße auf anderen Höhen sind für die spätere Genauigkeitsprüfung sinnvoll. Ein kleiner Fitfehler ist noch kein unabhängiger Nachweis einer Millimetergenauigkeit.

## Berechnung und Gültigkeit

- Poseninitialisierung aus der XY-Ebene mit OpenCV IPPE; anschließende gemeinsame Verfeinerung aller XY- und Z-Punkte mit `solvePnPRefineLM`.
- Verwendung von `output_camera_matrix` der vorhandenen Intrinsik. Da die Bildpunkte bereits entzerrt sind, wird Verzeichnung nicht nochmals angewendet.
- Prüfung von Kanal, Bildauflösung und Intrinsikhash. Keine räumliche Berechnung aus einem Durchreichprofil oder ohne bekannte Z-Länge; kein automatischer Rückfall auf ein gültiges XY-Ergebnis bei fehlgeschlagener räumlicher Berechnung.
- Referenzen müssen endlich und innerhalb des Referenzbildes sein. Der Abstand O–Z muss mindestens `max(5 px, 5 × Fehlergrenze)` betragen. Die bestehende Fehlergrenze gilt im räumlichen Modus sowohl für den RMS-Fehler als auch für jeden einzelnen Referenzpunkt.
- Die Lösung muss Referenzen vor der Kamera und die Kamera auf der konfigurierten Seite der Ebene ergeben. Nahezu gleich gut passende, um mehr als 1° unterschiedliche Kandidaten werden verworfen. Die auf die Referenzausdehnung skalierte Pose-Jacobi-Matrix wird auf schlechte Kondition geprüft; Grenze `1e6`. Diese Schranken sind Plausibilitätsprüfungen, keine kalibrierte Unsicherheitsangabe.
- Beim Laden werden Pose, Kamerazentrum, Projektionsmatrix, Reprojektionsdiagnostik und XY-Homographie gegeneinander geprüft. Die räumlichen Daten sind Teil des bestehenden Profilhashes.

## Weitergabe in der Pipeline und Kompatibilität

Der vorhandene `preprocessing.process_coordinate_rectify`-Knoten führt das ursprüngliche Profil im Frame mit. Zusätzlich enthält die Bildtransformation die in den jeweiligen Ausschnitt transformierte räumliche Projektionsmatrix und die Kameraposition. Crop und perspektivische Ansichten verwenden `P_Ansicht = H_Bild→Ansicht · P`; das interne achsengerechte Messraster führt diese Transformation ebenfalls mit.

Die neuen Projektionshilfen projizieren bekannte XYZ-Punkte ins Bild beziehungsweise schneiden Bildstrahlen mit einer **explizit vorgegebenen** Höhe. Aus einem einzelnen Bildpunkt wird keine unbekannte Höhe erfunden. Temporär abgeleitete Ansichtsprofile sind wie bisher keine neu persistierten Kalibrierungen; ihre Identität bleibt die des ursprünglichen Profils.

Vorhandene XY-Profile behalten ihren Hash und ihre bisherige Bedeutung. Neue Vertragsfelder sind optional; der Berechnungsaufruf aktiviert die räumliche Erweiterung ausdrücklich. API, Pipeline-Konfiguration und Speicherung bleiben im bestehenden System. Es ist keine Datenbankmigration erforderlich. Eine neue räumliche Kalibrierung ändert den Prozessprofilhash und damit die Bindung bestehender Phasen-/Messkalibrierungen; diese dürfen nicht mit der alten Bindung weiterverwendet werden.

## Dateien und Verifikation

Rechenkern: `backend/packages/cv-runtime/src/cv_runtime/stem_spatial_calibration.py`. Einbindung: `stem_process_coordinates.py` und der gleichnamige bestehende Runtime-Knoten. Python-Verträge, TypeScript-Verträge und `runtime-admin-api.v1.yaml` sind erweitert. Die Bedienung bleibt in `runtime-pipeline-designer-page.tsx`.

Die fokussierten Backend-Tests prüfen eine unabhängig perspektivisch gerenderte Szene mit bekannter Kamerapose, verschiedene Referenzhöhen, gestörte Bildpunkte, falsche Z-Maße, fehlende Intrinsik, widersprüchliche Kameraseite, manipulierte Profile, Crop-/Rasterprojektion, Runtime-Weitergabe und unveränderte alte XY-Hashes. Frontend-Tests prüfen unter anderem die Freigabevoraussetzungen und das Abweisen unvollständiger räumlicher Profile. TypeScript-Prüfung und Produktionsbuild der bestehenden Admin-Web-App sind Teil der lokalen Verifikation.

Ergebnis: 30 fokussierte Backend-Tests und 42 Tests der Frontend-Helfer bestanden; TypeScript-Prüfung und Vite-Produktionsbuild erfolgreich. Der OpenAPI-Vertrag wurde mit dem vorhandenen Exporter erzeugt. Die neuen Python-Dateien bestehen Ruff; in den geänderten Bestandsdateien kommen gegenüber dem vorherigen Arbeitsstand keine neuen Ruff-Befunde hinzu. Vite meldet weiterhin einen großen JavaScript-Chunk, beendet den Build aber erfolgreich.

Live-Abnahme und eine quantitative Genauigkeitsmessung an der Anlage stehen aus. Die räumliche Bewegungszerlegung und die Nutzung der Kamerapose im Scheibenfit sind die anschließenden Arbeiten.
