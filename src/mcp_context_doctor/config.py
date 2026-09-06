"""Read known configuration files without starting servers or retrieving credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import json5

MAX_FILE_BYTES = 8 * 1024 * 1024


def identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def read_data(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    text = raw.decode("utf-8-sig")
    # Hosts create an empty config file before anything is configured. That is an
    # empty inventory, not an unreadable file; reporting it as invalid would flag a
    # healthy machine as having incomplete coverage.
    if not text.strip():
        return {}
    data = tomllib.loads(text) if path.suffix == ".toml" else json5.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected_object")
    return data


# Commands whose job is to forward a local stdio session to a remote MCP endpoint.
# When one of these launches a server, the backend is the URL it is pointed at, not
# the wrapper process, and it is the same backend another entry may reach directly.
PROXY_COMMANDS = frozenset(
    {"mcp-remote", "mcp-proxy", "supergateway", "mcp-superassistant-proxy", "mcpremote"}
)
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})
DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_argv(argv: list[str]) -> str:
    return json.dumps(argv, sort_keys=True)


def normalize_endpoint(url: str) -> str | None:
    """Canonical identity for an MCP URL, or None when it is not one.

    Folds the spellings that denote one endpoint - case, the loopback aliases, an
    explicit default port, a trailing slash. Query and path are preserved: they can
    select a different server on the same host.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    host = "localhost" if host in LOOPBACK else host
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, host, path, parts.query, ""))


def proxied_endpoint(argv: list[str]) -> str | None:
    """The URL a proxy wrapper is pointed at, when the argv is such a wrapper."""
    tokens = [t for t in argv if isinstance(t, str)]
    names = {t.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].split("@")[0].lower() for t in tokens}
    if not (names & PROXY_COMMANDS):
        return None
    for token in tokens:
        if endpoint := normalize_endpoint(token):
            return endpoint
    return None


@dataclass
class Server:
    name: str
    config: dict[str, Any]
    source: str
    host: str = "generic"
    scope: str = "explicit"
    enabled: bool = True
    notes: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return "server-" + identity(self.source + "\0" + self.scope + "\0" + self.name)

    @property
    def group(self) -> str:
        return "config-" + identity(self.source + "\0" + self.scope)

    @property
    def endpoint(self) -> tuple[str, str] | None:
        """(kind, identity) naming the backend this entry reaches, or None.

        Two entries sharing an identity reach the same server even when their
        transports differ, which is what a proxy wrapper produces.
        """
        cfg = self.config
        if self.transport == "http":
            if url := normalize_endpoint(str(cfg.get("url", ""))):
                return ("url", url)
            return None
        if self.transport == "stdio":
            argv = [str(cfg.get("command", ""))] + [
                a for a in cfg.get("args", []) if isinstance(a, str)
            ]
            if url := proxied_endpoint(argv):
                return ("url", url)
            return ("command", canonical_argv(argv))
        return None

    @property
    def tool_name_prefix(self) -> str | None:
        """Prefix a host prepends to every advertised tool name, when it is known.

        Verified for the Claude hosts, which expose `mcp__<server>__<tool>`. Other
        hosts are left unprefixed rather than guessed: an invented convention would
        put a fabricated number in the floor.
        """
        if self.host in ("claude-code", "claude-desktop"):
            return f"mcp__{self.name}__"
        return None

    @property
    def transport(self) -> str:
        kind = self.config.get("type", "")
        if "url" in self.config and "command" in self.config:
            return "unresolved"
        if kind not in ("", "stdio", "http", "streamable-http", "sse"):
            return "unresolved"
        if kind == "sse":
            return "sse"
        if "url" in self.config:
            return "http"
        if "command" in self.config:
            return "stdio"
        return "unresolved"

    def public(self, include_names: bool = False) -> dict:
        result = {
            "id": self.key,
            "group": self.group,
            "host": self.host,
            "scope": self.scope,
            "enabled": self.enabled,
            "transport": self.transport,
            "findings": list(self.notes),
        }
        if include_names:
            # JSON escapes control characters; Markdown renderer escapes markup separately.
            result["name"] = self.name[:160]
        return result


