"""Role-aware tool-schema sanitization with local reference sensitivity propagation."""

from __future__ import annotations

import json
from typing import Protocol, cast

import referencing.jsonschema as rjs
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from fractalmem.security.redact import Redactor, _marker, is_sensitive_key

_STRUCTURAL_SCHEMA_KEYWORDS: frozenset[str] = frozenset({
    "$ref", "$anchor", "$defs", "definitions", "pattern", "type", "format", "$schema", "$id"
})
_SEMANTIC_CONSTRAINTS: frozenset[str] = frozenset({"enum", "const"})
_SUBSCHEMA_SINGLE_KEYWORDS: frozenset[str] = frozenset({
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "unevaluatedItems",
    "additionalItems",
})
_SUBSCHEMA_ARRAY_KEYWORDS: frozenset[str] = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_PROPERTY_MAP_KEYWORDS: frozenset[str] = frozenset({"properties", "patternProperties"})
_DEFINITION_MAP_KEYWORDS: frozenset[str] = frozenset({"$defs", "definitions"})
_UNSUPPORTED_DYNAMIC_KEYWORDS: frozenset[str] = frozenset({"$dynamicRef", "$recursiveRef"})
_DATA_ANNOTATION_KEYWORDS: frozenset[str] = frozenset({"default", "examples", "example"})


class _ResolvedRef(Protocol):
    contents: object
    resolver: _SchemaResolver


class _SchemaResolver(Protocol):
    def lookup(self, ref: str) -> _ResolvedRef:
        ...

    def in_subresource(self, subresource: Resource[object]) -> _SchemaResolver:
        ...


def _check_semantic_constraint(
    constraint_name: str,
    value: object,
    enclosing_sensitive: bool,
    redactor: Redactor,
) -> None:
    """Validate semantic constraints (enum, const).

    Static rejection occurs if:
    1. Redaction of value differs from value (contains unredacted credentials).
    2. Enclosing scope is sensitive and value is a non-empty / meaningful constraint
       (including 0, false, strings, non-empty containers).
    Empty containers, empty strings, and None are permitted.
    """
    if enclosing_sensitive:
        if value is None:
            return
        if isinstance(value, str):
            if value:
                raise ValueError(
                    f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
                ) from None
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            # 0, False, True, and numeric constants are meaningful constraints on sensitive fields
            raise ValueError(
                f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
            ) from None
        elif isinstance(value, (list, dict)):
            if value:
                raise ValueError(
                    f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
                ) from None
        return

    # Non-sensitive scope: ensure constraint itself does not contain credentials
    if isinstance(value, str):
        if redactor.redact(value) != value:
            raise ValueError(
                f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
            ) from None
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and redactor.redact(item) != item:
                raise ValueError(
                    f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
                ) from None
            elif isinstance(item, (dict, list)):
                item_str = json.dumps(item)
                if redactor.redact(item_str) != item_str:
                    raise ValueError(
                        f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
                    ) from None
    elif isinstance(value, dict):
        val_str = json.dumps(value)
        if redactor.redact(val_str) != val_str:
            raise ValueError(
                f"tool schema contains sensitive credentials in semantic constraint '{constraint_name}'"
            ) from None


def _sanitize_annotation_data(
    data: object,
    redactor: Redactor,
    enclosing_sensitive: bool = False,
) -> object:
    """Recursively scrub annotation data payloads (default, examples) while preserving structure.

    If enclosing_sensitive is True or a dictionary key is sensitive (e.g. password, api_key),
    all values within that scope are scrubbed to [REDACTED_SECRET_<hash>].
    Nested dictionaries and lists are traversed as data, preserving structural shapes.
    """
    if isinstance(data, str):
        if enclosing_sensitive:
            return _marker(data)
        return redactor.redact(data)
    elif isinstance(data, dict):
        res: dict[str, object] = {}
        for k, v in data.items():
            k_str = str(k)
            if redactor.redact(k_str) != k_str:
                raise ValueError("tool schema contains sensitive credentials in property name") from None
            sub_sensitive = enclosing_sensitive or is_sensitive_key(k_str)
            res[k_str] = _sanitize_annotation_data(v, redactor, enclosing_sensitive=sub_sensitive)
        return res
    elif isinstance(data, list):
        return [
            _sanitize_annotation_data(item, redactor, enclosing_sensitive=enclosing_sensitive)
            for item in data
        ]
    else:
        if enclosing_sensitive and data is not None:
            return _marker(str(data))
        return data


