# Runtime Plugin Loading

Authoritative extension model:

- `docs/runtime/pipeline-node-plugin-model.md`

This note documents the implemented runtime handler loading path.

## Resolution Model

`runtime_handler_ref` uses the form:

```text
module.path:attribute_name
```

Resolution is performed by `cv_runtime.loader.load_runtime_handler()`.

The loader:

1. validates the reference format
2. imports the module
3. resolves the attribute
4. instantiates it when the attribute is a class
5. checks `node_type`
6. checks that `execute(context, node, inputs)` exists

## Validation Stages

Handler availability is checked in two places:

- pipeline validation and publish-time checks
- executable graph build before execution

If a handler cannot be loaded, the pipeline is not publishable and the runtime
bind fails with a clear error.

## Failure Examples

- invalid `module:attribute` format
- import failure
- missing attribute
- `node_type` mismatch
- missing `execute()` method

These failures surface as:

- `handler_unavailable` during validation
- runtime bind or graph build failure during execution setup

## Safe Extension Path

To add a custom runtime node handler safely:

1. package the handler in a Python module
2. register the node type in `runtime_node_type_registry`
3. point `runtime_handler_ref` at the handler module and attribute
4. add validation and execution tests
5. deploy the package to runtime services before publishing a pipeline that
   depends on it

Custom handlers are never loaded through string evaluation, dynamic `exec`, or
operator-provided code. The runtime only imports installed Python modules.
