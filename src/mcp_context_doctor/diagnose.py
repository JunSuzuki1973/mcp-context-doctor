"""Turn measurements into reviewable items.

Every rule here must hold without knowing the host's loading mode, history, prompt
framing or which tools the operator actually uses. That rules out an absolute verdict
on context safety, so nothing below claims one. What remains is still decision-useful:
cost concentrated in a few definitions, a description far outside the distribution, one
backend reached twice, and the parts that could not be measured at all.

Thresholds are review heuristics chosen for legibility, not limits derived from any
model or host. They are named here so a reader can disagree with a number rather than
reverse-engineer it.
"""

from __future__ import annotations

from statistics import median

# A minority of tools this small carrying this much of a server's definitions is worth
# surfacing, because a host-side tool filter can act on exactly that minority.
CONCENTRATION_SHARE = 0.40
CONCENTRATION_MINORITY = 0.30
CONCENTRATION_MIN_TOOLS = 5

# A description this many times the median across everything measured is an outlier in
# its own report, not a violation of any rule.
OUTLIER_MULTIPLE = 5
OUTLIER_MIN_SAMPLE = 8


def _name(row: dict) -> str:
    return row.get("name", row["id"])


def _concentration(server: dict, measurement: dict) -> dict | None:
    tools = [t for t in measurement["tools"] if t["definition_tokens"] is not None]
    total = measurement.get("core_tools_tokens")
    if not total or len(tools) < CONCENTRATION_MIN_TOOLS:
        return None
    ranked = sorted(tools, key=lambda t: -t["definition_tokens"])
    running = 0
    for index, tool in enumerate(ranked, start=1):
        running += tool["definition_tokens"]
        if running >= total * CONCENTRATION_SHARE:
            if index > len(ranked) * CONCENTRATION_MINORITY:
                return None
            return {
                "code": "definition_cost_concentrated_in_few_tools",
                "server": server.get("id"),
                "server_name": server.get("name"),
                "impact_tokens": running,
                "evidence": {
                    "tools": index,
                    "of_tools": len(ranked),
                    "share_of_server_definitions": round(running / total, 3),
                    "largest": [_name(t) for t in ranked[:index]][:8],
                },
                "action": (
                    f"{index} of {len(ranked)} tools carry "
                    f"{round(running / total * 100)}% of this server's definitions. "
                    "If they are not used, excluding them in the host's tool filter "
                    f"removes about {running} tokens from an eager load."
                ),
            }
    return None


def _outliers(servers: list[dict]) -> list[dict]:
    sample = [
        (server, tool)
        for server in servers
        for tool in server["measurement"].get("tools", [])
        if tool["description_tokens"] > 0
    ]
    if len(sample) < OUTLIER_MIN_SAMPLE:
        return []
    middle = median(t["description_tokens"] for _, t in sample)
    if middle <= 0:
        return []
    items = []
    for server, tool in sample:
        if tool["description_tokens"] >= middle * OUTLIER_MULTIPLE:
            items.append(
                {
                    "code": "tool_description_far_above_median",
                    "server": server.get("id"),
                    "server_name": server.get("name"),
                    "impact_tokens": tool["description_tokens"],
                    "evidence": {
                        "tool": _name(tool),
                        "description_tokens": tool["description_tokens"],
                        "median_description_tokens": middle,
                        "multiple": round(tool["description_tokens"] / middle, 1),
                    },
                    "action": (
                        f"This description is {round(tool['description_tokens'] / middle, 1)}x "
                        "the median across everything measured here. Descriptions are paid "
                        "on every eager load; a shorter one is a change for the server "
                        "author, not the operator."
                    ),
                }
            )
    return items


def _duplicates(servers: list[dict]) -> list[dict]:
    shared = [
        s
        for s in servers
        if "repeated_endpoint_not_proof_of_duplicate_loading" in s.get("findings", [])
    ]
    if not shared:
        return []
    proxied = [
        s
        for s in shared
        if "same_backend_reached_through_different_transports" in s.get("findings", [])
    ]
    item = {
        "code": "one_backend_configured_more_than_once",
        "server": None,
        "server_name": None,
        "impact_tokens": 0,
        "evidence": {
            "entries": len(shared),
            "through_different_transports": len(proxied),
            "servers": [s.get("id") for s in shared][:12],
        },
        "action": (
            f"{len(shared)} config entries resolve to a backend that another entry also "
            "reaches. Separate hosts each pay for their own connection; this is not proof "
            "that one host loads it twice. Remove the entry a host no longer needs."
        ),
    }
    return [item]