def _get_sensitive_node_ids(root_schema: dict[str, object], redactor: Redactor) -> set[int]:
    """Find all schema node IDs that inherit sensitive context via properties or local references.

    Uses `referencing` to trace local fragment references (#/...) without fetching external URLs.
    Supports declared schema drafts ($schema), nested $id scopes, tuple items, and unevaluated
    keywords. Dynamic/recursive references ($dynamicRef, $recursiveRef) and external references
    are statically rejected with static ValueError messages.
    """
    if any(k in root_schema for k in _UNSUPPORTED_DYNAMIC_KEYWORDS):
        raise ValueError("tool schema contains unsupported dynamic reference") from None

    spec = DRAFT202012
    if "$schema" in root_schema and isinstance(root_schema["$schema"], str):
        schema_uri = root_schema["$schema"]
        try:
            spec = rjs.specification_with(schema_uri)
        except Exception:
            raise ValueError("tool schema contains unsupported schema draft") from None

    base_uri = str(root_schema.get("$id", "urn:tool_schema")) if "$id" in root_schema else "urn:tool_schema"
    try:
        resource = spec.create_resource(root_schema)
        registry = Registry().with_resource(base_uri, resource)
        root_resolver = cast(_SchemaResolver, registry.resolver(base_uri))
    except Exception:
        raise ValueError("tool schema contains invalid schema resource") from None

    sensitive_ids: set[int] = set()
    visited: set[tuple[int, bool]] = set()

    def walk(
        node: object,
        resolver: _SchemaResolver,
        enclosing_sensitive: bool = False,
        depth: int = 0,
        already_in_subresource: bool = False,
    ) -> None:
        if depth > 32:
            raise ValueError("tool schema exceeds maximum recursion depth") from None
        if isinstance(node, bool) or not isinstance(node, dict):
            return

        if any(k in node for k in _UNSUPPORTED_DYNAMIC_KEYWORDS):
            raise ValueError("tool schema contains unsupported dynamic reference") from None

        # Descent into subschema with $id enters that resource's resolver scope exactly once
        node_resolver = resolver
        if not already_in_subresource and "$id" in node and node is not root_schema:
            try:
                sub_res = spec.create_resource(node)
                node_resolver = resolver.in_subresource(sub_res)
            except Exception:
                raise ValueError("tool schema contains invalid sub-resource identifier") from None

        node_id = id(node)
        if node_id in sensitive_ids:
            enclosing_sensitive = True
        if (node_id, enclosing_sensitive) in visited:
            return
        visited.add((node_id, enclosing_sensitive))

        if enclosing_sensitive:
            sensitive_ids.add(node_id)

        # Check $ref
        ref = node.get("$ref")
        if isinstance(ref, str):
            if redactor.redact(ref) != ref:
                raise ValueError("tool schema contains sensitive credentials in structural field '$ref'") from None
            if not ref.startswith("#"):
                raise ValueError("tool schema contains external reference which is forbidden") from None
            try:
                resolved = node_resolver.lookup(ref)
            except Exception:
                raise ValueError("tool schema contains unresolvable reference") from None

            if isinstance(resolved.contents, bool):
                pass
            elif not isinstance(resolved.contents, dict):
                raise ValueError("tool schema reference does not resolve to a valid schema") from None
            else:
                walk(
                    resolved.contents,
                    resolver=resolved.resolver,
                    enclosing_sensitive=enclosing_sensitive,
                    depth=depth + 1,
                    already_in_subresource=True,
                )

        # Property maps (properties, patternProperties): check sensitive naming
        for kw in _PROPERTY_MAP_KEYWORDS:
            props = node.get(kw)
            if isinstance(props, dict):
                for p_name, p_val in props.items():
                    p_str = str(p_name)
                    is_sub = enclosing_sensitive or is_sensitive_key(p_str)
                    walk(p_val, resolver=node_resolver, enclosing_sensitive=is_sub, depth=depth + 1)

        # dependentSchemas: mapping keys are triggers, NOT child values
        dep_s = node.get("dependentSchemas")
        if isinstance(dep_s, dict):
            for _, ds_val in dep_s.items():
                walk(ds_val, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)

        # dependencies (Draft 7): values can be schema dependencies or property lists
        deps = node.get("dependencies")
        if isinstance(deps, dict):
            for _, dep_val in deps.items():
                if isinstance(dep_val, (dict, bool)):
                    walk(dep_val, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)

        # Definition maps ($defs, definitions): definition names are identifiers, not keywords
        for kw in _DEFINITION_MAP_KEYWORDS:
            defs = node.get(kw)
            if isinstance(defs, dict):
                for _, def_val in defs.items():
                    walk(def_val, resolver=node_resolver, enclosing_sensitive=False, depth=depth + 1)

        # Subschema arrays (allOf, anyOf, oneOf, prefixItems)
        for kw in _SUBSCHEMA_ARRAY_KEYWORDS:
            arr = node.get(kw)
            if isinstance(arr, list):
                for item in arr:
                    walk(item, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)

        # items keyword: can be a single subschema dict/bool or a tuple list of schemas (Draft 7)
        items = node.get("items")
        if isinstance(items, (dict, bool)):
            walk(items, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)
        elif isinstance(items, list):
            for item in items:
                walk(item, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)

        # Single subschema keywords (additionalProperties, unevaluatedProperties, propertyNames, etc.)
        for kw in _SUBSCHEMA_SINGLE_KEYWORDS:
            sub = node.get(kw)
            if isinstance(sub, (dict, bool)):
                walk(sub, resolver=node_resolver, enclosing_sensitive=enclosing_sensitive, depth=depth + 1)

    walk(root_schema, resolver=root_resolver, enclosing_sensitive=False, depth=0)
    return sensitive_ids