def parse_config(path: Path, host: str = "generic", project: Path | None = None) -> list[Server]:
    data = read_data(path)
    source = str(path.resolve())
    rows: list[Server] = []
    # Editor-predefined variables are resolved from the selected project, not the
    # environment. Without them a VS Code entry using ${workspaceFolder} is reported
    # as unresolved even though its value is known.
    predefined = predefined_variables(project)
    if path.suffix == ".toml":
        host = "codex" if host == "generic" else host
    mappings = [
        ("explicit", data.get("mcp_servers", data.get("mcpServers", data.get("servers", {}))))
    ]
    if host == "claude-code" and project:
        for key, value in data.get("projects", {}).items():
            if Path(key).resolve() == project.resolve() and isinstance(value, dict):
                mappings.append(("project-local", value.get("mcpServers", {})))
    for scope, servers in mappings:
        if not isinstance(servers, dict):
            raise ValueError("invalid_servers_mapping")
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                raise ValueError("invalid_server_entry")
            for flag in ("enabled", "disabled", "alwaysLoad"):
                if flag in cfg and not isinstance(cfg[flag], bool):
                    raise ValueError("invalid_boolean_flag")
            enabled = (
                cfg.get("enabled", True) is not False and cfg.get("disabled", False) is not True
            )
            row = Server(str(name), cfg, source, host, scope, enabled, variables=predefined)
            if row.transport == "stdio" and project:
                # Launch in the selected project, not the doctor's own checkout.
                cfg = dict(cfg)
                configured_cwd = cfg.get("cwd")
                if configured_cwd is None:
                    cfg["cwd"] = str(project.resolve())
                    row.notes.append("launch_cwd_from_selected_project")
                elif not isinstance(configured_cwd, str):
                    raise ValueError("invalid_cwd")
                elif not Path(configured_cwd).is_absolute() and "${" not in configured_cwd:
                    cfg["cwd"] = str((project / configured_cwd).resolve())
                row.config = cfg
            if "envFile" in cfg:
                row.notes.append("env_file_not_loaded")
            if cfg.get("oauth"):
                row.notes.append("oauth_credentials_not_reused")
            if row.transport == "unresolved":
                row.notes.append("transport_not_resolved")
            if any(k in cfg for k in ("env", "headers", "http_headers")):
                row.notes.append("configured_values_omitted_from_report")
            rows.append(row)
    return rows


def predefined_variables(project: Path | None) -> dict[str, str]:
    values = {"userHome": str(Path.home()), "pathSeparator": os.sep}
    if project is not None:
        resolved = project.resolve()
        values["workspaceFolder"] = str(resolved)
        values["workspaceFolderBasename"] = resolved.name
    return values


def candidates(home: Path, project: Path, appdata: Path | None = None) -> list[tuple[str, Path]]:
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    paths = [
        ("codex", codex / "config.toml"),
        ("codex", project / ".codex/config.toml"),
        ("claude-code", home / ".claude.json"),
        ("claude-code", project / ".mcp.json"),
        ("cursor", home / ".cursor/mcp.json"),
        ("cursor", project / ".cursor/mcp.json"),
        ("vscode", project / ".vscode/mcp.json"),
    ]
    if appdata:
        paths += [
            ("claude-desktop", appdata / "Claude/claude_desktop_config.json"),
            ("vscode", appdata / "Code/User/mcp.json"),
        ]
    elif sys.platform == "darwin":
        support = home / "Library/Application Support"
        paths += [
            ("claude-desktop", support / "Claude/claude_desktop_config.json"),
            ("vscode", support / "Code/User/mcp.json"),
        ]
    else:
        paths += [("vscode", home / ".config/Code/User/mcp.json")]
    return paths


def inventory(paths: list[tuple[str, Path]], project: Path) -> tuple[list[Server], list[dict]]:
    servers, sources = [], []
    seen = set()
    for host, path in paths:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        source = {"id": "config-" + identity(resolved), "host": host}
        if not path.is_file():
            source["status"] = "not_found"
        else:
            try:
                servers.extend(parse_config(path, host, project))
                source["status"] = "read"
            except (ValueError, OSError, TypeError, AttributeError, RecursionError):
                source["status"] = "unreadable_or_invalid"
        sources.append(source)
    endpoints: dict[tuple[str, str], list[Server]] = {}
    for server in servers:
        if backend := server.endpoint:
            endpoints.setdefault(backend, []).append(server)
    for backend, group in endpoints.items():
        if len(group) < 2:
            continue
        transports = {s.transport for s in group}
        for server in group:
            server.notes.append("repeated_endpoint_not_proof_of_duplicate_loading")
            # Naming the reason matters: a wrapper and a direct connection look like
            # two unrelated servers in the config but are one process to measure.
            if backend[0] == "url" and len(transports) > 1:
                server.notes.append("same_backend_reached_through_different_transports")
    return servers, sources


_VARIABLE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):

        def replace(match):
            key, fallback = match.groups()
            if key in env:
                return env[key]
            if fallback is not None:
                return fallback
            raise ValueError("unresolved_environment")

        text = _VARIABLE.sub(replace, value)
        if "${" in text:
            raise ValueError("unsupported_variable")
        return text
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


def effective_tools(tools: list[dict], cfg: dict) -> tuple[list[dict], list[str]]:
    allow = cfg.get("enabled_tools")
    deny = cfg.get("disabled_tools", cfg.get("disabledTools", []))
    if allow is not None and (
        not isinstance(allow, list) or not all(isinstance(x, str) for x in allow)
    ):
        raise ValueError("invalid_allowlist")
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise ValueError("invalid_denylist")
    names = {t["name"] for t in tools}
    notes = ["filter_names_not_advertised"] if (set(allow or []) | set(deny)) - names else []
    return [
        t for t in tools if (allow is None or t["name"] in allow) and t["name"] not in deny
    ], notes
