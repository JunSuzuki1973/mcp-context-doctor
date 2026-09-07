# A worked run

Recorded 2026-09-07 with v0.2.1 against a real machine: Windows 11, Python 3.11.15,
`o200k_base`, four MCP hosts configured (Codex, Claude Code, Claude Desktop, plus an
empty VS Code entry). Every figure below is output the tool produced, not an
illustration. Server and tool names are the machine owner's own, published here with
their consent; nothing else about the machine is disclosed, which is the point of the
next section.

## 1. Static scan: what is configured

A static scan reads known config files and starts nothing.

```sh
mcp-context-doctor scan --host all --project 'C:\Users\PC'
```

```text
# MCP Context Doctor

## Nothing was measured

Measured 0 of 10 enabled servers. 10 were not measured, so no total below is complete.

Server and tool names are hashed by default. Re-run with --include-names for a locally
readable report; review before sharing it.

1. **configuration** -- 10 enabled servers were not measured (not_probed). Their cost is
   unknown, not zero, so no total here is complete. A static scan reads configuration and
   starts nothing. Re-run with --live and a --server selection to measure the ones you
   trust.
2. **configuration** -- 8 config entries resolve to a backend that another entry also
   reaches. Separate hosts each pay for their own connection; this is not proof that one
   host loads it twice. Remove the entry a host no longer needs.
```

Three things to read here.

`Nothing was measured` is the verdict, and it is **not** a clean bill of health. A static
scan produces no token figures at all; every server is `not_probed`. The advice says so
and points at the next step.

The advice matches the reason. A `not_probed` server needs `--live`, not a credential.
A server that answered `auth_required` gets different advice.

Item 2 is the finding a config-field comparison misses. Adding `--include-names` shows
what it caught:

```text
| Server    | Host           | Status     | Selected tools | Always loaded | Eager projection |
|-----------|----------------|------------|---------------:|--------------:|-----------------:|
| node_repl | codex          | not_probed |              - |             - |                - |
| context7  | codex          | not_probed |              - |             - |                - |
| gbrain    | codex          | disabled   |              - |             - |                - |
| recall    | codex          | not_probed |              - |             - |                - |
| upwork    | codex          | not_probed |              - |             - |                - |
| context7  | claude-code    | not_probed |              - |             - |                - |
| gbrain    | claude-code    | not_probed |              - |             - |                - |
| ...       |                |            |                |               |                  |
```

`gbrain` appears three times. Under Claude Code it is an HTTP URL; under Claude Desktop
it is `cmd /c npx -y mcp-remote <the same URL>`; under Codex it is a separate disabled
stdio entry. The first two are one backend reached two ways, and both carry
`same_backend_reached_through_different_transports`. Comparing `command`, `args` and
`url` as literal fields would have called them unrelated servers.

## 2. Live measurement: the floor and the ceiling

`--live` starts the configured commands and connects to the configured URLs. It performs
discovery only and never calls a tool.

```sh
mcp-context-doctor scan --host claude-code --project 'C:\Users\PC' \
  --live --include-names --loading deferred --context-window 200000
```

```text
## Review recommended -- 3 items

Measured 2 of 5 enabled servers. 3 were not measured, so no total below is complete.

Declared loading: deferred. Across measured servers the names cost **326 tokens**; the
7076-token eager projection applies only to definitions the host actually loads.

1. **blender** (2575 tokens) -- 7 of 28 tools carry 43% of this server's definitions. If
   they are not used, excluding them in the host's tool filter removes about 2575 tokens
   from an eager load.
2. **configuration** -- 3 enabled servers were not measured (auth_required). Their cost
   is unknown, not zero, so no total here is complete. The endpoint rejected an
   unauthenticated request. Pass --bearer-env NAME to read a token from that environment
   variable for this run, set bearer_token_env_var or env_http_headers in the config, or
   export the catalog from an already-authenticated client and analyze that. A catalog
   carrying only names still measures the always-loaded floor.
3. **blender** -- 27 of 28 tools declare neither a result-limiting parameter nor a
   configured output token limit. Tool output is not measured here and no size is
   implied; this is what the catalog and config declare. Where the host supports a
   per-tool output limit, setting one bounds the case a large definition budget does not
   protect against.

| Server   | Host        | Status        | Selected tools | Always loaded | Eager projection |
|----------|-------------|---------------|---------------:|--------------:|-----------------:|
| context7 | claude-code | measured      |              2 |            18 |             1110 |
| gbrain   | claude-code | auth_required |              - |             - |                - |
| recall   | claude-code | auth_required |              - |             - |                - |
| upwork   | claude-code | auth_required |              - |             - |                - |
| blender  | claude-code | measured      |             28 |           308 |             5966 |
```