def sanitize_tool_wrapper(tool: dict[str, object], redactor: Redactor) -> dict[str, object]:
    """Sanitize a tool definition wrapper dictionary.

    Roots the JSON Schema graph strictly at tool['input_schema'] while preserving
    and sanitizing tool wrapper metadata (name, description, etc.) separately.
    Rejects sensitive or invalid tool names, unresolvable references, external URLs,
    and recursion depth limit violations with static errors.
    """
    if not isinstance(tool, dict):
        raise ValueError("tool definition must be a dictionary") from None

    tool_name = tool.get("name")
    if not isinstance(tool_name, str):
        raise ValueError("each tool requires a name and input_schema") from None
    if redactor.redact(tool_name) != tool_name:
        raise ValueError("tool name contains sensitive credentials") from None

    raw_input_schema = tool.get("input_schema")
    if not isinstance(raw_input_schema, dict):
        raise ValueError("each tool requires a name and input_schema") from None

    # Root the schema graph strictly at input_schema
    sanitized_input_schema = cast(
        dict[str, object],
        sanitize_tool_schema(raw_input_schema, redactor),
    )

    result: dict[str, object] = {
        "name": tool_name,
        "input_schema": sanitized_input_schema,
    }

    # Process wrapper metadata separately
    for key, value in tool.items():
        if key in ("name", "input_schema"):
            continue
        key_str = str(key)
        if redactor.redact(key_str) != key_str:
            raise ValueError("tool definition contains sensitive credentials in wrapper key") from None
        if isinstance(value, str):
            result[key_str] = redactor.redact(value)
        else:
            result[key_str] = _sanitize_annotation_data(
                value, redactor, enclosing_sensitive=False
            )

    return result


