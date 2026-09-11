# Change Report Prompt 06 – Shadow-Result und Vor-Einzug-Review

Stand: 2026-08-03

## Ergebnis

Das in Prompt 05 von `StemTemporalOrientationHandler.execute` eingefrorene
`stem.orientation.v2`-Result läuft weiterhin ausschließlich über den bestehenden
Pfad `ResultPayload -> output.result -> RuntimeExecutionResult.persisted_results
-> ResultRecord -> Result-Service -> Result-History`. Es wurde kein paralleler
Resultdienst und keine zweite Oberfläche angelegt.

`_orientation_result` in
`backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` ergänzt den
unveränderlichen Vor-Einzug-Datensatz um:

- Channel-/Pipeline-, Beobachtungs- und Entscheidungsidentität,
- begrenzte Krümmungs- und Rotationsspuren einschließlich Qualitätswerten,
- Modell-, Intrinsik-, Rotations-, Prozesszonen-, Feature- und
  Algorithmusversionen,
- Richtungskonvention und Blickstandpunkt,
- getrenntes Feld `post_rotation_verification`,
- explizite, derzeit noch nicht aufgelöste Artefaktverweise.

`_pre_entry_overlay_label` zeigt bei gültiger Entscheidung Winkel, physische
Richtung und Confidence und bei ungültigem Winkel ausdrücklich
`Keine sichere Winkelentscheidung – Bediener erforderlich`. Die Ausführung
bleibt `operating_mode="shadow"`; es wurde kein SPS-Schreibpfad ergänzt.

## Persistenzsemantik

`RealtimeRuntimeWorker._history_result_persist_due` und
`_stem_orientation_history_event_key` in
`backend/services/realtime-executor/src/realtime_executor/live.py` behandeln den
ersten entscheidungsbereiten Stand einer Session als Ereignis. Bei aktivierter
`RuntimeResultHistoryPolicy` wird er sofort gespeichert, auch wenn das normale
Sample-Intervall noch nicht abgelaufen ist. Derselbe eingefrorene Stand wird
nicht auf jedem späteren Frame erneut gespeichert.

`record_runtime_execution_result` und `_result_type` in
`backend/packages/cv-db/src/cv_db/runtime_execution_store.py` speichern diesen
Datensatz im vorhandenen `ResultRecord` mit `result_type="stem_orientation"`.
`_apply_filters` in `backend/packages/cv-db/src/cv_db/result_store.py` macht
Track- und Session-IDs über die bestehende Textsuche auffindbar.

Voraussetzung bleibt die vorhandene Architektur: Die Pipeline muss den
Temporal-Resultport an `output.result` anschließen und die Channel-History-Policy
muss aktiviert sein. Ohne dieses Opt-in wird kein versteckter Persistenzpfad
erfunden.

## Datenbankentscheidung

Für das Hauptresult ist keine neue Tabelle nötig: `ResultRecord` kann das
kompakte, unveränderliche `stem.orientation.v2`-JSON vollständig aufnehmen.

Für den Schnellreview reicht `ResultRecord` nicht sauber aus. Mehrere
Bedienerkorrekturen mit Revision, Supersession, Revieweridentität und optionalem
Zielintervall würden andernfalls das ursprüngliche Result-JSON mutieren oder
eine untypisierte, nicht unabhängig abfragbare Liste darin erzeugen. Deshalb
wurde genau eine append-only Tabelle ergänzt:

- `StemOrientationReview` in
  `backend/packages/cv-db/src/cv_db/models.py`,
- Migration `20260803_0035_stem_orientation_reviews.py` in
  `backend/migrations/versions`,
- `create_stem_orientation_review` und `list_stem_orientation_reviews` in
  `backend/packages/cv-db/src/cv_db/result_store.py`.

