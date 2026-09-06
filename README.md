# MCP Context Doctor

Local MCP inventory and tool-definition token diagnostics, with a portable Agent Skill for **Codex, Claude Code, Cursor, VS Code, and other skill-compatible agents**.

[日本語](README.ja.md) · [Design](docs/design.md) · [Existing tools](docs/existing-tools.md) · [Security](SECURITY.md)

Connecting many MCP servers can consume context, but server count alone does not tell you how much. Hosts that defer tool loading carry the advertised **names** and fetch a definition only when it is used, so a catalog's cost is a range, not a number. This CLI reports both ends: **always loaded** (the names, a floor) and the **eager projection** (every description and input schema, a ceiling). Actual host usage lies between them and is not measured.

```text
MCP Context Doctor | example catalog | o200k_base
Advertised tools        2
Selected tools          2
Always loaded           4 tokens   <- floor, paid in every loading mode
Core definitions      100 tokens
Server instructions    12 tokens
Eager projection      112 tokens   <- ceiling, only if the host loads them all
Actual host usage     unknown      <- lies between the two
```

Uses the official MCP Python SDK and tiktoken. The official MCP Inspector independently checks the integration fixture. No LLM API key is required, and the doctor never calls business tools.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) or pip.

```sh
git clone https://github.com/JunSuzuki1973/mcp-context-doctor.git
cd mcp-context-doctor
uv sync --locked
uv run mcp-context-doctor --help
```

For a CLI usable from any project:

```sh
uv tool install "git+https://github.com/JunSuzuki1973/mcp-context-doctor.git@v0.2.0"
```

Or, from a clone: `python -m pip install .`. This release is distributed on GitHub; it is **not published to PyPI**. The first tiktoken call can download/cache public encoding data. Subsequent offline use requires that cache.

## Quick start

Read known configurations without starting servers:

```sh
mcp-context-doctor scan --host claude-code --project /path/to/project
mcp-context-doctor scan --host codex --project /path/to/project --format json
```

PowerShell example:

```powershell
mcp-context-doctor scan --host codex --project 'C:\work\my-project'
```

Measure selected trusted servers. Use a server ID from the static report, or its exact configured name:

```sh
mcp-context-doctor scan --host claude-code --project /path/to/project --live --server my-server
```

Measure a server behind an authenticating endpoint. The flag takes a variable
**name**, never a token, and applies to this run only:

```sh
export GBRAIN_TOKEN=...
mcp-context-doctor scan --host claude-code --project /path --live \
  --server my-remote-server --bearer-env GBRAIN_TOKEN
```

The same can be stored in the server's config entry instead, as `bearer_token_env_var`
(a variable name holding the token) or `env_http_headers` (a header-name to
variable-name mapping). The doctor performs no OAuth flow and reads no credential
store, so a host that authenticated interactively holds a token the doctor cannot see.
When no token is available, export the catalog from an already-authenticated client and
pass it to `analyze`: a catalog carrying only tool names still measures the
always-loaded floor.

`--live` starts configured local commands or connects to remote URLs. Package launchers can download code; servers can have startup effects. Review the target first. Omitting `--server` selects every enabled server in the chosen configs. The doctor performs discovery, not `tools/call`.

Analyze an existing **complete** MCP Inspector export without connecting:

```sh
mcp-context-doctor analyze examples/tools-list.json --context-window 128000 --reserve 32000
mcp-context-doctor analyze /path/to/inspector-export.json --format json --output report.json
mcp-context-doctor diff before.json after.json
```

The example window is an illustrative budget, not a claimed model specification. Reports refuse to overwrite existing files. Default reports omit names, paths, URLs, raw schemas and credentials. Use `--include-names` for locally reviewed names.

## Supported scope

| Input / feature | Support |
|---|---|
| Codex `config.toml` | Server mappings, enabled flag, tool allow/deny lists |
| Claude Code | User `.claude.json`, selected project-local entries, project `.mcp.json` |
| Claude Desktop | Known Windows/macOS config path |
| Cursor | User/project `.cursor/mcp.json` |
| VS Code | User/project `mcp.json`, JSON comments/trailing commas, `${workspaceFolder}`/`${userHome}` |
| Other hosts / plugins | Explicit `--config` with `mcpServers`, `servers`, or `mcp_servers` mapping |
| Live transport | STDIO and Streamable HTTP, static/environment headers, `--bearer-env` |
| Offline capture | Raw `tools/list` or Inspector JSON envelope; a names-only catalog measures the floor |
| Schemas | Validation and separate input/output schema counts |
| Comparison | Stable fingerprints, token deltas, unknown-state handling |
| Report length | Verdict and items by default; `--verbose` restores the full methodology |

