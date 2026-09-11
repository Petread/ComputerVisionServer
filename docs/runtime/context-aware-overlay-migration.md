# Migration Note: Context-Aware Overlay Rendering

## What Changed

The runtime no longer treats live rendering as `latest image + overlay`.
Pipeline execution now carries:

- image contexts
- object references
- ROI provenance
- object enrichments
- display output selection
- context-bound overlay primitives

Existing simple pipelines still render safely when they have one clear visual
path. The live view uses the latest image context on that path.

## Required Pipeline Changes

Pipelines that branch visually must add `output.display_output`.

Examples that need Display output:

- original frame detection plus crop/ROI branch
- parallel crop and resize branches
- ROI-derived classification rendered back with detection boxes
- any pipeline where operators could reasonably ask which image should be shown

The Display output node is configured by wiring its frame input to the intended
live-view frame. Operators do not type image context ids.

## Overlay Behavior

Overlay primitives carry image context and object references. A primitive is
rendered on the selected display context only when it belongs to that context or
has a valid projection relationship.

If projection is not known to be safe, the primitive is not silently rendered on
the wrong image.

## ROI Workflows

ROI capture remains supported. Future ROI Fan-Out will build on the same
contracts by producing one ROI image context per object and preserving source
object provenance for downstream enrichment.
