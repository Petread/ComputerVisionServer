# Modellladen über gestartete Kanäle – Umsetzung vom 16.09.2026

Deployment stellt Modellartefakte bereit. Das Laden für die Inferenz folgt jetzt den konkreten Modellbindungen gestarteter Kanal-Runtimes. Grundlage ist die [vorherige Prüfung](model-loading-channel-lifecycle-review-2026-09-16.md).

## Auswahl einzelner Modellversionen

| Kanalbedarf | Geladene Versionen derselben Modellfamilie |
| --- | --- |
| Versionen 1, 2 und 3 deployt, alle Kanäle gestoppt | Keine |
| Kanal A startet mit Version 3 | Nur 3 |
| Zusätzlich Kanal B mit Version 1 | 1 und 3; Version 2 bleibt ungeladen |
| Kanal B stoppt und seine Aufträge sind abgeschlossen | Nur 3 |
| Auch Kanal A stoppt und seine Aufträge sind abgeschlossen | Keine |

Explizit ausgewählte Releases bleiben fest gebunden. Bei `active_production` wird die Auswahl beim Runtime-Start in ein konkretes Release aufgelöst. Alle Inferenzaufrufe dieser Runtime verwenden diese gespeicherte Bindung. Der bestehende Abgleich kann bei geänderter Pipeline-/Produktionsbindung einen Kanalneustart anfordern; erst die neue Runtime erhält die neue Auswahl. Ein beliebiger neuer Trainings- oder Deployment-Zwischenstand wird dadurch nicht automatisch geladen.

Benötigt ein Kanal mehrere Modelle, umfasst sein Bedarf die Modellknoten seiner veröffentlichten Pipeline und seiner aktivierten, zugeordneten Trigger-Pipelines. Ein Modell einer anderen Pipeline ist dadurch nicht für beliebige Aufrufe freigegeben.

Triton erhält beim Laden eine explizite `version_policy.specific.versions` über den bestehenden Model-Repository-Endpunkt. Die auf der Platte vorhandenen Versionen werden nicht pauschal geladen. Die Konfiguration wird als JSON-Ladeparameter übergeben; die Paketmetadaten werden dafür nicht umgeschrieben. Grundlage: [NVIDIA Model Repository Extension](https://docs.nvidia.com/deeplearning/triton-inference-server/archives/triton-inference-server-2450/user-guide/docs/protocol/extension_model_repository.html).

## Lebenszyklus und Ausführung

- Staging, Aktivierung und Rollback stellen Artefakte und Auswahlstatus bereit. Paket-/Kompatibilitätsprüfung erfolgt weiterhin beim Deployment; der ausführende Lade-/Readiness-Test erfolgt beim Kanalstart.
- Der Runtime-Orchestrator registriert den Bedarf, lädt die benötigten Versionen und prüft deren Bereitschaft vor der Freigabe des Kanals. Ein Ladefehler verhindert den erfolgreichen Start.
- Trigger-Pipelines lassen sich auch auf bestehenden Realtime-Kanälen starten. Ihr Executor hält die Bereitschaft; die Pipeline wird erst beim Trigger/Abruf ausgeführt.
- Trigger benötigen eine gestartete, bereite Kanal-Runtime, auch bei Testbildern und Snapshot-Quellen. Nach Wartezeiten wird dies vor Ausführung erneut geprüft.
- Das Gateway akzeptiert interne Inferenz nur mit gültiger Runtime-/Pipeline-Zuordnung und für deren gebundene Releases. Direkte Modellaufrufe und Batch-Job-Einreichungen ohne diesen Kontext werden abgelehnt. Vorhandene direkte Integrationen müssen den Kanal-Trigger verwenden.
- Einzelframe-Ausführung und Modell-Preview verwenden ebenfalls die gestartete Runtime und deren Modellbindungen. Vorschauen ohne Modellinferenz benötigen keine Modellaktivierung.
- Beim Stopp bleiben Modelle für laufende Trigger-/Gateway-Aufträge erhalten. PostgreSQL-Advisory-Locks schützen die Freigabe vor konkurrierenden Aufrufen; erst nach deren Abschluss wird der letzte Bedarf entfernt.
- Der bestehende Deployment-Controller gleicht den Modellbedarf regelmäßig ab und stellt ihn nach Dienstneustarts wieder her. Ungenutzte Modelle werden vor dem Laden anderer Modelle entladen.
- Lokale ONNX-/OpenVINO-Modelle werden beim Start auf Ladbarkeit geprüft. Ihre eigentlichen Gateway-Caches entstehen beim ersten zulässigen Inferenzaufruf; ein periodischer Abgleich entfernt ungenutzte Einträge.

Der Modellkatalog bleibt unabhängig vom Ladestatus auswählbar. Es entstehen keine neuen Dienste, Datenbanken oder Migrationen. Der vorhandene Windows-/WSL2-Pfad mit Triton im expliziten Kontrollmodus bleibt bestehen.

## Verifikation

Gezielte Unit-Tests decken Deployment ohne Laden, Startfehler, Trigger-Bereitschaft, Versionsauswahl, gemeinsame Nutzung, Entladen nach Stopp, auslaufende Aufträge, Gateway-Zugang und lokale Cache-Freigabe ab. Bestehende Gateway-Tests verwenden für zulässige interne Aufrufe eine gestartete Kanal-Runtime; öffentliche direkte Aufrufe prüfen die neue Ablehnung.

Frontend-Kanaltests, TypeScript-Prüfung und Vite-Build wurden lokal ausgeführt. Die OpenAPI-Schemas der beteiligten Ausführungsdienste bleiben unverändert; die internen Runtime-Header ergänzen die bestehende Kommunikation.

Die Anwendung wurde auf diesem Entwicklungsrechner nicht gestartet oder deployt. Auf dem Zielserver verbleiben die Prüfung der tatsächlichen GPU-Belegung mit mehreren Modellversionen, paralleler PostgreSQL-Aufrufe beim Stopp sowie Live-/Kamera- und Recovery-Tests. Die Unit-Tests ersetzen diese Hardware- und Mehrprozessprüfung nicht.
