"""Token projections are not host usage measurements or overflow predictions."""

import hashlib
import json

import tiktoken
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .config import effective_tools, identity


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Counter:
    def __init__(self, encoding: str = "o200k_base"):
        # No fallback: an unavailable encoding/cache must remain an explicit error.
        self.encoding = encoding
        self.tokenizer = tiktoken.get_encoding(encoding)

    def text(self, text: str) -> int:
        return len(self.tokenizer.encode(text, disallowed_special=()))

    def json(self, value) -> int:
        return self.text(canonical(value))


def validate_capture(data: dict) -> tuple[list[dict], str, str]:
    """Return (tools, instructions, detail).

    detail is "definitions" when every tool carries an inputSchema, and "names_only"
    when none of them does. A catalog behind an authenticating endpoint is often only
    reachable in a reduced form; that still measures the always-loaded floor, which is
    the part a deferred-loading host keeps regardless. A capture where only some tools
    carry a schema is malformed, not reduced, and is still rejected.
    """
    # Accept own capture, raw tools/list result, and Inspector JSON-RPC wrapper.
    result = data.get("result", data)
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise ValueError("expected_tools_list")
    if result.get("nextCursor") is not None:
        raise ValueError("incomplete_capture_next_cursor")
    tools = result["tools"]
    schemas = sum(
        1 for t in tools if isinstance(t, dict) and isinstance(t.get("inputSchema"), dict)
    )
    if tools and schemas == 0:
        detail = "names_only"
    elif schemas == len(tools):
        detail = "definitions"
    else:
        raise ValueError("mixed_capture_some_tools_lack_input_schema")
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("invalid_tool")
        if tool["name"] in seen:
            raise ValueError("duplicate_tool_name")
        seen.add(tool["name"])
        if detail == "definitions":
            if tool["inputSchema"].get("type") != "object":
                raise ValueError("input_schema_must_be_object")
            try:
                Draft202012Validator.check_schema(tool["inputSchema"])
                if "outputSchema" in tool:
                    Draft202012Validator.check_schema(tool["outputSchema"])
            except SchemaError as exc:
                raise ValueError("invalid_json_schema") from exc
        if not isinstance(tool.get("description", ""), str):
            raise ValueError("invalid_description")
    instructions = data.get("instructions", "") or ""
    if not isinstance(instructions, str):
        raise ValueError("invalid_instructions")
    return tools, instructions, detail


