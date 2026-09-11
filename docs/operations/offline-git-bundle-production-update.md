# Offline Repo Update And Docker Rebuild

Diese Anleitung beschreibt, wie der Zielrechner ohne GitHub aus dem Entwicklungsrechner aktualisiert wird und wie danach der Docker-Production-Stack neu gebaut oder neu gestartet wird.

Fuer den browserbasierten Update-Plan mit Worker-Uebersicht siehe auch [Offline Update Control Plane](./offline-update-control-plane.md).

## Ausgangslage

- Entwicklungs-PC: Windows, Repo unter `C:\KI\YBB_Inferenzserver`
- Zielrechner: Windows mit WSL2/Ubuntu und Docker Desktop
- Zielrechner-Repo: `C:\KI\YBB_Inferenzserver`, in WSL erreichbar unter `/mnt/c/KI/YBB_Inferenzserver`
- Production-Compose: `infra/compose/compose.prod.yaml`
- Persistente Daten: `/srv/cv-platform`

Die persistenten Daten werden nicht ueber Git aktualisiert. Sie bleiben auf dem Zielrechner erhalten.

## Update-Bundle Am Entwicklungs-PC Erstellen

Auf dem Entwicklungs-PC in PowerShell:

```powershell
cd C:\KI\YBB_Inferenzserver

git status
git add <geaenderte-dateien-oder-verzeichnisse>
git commit -m "Update production deployment"

git bundle create YBB_Inferenzserver-update.bundle --all
git bundle verify YBB_Inferenzserver-update.bundle
```

Die Datei `YBB_Inferenzserver-update.bundle` danach auf den Zielrechner kopieren, zum Beispiel nach:

```text
C:\KI\YBB_Inferenzserver-update.bundle
```

Nicht versehentlich lokale Runtime-Dateien committen. Vor dem Commit sollte `git status` keine Secrets, keine `.env`-Dateien und keine alten Bundle-Dateien wie `*.bundle` in der Staging-Area zeigen.

## Zielrechner Vor Dem Update Pruefen

Auf dem Zielrechner in WSL:

```bash
cd /mnt/c/KI/YBB_Inferenzserver

git status
git branch
```

Wenn `git status` lokale Aenderungen zeigt, zuerst pruefen, ob diese noch gebraucht werden. Lokale Runtime-Dateien wie `infra/compose/.env` sollten nicht committed sein.

Wenn lokale Aenderungen bewusst auf dem Zielrechner entstanden sind und erhalten bleiben sollen:

```bash
git stash push -m "target-local changes before bundle update"
```

## Bundle Auf Dem Zielrechner Einspielen

Auf dem Zielrechner in WSL:

```bash
cd /mnt/c/KI/YBB_Inferenzserver

git remote add localbundle /mnt/c/KI/YBB_Inferenzserver-update.bundle 2>/dev/null || \
  git remote set-url localbundle /mnt/c/KI/YBB_Inferenzserver-update.bundle

git fetch localbundle
git merge localbundle/main
```

Falls der Hauptbranch nicht `main` heisst:

```bash
git branch -r
```

Dann den passenden Branch mergen, zum Beispiel:

```bash
git merge localbundle/master
```

Wenn vorher ein Stash erstellt wurde und die lokalen Aenderungen wieder gebraucht werden:

```bash
git stash pop
```

Bei Konflikten die betroffenen Dateien manuell aufloesen und danach:

```bash
git add <datei>
git commit
```

## Offline-Preflight Vor Dem Docker Update

Wenn der Zielrechner keine Internetverbindung hat, vor dem Docker-Schritt immer zuerst pruefen, ob ein Rebuild oder Pull noetig waere.

### Aenderungen Aus Dem Bundle Vor Dem Merge Pruefen

Nach `git fetch localbundle`, aber vor `git merge`:

```bash
git diff --name-only HEAD..localbundle/main
```

Falls der Hauptbranch nicht `main` heisst:

```bash
git branch -r
git diff --name-only HEAD..localbundle/master
```

Faustregel:

- Nur `docs/` geaendert: kein Docker-Schritt noetig.
- Nur `config/`, `infra/reverse-proxy/`, `infra/redis/`, `infra/postgres/init/` oder Compose-Umgebungswerte geaendert: meist nur betroffene Container neu starten, kein Build.
- `frontend/`, `backend/`, `contracts/`, `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, `infra/docker/` oder `infra/compose/compose.prod.yaml` geaendert: ein Image-Rebuild kann noetig sein.
- `backend/migrations/` geaendert: Migrationen ausfuehren.

### Lokale Images Pruefen

Alle in Compose referenzierten externen Images auflisten:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml config --images
```