Config files/scopes stay separate: totals are **not** a combined active host context. Managed policies, plugin enablement, host precedence, OAuth credential stores, remote hosts and lazy-loading state are not automatically resolved. Supply an explicit trusted exported inventory when needed. Legacy SSE is currently an offline-import path.

## Install the portable Skill

Copy `skills/mcp-context-doctor/` as one folder into the host's supported skills directory. Install the CLI separately. The Skill requires standard shell access and the CLI; it does not depend on Codex-private APIs.

| Host | Example personal Skill directory |
|---|---|
| Codex | `~/.codex/skills/mcp-context-doctor/` |
| Claude Code | `~/.claude/skills/mcp-context-doctor/` |
| Other Agent Skills hosts | Their documented skills directory |

Codex example: `Use $mcp-context-doctor to inspect this project's MCP configuration.`

Claude Code example: `/mcp-context-doctor Diagnose this project's MCP context cost.`

GitHub releases include `mcp-context-doctor-skill.zip`. Cursor/VS Code configuration support does not imply every edition supports the same Skill installation path.

## What the report says

Reports open with a verdict, the coverage it rests on, and the items worth acting on,
ordered by measured impact:

```text
## Review recommended -- 2 items

Measured 2 of 5 enabled servers. 3 were not measured, so no total below is complete.

Always loaded across measured servers: 326 tokens. This is the floor a host carries
in every loading mode.

1. blender (2575 tokens) -- 7 of 28 tools carry 43% of this server's definitions. If
   they are not used, excluding them in the host's tool filter removes about 2575
   tokens from an eager load.
2. configuration -- 3 enabled servers were not measured (auth_required). Their cost is
   unknown, not zero, so no total here is complete. ...
```

Every item is derived only from what this run measured, so each holds without knowing
the host's loading mode, history or which tools you actually use:

| Item | Basis |
|---|---|
| `definition_cost_concentrated_in_few_tools` | A minority of tools carries most of a server's definitions, and a host tool filter can act on exactly that minority |
| `tool_description_far_above_median` | Relative to the median of everything measured in this run |
| `one_backend_configured_more_than_once` | Endpoints compared after normalization, including through proxy wrappers |
| `enabled_servers_not_measured` | Coverage; the advice differs by reason (`not_probed` vs `auth_required`) |
| `tools_without_a_description` | The model has only the name to select on |

Thresholds are review heuristics, named as constants in `diagnose.py`, not limits
derived from any model or host. **No item asserts that a context window will overflow**,
and `nothing_measured` is never a clean bill of health.

## Interpret results

- `always_loaded_tokens`: the advertised tool names. A host carries these whether or not it has loaded the definitions, so this is a floor that holds under deferred loading. It is not a total: host framing, separators and built-in instructions are excluded. `always_loaded_basis` says whether names were counted bare or with the host's `mcp__<server>__` prefix, which is applied only for hosts whose convention is verified.
- `eager_projection_tokens`: selected names/descriptions/input schemas plus server instructions, measured as canonical JSON/text. This is the ceiling, and it is `null` when the capture carried no input schemas.
- `selected_wire_catalog_tokens`: catalog including output schemas and metadata. This overlaps the first metric; **do not add them**.
- `runtime_output_tokens: null`: tool output is unmeasured; large responses can dominate context even with small definitions.
- Named tokenizer proxies (`o200k_base`, `cl100k_base`) are not exact Claude counts or provider usage reports.
- A timeout, missing server or incomplete page is unknown, not zero. Static scan completion does not mean the host is healthy.
- `auth_required` means the endpoint rejected an unauthenticated request; the doctor performs no OAuth. Supply credentials through the config's referenced environment variables.
- Errors are reported as a fixed code so that raw config values and server text cannot leak. Set `DOCTOR_DEBUG=1` for a local traceback.
- `--fail-on-budget` exits 3 only for a supplied eager scenario budget. There is no universal server-count threshold.

Exit codes: **0** completed; **2** invalid input or incomplete live collection; **3** supplied projection budget exceeded. See [design](docs/design.md) for formulas and limitations.

## Develop

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

Tests use isolated local servers and synthetic definitions, including pagination, timeouts, non-execution, redaction, filters, schema errors, and incomplete collections. [Inspector cross-check](docs/existing-tools.md) is optional for contributors.

MIT licensed. Independent project; not affiliated with or endorsed by MCP maintainers, OpenAI, or Anthropic.