def _coverage(servers: list[dict]) -> list[dict]:
    unmeasured = [
        s for s in servers if s.get("enabled") and s["measurement"]["status"] not in ("measured",)
    ]
    floor_only = [
        s
        for s in servers
        if s["measurement"]["status"] == "measured"
        and s["measurement"].get("eager_projection_tokens") is None
    ]
    items = []
    if unmeasured:
        reasons = sorted({s["measurement"]["status"] for s in unmeasured})
        # The next step depends on why a server was skipped. Advising a token for a
        # server that was simply never probed sends the reader down the wrong path.
        remedy = {
            "not_probed": (
                "A static scan reads configuration and starts nothing. Re-run with --live "
                "and a --server selection to measure the ones you trust."
            ),
            "auth_required": (
                "The endpoint rejected an unauthenticated request. Supply a token through "
                "the config's referenced environment variable, or export the catalog from "
                "an already-authenticated client and pass it to analyze."
            ),
            "timeout": "The server did not answer in time. Raise --timeout or start it first.",
            "command_not_found": "The configured command is not on PATH from this shell.",
            "disabled": "",
        }
        advice = " ".join(dict.fromkeys(remedy.get(r, "") for r in reasons)).strip()
        items.append(
            {
                "code": "enabled_servers_not_measured",
                "server": None,
                "server_name": None,
                "impact_tokens": 0,
                "evidence": {"servers": len(unmeasured), "reasons": reasons},
                "action": (
                    f"{len(unmeasured)} enabled servers were not measured "
                    f"({', '.join(reasons)}). Their cost is unknown, not zero, so no total "
                    f"here is complete. {advice}".strip()
                ),
            }
        )
    if floor_only:
        items.append(
            {
                "code": "definitions_unavailable_for_some_servers",
                "server": None,
                "server_name": None,
                "impact_tokens": 0,
                "evidence": {"servers": [s.get("id") for s in floor_only][:12]},
                "action": (
                    f"{len(floor_only)} servers were measured to their always-loaded floor "
                    "only. The floor is exact; the eager projection for them is unknown."
                ),
            }
        )
    return items


def _rollup(servers: list[dict]) -> list[dict]:
    codes = {
        "identical_tool_descriptions_review": (
            "Two or more tools advertise the same description. A model selects between "
            "them on that text, so identical descriptions cost tokens without "
            "distinguishing the tools."
        ),
        "large_server_instructions_review": (
            "Server instructions are large. They are counted separately from tool "
            "definitions and a host may present or trim them differently."
        ),
    }
    items = []
    for server in servers:
        for code, action in codes.items():
            if code in server["measurement"].get("findings", []):
                items.append(
                    {
                        "code": code,
                        "server": server.get("id"),
                        "server_name": server.get("name"),
                        "impact_tokens": server["measurement"].get("instructions_tokens", 0)
                        if "instructions" in code
                        else 0,
                        "evidence": {},
                        "action": action,
                    }
                )
        missing = [
            t
            for t in server["measurement"].get("tools", [])
            if "missing_tool_description" in t["findings"]
        ]
        if missing:
            items.append(
                {
                    "code": "tools_without_a_description",
                    "server": server.get("id"),
                    "server_name": server.get("name"),
                    "impact_tokens": 0,
                    "evidence": {"tools": len(missing), "names": [_name(t) for t in missing][:8]},
                    "action": (
                        f"{len(missing)} tools advertise no description. A model has only the "
                        "name to select on, which tends to cost more in wrong calls than a "
                        "description would in tokens."
                    ),
                }
            )
    return items


def diagnose(report: dict) -> dict:
    """Build the reviewable-items block for a finished report."""
    servers = report.get("servers", [])
    measured = [s for s in servers if s["measurement"]["status"] == "measured"]
    items: list[dict] = []
    for server in measured:
        if found := _concentration(server, server["measurement"]):
            items.append(found)
    items += _outliers(measured)
    items += _duplicates(servers)
    items += _rollup(measured)
    items += _coverage(servers)
    items.sort(key=lambda i: (-i["impact_tokens"], i["code"]))

    enabled = [s for s in servers if s.get("enabled")]
    unmeasured = len(enabled) - len(measured)
    if not measured:
        verdict = "nothing_measured"
    elif items:
        verdict = "review_recommended"
    else:
        verdict = "no_findings_in_measured_scope"
    return {
        "verdict": verdict,
        # Stated up front so a verdict is never read as covering more than it saw.
        "coverage": {
            "measured_servers": len(measured),
            "enabled_servers": len(enabled),
            "unmeasured_enabled_servers": unmeasured,
            "complete": unmeasured == 0 and bool(measured),
        },
        "always_loaded_tokens": sum(
            s["measurement"].get("always_loaded_tokens", 0) for s in measured
        ),
        "items": items,
        "basis": [
            "Items are derived only from what was measured in this run.",
            "Thresholds are review heuristics, not model or host limits.",
            "No item asserts that a context window will overflow.",
        ],
    }
