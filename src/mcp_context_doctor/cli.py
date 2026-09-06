"""Agent-neutral CLI; stdout is a compact report, never a raw tool catalog."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import anyio

from . import __version__
from .config import Server, candidates, inventory, read_data
from .diagnose import diagnose
from .measure import Counter, analyze, budget, compare
from .probe import error_code, probe

COVERAGE = [
    "Configuration inventory is not the host's effective runtime inventory.",
    "Groups are independent config scopes; do not sum them as simultaneous host usage.",
    "Plugin manifests, managed policies, trust gates, caches and remote hosts are not auto-resolved.",
    "Use --config for a trusted exported/merged inventory or an explicit plugin .mcp.json.",
]
METHOD = [
    "Always loaded counts the advertised tool names only; a host carries them in every loading mode.",
    "It is a floor, not a total: host framing, separators and built-in instructions are not included.",
    "Eager projection counts canonical JSON of selected name/description/inputSchema plus instructions.",
    "Selected wire catalog separately includes outputSchema, annotations and metadata; hosts may omit them.",
    "This is neither a lower bound nor an upper bound for actual host context usage.",
    "Prompt framing, history, built-ins, skills, loaded subset, resources and tool outputs are unknown.",
    "The tokenizer is a named proxy; it is not Claude's tokenizer or a provider usage measurement.",
    "Missing output-bound parameters are review hints, not proof of unbounded output.",
]


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MCP inventory and context-budget projections. No LLM API key needed."
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("scan", "analyze"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--encoding", choices=["o200k_base", "cl100k_base"], default="o200k_base")
        cmd.add_argument("--context-window", type=positive)
        cmd.add_argument("--reserve", type=int, default=0)
        cmd.add_argument("--format", choices=["markdown", "json"], default="markdown")
        cmd.add_argument("--output", type=Path, help="Create a new report file; refuses overwrite.")
        cmd.add_argument(
            "--include-names",
            action="store_true",
            help="Include server/tool names; review before sharing.",
        )
        cmd.add_argument(
            "--verbose",
            action="store_true",
            help="Print the full methodology and coverage notes, not the one-line basis.",
        )
        cmd.add_argument(
            "--fail-on-budget",
            action="store_true",
            help="Exit 3 when an eager projection exceeds the supplied budget.",
        )
        if action == "scan":
            cmd.add_argument("--config", action="append", type=Path, default=[])
            cmd.add_argument("--project", type=Path, default=Path.cwd())
            cmd.add_argument(
                "--host",
                choices=[
                    "all",
                    "codex",
                    "claude-code",
                    "claude-desktop",
                    "cursor",
                    "vscode",
                    "generic",
                ],
                default="all",
            )
            cmd.add_argument(
                "--live",
                action="store_true",
                help="Start selected configured commands/connect to URLs for discovery. No tools/call.",
            )
            cmd.add_argument(
                "--server",
                action="append",
                default=[],
                help="Exact name or report ID; repeatable. Use with --live to limit scope.",
            )
            cmd.add_argument("--timeout", type=positive, default=20)
            cmd.add_argument("--max-pages", type=positive, default=50)
        else:
            cmd.add_argument(
                "capture", type=Path, help="Complete tools/list JSON or Inspector JSON envelope."
            )
    diff = sub.add_parser("diff")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    return p


async def scan(args) -> dict:
    paths = [(args.host if args.host != "all" else "generic", path) for path in args.config]
    if not paths:
        appdata = Path(os.environ["APPDATA"]) if "APPDATA" in os.environ else None
        paths = candidates(Path.home(), args.project, appdata)
        if args.host != "all":
            paths = [(host, path) for host, path in paths if host == args.host]
    servers, sources = inventory(paths, args.project)
    selector_notes = []
    if args.server:
        unmatched = set(args.server) - {x for s in servers for x in (s.name, s.key)}
        if unmatched:
            raise ValueError("server_selector_not_found")
        # A configured name is not unique across scopes. Selecting by name can start
        # more endpoints than the caller named, so say so before --live acts on it.
        for selector in args.server:
            if sum(1 for s in servers if s.name == selector) > 1:
                selector_notes.append("name_selector_matched_multiple_scopes")
        servers = [s for s in servers if s.name in args.server or s.key in args.server]
    report = base(args)
    report["coverage"] = COVERAGE + sorted(set(selector_notes))
    report["sources"] = sources
    report["mode"] = "live-discovery" if args.live else "static"
    counter = Counter(args.encoding) if args.live else None
    for server in servers:
        row = server.public(args.include_names)
        row["loading_behavior"] = "unknown"
        if server.config.get("alwaysLoad") is True:
            row["findings"].append("always_load_requested")
        if not server.enabled:
            row["measurement"] = {"status": "disabled"}
        elif not args.live:
            row["measurement"] = {"status": "not_probed"}
        else:
            try:
                capture = await probe(server, args.timeout, args.max_pages)
                row["measurement"] = analyze(
                    capture,
                    server.config,
                    counter,
                    args.include_names,
                    server.tool_name_prefix,
                )
                row["protocol_version"] = capture["protocol_version"]
            except Exception as exc:
                row["measurement"] = {"status": error_code(exc)}
        report["servers"].append(row)
    return finalize(report, args)


def base(args) -> dict:
    return {
        "schema_version": 1,
        "version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "encoding": args.encoding,
        "methodology": METHOD,
        "servers": [],
    }


def finalize(report: dict, args) -> dict:
    groups = {}
    for row in report["servers"]:
        g = groups.setdefault(
            row["group"],
            {
                "id": row["group"],
                "measured_servers": 0,
                "unmeasured_enabled_servers": 0,
                "floor_only_servers": 0,
                "always_loaded_tokens": 0,
                "tokens": 0,
            },
        )
        m = row["measurement"]
        if m["status"] == "measured":
            g["measured_servers"] += 1
            g["always_loaded_tokens"] += m["always_loaded_tokens"]
            if m["eager_projection_tokens"] is None:
                g["floor_only_servers"] += 1
            else:
                g["tokens"] += m["eager_projection_tokens"]
        elif row["enabled"]:
            g["unmeasured_enabled_servers"] += 1
    for group in groups.values():
        group["complete"] = group["unmeasured_enabled_servers"] == 0
        # A group holding a floor-only server has no complete ceiling, so its
        # projection is a partial sum and must not be read as the group total.
        group["projection_complete"] = group["complete"] and group["floor_only_servers"] == 0
        if args.context_window and group["measured_servers"]:
            group["budget"] = budget(
                group["tokens"],
                args.context_window,
                args.reserve,
                group["always_loaded_tokens"],
            )
    report["groups"] = list(groups.values())
    invalid = any(s["status"] == "unreadable_or_invalid" for s in report.get("sources", []))
    missing_explicit = bool(getattr(args, "config", [])) and any(
        s["status"] == "not_found" for s in report.get("sources", [])
    )
    report["diagnosis"] = diagnose(report)
    report["collection_complete"] = (
        not invalid
        and not missing_explicit
        and all(g["complete"] for g in groups.values())
        and bool(groups)
    )
    report["host_context_usage"] = "unknown"
    return report


def markdown(report: dict, verbose: bool = False) -> str:
    def safe(text):
        return (
            str(text)
            .replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("`", "'")
        )

    def cell(value):
        return "unknown" if value is None else value

    lines = ["# MCP Context Doctor", ""]
    if d := report.get("diagnosis"):
        c = d["coverage"]
        headline = {
            "review_recommended": f"Review recommended -- {len(d['items'])} items",
            "no_findings_in_measured_scope": "No findings in the measured scope",
            "nothing_measured": "Nothing was measured",
        }[d["verdict"]]
        lines += [
            f"## {headline}",
            "",
            f"Measured {c['measured_servers']} of {c['enabled_servers']} enabled servers. "
            + (
                "Coverage is complete."
                if c["complete"]
                else f"{c['unmeasured_enabled_servers']} were not measured, so no total "
                "below is complete."
            ),
            "",
        ]
        if c["measured_servers"]:
            lines += [
                f"Always loaded across measured servers: "
                f"**{d['always_loaded_tokens']} tokens**. This is the floor a host carries "
                "in every loading mode.",
                "",
            ]
        for index, item in enumerate(d["items"], start=1):
            where = safe(item["server_name"] or item["server"] or "configuration")
            cost = f" ({item['impact_tokens']} tokens)" if item["impact_tokens"] else ""
            lines += [f"{index}. **{where}**{cost} -- {safe(item['action'])}"]
        lines += [""]
    lines += [
        f"Mode: {report['mode']} | Encoding: {report['encoding']}",
        "",
        "**Always loaded** counts the advertised tool names, which a host carries whether or",
        "not it has loaded the definitions. It is a floor. **Eager projection** adds every",
        "description and input schema, which only a host that loads them all pays. Actual host",
        "context usage is **unknown** and lies between them.",
        "",
        "| Server | Host | Status | Selected tools | Always loaded | Eager projection |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in report["servers"]:
        m = row["measurement"]
        lines.append(
            f"| {safe(row.get('name', row['id']))} | {row['host']} | {m['status']} "
            f"| {m.get('selected_tools', '-')} | {cell(m.get('always_loaded_tokens', '-'))} "
            f"| {cell(m.get('eager_projection_tokens', '-'))} |"
        )
    lines += ["", "## Evidence", ""]
    for source in report.get("sources", []):
        if source["status"] == "unreadable_or_invalid":
            lines.append(
                f"- {source['id']}: configuration unreadable or invalid; coverage incomplete."
            )
    if not report["servers"]:
        lines.append("- No server entries collected. This is not evidence of a clean host.")
    for row in report["servers"]:
        findings = row["findings"] + row["measurement"].get("findings", [])
        if findings:
            lines.append(f"- {row['id']}: {', '.join(findings)}")
        for tool in row["measurement"].get("tools", [])[:5]:
            cost = (
                f"{tool['definition_tokens']} definition tokens"
                if tool["definition_tokens"] is not None
                else f"{tool['always_loaded_tokens']} name tokens; definition unmeasured"
            )
            lines.append(
                f"- {row['id']} / {safe(tool.get('name', tool['id']))}: {cost}; "
                + (", ".join(tool["findings"]) or "no heuristic finding")
            )
    for g in report["groups"]:
        if b := g.get("budget"):
            lines.append(
                f"- {g['id']}: floor {b['always_loaded_tokens']} tokens "
                f"({b['floor_percent_of_window']}% of supplied window); "
                f"eager projection {b['percent_of_window']}%, headroom "
                f"{b['remaining_in_projection']} tokens; collection complete: {g['complete']}; "
                f"projection complete: {g['projection_complete']}."
            )
    notes = list(report["methodology"]) + list(report.get("coverage", []))
    notes += [
        "JSON includes per-tool measurements and independent config groups.",
        "No runtime tool output was measured. No universal safe server-count threshold is assumed.",
    ]
    if verbose:
        lines += ["", "## Interpretation and coverage", ""]
        lines += ["- " + item for item in notes]
        lines += [""]
    else:
        # The notes are correct and they are long. Left inline they bury the finding
        # the reader came for, so the default keeps a pointer and the JSON keeps them.
        lines += [
            "",
            "---",
            "",
            f"Token figures are eager-loading projections against a named tokenizer proxy, "
            f"not host usage. {len(notes)} methodology and coverage notes apply: re-run with "
            "--verbose, or read `methodology` and `coverage` in --format json.",
            "",
        ]
    return "\n".join(lines)


def main(argv=None) -> int:
    # A console that cannot encode a character must not turn a valid report into a
    # failure; report names are attacker-influenced text and consoles are not UTF-8
    # everywhere. Lossy rendering is preferred over an unencodable crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    p = parser()
    args = p.parse_args(argv)
    if args.action != "diff":
        if (
            args.reserve < 0
            or (args.reserve and not args.context_window)
            or (args.context_window and args.reserve >= args.context_window)
        ):
            p.error("reserve must be nonnegative and smaller than the supplied context window")
        if args.fail_on_budget and not args.context_window:
            p.error("--fail-on-budget requires --context-window")
        if args.output and args.output.exists():
            p.error("output already exists; choose a new report path")
    try:
        if args.action == "diff":
            report = compare(read_data(args.before), read_data(args.after))
            print(json.dumps(report, indent=2, ensure_ascii=True))
            return 0
        if args.action == "scan":
            report = anyio.run(scan, args)
        else:
            capture = read_data(args.capture)
            report = base(args)
            report["mode"] = "offline-capture"
            server = Server("capture", {}, "offline-capture")
            row = server.public(args.include_names)
            row["measurement"] = analyze(capture, {}, Counter(args.encoding), args.include_names)
            report["servers"] = [row]
            finalize(report, args)
        rendered = (
            json.dumps(report, indent=2, ensure_ascii=True) + "\n"
            if args.format == "json"
            else markdown(report, args.verbose)
        )
        if args.output:
            # Atomic exclusive creation prevents an existing file from being overwritten.
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
            print("Report created.")
        else:
            print(rendered)
        if args.fail_on_budget and any(
            g.get("budget", {}).get("exceeds_projection_budget") for g in report["groups"]
        ):
            return 3
        if args.action == "scan" and (args.live or args.config):
            if any(
                s["status"] in ("unreadable_or_invalid", "not_found")
                for s in report["sources"]
                if args.config or s["status"] != "not_found"
            ):
                return 2
            if args.live and not report["collection_complete"]:
                return 2
        return 0
    except Exception as exc:
        # Never echo an exception that may contain raw config values or server text.
        # DOCTOR_DEBUG opts a local operator into the traceback; it can disclose
        # configuration values and server output, so it is never on by default.
        if os.environ.get("DOCTOR_DEBUG"):
            traceback.print_exc()
        print(
            json.dumps({"error": "invalid_input_or_environment", "type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
