# Published And Runnable

For runtime pipelines, `published` is only meaningful when the version is also
actually executable.

## A Pipeline Version Is Runnable When

- every node type resolves from the node registry
- every handler loads successfully
- the graph is acyclic
- every required input port is connected once
- required runtime properties are present and well-formed
- inference nodes point to valid deployed model capabilities
- at least one output or capture terminal exists
- the graph can be built into an `ExecutablePipelineGraph`

## Operator Meaning

In the UI, a version should be treated as runnable only when:

- validation reports zero errors
- model bindings are chosen through the deployed-model picker
- no node still shows an unbound required model placeholder

## Templates

Template pipelines may intentionally contain unbound model placeholders such as
`required-detection-model`. Those drafts are starting points, not runnable
deployments. Operators must bind real deployed capabilities before publishing a
channel-ready version.

## Channel Start Behavior

Starting a channel now requires a resolvable published pipeline version.
Channels with missing or unpublished runtime pipelines are rejected before the
runtime is marked as starting.
