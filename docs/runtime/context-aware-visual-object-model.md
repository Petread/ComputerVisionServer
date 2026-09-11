# Context-Aware Visual/Object Runtime Model

## Purpose

The runtime live view and pipeline engine now treat image state, object identity,
ROI provenance, enrichment, and display selection as separate concepts. This
replaces the older mental model of rendering overlays on whichever frame was
latest.

## Core Model

`Image Context`

An image context is a concrete visual state: original frame, resize, crop, ROI,
masked image, or processed image. Transforming a frame creates a new image
context with a parent/source context link and a coordinate projection policy.

`Object Reference`

Detection and segmentation produce object references. Object ids are stable
within the current frame/execution and carry source image context, geometry,
label, score, producer node, and provenance. Future tracking can add track ids.

`Provenance`

ROI and crop paths retain parent image context, source image context, source
object id, transform metadata, and projection policy. A downstream node can
therefore prove which original object caused an ROI result.

`Enrichment`

Per-object downstream outputs, such as ROI classification labels, are modeled
as object enrichments. They target a source object id and record the derived ROI
image context that produced the result.

`Display Output`

`output.display_output` selects the image context for live-view rendering by
receiving a frame input. Operators wire the node visually instead of typing
internal context ids.

## Default Rendering

Simple pipelines with one clear visual path continue to work without an
explicit Display output. The live view uses the latest image context on that
path.

Pipelines with multiple visual branches must add `output.display_output`.
Validation blocks ambiguous multi-branch display behavior.

Overlays render only on their own image context unless projection through image
context lineage is known to be valid.

## ROI Fan-Out Readiness

The model is ready for a future ROI Fan-Out node:

```text
Input Frame -> Detection -> ROI Fan-Out -> Classification per ROI
            -> Merge/Enrich -> Overlay -> Display Output
```

The future node should consume a frame and object set, produce one ROI image
context per object, preserve source object links, and allow downstream
classification or segmentation to enrich the source objects.
