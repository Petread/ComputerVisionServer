# Object Spatial Signal Node

`analysis.object_spatial_signal` emits a boolean runtime signal when detected
or segmented objects satisfy spatial rules. It remains the generic compatibility
node. New operator-facing pipelines should normally use:

- `analysis.zone_detection` for zone membership and per-zone class counts.
- `analysis.close_detection` for distance, overlap, and full-cover checks.

Both nodes consume detections, segmentation outputs, or compatible CV region
sets and emit normal `result` payloads that can connect to `output.result`.
`analysis.zone_detection` also passes an enriched object set downstream so a
linear pipeline can do zone detection before close/overlap detection.

## Supported Relations

- `distance`: two objects are within or outside a frame-relative distance.
- `approaching`: the distance between a matched object pair decreases across
  consecutive runtime executions.
- `overlap`: object boxes overlap; segmentation polygons are used when both
  objects provide polygon geometry.
- `zone_occupancy`: a configured number of objects is inside or touching an
  operator-drawn field.

The node accepts detections, segmentation outputs, or compatible region sets on
`primary_objects`. `secondary_objects` is optional. If no secondary input is
connected, the node compares pairs from the primary object set.

`analysis.zone_detection` accepts objects on `objects`, stores zone membership
in the forwarded object metadata, and supports `center_inside`, `box_overlaps`,
and `fully_inside` matching. Configure multiple `zone_occupancy` rules with
`match_policy=all` to require combinations such as one `LKW` and one `Lader`
in `Zone A`.

When a zone requirement uses `box_overlaps`, `min_overlap_percent` means the
minimum percentage of the object bounding box that must lie inside the zone.
This lets operators distinguish a small touch from a meaningful zone entry.

`analysis.close_detection` accepts objects on `objects` and optional
`secondary_objects`. Use `overlap` with `min_overlap_percent=100` when the
smaller object must be fully covered, or `distance` when objects may be close
without touching.

## Operator Configuration

Operators configure fields with the same live-frame editor used by zones and
ROIs. They draw rectangles directly on the stream image; stored coordinates are
normalized to the frame and are not absolute camera pixels.

Spatial rule thresholds are frame-relative percentages:

- distance thresholds are percentages of the frame diagonal
- overlap thresholds are percentages of the smaller object footprint
- zone-overlap thresholds are percentages of the object footprint inside the
  zone
- approaching thresholds use percentage-point closing distance per frame

Class filters are optional. Empty class filters mean "any class". A rule can
require same-label pairs, different-label pairs, or any pair.

## Result Contract

The `signal` output is a `ResultPayload`:

```json
{
  "signal": true,
  "active": true,
  "signal_key": "spatial_signal",
  "match_policy": "any",
  "match_count": 1,
  "rules": [
    {
      "rule_key": "person_forklift_approach",
      "relation": "approaching",
      "active": true,
      "match_count": 1
    }
  ],
  "matches": [
    {
      "relation": "approaching",
      "distance_percent": 7.1,
      "previous_distance_percent": 35.4,
      "closing_percent": 28.3
    }
  ]
}
```

The payload `decision` is the same boolean as `signal`. The node also emits an
`overlay` output with object boxes, pair-distance lines, and configured fields
so draft preview and live view can show why the signal fired.

## Runtime State

`approaching` uses the node runtime state for the channel/runtime execution. It
matches object pairs between frames by labels and nearest pair centers, so it
does not require a tracker. Future tracking nodes can improve this by adding
stable cross-frame `track_id` values to object references.

## Notes

- Segmentation overlap uses polygon geometry when available; otherwise the
  node falls back to bounding boxes.
- Field occupancy supports center-inside, box-overlap, and fully-inside modes.
- The node stays in the runtime plane. It does not call the inference plane and
  does not change model outputs.