The two columns are the whole point:

| Server | Tools | Always loaded | Eager projection | Ratio |
|---|---:|---:|---:|---:|
| context7 | 2 | 18 | 1,110 | 62x |
| blender | 28 | 308 | 5,966 | 19x |

A host that defers loading carries the names and fetches a definition when it is used.
Reporting only the projection would overstate what such a host pays by one to two orders
of magnitude. Reporting only the floor would understate a host that loads everything.
Neither is "the" number, so both are printed and `--loading` says which to headline.
Actual host usage is not measured and lies between them.

Three of five servers are `auth_required`. **That is unknown, not zero**, and the report
refuses to present any total as complete while it is true: `collection_complete: false`,
`projection_complete: false` on both groups, and exit code 2.

## 3. Why blender costs what it does

The per-tool breakdown, from `--format json`:

```text
  445  download_polypizza_model            desc=328  schema= 86  bound=False
  433  generate_hyper3d_model_via_images   desc=288  schema=113  bound=False
  403  search_polypizza_models             desc=267  schema=105  bound=True
  359  download_sketchfab_model            desc=265  schema= 63  bound=False
  326  generate_hunyuan3d_model            desc=230  schema= 69  bound=False
```

Descriptions run three to four times the input schemas. This server's cost is prose, not
parameter complexity, and the seven largest tools are all 3D asset acquisition. That is
what makes item 1 actionable: if this operator does not use Sketchfab, Hyper3D, Hunyuan3D
or PolyPizza, a host-side tool filter removes 2,575 tokens without touching the tools
they do use.

The comparison is worth noting. `gbrain` advertises 89 tools whose descriptions average
46 tokens; `blender` advertises 28 averaging 133. Tool count is a poor predictor of cost.
Description length is the lever.

## 4. A catalog behind an authenticating endpoint

`gbrain` runs on `localhost` and still requires OAuth, because several separate clients
connect to one instance. The doctor performs no OAuth flow and reads no credential store,
so it cannot reach the catalog — but the server's own CLI can export one, in a reduced
form that carries names and descriptions but no input schemas.

```sh
mcp-context-doctor analyze gbrain-catalog.json --include-names
```

```text
| Server               | Host    | Status   | Selected tools | Always loaded | Eager projection |
|----------------------|---------|----------|---------------:|--------------:|-----------------:|
| server-aa563ffd2b84  | generic | measured |             89 |           238 |          unknown |

1. **server-aa563ffd2b84** (172 tokens) -- This description is 5.2x the median across
   everything measured here. Descriptions are paid on every eager load; a shorter one is
   a change for the server author, not the operator.
2. **configuration** -- 1 servers were measured to their always-loaded floor only. The
   floor is exact; the eager projection for them is unknown.
```

The floor is measured exactly. The ceiling reads `unknown`, **not `0`** — a catalog that
could not be measured in full is not a free server. Note also that the outlier rule still
works on a reduced capture, because it needs only descriptions.

Earlier versions rejected this file outright for lacking `inputSchema`, which meant the
89-tool server contributed nothing at all to any report.

## 5. Exit codes from this session

| Command | Exit | Why |
|---|---:|---|
| static scan | 0 | The command completed. It says nothing about the host's health. |
| live scan | 2 | Three servers unmeasured, so the collection is incomplete. |
| analyze, reduced catalog | 0 | The capture was measured to the detail it carried. |

Exit 0 from a static scan is the one most likely to be misread: it means the command ran,
not that anything was measured.

## What this run does not tell you

- **Actual host context usage.** Not measured, in any mode. The floor and the ceiling
  bracket it; nothing here narrows it further.
- **Tool output.** `runtime_output_tokens` is null and always will be: discovery never
  issues `tools/call`, so no response exists to count. Item 3 above reports what the
  catalog *declares* about bounding, and no size.
- **Whether these totals coexist.** The two config groups are separate scopes. Codex and
  Claude Code each pay for their own connection; the figures are not added.
- **Anything about the three unmeasured servers**, including whether they are the largest
  cost here. On the evidence available, they might be.
