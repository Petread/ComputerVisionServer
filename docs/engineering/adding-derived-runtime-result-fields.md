# Adding Derived Runtime Result Fields

Derived result fields should come from runtime analysis or output nodes, not
from UI-only formatting.

## Rules

1. Add or update a runtime node handler that emits `ResultPayload`,
   `MeasurementsPayload`, `PositionsPayload`, or `EventsPayload`.
2. Keep field names stable, lowercase, and explicit. Prefer `count`,
   `direction`, `segment_area`, or domain-specific names over display labels.
3. Preserve raw and normalized inference outputs. Do not overwrite model output
   payloads to create business fields.
4. Put final trigger fields in `output.http_trigger_response` and persisted
   operator fields in `output.persisted_result`.
5. Add tests at the engine level and result-service query/detail level.
6. Document operator meaning when a field drives a production decision.

## Compatibility

Existing records are immutable. If a derived field changes meaning, introduce a
new field name or schema version and keep the old field readable for history.

## UI Expectations

The Result History page reads:

- `trigger_response` for final response fields
- `derived_preview` for compact calculated values
- `derived_outputs` for detailed calculated payloads

Do not add page-specific parsing of the legacy `result` envelope unless it is a
temporary migration fallback.
