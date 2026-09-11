# Persistenzentscheidung zur intrinsischen Kamerakalibrierung

## Entscheidung

Für `preprocessing.camera_undistort` wird keine Datenbankmigration und keine neue
Kalibrierungstabelle angelegt. Das Profil liegt im Config-Feld `calibration` des Nodes.
`backend/packages/cv-runtime/src/cv_runtime/registry.py` —
`BUILTIN_NODE_TYPES` beschreibt dieses Feld als
`camera_intrinsic_calibration`.

Die Entscheidung verwendet die vorhandenen Persistenz- und Freigabesymbole:

- `backend/packages/cv-db/src/cv_db/models.py` — `PipelineVersion.graph` und
  `PipelineNode.config` speichern die Node-Konfiguration in PostgreSQL.
- `backend/packages/cv-db/src/cv_db/models.py` —
  `RuntimeChannel.pipeline_version_id` ordnet die publizierte Version dem Channel zu.
- `backend/packages/cv-db/src/cv_db/pipeline_store.py` —
  `create_pipeline_version` legt eine neue unveränderliche Version an;
  `publish_pipeline_version` validiert sie und bindet bei einer Channel-Instanz mit
  `_bind_owner_channel_to_published_version` genau diese Version an den Channel.
- `backend/packages/cv-db/src/cv_db/pipeline_store.py` —
  `_valid_camera_intrinsic_calibration` verhindert die Freigabe eines fehlenden,
  ungültigen oder nachträglich veränderten Profils.

Damit sind Profilversion, Produktionsfreigabe, Channel-Referenz und fachlicher Rollback
bereits durch denselben Pipelineversionsdatensatz atomar gekoppelt. Eine zusätzliche
Tabelle würde einen zweiten, potentiell widersprüchlichen Freigabe- und Rollbackzustand
erzeugen. Eine eigene Profiltabelle wird erst benötigt, wenn ein Profil unabhängig von
Pipelineversionen zwischen mehreren Channels geteilt werden soll; diese Anforderung ist
offen und nicht Bestandteil dieser Ausbaustufe.

## Lebenszyklus der Beobachtungsbilder

`backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` —
`capture_channel_calibration_frame` nimmt weiterhin exklusiv genau ein Bild auf und gibt
die Kamera sofort frei. Der Designer sammelt mehrere Antworten ausschließlich im lokalen
React-Zustand von
`frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`
— `CameraIntrinsicCalibrationEditor` und sendet die angenommenen Beobachtungen an
`calculate_channel_camera_calibration`. Weder `RuntimeSource.config` noch
`RuntimeChannel` noch die Node-Konfiguration enthalten Kalibrierungsbilder.

Persistiert wird erst das kleine Ergebnis
`RuntimeCameraIntrinsicCalibrationProfile` aus
`backend/packages/cv-contracts/src/cv_contracts/runtime.py`. Der API-Audit-Eintrag
`runtime.channel.camera_calibration.calculated.v1` enthält nur Status, Hash, Methode,
Anzahlen und Fehlerwerte, keine Inline-Bilder.

## Update und Rollback

`deployment/README.md` — Abschnitt zum dockerfreien Windows-/WSL2-Installer dokumentiert
`/srv/cv-platform` als persistenten Datenbereich und stellt klar, dass Updates nur den
Releasepfad austauschen, während PostgreSQL-Daten erhalten bleiben. Da das Profil Teil
von `PipelineVersion.graph`/`PipelineNode.config` ist und keine neue Ablage besitzt,
bleibt es unter demselben vorhandenen Persistenzversprechen erhalten.

Der fachliche Rollback auf ein früheres Kalibrierungsprofil erfolgt durch erneutes Binden
beziehungsweise Publizieren der dazugehörigen älteren Pipelineversion; sie enthält
weiterhin ihren eigenen Profilhash und ihre eigenen intrinsischen Parameter. Ein eigener
dockerfreier Release-Rollbackbefehl ist laut
`docs/stem-orientation/implementation-map.md` — Kollision 10 weiterhin offen. Diese
Ausbaustufe behauptet daher keinen neuen Installer-Rollbackmechanismus, verändert aber
auch keinen persistenten Datenpfad.

## Bewusst nicht verwendete Ablagen

- `backend/packages/cv-db/src/cv_db/models.py` — `RuntimeSource.config` ist ungeeignet,
  weil es keine eigene Version/Freigabe des Kalibrierungsstands gegenüber einer
  Pipelineversion besitzt.
- `backend/packages/cv-db/src/cv_db/models.py` — `RuntimeChannel.stream_profile` ist ein
  Streamtransportprofil und kein versionierter Pipelinevertrag.
- Eine neue Tabelle ist nicht erforderlich, solange keine channelübergreifende
  Profilfreigabe verlangt wird.
