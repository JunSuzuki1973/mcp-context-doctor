# Validation record

Local validation date: 2026-09-07 (0.2.1). Test data is synthetic. This is a scoped engineering record, not security certification.

- Windows / Python 3.11.15 and 3.14.4: unit and integration suite passes (73 tests), including legacy `2025-11-25` STDIO and current `2026-07-28` Streamable HTTP discovery.
- 0.1.1 fixes were each confirmed to fail before the change and pass after it. The non-UTF-8 console regression was reproduced on a live cp932 console, and the `auth_required` classification against a real OAuth-protected local endpoint.
- 0.2.0 was measured against a live multi-host configuration (Codex, Claude Code, Claude Desktop) holding stdio, HTTP and proxy-wrapped entries. The always-loaded floor ran 19x and 62x below the eager projection for the two servers measurable without credentials, a 89-tool catalog carrying no input schemas was measured to its floor, and one backend reached both directly and through `mcp-remote` was grouped as a repeat. `--bearer-env` was exercised against a guarded local endpoint: unauthenticated discovery reports `auth_required`, and the same endpoint is measured once the named variable holds the token.
- 0.2.1 was checked against the same configuration: each `--loading` value headlines its own figure without changing any measurement, and `output_bound` reported 27 of 28 tools on one live server as declaring neither a result-limiting parameter nor a configured output limit. No `tools/call` was issued at any point.
- SDK: `mcp==2.1.1`; tokenizer: `tiktoken==0.14.0`; full environment is pinned in `uv.lock`.
- Official MCP Inspector 2.5.0 independently returns the same two tools across the paginated test fixture. Both measurements produce **46 core definition tokens** with `o200k_base`. Instructions are excluded from this cross-check because Inspector's tools/list export does not include them.
- The standalone synthetic example produces **100 core definition tokens + 12 instruction tokens = 112 eager projection tokens**. It is not a measurement of any user's PC or of a provider request.
- Tests cover config formats, explicit project scopes, empty allow lists, deny-list precedence, disabled-server non-execution, environment interpolation, editor-predefined variables, empty config files, credential omission, output overwrite refusal, stable fingerprints, malformed schemas, incomplete captures, pagination cycles, timeouts, non-UTF-8 console rendering, and HTTP authorization failure separated from a server error.
- Portable Skill frontmatter is validated using the skill-creator validator. CLI-only and Skill-only packaging are separate; Skill installation requires the CLI.

GitHub Actions runs the suite on Windows, macOS and Linux, with Python 3.11 and 3.14. The Actions result for the exact public commit is the authority for those environments; a local Windows pass alone does not establish macOS/Linux support.

Reproduce with `uv sync --locked`, `uv run pytest -q`, and the Inspector command in [existing-tools.md](existing-tools.md). No production MCP business tools are called during this validation.
