# Security and privacy

For a suspected vulnerability, use GitHub private vulnerability reporting if enabled, or contact the maintainer privately through the GitHub profile. Do not open an issue containing credentials, raw configs or private tool definitions.

Static scans read a bounded set of known files and do not connect to MCP servers. Live scans execute the selected configured commands and contact the selected URLs. A server can read local files, use credentials or mutate its environment on startup even when the doctor never calls a tool. This tool is not a process sandbox. Review the target command and use isolation for untrusted servers.

The runtime uses the official SDK's limited inherited environment plus explicitly configured variables. It does not search credential stores or perform OAuth. Static header values and explicitly referenced environment credentials are used only for the selected target. Redirect following and environment-based HTTP proxies are disabled; non-loopback cleartext HTTP is rejected. Loopback HTTP is supported for local servers. Legacy SSE must be inspected externally and imported as a capture in this release.

Default reports contain counts, fixed finding codes and pseudonymous IDs; raw names can be opted into. Definition fingerprints can reveal equality and low-entropy names may be guessable. Review reports before publication. Raw capture files and tool definitions can contain secrets or adversarial instructions; keep them outside the repository or in `.local/` (gitignored).

No LLM calls, paid token-count API calls, telemetry exports, automatic fixes or business-tool invocations are implemented. Package installation and the first tokenizer use may download public dependency/encoding data. SDK cancellation and post-decode size checks are best-effort resource controls, not a guarantee against a hostile process or oversized network message.