Pruefen, ob diese Images lokal vorhanden sind:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml config --images \
  | sort -u \
  | while read image; do
      docker image inspect "$image" >/dev/null 2>&1 \
        && echo "OK     $image" \
        || echo "FEHLT  $image";
    done
```

Wenn ein Image mit `FEHLT` angezeigt wird, kann der Zielrechner es offline nicht starten, solange es nicht vorher als Docker-Image-Tar vom Entwicklungs-PC uebertragen wurde.

Die aktuell lokal gebauten Compose-Images pruefen:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml images
```

### Trockenen Compose-Lauf Pruefen

Docker Compose kann den geplanten Ablauf anzeigen, ohne Container zu aendern:

```bash
docker compose --dry-run --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --no-build --pull never
```

Wichtig:

- `--no-build` verhindert automatische Builds.
- `--pull never` verhindert Pulls aus Docker Hub/NVIDIA/Grafana/etc.
- Wenn dieser Dry-Run wegen fehlender Images scheitert, wuerde der echte Offline-Start ebenfalls scheitern.

## Docker Stack Neu Bauen

Nach einem Code-Update auf dem Zielrechner in WSL:

```bash
cd /mnt/c/KI/YBB_Inferenzserver
```

### Vollstaendiger Rebuild

Wenn unklar ist, welche Teile geaendert wurden, oder wenn Backend, Frontend, Dockerfiles oder Compose-Dateien betroffen sind:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml build
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --remove-orphans
```

Auf einem Offline-Zielrechner diesen Schritt nur ausfuehren, wenn alle benoetigten Base-Images und Paketquellen bereits im Docker-Build-Cache vorhanden sind. Wenn das nicht sicher ist, zuerst den Offline-Preflight oben ausfuehren oder die Images auf dem Entwicklungs-PC bauen und als Docker-Image-Tar uebertragen.

### Nur Frontend

Wenn nur das Admin-Frontend geaendert wurde:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml build admin-web
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --no-deps admin-web
```

### Nur Python-Backend

Wenn Python-Services, Worker oder gemeinsame Backend-Packages geaendert wurden:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml build admin-api dataset-service training-orchestrator model-registry-service deployment-controller runtime-admin-api channel-service pipeline-definition-service runtime-orchestrator realtime-executor trigger-executor overlay-service capture-service result-service connector-service webhook-receiver control-worker batch-worker maintenance-worker label-studio-ml-adapter

docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --remove-orphans
```

### Nur Einzelne Infrastruktur-Services

Wenn nur ein einzelner Service betroffen ist:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml build <service>
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --no-deps <service>
```

Beispiele:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml build mlflow
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --no-deps mlflow
```

## Datenbankmigrationen

Wenn Dateien unter `backend/migrations/` geaendert wurden, Migrationen vor oder zusammen mit dem Neustart ausfuehren:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml run --rm migrations
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml up -d --remove-orphans
```

## Status Pruefen

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml ps
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml logs --tail=100
```

Gezielt fuer einzelne Services:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml logs --tail=120 admin-api admin-web reverse-proxy
```

Health-Check:

```bash
curl -k https://127.0.0.1/health/ready
```

## Wichtige Hinweise

Nicht verwenden, ausser Daten sollen bewusst geloescht werden:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.prod.yaml down -v
```

`down -v` loescht Docker-Volumes. Die Plattform nutzt zwar hauptsaechlich `/srv/cv-platform`, aber produktive Loeschbefehle sollten immer gezielt und bewusst ausgefuehrt werden.

Diese Verzeichnisse auf dem Zielrechner bleiben erhalten und werden nicht durch das Git-Bundle ersetzt:

```text
/srv/cv-platform/postgres
/srv/cv-platform/labelstudio
/srv/cv-platform/mlflow
/srv/cv-platform/grafana
/srv/cv-platform/prometheus
/srv/cv-platform/secrets
/srv/cv-platform/training
/srv/cv-platform/serving
```

Runtime-Konfiguration auf dem Zielrechner, zum Beispiel `infra/compose/.env`, sollte nicht in Git committed werden. Sie enthaelt zielgeraetespezifische Werte wie IP-Adresse, Hostname und Label-Studio-Origin.
