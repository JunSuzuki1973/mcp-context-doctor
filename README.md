# MCP Context Doctor

Local MCP inventory and tool-definition token diagnostics, with a portable Agent Skill for **Codex, Claude Code, Cursor, VS Code, and other skill-compatible agents**.

[日本語](README.ja.md) · [Design](docs/design.md) · [Existing tools](docs/existing-tools.md) · [Security](SECURITY.md)

Connecting many MCP servers can consume context, but server count alone does not tell you how much. This CLI measures advertised definitions and labels the result as an **eager-loading projection**, not actual host usage or an overflow prediction. Hosts with Tool Search can load only a subset.

```text
MCP Context Doctor · example catalog · o200k_base
Advertised tools        2
Selected tools          2
Core definitions      100 tokens
Server instructions    12 tokens
Eager projection      112 tokens
Actual host usage     unknown
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
uv tool install "git+https://github.com/JunSuzuki1973/mcp-context-doctor.git@v0.1.0"
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
| Live transport | STDIO and Streamable HTTP, static/environment headers |
| Offline capture | Raw `tools/list` or Inspector JSON envelope |
| Schemas | Validation and separate input/output schema counts |
| Comparison | Stable fingerprints, token deltas, unknown-state handling |

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

## Interpret results

- `eager_projection_tokens`: selected names/descriptions/input schemas plus server instructions, measured as canonical JSON/text.
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
