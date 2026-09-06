# Validation record

Local validation date: 2026-09-06. Test data is synthetic. This is a scoped engineering record, not security certification.

- Windows / Python 3.14.4: unit and integration suite passes, including legacy `2025-11-25` STDIO and current `2026-07-28` Streamable HTTP discovery.
- SDK: `mcp==2.1.1`; tokenizer: `tiktoken==0.14.0`; full environment is pinned in `uv.lock`.
- Official MCP Inspector 2.5.0 independently returns the same two tools across the paginated test fixture. Both measurements produce **46 core definition tokens** with `o200k_base`. Instructions are excluded from this cross-check because Inspector's tools/list export does not include them.
- The standalone synthetic example produces **100 core definition tokens + 12 instruction tokens = 112 eager projection tokens**. It is not a measurement of any user's PC or of a provider request.
- Tests cover config formats, explicit project scopes, empty allow lists, deny-list precedence, disabled-server non-execution, environment interpolation, credential omission, output overwrite refusal, stable fingerprints, malformed schemas, incomplete captures, pagination cycles, timeouts and HTTP authorization failure.
- Portable Skill frontmatter is validated using the skill-creator validator. CLI-only and Skill-only packaging are separate; Skill installation requires the CLI.

GitHub Actions runs the suite on Windows, macOS and Linux, with Python 3.11 and 3.14. The Actions result for the exact public commit is the authority for those environments; a local Windows pass alone does not establish macOS/Linux support.

Reproduce with `uv sync --locked`, `uv run pytest -q`, and the Inspector command in [existing-tools.md](existing-tools.md). No production MCP business tools are called during this validation.
