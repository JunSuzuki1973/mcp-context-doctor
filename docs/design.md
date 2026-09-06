# Design and measurement contract

Version 0.1.0 uses a CLI plus an Agent Skill. It does not add another always-connected MCP server.

## Data flow

Known config files or explicit config → independent server inventory → optional SDK discovery → local tokenizer → redacted report → optional report diff.

Static mode never launches servers. Live mode selects configured commands/URLs and uses the official MCP Python SDK 2.x. The SDK negotiates the protocol; the report records the actual revision. Legacy stdio and modern Streamable HTTP are tested separately. There is no `tools/call`, resource read, prompt rendering, sampling callback or elicitation callback in the collector.

## Scope and precedence

Codex TOML, Claude/Cursor `mcpServers`, and VS Code `servers` mappings are accepted. JSON comments and trailing commas are supported through json5. Known local paths are inspected; `--config` supports other exported mappings and plugin `.mcp.json` files. Claude Code project-local entries in `.claude.json` are selected for the explicit project.

Files/scopes remain independent. A global entry and project entry with the same name do not prove two tools are loaded. A repeated endpoint is a review hint. We do not implement a speculative universal merge policy. Plugin enablement, managed settings, profile overrides, OAuth stores, host trust gates, and remote host configuration need host-specific evidence. Missing coverage appears in every scan report.

## Measurements

| Field | Definition | Interpretation |
|---|---|---|
| instructions_tokens | Encoded server instructions | Host may trim, omit or present differently |
| core_tools_tokens | Canonical JSON array of selected name, description, inputSchema | Hypothetical eager model-facing surface |
| eager_projection_tokens | core_tools_tokens + instructions_tokens | Scenario only; neither bound on host usage |
| selected_wire_catalog_tokens | Canonical selected tool definitions including metadata/outputSchema | Overlaps core; never add the two |
| per-tool components | Independently encoded description/schema fields | Diagnostic breakdown; not additive exact attribution |
| configured_output_token_limit | Declared per-tool policy, when present | Not a measured response or verified enforcement |
| fingerprint | SHA-256 of sorted selected definitions plus instructions | Contract drift, not a malware verdict |
| runtime_output_tokens | null | Discovery cannot measure future tool output |

`o200k_base` is the default named tokenizer proxy. `cl100k_base` is available for comparison. Neither is presented as Claude's actual tokenizer. Canonical serialization does not reproduce hidden provider framing or host conversion. Empty arrays still have serialization overhead.

The budget formula is `context_window - reserved_tokens - eager_projection_tokens`. A negative value is an exceeded **user-supplied scenario budget**, not an observed overflow. There is no universal 10-server / 50-tool danger threshold. The 1,000-token definition/instructions finding is a configurable-in-code review heuristic, not a model limit.

Reports omit raw config, URLs, commands, arguments, environment values, instructions, schema bodies, and exception text. Server/tool names are hashed unless `--include-names` is requested. Hashes support local correlation; they are pseudonyms, not a promise of anonymity.

## Failure behavior

Missing, disabled, malformed, unsupported, timeout, and failed discovery states are not counted as zero measured cost. Incomplete groups remain incomplete. A live scan with missing measurements exits 2. Budget exit 3 takes precedence when the measured portion already exceeds its supplied budget. Static exit 0 means the command completed, not that the host is healthy.

Tool pagination uses opaque cursors, rejects cycles, and limits page count, tool count, and retained serialized catalog size. The catalog byte check occurs after the SDK decodes a page. It is not an OS-level memory cap or protection against hostile binaries. The timeout uses AnyIO cancellation plus SDK cleanup; it is not a full sandbox. Run untrusted targets in an OS/container sandbox or use offline captures.

## Future evidence-driven extensions

Host adapters can import an effective exported inventory or measured request usage. Runtime output receipts could record bytes/tokens without retaining sensitive text. Both should preserve the distinction between raw MCP data, host conversion, and provider-reported usage. These features are not implemented in 0.1.0.
