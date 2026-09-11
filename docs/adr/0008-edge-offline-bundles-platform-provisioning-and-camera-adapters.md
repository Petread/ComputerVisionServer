# ADR 0008: Offline-Bundles, Plattformprovisionierung und Kameraadapter

- Status: Accepted
- Datum: 2026-07-22
- Entscheider: YBB Plattformteam

## Kontext

Die Edge-Geräte besitzen im Anlagenbetrieb keinen Internetzugang. Bisherige Installer setzten ein
manuell zusammengestelltes Wheelhouse und bereits vorbereitete Betriebssysteme voraus. Außerdem
meldete der Agent überwiegend konfigurierte Capability-Werte und besaß nur einen generischen
OpenCV-Quellenpfad. Für Orin Nano, AGX Orin und Windows 11 werden reproduzierbare Releases,
nachweisbare Plattformstände und freigegebene Kamera-SDKs benötigt.

## Entscheidung

1. Ein Release-Bundle wird ausschließlich aus lokalen Inputs und einem versionierten
   Plattformprofil gebaut. Der Builder führt keine Downloads aus.
2. Der kanonische Build-Kern ist eine deterministische Python-CLI, damit derselbe Algorithmus in
   CI und GUI läuft. Die Bedienoberfläche ist eine kompilierte native C#-WinForms-Anwendung; sie
   ist kein PowerShell-Formular und kein Skript-Launcher.
3. Wheels werden normalisiert, Archive sortiert und mit `SOURCE_DATE_EPOCH` erzeugt. Das Bundle
   enthält einen Wheel-Lock, Release-Manifest und verpflichtende `SHA256SUMS`.
4. Die zentrale Release-Signatur verwendet eine abgesetzte RSA-SHA-256-Signatur über die
   verpflichtende kanonische Prüfsummenliste. Der Public Key wird außerhalb des Bundles im
   Golden Image gepinnt; Signaturschlüssel verbleiben auf Buildserver beziehungsweise CI. Das
   feste-IP-Gerätevertrauen bleibt davon unberührt.
5. Signaturen besitzen die Policies `prepared`, `signed` und `required`. Checksummen sind immer
   Pflicht. Bei `signed` und `required` verweigern Installer bei fehlender oder ungültiger
   Signatur die Installation.
6. Golden-Image-Manifeste fixieren OS-, NVIDIA-, Python- und Storage-Basis. Provisioner
   konfigurieren Hostname, statische IP, Zeit, Benutzer, Verzeichnisse und Leistungsprofil
   idempotent. Sie formatieren oder partitionieren absichtlich keinen Datenträger.
7. Der Agent probt produktiv OS, L4T/JetPack, CUDA, TensorRT, DeepStream, GPU, Treiber, RAM,
   Storage sowie installierte Kamera-Provider. Nicht ermittelbare Werte werden als Warnung und
   niemals als erfolgreich probter Wunschwert ausgegeben.
8. Freigegebene Adapter sind OpenCV RTSP/USB/File, GStreamer, Aravis GigE Vision, Basler Pylon
   und FLIR Spinnaker. Vendor-SDKs werden lazy geladen. Ein nicht freigegebener Adapter wird
   bereits beim lokalen Deployment-Prepare abgelehnt.
9. Quellenfehler werden vom bestehenden Supervisor mit begrenztem exponentiellem Restart
   behandelt. Der Plattformadapter hält Auswahl, Timeout, Trigger und Kameraeigenschaften in
   sicheren Grenzen.
10. Die Windows-Erstinstallation besitzt eine kompilierte native WinForms-Oberfläche. Jetson Orin
    Nano und AGX Orin besitzen eine lokale Tk-Oberfläche mit `whiptail`-/Terminal-Fallback. Beide
    erzeugen die Gerätekonfiguration intern und delegieren an die vorhandenen Provisionierungs- und
    Offline-Installationsskripte; sie bilden keinen zweiten Installationspfad.
11. Eine UI-generierte Gerätekonfiguration liegt außerhalb der signierten Bundle-Dateien, wird
    aber vor Verwendung gegen eine feste Key-Allowlist und das signierte Plattformprofil
    kanonisiert. Unbekannte Variablen, Plattformabweichungen und ungültige Netzwerkwerte werden
    abgelehnt. Benutzer müssen keine `device.env` materialisieren oder bearbeiten.

## Konsequenzen

- Gleiche Inputs erzeugen byte-identische Release-Archive.
- Internetzugang ist weder zum Bauen aus einem vorbereiteten Wheelhouse noch zur Installation
  erforderlich.
- Windows-Bundles sind mehrere Gigabyte groß, weil sie zusätzlich zur vollständigen relocatable
  Python-3.12-Runtime mit `venv`/`pip` und WinSW auch den vollständigen lokalen CUDA-Installer und
  die native TensorRT-Laufzeit enthalten. Das reduzierte offizielle Embeddable-Paket genügt
  dafür nicht.
- Jetson-Images müssen NVIDIA-Systempakete bereits enthalten; sie werden nicht als Python-Wheel
  repliziert.
- Das gleiche Jetson-Installer-UI bedient Orin Nano und AGX Orin anhand des signierten
  Plattformprofils; die Windows-Installer-EXE wird nur in Windows-Bundles aufgenommen.
- Golden Images und Kameraadapter sind softwareseitig definiert, benötigen aber je freigegebener
  Hardware-/Treiberkombination eine HIL-Abnahme.
- Ein `prepared` Bundle schützt durch SHA-256 gegen Übertragungsfehler, nicht gegen einen
  kompromittierten Buildkanal. Produktionsreleases sollen auf `required` umgestellt werden.

## Verworfen

- PowerShell-GUI: erfüllt die Vorgabe einer nativen Windows-Anwendung nicht.
- Downloads während der Installation: widersprechen dem isolierten Maschinen-VLAN.
- Automatisches NVMe-Formatieren: zu hohes Risiko bei falscher Geräteauswahl.
- Konfigurierte Capability-Werte als Hardwarewahrheit: Drift bliebe unentdeckt.
- Ein universeller Kameraadapter: Vendor-Trigger, SDK-Lifecycle und Pixelkonvertierung sind nicht
  ausreichend kompatibel.
