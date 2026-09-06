# Design and measurement contract

Version 0.1.1 uses a CLI plus an Agent Skill. It does not add another always-connected MCP server.

## Data flow

Known config files or explicit config → independent server inventory → optional SDK discovery → local tokenizer → redacted report → optional report diff.

Static mode never launches servers. Live mode selects configured commands/URLs and uses the official MCP Python SDK 2.x. The SDK negotiates the protocol; the report records the actual revision. Legacy stdio and modern Streamable HTTP are tested separately. There is no `tools/call`, resource read, prompt rendering, sampling callback or elicitation callback in the collector.

## Scope and precedence

Codex TOML, Claude/Cursor `mcpServers`, and VS Code `servers` mappings are accepted. JSON comments and trailing commas are supported through json5. An empty config file is an empty inventory, not an unreadable one; hosts create the file before anything is configured. Editor-predefined variables (`${workspaceFolder}`, `${workspaceFolderBasename}`, `${userHome}`, `${pathSeparator}`) resolve from the selected project. `${input:...}` has no known value outside the editor and stays unresolved. Known local paths are inspected; `--config` supports other exported mappings and plugin `.mcp.json` files. Claude Code project-local entries in `.claude.json` are selected for the explicit project.

Files/scopes remain independent. A global entry and project entry with the same name do not prove two tools are loaded. A repeated endpoint is a review hint. Endpoints are compared after normalization, not as raw config fields: URL case, the loopback aliases, an explicit default port and a trailing slash all fold together, and a stdio entry launching a known transport wrapper (`mcp-remote`, `mcp-proxy`, `supergateway`) resolves to the URL it forwards to. A wrapper and a direct connection to one endpoint therefore group, and both carry `same_backend_reached_through_different_transports`, which says why. Path and query are preserved because they select a server. We do not implement a speculative universal merge policy. Plugin enablement, managed settings, profile overrides, OAuth stores, host trust gates, and remote host configuration need host-specific evidence. Missing coverage appears in every scan report.

## Measurements

| Field | Definition | Interpretation |
|---|---|---|
| always_loaded_tokens | Encoded advertised tool names | Floor across loading modes; excludes host framing and separators |
| always_loaded_basis | Whether names were counted bare or host-qualified | Qualified only where the convention is verified, never guessed |
| detail | `definitions` or `names_only` | A reduced capture measures the floor and leaves the ceiling null |
| instructions_tokens | Encoded server instructions | Host may trim, omit or present differently |
| core_tools_tokens | Canonical JSON array of selected name, description, inputSchema | Hypothetical eager model-facing surface |
| eager_projection_tokens | core_tools_tokens + instructions_tokens | Scenario only; neither bound on host usage |
| selected_wire_catalog_tokens | Canonical selected tool definitions including metadata/outputSchema | Overlaps core; never add the two |
| per-tool components | Independently encoded description/schema fields | Diagnostic breakdown; not additive exact attribution |
| configured_output_token_limit | Declared per-tool policy, when present | Not a measured response or verified enforcement |
| fingerprint | SHA-256 of sorted selected definitions plus instructions | Contract drift, not a malware verdict |
| runtime_output_tokens | null | Discovery cannot measure future tool output |

A capture whose tools all lack `inputSchema` is measured as `names_only`: the floor is
reported and every schema-derived field is `null`, never `0`. A capture where only some
tools carry a schema is malformed rather than reduced and is still rejected. A group
containing a floor-only server reports `projection_complete: false`, because its ceiling
is a partial sum.

`o200k_base` is the default named tokenizer proxy. `cl100k_base` is available for comparison. Neither is presented as Claude's actual tokenizer. Canonical serialization does not reproduce hidden provider framing or host conversion. Empty arrays still have serialization overhead.

The budget formula is `context_window - reserved_tokens - eager_projection_tokens`. A negative value is an exceeded **user-supplied scenario budget**, not an observed overflow. There is no universal 10-server / 50-tool danger threshold. The 1,000-token definition/instructions finding is a configurable-in-code review heuristic, not a model limit.

Reports omit raw config, URLs, commands, arguments, environment values, instructions, schema bodies, and exception text. `DOCTOR_DEBUG` prints a traceback for local debugging; it can disclose configuration values and server output and is never on by default. Server/tool names are hashed unless `--include-names` is requested. Hashes support local correlation; they are pseudonyms, not a promise of anonymity.

## Diagnosis

Reviewable items are derived in `diagnose.py` from the finished report and nowhere else.
A rule qualifies only if it holds without knowing the host's loading mode, conversation
history, prompt framing, or which tools the operator invokes. That excludes any absolute
verdict on context safety, and no item claims one.

What survives that filter is relative or factual: cost concentrated in a minority of a
server's tools, a description far outside the distribution this run measured, one backend
reached by more than one config entry, tools advertising no description, and the servers
that could not be measured at all. Items carry `impact_tokens` and are ordered by it, so
ranking is measured rather than asserted.

The verdict is `review_recommended`, `no_findings_in_measured_scope`, or
`nothing_measured`, and it is always printed with the coverage it rests on. Coverage is
stated first precisely so a verdict is not read as covering servers that were skipped.
Thresholds are module constants documented as review heuristics; a reader can disagree
with a number instead of reverse-engineering it.

## Failure behavior

Missing, disabled, malformed, unsupported, timeout, and failed discovery states are not counted as zero measured cost. SDK v2 reports a rejected HTTP handshake as an opaque error without its status, so a failed HTTP attempt is re-checked once against the same endpoint to separate `auth_required` from `probe_failed`. Only the status line is read; the body is discarded. That check runs after the failed attempt has unwound and never on a timeout. Incomplete groups remain incomplete. A live scan with missing measurements exits 2. Budget exit 3 takes precedence when the measured portion already exceeds its supplied budget. Static exit 0 means the command completed, not that the host is healthy.

Tool pagination uses opaque cursors, rejects cycles, and limits page count, tool count, and retained serialized catalog size. The catalog byte check occurs after the SDK decodes a page. It is not an OS-level memory cap or protection against hostile binaries. The timeout uses AnyIO cancellation plus SDK cleanup; it is not a full sandbox. Run untrusted targets in an OS/container sandbox or use offline captures.

## Future evidence-driven extensions

Host adapters can import an effective exported inventory or measured request usage. Runtime output receipts could record bytes/tokens without retaining sensitive text. Both should preserve the distinction between raw MCP data, host conversion, and provider-reported usage. These features are not implemented in 0.1.1.
