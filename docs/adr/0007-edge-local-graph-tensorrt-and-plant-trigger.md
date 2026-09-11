# ADR 0007: Lokale Graphausführung, TensorRT und begrenzte Plant-API

- Status: Accepted
- Datum: 2026-07-22

## Kontext

M0–M5 konnten Workloads übertragen, Engines bauen, Quellen lesen und Ergebnisse synchronisieren,
führten den versionierten Pipeline-Graphen aber noch nicht produktiv auf dem Edge aus. Außerdem
war Port 9444 nur reserviert. Eine SPS benötigt eine kleine lokale Triggeroberfläche, darf jedoch
keinen Zugriff auf Fleet-, Deployment- oder Artefaktfunktionen erhalten.

## Entscheidung

Der Edge-Agent erhält einen eigenen, deterministischen Graph-Executor mit expliziter
Handler-Registry. Nur kompilierte und in der Registry freigegebene Node-Arten sind ausführbar.
`runtime_plugin` ist keine allgemeine Code-Loading-Erlaubnis. Capabilities sind die Schnittmenge
aus Gerätekonfiguration und wirklich vorhandener Registry.

Modelle laufen in lokalen TensorRT-Sessions. ONNX wird während `prepare` für das konkrete
Plattformprofil gebaut: Windows verwendet die mitgelieferte TensorRT-Python-Builder-API, Jetson
den im freigegebenen NVIDIA-Systemimage vorhandenen `trtexec`-Pfad. `trtexec` bleibt auf Windows
ein expliziter Altmodus, ist aber keine Voraussetzung des Windows-Bundles. Der Plan wird bei der
lokalen Admission deserialisiert und beim Workerstart erneut geladen. CUDA-Buffer und Streams
gehören der jeweiligen Session; gleichzeitige Ausführungen derselben Session werden serialisiert.
Ein fehlendes TensorRT/CUDA-Modul ist ein expliziter Fehler und aktiviert keinen CPU- oder
Simulationsfallback.

Port 9444 wird als separater FastAPI-Listener mit eigenem OpenAPI-Vertrag betrieben. Zugelassen
sind nur Health, idempotenter Kanaltrigger und das letzte Triggerergebnis. Der Listener bindet die
feste Management-IP, wertet keine Proxy-Header aus und erzwingt IP-Allowlist, Body-, Rate-,
Concurrency- und Timeoutgrenzen. VLAN, ACL und Host-Firewall bleiben zwingend.

Triggerresultate benutzen dieselbe durable `EdgeResultEnvelope`-Outbox wie kontinuierliche
Ausführungen. Ein Antworttimeout bricht einen bereits laufenden GPU-Auftrag nicht gewaltsam ab;
das Ergebnis wird persistiert und über denselben Idempotency-Key oder `last-result` wiedergefunden.

## Konsequenzen

- Kontinuierliche und getriggerte Kanäle haben denselben fachlichen Ausführungspfad.
- Der Server erhält echte Inferenzresultate ohne paralleles SPS-Sonderdatenmodell.
- Modell-Export-Manifeste werden zum verbindlichen Vertrag für Preprocessing, IO und
  Postprocessing.
- Neue Nodes erfordern eine geprüfte Registry-Erweiterung und ein neues Offline-Bundle.
- TensorRT-Engines bleiben plattformgebunden und werden nicht zwischen unterschiedlichen
  GPU-/TensorRT-Profilen übertragen.
- Die Plant-API ersetzt keine Netzwerksegmentierung und ist wegen des vereinbarten
  Fixed-IP-Vertrauensmodells nicht kryptografisch authentisiert.

## Alternativen

Ein vollständiger zweiter Verwaltungsagent auf 9444 wurde verworfen, weil er die Angriffs- und
Fehlbedienungsfläche unnötig vergrößert. Eine ausgehende Edge-Verbindung oder Cloud-Steuerung
widerspricht dem Anlagen-VLAN-Modell. Freies Python-Plugin-Loading wurde wegen Supply-Chain- und
Prozessisolationsrisiken nicht in E1 aufgenommen.
