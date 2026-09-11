# Go-Live Checklist

This is the canonical release gate before declaring the workstation ready for normal operator use.

## 1. Secrets And Config

- `config/prod/platform.yaml` validates with the intended production overlays.
- `/srv/cv-platform` exists and all production roots resolve underneath it.
- Mounted secret files exist for database password, session secret, bootstrap admin password hash, Label Studio token, webhook secret when enabled, bootstrap API keys, and TLS material.
- `service.public_base_url` uses `https`.
- production cookies are secure.

## 2. Service Bring-Up

- infrastructure services are healthy
- control-plane services are healthy
- both inference gateways are healthy
- all worker heartbeats are current
- Prometheus can scrape the expected targets

## 3. Security Gate

- admin login works only with valid credentials
- mutating admin requests require valid CSRF
- realtime inference rejects anonymous access
- batch submission rejects anonymous access
- internal batch execution accepts only the scoped internal credential
- blocked internal paths stay inaccessible through the reverse proxy

## 4. Workflow Gate

- dataset creation works
- browser upload session completes and materializes assets
- Label Studio project sync or mapping works
- snapshot creation reaches `ready`
- training job reaches background execution and completes
- trained model version records lineage and immutable manifests
- deployment package build succeeds
- staged deployment passes smoke validation
- production promotion switches only after a passing smoke test
- realtime testing works against the active release
- batch inference completes and publishes a results bundle

## 5. Reliability Gate

- duplicate webhook delivery does not duplicate irreversible side effects
- training worker interruption resumes safely from checkpoint or fails safely with the same job identity
- admin UI restart does not stop active training
- failed deployment leaves the current production release active
- rollback restores the previous immutable release without rebuild

## 6. Evidence Pack

- latest backend test results
- latest admin-web typecheck/build result
- staged smoke-test report
- rollback drill evidence
- screenshots or exported evidence from the health dashboard
- updated acceptance report

## 7. Sign-Off

- ML engineer sign-off recorded
- platform/operator sign-off recorded
- node declared live only after every section above is complete
