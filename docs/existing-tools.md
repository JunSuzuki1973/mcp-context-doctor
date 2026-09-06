# Existing tools used and evaluated

Reviewed 2026-09-06. This project is independent and is not an official MCP, OpenAI or Anthropic product.

## Reused

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): runtime client, protocol negotiation, stdio/HTTP transport and cleanup. Installed version is fixed by `uv.lock`. No transport implementation was copied.
- [OpenAI tiktoken](https://github.com/openai/tiktoken): local token counts with an explicitly named encoding. Its initial encoding data download is distinct from an LLM API request.
- [Official MCP Inspector](https://github.com/modelcontextprotocol/inspector): independent integration check and supported offline input format. Inspector 2.5.0 is a development tool, not a runtime dependency.

Install Inspector in an isolated development directory and compare both clients against the harmless paginated fixture:

```sh
npm install --prefix .local/inspector --ignore-scripts @modelcontextprotocol/inspector@2.5.0
uv run python scripts/check_inspector.py --inspector-js .local/inspector/node_modules/@modelcontextprotocol/inspector/clients/launcher/build/index.js
```

The check compares both tool catalogs and core token projections. It excludes instructions because Inspector's `tools/list` output does not contain initialization instructions. No business tool is called.

To analyze a complete Inspector JSON export:

```sh
mcp-context-doctor analyze /path/to/inspector-export.json --format json
```

A raw result `{ "tools": [...] }` or an envelope `{ "result": { "tools": [...] } }` is accepted. `nextCursor` means the export is incomplete and is rejected. Add a top-level `instructions` string only if captured from the same server/session initialization.

## Alternatives

| Project | Useful capability | Why it is not embedded |
|---|---|---|
| [tjhop/mcp-token-analyzer](https://github.com/tjhop/mcp-token-analyzer) | Multi-server token breakdown, instructions, schemas | Additional Go runtime/tool; uses its own counting and config assumptions |
| [EnjoyableWork/mcp-doctor](https://github.com/EnjoyableWork/mcp-doctor) | Protocol/schema inspection and reviewed active testing | Broader protocol-testing purpose; active modes can call real tools |
| [Mariomarquezt/mcp-doctor](https://github.com/Mariomarquezt/mcp-doctor) | PC config discovery and connectivity checks | Reachability is different from model-context measurement |
| [ytkoka/mcp-tester](https://github.com/ytkoka/mcp-tester) | Web interface, definition comparison, provider token-count option | Additional service/API-key workflow; not needed for local first version |

Alternative projects were reviewed as documentation, not independently audited or executed. This repository does not claim their token estimates are exact host usage. No code from them is vendored. Third-party dependencies retain their own licenses.

## Host behavior references

- [MCP Tools specification](https://modelcontextprotocol.io/specification/draft/server/tools): discovery, pagination and tool definitions. The draft URL changes; record the negotiated revision when measuring.
- [MCP Python SDK migration](https://py.sdk.modelcontextprotocol.io/migration/): 2.x and 2026-07-28 compatibility changes.
- [Claude Code MCP](https://code.claude.com/docs/en/mcp): Tool Search, provider fallbacks, `ENABLE_TOOL_SEARCH`, and `alwaysLoad`.
- [OpenAI Tool Search](https://developers.openai.com/api/docs/guides/tools-tool-search): deferred loading is an API/host behavior.
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp): server instructions, tool allow/deny lists, and plugin settings.
- [GitHub Copilot CLI Tool Search](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/tool-search): on-demand loading and loaded-tool retention.

Absence of a setting in documentation is not proof a host eagerly loads all tools. An eager projection must never be relabeled as a verified host-specific measurement.