def analyze(
    data: dict,
    cfg: dict,
    counter: Counter,
    include_names: bool = False,
    name_prefix: str | None = None,
) -> dict:
    tools, instructions, detail = validate_capture(data)
    full = detail == "definitions"
    enabled, notes = effective_tools(tools, cfg)
    details, schemas, description_hashes = [], [], {}
    output_policies = cfg.get("tools", {})
    name_tokens = 0
    for tool in sorted(enabled, key=lambda t: t["name"]):
        schema = {k: tool[k] for k in ("name", "description", "inputSchema") if k in tool}
        schemas.append(schema)
        desc = tool.get("description", "")
        findings = []
        # A host that defers loading still carries the advertised name, so the name is
        # counted apart from the definition it may or may not have loaded.
        exposed = (name_prefix or "") + tool["name"]
        exposed_tokens = counter.text(exposed)
        name_tokens += exposed_tokens
        if not desc.strip():
            findings.append("missing_tool_description")
        if full:
            if counter.json(schema) > 1000:
                findings.append("large_definition_review")
            props = tool["inputSchema"].get("properties", {})
            if not isinstance(props, dict):
                raise ValueError("invalid_schema_properties")
            if not any(
                k in props
                for k in ("limit", "cursor", "page", "max_results", "maxResults", "max_tokens")
            ):
                findings.append("no_recognized_output_bound_parameter")
        policy = output_policies.get(tool["name"], {}) if isinstance(output_policies, dict) else {}
        limit = (
            policy.get("output_token_limit")
            if isinstance(policy.get("output_token_limit"), int)
            else None
        )
        # What the catalog and config declare about constraining a response. An
        # outputSchema is deliberately not counted: it fixes shape, not size. Discovery
        # cannot observe an actual response, so this stays a declaration, not a measurement.
        bound = {
            "input_parameter": full and "no_recognized_output_bound_parameter" not in findings,
            "configured_limit": limit,
            "output_schema_declared": "outputSchema" in tool,
        }
        bound["declared"] = bool(bound["input_parameter"] or limit is not None)
        details.append(
            {
                "id": "tool-" + identity(tool["name"]),
                **({"name": tool["name"][:160]} if include_names else {}),
                # Unmeasured components stay null. A reduced capture is not a cheap tool.
                "always_loaded_tokens": exposed_tokens,
                "definition_tokens": counter.json(schema) if full else None,
                "input_schema_tokens": counter.json(tool["inputSchema"]) if full else None,
                "output_schema_tokens": counter.json(tool["outputSchema"])
                if "outputSchema" in tool
                else (0 if full else None),
                "description_tokens": counter.text(desc),
                "configured_output_token_limit": limit,
                "output_bound": bound,
                "findings": findings,
            }
        )
        if desc.strip():
            description_hashes.setdefault(desc.strip().casefold(), []).append(tool["name"])
    if any(len(v) > 1 for v in description_hashes.values()):
        notes.append("identical_tool_descriptions_review")
    if counter.text(instructions) > 1000:
        notes.append("large_server_instructions_review")
    if not full:
        notes.append("definitions_unavailable_floor_only")
    # Stable across server ordering changes; metadata included for contract-drift detection.
    ordered = sorted(enabled, key=lambda t: t["name"])
    core_tokens = counter.json(schemas) if full else None
    instruction_tokens = counter.text(instructions)

    def rank(row):
        return -(row["definition_tokens"] if full else row["always_loaded_tokens"])

    return {
        "status": "measured",
        "encoding": counter.encoding,
        "detail": detail,
        "advertised_tools": len(tools),
        "selected_tools": len(enabled),
        # Floor: advertised names, which a host carries in every loading mode.
        "always_loaded_tokens": name_tokens,
        "always_loaded_basis": "qualified_names" if name_prefix else "bare_names",
        "instructions_tokens": instruction_tokens,
        "core_tools_tokens": core_tokens,
        # Ceiling for definitions. Null when the capture carried no schemas.
        "eager_projection_tokens": (
            None if core_tokens is None else core_tokens + instruction_tokens
        ),
        "selected_wire_catalog_tokens": counter.json(ordered) if full else None,
        "fingerprint": hashlib.sha256(
            canonical({"tools": ordered, "instructions": instructions}).encode()
        ).hexdigest(),
        "findings": notes,
        "tools": sorted(details, key=rank),
        "runtime_output_tokens": None,
    }


def budget(measured: int, window: int, reserve: int, floor: int = 0) -> dict:
    available = window - reserve
    return {
        "context_window": window,
        "reserved_tokens": reserve,
        # The floor is what the names cost in every loading mode; the projection is
        # the definitions on top of it, and only applies to a host loading them all.
        "always_loaded_tokens": floor,
        "floor_percent_of_window": round(floor / window * 100, 2),
        "eager_projection_tokens": measured,
        "percent_of_window": round(measured / window * 100, 2),
        "remaining_in_projection": available - measured,
        "exceeds_projection_budget": measured > available,
        "actual_host_usage": "unknown",
    }


def compare(before: dict, after: dict) -> dict:
    if before.get("schema_version") != 1 or after.get("schema_version") != 1:
        raise ValueError("unsupported_report")
    if before.get("encoding") != after.get("encoding"):
        raise ValueError("incompatible_encodings")
    left = {s["id"]: s for s in before.get("servers", [])}
    right = {s["id"]: s for s in after.get("servers", [])}
    changes = []
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key, {}), right.get(key, {})
        ma, mb = a.get("measurement", {}), b.get("measurement", {})
        if ma.get("status") == mb.get("status") == "measured":
            # Compare the ceiling when both sides measured it, else the floor, which
            # every measured server has. Never subtract a measured value from a null.
            field = (
                "eager_projection_tokens"
                if ma.get("eager_projection_tokens") is not None
                and mb.get("eager_projection_tokens") is not None
                else "always_loaded_tokens"
            )
            changes.append(
                {
                    "id": key,
                    "status": "comparable",
                    "basis": field,
                    "token_delta": mb.get(field, 0) - ma.get(field, 0),
                    "contract_changed": ma["fingerprint"] != mb["fingerprint"],
                }
            )
        else:
            changes.append({"id": key, "status": "not_comparable"})
    return {"schema_version": 1, "encoding": after.get("encoding"), "changes": changes}
