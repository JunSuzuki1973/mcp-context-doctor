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


def validate_capture(data: dict) -> tuple[list[dict], str]:
    # Accept own capture, raw tools/list result, and Inspector JSON-RPC wrapper.
    result = data.get("result", data)
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise ValueError("expected_tools_list")
    if result.get("nextCursor") is not None:
        raise ValueError("incomplete_capture_next_cursor")
    tools = result["tools"]
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("invalid_tool")
        if tool["name"] in seen:
            raise ValueError("duplicate_tool_name")
        seen.add(tool["name"])
        if not isinstance(tool.get("inputSchema"), dict):
            raise ValueError("missing_input_schema")
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
    return tools, instructions


def analyze(data: dict, cfg: dict, counter: Counter, include_names: bool = False) -> dict:
    tools, instructions = validate_capture(data)
    enabled, notes = effective_tools(tools, cfg)
    details, schemas, description_hashes = [], [], {}
    output_policies = cfg.get("tools", {})
    for tool in sorted(enabled, key=lambda t: t["name"]):
        schema = {k: tool[k] for k in ("name", "description", "inputSchema") if k in tool}
        schemas.append(schema)
        desc = tool.get("description", "")
        findings = []
        if not desc.strip():
            findings.append("missing_tool_description")
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
        details.append(
            {
                "id": "tool-" + identity(tool["name"]),
                **({"name": tool["name"][:160]} if include_names else {}),
                "definition_tokens": counter.json(schema),
                "input_schema_tokens": counter.json(tool["inputSchema"]),
                "output_schema_tokens": counter.json(tool["outputSchema"])
                if "outputSchema" in tool
                else 0,
                "description_tokens": counter.text(desc),
                "configured_output_token_limit": policy.get("output_token_limit")
                if isinstance(policy.get("output_token_limit"), int)
                else None,
                "findings": findings,
            }
        )
        if desc.strip():
            description_hashes.setdefault(desc.strip().casefold(), []).append(tool["name"])
    if any(len(v) > 1 for v in description_hashes.values()):
        notes.append("identical_tool_descriptions_review")
    if counter.text(instructions) > 1000:
        notes.append("large_server_instructions_review")
    # Stable across server ordering changes; metadata included for contract-drift detection.
    ordered = sorted(enabled, key=lambda t: t["name"])
    core_tokens = counter.json(schemas)
    instruction_tokens = counter.text(instructions)
    return {
        "status": "measured",
        "encoding": counter.encoding,
        "advertised_tools": len(tools),
        "selected_tools": len(enabled),
        "instructions_tokens": instruction_tokens,
        "core_tools_tokens": core_tokens,
        "eager_projection_tokens": core_tokens + instruction_tokens,
        "selected_wire_catalog_tokens": counter.json(ordered),
        "fingerprint": hashlib.sha256(
            canonical({"tools": ordered, "instructions": instructions}).encode()
        ).hexdigest(),
        "findings": notes,
        "tools": sorted(details, key=lambda x: -x["definition_tokens"]),
        "runtime_output_tokens": None,
    }


def budget(measured: int, window: int, reserve: int) -> dict:
    available = window - reserve
    return {
        "context_window": window,
        "reserved_tokens": reserve,
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
            changes.append(
                {
                    "id": key,
                    "status": "comparable",
                    "token_delta": mb["eager_projection_tokens"] - ma["eager_projection_tokens"],
                    "contract_changed": ma["fingerprint"] != mb["fingerprint"],
                }
            )
        else:
            changes.append({"id": key, "status": "not_comparable"})
    return {"schema_version": 1, "encoding": after.get("encoding"), "changes": changes}
