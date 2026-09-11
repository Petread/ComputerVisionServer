# ROI Capture Guide

ROI capture routes cropped runtime images into Label Studio projects for
downstream labeling tasks.

## Sources

ROI capture can use:

- `preprocessing.roi_extract` output
- detection-derived ROIs from `capture.roi_labelstudio` when it receives a
  frame plus detection payload

Each ROI intent carries:

- cropped frame bytes
- normalized box
- ROI key
- label and source region key when available
- score or other metadata from the source detection/region

## Delivery

Capture-service materializes each ROI image under runtime capture storage using
a temp file and atomic replace, then submits the stored image reference to Label
Studio. The Label Studio task metadata includes channel, capture rule, result
record, runtime execution, ROI box, source frame metadata, digest, and asset URI.

## Recommended Pipeline Shape

```text
Source -> Detection -> ROI Extract -> ROI Capture
```

For simpler detection-derived capture:

```text
Source -> Detection -> ROI Capture
```

In the second form, wire both `frame` and `detections` into
`capture.roi_labelstudio`.

## Multi-Project ROI Capture

Set multiple target projects on the capture node or matching capture rule. The
service creates one durable delivery row per ROI/project pair so a failed project
does not hide successful delivery to another project.
