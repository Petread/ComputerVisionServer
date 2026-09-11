# Runtime live diagnostics

`runtime_live_probe.py` polls the existing read-only runtime live-view endpoint and
records frame, overlay, active-stem, eligibility, curvature, phase, and solver state
transitions. It never starts, stops, or modifies a channel and does not download image
pixels.

The API key is accepted only through an environment variable so it cannot accidentally
be committed or appear in command-line help:

```powershell
$env:YBB_RUNTIME_API_KEY = '<one-time or short-lived runtime read key>'
python scripts/diagnostics/runtime_live_probe.py `
  --base-url https://server.example/runtime-api `
  --channel-id chn_example `
  --duration-seconds 180 `
  --interval-seconds 0.25 `
  --output .test-tmp/runtime-live-probe.json
```

Use `--insecure` only for a trusted plant LAN with a self-signed server certificate.
Revoke or rotate a key after it has been shared for an interactive diagnostic session.
