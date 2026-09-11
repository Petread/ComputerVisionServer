# Beispiel-Konfiguration fuer eine Anwendung.
# Kopieren Sie diese Datei z.B. nach app.config.ps1 und passen Sie die Werte an.

# Eindeutiger Name der Anwendung. Wird fuer Verzeichnisse, Containername und .env genutzt.
$AppName = "myapp"

# Version des Release-Artefakts und Docker-Image-Tags. Verwenden Sie niemals "latest".
$AppVersion = "1.2.3"

# Name des Docker Images ohne Versions-Tag. Das Build-Skript taggt daraus <Name>:<Version>.
$DockerImageName = "myapp"

# Pfad zum Dockerfile, relativ zum Verzeichnis, aus dem build-release.ps1 gestartet wird.
$Dockerfile = ".\Dockerfile"

# Docker Build Context, relativ zum Verzeichnis, aus dem build-release.ps1 gestartet wird.
$DockerContext = "."

# Zielplattform fuer Linux-Container. Fuer typische Windows-Server mit x86_64 CPU: linux/amd64.
$DockerPlatform = "linux/amd64"

# Produktive Compose-Datei ohne build:-Abschnitt. Sie wird im Bundle als compose.yaml abgelegt.
$ComposeFile = ".\compose.prod.yaml"

# Basisverzeichnis auf dem Windows-Server. Darunter entsteht <ServerBaseDir>\<AppName>.
$ServerBaseDir = "C:\DockerApps"

# Port auf dem Server/Host, der nach aussen erreichbar sein soll.
$HostPort = "8080"

# Port im Container, auf den der Host-Port gemappt wird.
$ContainerPort = "8080"
