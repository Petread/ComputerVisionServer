"""Read registered node metadata from a wheel without importing target-native libraries.

Follow the actual catalogue expressions, including imported factories and wrappers.
Unsupported dynamic registrations fail the export instead of silently omitting nodes.
"""

from __future__ import annotations

import ast
import zipfile
from dataclasses import dataclass
from typing import Any


@dataclass
class Bound:
    expression: ast.AST
    scope: dict[str, Any]


def wheel_node_contracts(wheel: zipfile.ZipFile) -> dict[str, int]:
    modules: dict[str, dict[str, Any]] = {}

    def module_scope(name: str) -> dict[str, Any]:
        if name in modules:
            return modules[name]
        tree = ast.parse(wheel.read(name.replace(".", "/") + ".py"))
        scope: dict[str, Any] = {}
        modules[name] = scope
        for item in tree.body:
            if isinstance(item, ast.FunctionDef):
                scope[item.name] = Bound(item, scope)
            elif isinstance(item, ast.ImportFrom) and (item.module or "").startswith("cv_runtime."):
                imported = module_scope(item.module)
                for alias in item.names:
                    scope[alias.asname or alias.name] = imported[alias.name]
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        scope[target.id] = Bound(item.value, scope)
        scope["__tree__"] = tree
        return scope

    scope = module_scope("cv_runtime.registry")
    factory = scope["_node"].expression
    versions = [
        ast.literal_eval(keyword.value)
        for item in ast.walk(factory)
        if isinstance(item, ast.Call)
        for keyword in item.keywords
        if keyword.arg == "contract_version"
    ]
    if len(versions) != 1 or not isinstance(versions[0], int):
        raise ValueError("Cannot determine the node factory contract version.")
    scope["_node"] = "node_factory"
    distribution = ast.parse(wheel.read("cv_runtime/distribution.py"))
    unavailable = next(
        set(ast.literal_eval(item.value.args[0]))
        for item in distribution.body
        if isinstance(item, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "EDGE_UNAVAILABLE_NODES" for t in item.targets)
    )

    def resolve(expression: ast.AST, local: dict[str, Any], depth: int = 0) -> object:
        if depth > 40:
            raise ValueError("Recursive node metadata expression.")
        if isinstance(expression, ast.Name) and expression.id in local:
            value = local[expression.id]
            if isinstance(value, Bound) and not isinstance(value.expression, ast.FunctionDef):
                return resolve(value.expression, value.scope, depth + 1)
            return value
        if isinstance(expression, ast.Constant):
            return expression.value
        if isinstance(expression, ast.Attribute):
            return expression.attr
        raise ValueError(f"Unsupported dynamic node metadata: {ast.dump(expression)}")

    contracts: dict[str, int] = {}

    def collect(expression: ast.AST, local: dict[str, Any], depth: int = 0) -> None:
        if depth > 40:
            raise ValueError("Recursive node registration.")
        if isinstance(expression, ast.Tuple | ast.List):
            for element in expression.elts:
                collect(element, local, depth + 1)
            return
        if isinstance(expression, ast.Starred):
            collect(expression.value, local, depth + 1)
            return
        if not isinstance(expression, ast.Call):
            raise ValueError(f"Unsupported node registration: {ast.dump(expression)}")
        function = resolve(expression.func, local)
        if function == "node_factory":
            arguments = {item.arg: item.value for item in expression.keywords}
            name = resolve(arguments["node_type"], local)
            category = resolve(arguments["category"], local)
            excluded = name in unavailable or category == "CAPTURE"
            constraints = arguments.get("constraints")
            if constraints is not None:
                if not isinstance(constraints, ast.Dict):
                    raise ValueError(
                        "Dynamic Edge compatibility constraints "
                        "require an explicit metadata exporter."
                    )
                excluded |= any(
                    resolve(k, local) == "edge_supported" and resolve(v, local) is False
                    for k, v in zip(constraints.keys, constraints.values, strict=True)
                    if k is not None and resolve(k, local) == "edge_supported"
                )
            if not excluded:
                if name in contracts:
                    raise ValueError(f"Duplicate registered node: {name}")
                contracts[name] = versions[0]
            return
        if not isinstance(function, Bound) or not isinstance(function.expression, ast.FunctionDef):
            raise ValueError("Unsupported node factory.")
        definition = function.expression
        child = dict(function.scope)
        for parameter, argument in zip(definition.args.args, expression.args, strict=False):
            child[parameter.arg] = Bound(argument, local)
        for keyword in expression.keywords:
            child[keyword.arg] = Bound(keyword.value, local)
        returns = []
        for statement in definition.body:
            if isinstance(statement, ast.FunctionDef):
                child[statement.name] = Bound(statement, child)
            elif isinstance(statement, ast.Return):
                returns.append(statement.value)
        if len(returns) != 1:
            raise ValueError(f"Dynamic node factory return: {definition.name}")
        collect(returns[0], child, depth + 1)

    tree = scope["__tree__"]
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BUILTIN_NODE_TYPES" for t in item.targets
        ):
            collect(item.value, scope)
        elif (
            isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and item.target.id == "BUILTIN_NODE_TYPES"
        ):
            collect(item.value, scope)
        elif (
            isinstance(item, ast.AugAssign)
            and isinstance(item.target, ast.Name)
            and item.target.id == "BUILTIN_NODE_TYPES"
        ):
            collect(item.value, scope)
    if not contracts:
        raise ValueError("Runtime wheel contains no exportable registered nodes.")
    return contracts