Jede neue Revision verweist über `supersedes_review_id` auf die vorherige;
`create_stem_orientation_review` sperrt dafür den Eltern-Resultdatensatz und
ändert dessen Nutzdaten nicht. `_result_has_no_active_dependents` in
`backend/workers/maintenance-worker/src/maintenance_worker/jobs.py` schützt
reviewte Resultate vor der normalen History-Löschung. Der Migrations-Downgrade
behält die Bedienerdaten absichtlich; ein erneutes Upgrade verwendet die Tabelle
idempotent weiter. Damit bleiben Reviewdaten über normale Installer-Updates und
Anwendungs-Rollbacks erhalten.

## Bestehender Result-Service und bestehende UI

Die Verträge `RuntimeStemOrientationReviewCreateRequest`,
`RuntimeStemOrientationReview` und die beiden Response-Typen liegen in
`backend/packages/cv-contracts/src/cv_contracts/runtime.py`. Der bestehende
Result-Service stellt darunter nur zwei integrierte Routen bereit:

- `GET /api/v1/runtime/results/{result_record_id}/stem-orientation-reviews`
  über `get_stem_orientation_reviews`,
- `POST /api/v1/runtime/results/{result_record_id}/stem-orientation-reviews`
  über `append_stem_orientation_review`.

Lesen folgt `runtime.results.read`; Schreiben ist über
`runtime.results.review` auf Plattformadministration, ML Engineering und
Operatoren begrenzt. Das generierte Result-Service-Schema ist in
`contracts/openapi/result-service.v1.yaml` fortgeschrieben.

`StemOrientationResultSummary` und `StemOrientationReviewPanel` in
`frontend/apps/admin-web/src/features/results/result-history-page.tsx` sind in
den bestehenden `ResultDetailPanel` eingebettet. Sie zeigen die ursprüngliche
Vor-Einzug-Empfehlung, die getrennte optionale Nachkontrolle und append-only
Reviewrevisionen. Der Review ist ausdrücklich Diagnose/Vorannotation und kein
verbindliches Ground-Truth-Label.

## Offene Grenzen

- Rohvideo, Diagnosevideo und konkrete Frameartefakte werden in dieser Phase
  noch nicht aufgezeichnet. `_orientation_result` speichert daher
  `asset_ref=null`, `status="not_recorded"` und den Reason Code
  `temporal_asset_recorder_not_configured`; es werden keine URIs geraten.
- Ein Produzent für `post_rotation_verification` ist noch nicht vorhanden. Das
  Feld und die UI-Trennung verhindern bereits, dass eine spätere Nachkontrolle
  die eingefrorene Hauptempfehlung unbemerkt ersetzt.
- Der Export des Schnellreviews in das verbindliche Label-Studio-Schema bleibt
  eine spätere Phase. Die jetzigen Revisionen sind nur Vorannotation und
  Kandidatenauswahl.
- Das in der Spezifikation geforderte Result
  `aborted_runtime_restart` bleibt wegen des fehlenden Handler-Finalize-Hooks
  offen; siehe Kollision 7 in `docs/stem-orientation/implementation-map.md`.

## Verifikation auf dem Entwicklungs-PC

Es wurden keine Anwendung, kein Dienst und kein Container gestartet.

- Python: 17 gezielte Tests bestanden, darunter Temporal-Result, bestehender
  Output-/History-Pfad, Result-Service/Review und semantische Live-Persistenz.
- Frontend: `result-history-page.test.tsx` – 5 Tests bestanden.
- TypeScript: `tsc --noEmit -p frontend/apps/admin-web/tsconfig.json` bestanden.
- Frontend-Build: vorhandener Vite-Produktionsbuild bestanden; nur die bereits
  bekannte Chunk-Größenwarnung wurde ausgegeben.
- Importsortierung: Ruff `--select I` für alle berührten Python-Dateien
  bestanden.
- OpenAPI: `result-service.v1.yaml` wurde über den vorhandenen
  `scripts/dev/export_openapi.py`-Pfad neu erzeugt.

