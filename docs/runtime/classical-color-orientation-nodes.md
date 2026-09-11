# Classical Color Orientation Nodes

These nodes solve deterministic, non-AI inspection cases where objects are
visually separated by color and their pose matters.

## Color Object Segmentation

`computer_vision.color_object_segmentation` consumes a `frame` and emits:

- `objects`: a `region_set` with normalized boxes, contours, oriented boxes,
  major/minor axes, aspect ratio, area, and orientation angle
- `counts`: per-label and total object counts as `measurements`
- `overlay`: contour, box, and long-axis primitives
- `mask`: a processed binary mask frame for debugging

Operators configure one or more HSV color profiles. Each enabled profile
becomes the object label. Processing can be limited to `zone_list` fields drawn
on the live image, so camera-specific inspection areas do not require pixel
coordinates.

Professional runtime workflow:

1. Calibrate one HSV profile per object type from a representative live frame.
2. Keep profile ranges mutually meaningful; the runtime assigns pixels
   exclusively to the first matching enabled profile by default to avoid
   duplicate objects when ranges overlap.
3. Use inspection fields to remove conveyor/background clutter before contour
   extraction.
4. Tune minimum/maximum area and minimum aspect ratio per object type before
   downstream rules are added.
5. Keep `split_touching_objects` enabled for compact parts that may touch. The
   node uses distance-transform watershed inside each color blob and skips
   strongly elongated components to avoid over-splitting boards.
6. Feed `objects` into per-object analysis nodes such as orientation, spatial
   signal, ROI extraction, or capture. Feed `counts` or `objects` directly into
   Result Output when the business result is object count by type.
7. For live visualization, connect the node's `overlay` output to Overlay
   Output. The Overlay Output passes through `color_object_box`,
   `color_object_contour`, and `color_object_axis` primitives so each accepted
   object can be inspected visually.

Each region carries a stable per-execution object reference in metadata,
including profile, geometry, contour/split provenance, normalized contour
points, oriented box, axis, area, solidity, and fill ratio. This is the
contract downstream analysis uses to inspect objects individually.

## Object Orientation Signal

`analysis.object_orientation_signal` consumes the color `region_set` or any
compatible region source. It evaluates only selected labels and fields.

It can compare each object against:

- a reference direction drawn on the live frame
- a configured angle
- the longitudinal or transverse axis of an `InspectionCoordinateFrame`

The node flags violations for angle deviation, low aspect ratio, too-short
major axis, too-wide minor axis, and object count outside configured limits.
The output is a normal `result` payload, so it can feed persisted results,
HTTP responses, conditional capture, internal events, and connectors.

For conveyor-board cases, the color node detects boards; the orientation node
flags skewed boards or merged/crossed contours through angle and shape rules.
If crossed objects remain separated, `analysis.object_spatial_signal` can also
check overlap and distance from the same color-generated object set.
