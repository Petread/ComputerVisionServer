# Write to Siemens SPS Runtime Node

`output.siemens_sps_write` writes selected runtime result values to Siemens
S7/SPS DB addresses during pipeline execution.

## Library And PLC Assumptions

The runtime uses the open-source `python-snap7` package (`python-snap7>=3,<4`)
as the Siemens S7 communication implementation. It targets Siemens S7-compatible
TCP communication on industrial LANs. Default port is `102`; rack and slot are
operator-configured and are not hardcoded to one PLC family.

Reference material:

- PyPI: <https://pypi.org/project/python-snap7/>
- python-snap7 client API: <https://python-snap7.readthedocs.io/en/3.0.0/API/client.html>
- Snap7 API reference: <https://snap7.sourceforge.net/snap7_api_ref.html>

If `python-snap7` is not importable in the service runtime, the built-in node
type is deactivated in the runtime node registry and cannot be published.

Initial security scope is plain S7 communication on a trusted industrial LAN.
No credentials, TLS, or password storage are implemented for this node. The PLC
must allow the configured client access and DB writes. For S7-1200/S7-1500
installations, PLC-side protection, external S7/PUT-GET style access, and DB
layout/optimized-block settings must be configured by the controls engineer.

## Supported Targets

v1 supports DB area writes only:

- `BOOL`: `DB101.DBX8.0`
- `BYTE`: `DB101.DBB12`
- `WORD` or `INT`: `DB101.DBW0`
- `DWORD`, `DINT`, or `REAL`: `DB101.DBD10`

`STRING`, M/I/Q areas, and symbolic TIA tags are not exposed until fully
implemented and validated.

## Mapping Examples

The editor shows operator-facing aliases such as `classification.top1.label`.
Existing native result fields such as `top_label.label` remain accepted for
stored pipeline configs.

Classification decision:

- source field `classification.top1.label`
- enum mapping `gut -> 1`, `schlecht -> 0`
- target `DB101.DBW0`, datatype `INT`

Classification confidence:

- source field `classification.top1.score`
- scale multiplier `100`, rounding `nearest`, clamp `0..100`
- target `DB101.DBW10`, datatype `INT`

Measurement:

- source field `measurement.distance_mm`
- direct or scaled numeric conversion
- target `DB101.DBD20`, datatype `REAL`

Quality decision:

- source field `result` or `decision`
- boolean conversion
- target `DB101.DBX8.0`, datatype `BOOL`

## Safety Behavior

- Node default is `enabled=false` and `dry_run=true`.
- Each mapping row has its own enabled flag.
- Editor preview, static validation, publish validation, and docs examples do
  not write to PLC hardware.
- Runtime writes require valid node config, valid source resolution, valid type
  conversion, `dry_run=false`, and the runtime PLC write allow gate. Operators
  can inspect and change the deployment gate in `Operations -> Access &
  Security`; the underlying env key is
  `CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=true`.
- Unknown enum values, missing source fields, and range/type failures do not
  write silently. Configure fallback behavior explicitly to skip or write a
  default value.
- Realtime writes require throttling. Trigger and interval executions use
  bounded synchronous writes.
- The runtime closes failed Snap7 clients before reconnecting. `per_runtime`
  reuse keeps at most one cached client per PLC endpoint in the active pipeline
  state, and Live Runtime treats this node as stateful so frames are not
  processed concurrently against the same PLC connection cache.

Writes are ordered sequentially and report per-mapping status. There is no
transaction across multiple DB addresses; partial failures are visible in the
runtime execution result under `plc_write_outputs`.

## Offline Deployment

The Windows/WSL offline installer and updater carry `runtime/python-wheels.tar.gz`.
If the archive is not supplied through `PythonWheelBundlePath` or the local
cache, the export scripts download the Linux wheel set for `python-snap7>=3,<4`
on the development machine with `pip download` and bundle it with
`requirements.txt`. `apply-release.sh` installs the bundle into the service
runtime venv with `pip --no-index` before services restart.

Offline export keeps PLC hardware writes blocked unless `PlcWritesAllowed` is
explicitly enabled in the export config or the deployment UI checkbox
`PLC-Schreiben erlauben` is selected. The access page can update the target
runtime env later when the admin service can write
`/etc/ybb-inferenzserver/runtime.env`; a runtime service restart is still
required before running executors observe the changed allow gate.
