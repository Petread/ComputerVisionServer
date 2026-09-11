# Adding A New Runtime Node Type

This guide describes the smallest safe path for adding a new runtime node type.

## 1. Register The Node Contract

Add the node to `cv_runtime.registry` with:

- `node_type`
- category
- supported modes
- input ports
- output ports
- default config
- property schema
- `runtime_handler_ref`
- compatibility constraints

Keep the contract narrow and typed. Prefer existing property field types before
adding a new one.

For normal operator authoring the node must also define:

- `compatibility_constraints.authoring.starter`
- `compatibility_constraints.authoring.singleton`
- `compatibility_constraints.authoring.blocks_with_node_types`
- `compatibility_constraints.authoring.requires_upstream_data_types`
- `compatibility_constraints.authoring.terminal`
- `compatibility_constraints.authoring.palette_reason`

Required input ports are still the source of truth for data-flow
compatibility. Use `requires_upstream_data_types` only for extra palette rules
that ports cannot express. Future visible built-ins without authoring metadata
are expected to fail registry tests.

## 2. Implement The Handler

Create or extend a handler under:

- `cv_runtime.nodes.builtin`
- or an installed extension package

The handler must declare the exact `node_type` and implement
`execute(context, node, inputs)`.

## 3. Validate Runtime Semantics

Add tests for:

- handler loading
- port compatibility
- required properties
- graph execution
- result payloads
- publish-time validation

If the node needs inference, use logical model refs and the runtime inference
client. Do not call Triton directly from the node.

## 4. Surface The Property Editor

If the node uses:

- `model_ref`, the deployed-model picker is already available
- `label_project_list`, the Label Studio project selector is already available
- `zone_list` or `roi`, the structured box editor is already available

Only add a new field type when the existing ones cannot model the operator
workflow cleanly.

Every visible property field must set:

- `label`
- `section`
- concrete `help_text`
- options or bounds for constrained values

The node properties UI is a modal grouped by section, so choose short section
names such as `Configuration`, `Thresholds`, `Geometry`, `Output`, or
`Advanced`. If a field references labels, classes, response fields, PLC source
fields, or upstream result paths, prefer a dynamic select/combobox fed by the
channel authoring context or connected upstream nodes. Keep manual advanced
entry only where publish-time validation supports it.

## 5. Publish Safely

Do not mark the node supported until:

- the handler actually runs
- validation blocks bad configs
- the runtime result model captures the node outputs correctly
- docs explain operator expectations and scope limits
