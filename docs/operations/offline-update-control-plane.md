# Offline Update Control Plane

Diese Seite beschreibt das browserbasierte Update-Modell fuer den Docker-Production-Stack.

## Ziel

Der Zielrechner kann offline bleiben. Code- oder Konfigurationsaenderungen werden zuerst in das lokale Repo eingespielt. Danach zeigt die Admin-Oberflaeche unter `Operations -> Workers & Updates`, welche Dateien geaendert sind, welche Dienste betroffen sind und ob ein sicherer Hot-Apply moeglich ist.

## Sicherheitsmodell

- Die Admin-API nimmt nur Bedienhandlungen aus dem Browser entgegen.
- Der interne `operations-agent` hat als einziger Dienst Zugriff auf den Docker Socket.
- Der Agent kopiert nur Dateien aus dem gemounteten Repo in laufende Container und startet nur die geplanten Services neu.
- Aenderungen an Dependencies, Dockerfiles, Compose-Dateien, geloeschten Dateien oder Datenbankmigrationen werden geblockt und nicht automatisch angewendet.
- Mutierende Update-Aktionen schreiben Audit Events.

## Ablauf

1. Update-Bundle oder Git-Bundle auf dem Zielrechner einspielen.
2. `Operations -> Workers & Updates` oeffnen.
3. Den Update-Plan pruefen:
   - Pending files
   - betroffene Services
   - geplante Restarts
   - Blocker
4. Wenn kein Blocker vorhanden ist, `Update anwenden` ausfuehren.
5. Die Seite prueft danach Worker-Heartbeats, Queues und Service-Readiness.

Wenn ein System zum ersten Mal ueber diese Seite verwaltet wird, einmal `Aktuellen Stand als Baseline speichern` ausfuehren. Dadurch merkt sich der Agent den aktuell laufenden Git-Stand als bekannte Basis.

## Wann Nicht Automatisch Anwenden

Der Agent blockiert bewusst bei:

- `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`
- `infra/docker/*`
- `infra/compose/compose.prod.yaml`
- `backend/migrations/*`
- geloeschten oder umbenannten Runtime-Dateien

Diese Faelle brauchen ein vorbereitetes Image, eine Migration oder ein geplantes Wartungsfenster.