def sanitize_tool_schema(
    schema: object,
    redactor: Redactor,
    parent_key: str | None = None,
    enclosing_sensitive: bool = False,
    sensitive_node_ids: set[int] | None = None,
    depth: int = 0,
) -> object:
    """Recursively sanitize literal text values in tool schemas while preserving structure.

    Property names and required field identifiers are preserved unchanged.
    Semantic schema constraints (enum, const) and structural keywords ($ref, pattern, etc.)
    are never silently rewritten; if they contain credentials, they are statically rejected.
    Literal text annotations (description, default, examples, title) are sanitized, preserving
    sensitive property context (e.g. low-entropy passwords under sensitive property names or
    sensitive definitions referenced via $ref).
    All error messages are static and never reflect caller-supplied secret inputs.
    """
    if depth > 32:
        raise ValueError("tool schema exceeds maximum recursion depth") from None

    # Boolean schemas (true / false) are valid in JSON Schema and contain no secret strings
    if isinstance(schema, bool):
        return schema

    # If called on a tool wrapper dictionary directly, delegate to sanitize_tool_wrapper
    if (
        parent_key is None
        and isinstance(schema, dict)
        and "name" in schema
        and isinstance(schema.get("name"), str)
        and "input_schema" in schema
        and isinstance(schema.get("input_schema"), dict)
    ):
        return sanitize_tool_wrapper(schema, redactor)

    if isinstance(schema, str):
        if parent_key in _STRUCTURAL_SCHEMA_KEYWORDS:
            if redactor.redact(schema) != schema:
                raise ValueError(
                    f"tool schema contains sensitive credentials in structural field '{parent_key}'"
                ) from None
            return schema
        if parent_key == "required":
            if redactor.redact(schema) != schema:
                raise ValueError("tool schema contains sensitive credentials in required field") from None
            return schema
        if parent_key in _SEMANTIC_CONSTRAINTS:
            _check_semantic_constraint(parent_key, schema, enclosing_sensitive, redactor)
            return schema
        if parent_key in _DATA_ANNOTATION_KEYWORDS and enclosing_sensitive:
            return _marker(schema)
        return redactor.redact(schema)

    if isinstance(schema, dict):
        if any(k in schema for k in _UNSUPPORTED_DYNAMIC_KEYWORDS):
            raise ValueError("tool schema contains unsupported dynamic reference") from None

        if sensitive_node_ids is None and parent_key is None:
            sensitive_node_ids = _get_sensitive_node_ids(schema, redactor)

        is_node_sensitive = enclosing_sensitive or (
            sensitive_node_ids is not None and id(schema) in sensitive_node_ids
        )

        result: dict[str, object] = {}
        for k, v in schema.items():
            key_str = str(k)
            if redactor.redact(key_str) != key_str:
                raise ValueError("tool schema contains sensitive credentials in property name") from None

            if key_str in _SEMANTIC_CONSTRAINTS:
                _check_semantic_constraint(key_str, v, is_node_sensitive, redactor)
                result[key_str] = v
            elif key_str == "required" and isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and redactor.redact(item) != item:
                        raise ValueError("tool schema contains sensitive credentials in required field") from None
                result[key_str] = v
            elif key_str == "dependentRequired" and isinstance(v, dict):
                for dep_k, req_list in v.items():
                    if redactor.redact(str(dep_k)) != str(dep_k):
                        raise ValueError("tool schema contains sensitive credentials in required field") from None
                    if isinstance(req_list, list):
                        for item in req_list:
                            if isinstance(item, str) and redactor.redact(item) != item:
                                raise ValueError("tool schema contains sensitive credentials in required field") from None
                result[key_str] = v
            elif key_str in _STRUCTURAL_SCHEMA_KEYWORDS and isinstance(v, str):
                if redactor.redact(v) != v:
                    raise ValueError(
                        f"tool schema contains sensitive credentials in structural field '{key_str}'"
                    ) from None
                result[key_str] = v
            elif key_str in _DATA_ANNOTATION_KEYWORDS:
                result[key_str] = _sanitize_annotation_data(
                    v, redactor, enclosing_sensitive=is_node_sensitive
                )
            elif key_str in _DEFINITION_MAP_KEYWORDS and isinstance(v, dict):
                # Definition map entries are identifiers (not schema keywords); traverse values as schemas
                sub_defs: dict[str, object] = {}
                for def_name, def_val in v.items():
                    def_name_str = str(def_name)
                    if redactor.redact(def_name_str) != def_name_str:
                        raise ValueError("tool schema contains sensitive credentials in definition name") from None
                    sub_defs[def_name_str] = sanitize_tool_schema(
                        def_val,
                        redactor,
                        parent_key=None,
                        enclosing_sensitive=is_node_sensitive,
                        sensitive_node_ids=sensitive_node_ids,
                        depth=depth + 1,
                    )
                result[key_str] = sub_defs
            elif key_str in _PROPERTY_MAP_KEYWORDS and isinstance(v, dict):
                # Property and patternProperty map entries
                sub_dict: dict[str, object] = {}
                for prop_name, prop_val in v.items():
                    prop_name_str = str(prop_name)
                    if redactor.redact(prop_name_str) != prop_name_str:
                        raise ValueError("tool schema contains sensitive credentials in property name") from None
                    is_sub_sensitive = is_node_sensitive or is_sensitive_key(prop_name_str)
                    sub_dict[prop_name_str] = sanitize_tool_schema(
                        prop_val,
                        redactor,
                        parent_key=prop_name_str,
                        enclosing_sensitive=is_sub_sensitive,
                        sensitive_node_ids=sensitive_node_ids,
                        depth=depth + 1,
                    )
                result[key_str] = sub_dict
            elif key_str == "dependentSchemas" and isinstance(v, dict):
                # dependentSchemas keys are presence triggers, NOT values. Do NOT mark sensitive based on trigger key!
                sub_ds: dict[str, object] = {}
                for trigger_name, dep_schema in v.items():
                    trigger_name_str = str(trigger_name)
                    if redactor.redact(trigger_name_str) != trigger_name_str:
                        raise ValueError("tool schema contains sensitive credentials in property name") from None
                    sub_ds[trigger_name_str] = sanitize_tool_schema(
                        dep_schema,
                        redactor,
                        parent_key=trigger_name_str,
                        enclosing_sensitive=is_node_sensitive,
                        sensitive_node_ids=sensitive_node_ids,
                        depth=depth + 1,
                    )
                result[key_str] = sub_ds
            elif key_str == "dependencies":
                if not isinstance(v, dict):
                    raise ValueError("tool schema contains invalid dependencies keyword") from None
                sub_deps: dict[str, object] = {}
                for dep_trigger, dep_val in v.items():
                    dep_trigger_str = str(dep_trigger)
                    if redactor.redact(dep_trigger_str) != dep_trigger_str:
                        raise ValueError("tool schema contains sensitive credentials in property name") from None
                    if isinstance(dep_val, list):
                        for item in dep_val:
                            if isinstance(item, str) and redactor.redact(item) != item:
                                raise ValueError("tool schema contains sensitive credentials in required field") from None
                        sub_deps[dep_trigger_str] = dep_val
                    elif isinstance(dep_val, (dict, bool)):
                        sub_deps[dep_trigger_str] = sanitize_tool_schema(
                            dep_val,
                            redactor,
                            parent_key=dep_trigger_str,
                            enclosing_sensitive=is_node_sensitive,
                            sensitive_node_ids=sensitive_node_ids,
                            depth=depth + 1,
                        )
                    else:
                        raise ValueError("tool schema contains invalid dependency") from None
                result[key_str] = sub_deps
            elif key_str == "items" and isinstance(v, list):
                # Tuple items list in older draft schema
                result[key_str] = [
                    sanitize_tool_schema(
                        item,
                        redactor,
                        parent_key=None,
                        enclosing_sensitive=is_node_sensitive,
                        sensitive_node_ids=sensitive_node_ids,
                        depth=depth + 1,
                    )
                    for item in v
                ]
            elif key_str in _SUBSCHEMA_ARRAY_KEYWORDS and isinstance(v, list):
                result[key_str] = [
                    sanitize_tool_schema(
                        item,
                        redactor,
                        parent_key=None,
                        enclosing_sensitive=is_node_sensitive,
                        sensitive_node_ids=sensitive_node_ids,
                        depth=depth + 1,
                    )
                    for item in v
                ]
            elif key_str in _SUBSCHEMA_SINGLE_KEYWORDS:
                result[key_str] = sanitize_tool_schema(
                    v,
                    redactor,
                    parent_key=None,
                    enclosing_sensitive=is_node_sensitive,
                    sensitive_node_ids=sensitive_node_ids,
                    depth=depth + 1,
                )
            else:
                result[key_str] = sanitize_tool_schema(
                    v,
                    redactor,
                    parent_key=key_str,
                    enclosing_sensitive=is_node_sensitive,
                    sensitive_node_ids=sensitive_node_ids,
                    depth=depth + 1,
                )
        return result

    if isinstance(schema, list):
        if parent_key in _DATA_ANNOTATION_KEYWORDS:
            return _sanitize_annotation_data(
                schema, redactor, enclosing_sensitive=enclosing_sensitive
            )
        return [
            sanitize_tool_schema(
                item,
                redactor,
                parent_key=parent_key,
                enclosing_sensitive=enclosing_sensitive,
                sensitive_node_ids=sensitive_node_ids,
                depth=depth + 1,
            )
            for item in schema
        ]

    if parent_key in _SEMANTIC_CONSTRAINTS:
        _check_semantic_constraint(parent_key, schema, enclosing_sensitive, redactor)
        return schema
    if parent_key in _DATA_ANNOTATION_KEYWORDS and enclosing_sensitive and schema is not None:
        return _marker(str(schema))

    return schema
