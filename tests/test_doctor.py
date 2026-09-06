import argparse
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from mcp_context_doctor.cli import finalize, main, markdown
from mcp_context_doctor.config import (
    Server,
    candidates,
    effective_tools,
    expand,
    inventory,
    normalize_endpoint,
    parse_config,
    predefined_variables,
    proxied_endpoint,
    read_data,
)
from mcp_context_doctor.diagnose import diagnose
from mcp_context_doctor.measure import Counter, analyze, budget, compare, validate_capture
from mcp_context_doctor.probe import ProbeLimit, collect, prepare, probe

FIXTURE = Path(__file__).parent / "fixtures/server.py"
CAPTURE = {
    "instructions": "Search first.",
    "tools": [
        {
            "name": "search",
            "description": "Search records.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "read",
            "description": "Read a record.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ],
}


@pytest.fixture(scope="session")
def counter():
    return Counter()


def write_config(tmp_path, text, name="mcp.json"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_codex_filters_empty_allow_and_deny(tmp_path):
    cfg = parse_config(
        write_config(
            tmp_path, '[mcp_servers.example]\ncommand="python"\nenabled_tools=[]\n', "config.toml"
        )
    )[0]
    assert cfg.host == "codex"
    assert effective_tools(CAPTURE["tools"], cfg.config)[0] == []
    selected, _ = effective_tools(
        CAPTURE["tools"], {"enabled_tools": ["search", "read"], "disabled_tools": ["read"]}
    )
    assert [x["name"] for x in selected] == ["search"]


def test_jsonc_and_disabled(tmp_path):
    path = write_config(
        tmp_path, '{// comment\n"servers":{"x":{"command":"python","disabled":true,},},}'
    )
    server = parse_config(path, "vscode")[0]
    assert server.enabled is False


def test_project_local_and_global_not_combined(tmp_path):
    data = {
        "mcpServers": {"x": {"command": "python"}},
        "projects": {str(tmp_path): {"mcpServers": {"y": {"command": "node"}}}},
    }
    rows = parse_config(write_config(tmp_path, json.dumps(data)), "claude-code", tmp_path)
    assert len(rows) == 2 and rows[0].group != rows[1].group


def test_invalid_and_missing_sources_not_silently_clean(tmp_path):
    path = write_config(tmp_path, '{"mcpServers": []}')
    rows, sources = inventory([("generic", path), ("generic", tmp_path / "missing.json")], tmp_path)
    assert not rows
    assert [x["status"] for x in sources] == ["unreadable_or_invalid", "not_found"]


def test_discovery_does_not_spawn(monkeypatch, tmp_path, capsys):
    path = write_config(tmp_path, '{"mcpServers":{"x":{"command":"NEVER_RUN_THIS"}}}')
    monkeypatch.setattr(
        "mcp_context_doctor.cli.probe", lambda *args: pytest.fail("probe in static mode")
    )
    assert main(["scan", "--config", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["servers"][0]["measurement"]["status"] == "not_probed"
    assert data["groups"][0]["complete"] is False


def test_config_credentials_and_paths_omitted(tmp_path, capsys):
    secret = "SENSITIVE_VALUE_DO_NOT_PRINT"
    path = write_config(
        tmp_path,
        json.dumps(
            {
                "mcpServers": {
                    secret: {
                        "command": "node",
                        "args": [secret],
                        "env": {"TOKEN": secret},
                        "url": "https://example.com/" + secret,
                    }
                }
            }
        ),
    )
    main(["scan", "--config", str(path), "--format", "json"])
    output = capsys.readouterr().out
    assert secret not in output and str(tmp_path) not in output


def test_interpolation_no_shell_execution():
    assert expand("${env:API_KEY}", {"API_KEY": "value"}) == "value"
    assert expand("${MISSING:-fallback}", {}) == "fallback"
    assert expand("$(never-run)", {}) == "$(never-run)"
    with pytest.raises(ValueError):
        expand("${input:token}", {})
    with pytest.raises(ValueError):
        expand("${ABSENT}", {})


def test_token_counts_match_tokenizer_and_filter(counter):
    whole = analyze(CAPTURE, {}, counter)
    small = analyze(CAPTURE, {"enabled_tools": ["read"]}, counter)
    assert small["selected_tools"] == 1
    assert small["eager_projection_tokens"] < whole["eager_projection_tokens"]
    assert whole["instructions_tokens"] == counter.text(CAPTURE["instructions"])
    assert whole["runtime_output_tokens"] is None
    assert counter.text("<|endoftext|>") > 0


def test_output_schema_measured_separately(counter):
    data = copy.deepcopy(CAPTURE)
    data["tools"][0]["outputSchema"] = {"type": "object", "description": "long " * 200}
    a, b = analyze(CAPTURE, {}, counter), analyze(data, {}, counter)
    assert a["eager_projection_tokens"] == b["eager_projection_tokens"]
    assert a["selected_wire_catalog_tokens"] < b["selected_wire_catalog_tokens"]


@pytest.mark.parametrize(
    "patch", [{"nextCursor": "next"}, {"tools": [{}]}, {"tools": CAPTURE["tools"] * 2}]
)
def test_reject_incomplete_or_invalid_capture(patch):
    with pytest.raises(ValueError):
        validate_capture(CAPTURE | patch)


def test_inspector_envelope():
    tools, instructions, detail = validate_capture({"result": {"tools": CAPTURE["tools"]}})
    assert len(tools) == 2 and instructions == "" and detail == "definitions"


def test_drift_detects_schema_edit_but_not_order(counter):
    a = analyze(CAPTURE, {}, counter)
    b = analyze(CAPTURE | {"tools": list(reversed(CAPTURE["tools"]))}, {}, counter)
    assert a["fingerprint"] == b["fingerprint"]
    changed = copy.deepcopy(CAPTURE)
    changed["tools"][0]["description"] = "Changed semantics."
    assert analyze(changed, {}, counter)["fingerprint"] != a["fingerprint"]


def test_budget_and_partial_group():
    assert budget(100, 1000, 900)["exceeds_projection_budget"] is False
    assert budget(101, 1000, 900)["exceeds_projection_budget"] is True
    report = {"servers": [{"group": "x", "enabled": True, "measurement": {"status": "timeout"}}]}
    args = argparse.Namespace(context_window=1000, reserve=0, config=[])
    finalize(report, args)
    assert "budget" not in report["groups"][0]
    assert report["collection_complete"] is False


def test_diff_refuses_incompatible_tokenizers():
    with pytest.raises(ValueError):
        compare({"schema_version": 1, "encoding": "a"}, {"schema_version": 1, "encoding": "b"})


def test_explicit_output_no_overwrite(tmp_path, capsys):
    capture = write_config(tmp_path, json.dumps(CAPTURE))
    dest = tmp_path / "report.json"
    assert main(["analyze", str(capture), "--output", str(dest), "--format", "json"]) == 0
    original = dest.read_bytes()
    with pytest.raises(SystemExit):
        main(["analyze", str(capture), "--output", str(dest)])
    assert dest.read_bytes() == original


def test_cli_budget_exit(tmp_path, capsys):
    path = write_config(tmp_path, json.dumps(CAPTURE))
    assert main(["analyze", str(path), "--context-window", "1", "--fail-on-budget"]) == 3


def test_failure_report_no_raw_server_data(tmp_path, capsys):
    path = write_config(
        tmp_path, '{"mcpServers":{"x":{"url":"https://example.com/${input:SECRET}"}}}'
    )
    assert main(["scan", "--config", str(path), "--live", "--format", "json"]) == 2
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err
    assert json.loads(out.out)["servers"][0]["measurement"]["status"] == "configuration_unresolved"


def test_http_credentials_and_cleartext():
    with pytest.raises(ValueError):
        prepare(Server("x", {"url": "http://example.com/mcp"}, "test"))
    with pytest.raises(ValueError):
        prepare(Server("x", {"url": "https://user:pass@example.com/mcp"}, "test"))
    assert prepare(Server("x", {"url": "http://127.0.0.1:8000/mcp"}, "test"))["headers"] == {}


def test_collect_cycle_and_max_pages():
    class Fake:
        instructions = ""

        async def list_tools(self, **kwargs):
            return SimpleNamespace(tools=[], next_cursor="loop")

    with pytest.raises(ProbeLimit):
        anyio.run(collect, Fake())
    with pytest.raises(ProbeLimit):
        anyio.run(collect, Fake(), 1)


def test_actual_stdio_discovery_two_pages():
    server = Server("fixture", {"command": sys.executable, "args": [str(FIXTURE)]}, "test")
    result = anyio.run(probe, server, 10)
    assert [t["name"] for t in result["tools"]] == ["search", "read"]
    assert result["protocol_version"] == "2025-11-25"


def test_actual_stdio_timeout():
    server = Server(
        "fixture", {"command": sys.executable, "args": [str(FIXTURE), "timeout"]}, "test"
    )
    with pytest.raises(TimeoutError):
        anyio.run(probe, server, 1)


def test_disabled_server_skips_live_probe(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, '{"mcpServers":{"x":{"command":"never","enabled":false}}}')
    monkeypatch.setattr("mcp_context_doctor.cli.probe", lambda *args: pytest.fail("disabled probe"))
    assert main(["scan", "--config", str(path), "--live", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["servers"][0]["measurement"]["status"] == "disabled"


def test_candidates_platform_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    paths = candidates(tmp_path, tmp_path / "project", tmp_path / "appdata")
    assert ("codex", tmp_path / ".codex/config.toml") in paths
    assert ("claude-desktop", tmp_path / "appdata/Claude/claude_desktop_config.json") in paths


def test_selected_project_controls_stdio_cwd(tmp_path):
    project = tmp_path / "actual-project"
    cfg = parse_config(
        write_config(tmp_path, '{"mcpServers":{"x":{"command":"python"}}}'), project=project
    )[0]
    assert cfg.config["cwd"] == str(project.resolve())


def test_string_false_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        parse_config(
            write_config(tmp_path, '{"mcpServers":{"x":{"command":"python","enabled":"false"}}}')
        )


def test_malformed_schema_is_rejected():
    data = copy.deepcopy(CAPTURE)
    data["tools"][0]["inputSchema"]["required"] = "not-an-array"
    with pytest.raises(ValueError):
        validate_capture(data)


def test_empty_next_cursor_still_means_incomplete():
    with pytest.raises(ValueError):
        validate_capture(CAPTURE | {"nextCursor": ""})


def test_markdown_does_not_render_server_markup(counter):
    report = {
        "mode": "test",
        "encoding": "o200k_base",
        "methodology": [],
        "groups": [],
        "servers": [
            {
                "id": "x",
                "name": "<script>|\n",
                "host": "generic",
                "findings": [],
                "measurement": analyze(CAPTURE, {}, counter),
            }
        ],
    }
    assert "<script>" not in markdown(report)


def test_empty_config_file_is_an_empty_inventory_not_a_broken_file(tmp_path):
    # Hosts create the file before anything is configured; flagging it as invalid
    # would report a healthy machine as having incomplete coverage.
    for name in ("mcp.json", "config.toml"):
        blank = tmp_path / name
        blank.write_text(" " * 4, encoding="utf-8")
        assert read_data(blank) == {}
        assert parse_config(blank, "vscode", tmp_path) == []
    servers, sources = inventory([("vscode", tmp_path / "mcp.json")], tmp_path)
    assert servers == []
    assert sources[0]["status"] == "read"


def test_editor_predefined_variables_resolve_from_selected_project(tmp_path):
    project = tmp_path / "work"
    project.mkdir()
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": {
                    "s": {
                        "command": "node",
                        "args": ["${workspaceFolder}/server.js", "${workspaceFolderBasename}"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    server = parse_config(config, "vscode", project)[0]
    assert prepare(server)["args"] == [str(project.resolve()) + "/server.js", "work"]
    # An interactive input prompt still has no known value and must stay unresolved.
    server.config = {"command": "node", "args": ["${input:token}"]}
    with pytest.raises(ValueError):
        prepare(server)


def test_predefined_variables_do_not_leak_when_no_project_is_selected():
    values = predefined_variables(None)
    assert "workspaceFolder" not in values
    assert "userHome" in values


def test_report_renders_on_a_console_that_cannot_encode_every_character(tmp_path, capsys):
    # A non-UTF-8 console must not turn a valid report into an encoding failure.
    capture = tmp_path / "capture.json"
    # A tool name no console encoding covers. Names are server-controlled text.
    payload = {
        "instructions": "",
        "tools": [
            {
                "name": "слово-例-ἀ",
                "description": "Read a record.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
    }
    capture.write_text(json.dumps(payload), encoding="utf-8")
    stdout = sys.stdout
    sys.stdout = open(tmp_path / "out.txt", "w", encoding="cp932")
    try:
        assert main(["analyze", str(capture), "--include-names"]) == 0
    finally:
        sys.stdout.close()
        sys.stdout = stdout
    rendered = (tmp_path / "out.txt").read_text(encoding="cp932")
    assert "MCP Context Doctor" in rendered
    assert "methodology and coverage notes apply" in rendered


def test_markdown_body_is_ascii_so_narrow_consoles_cannot_fail():
    report = {
        "mode": "static",
        "encoding": "o200k_base",
        "servers": [],
        "groups": [],
        "methodology": [],
        "sources": [],
    }
    assert markdown(report).isascii()


def test_name_selector_matching_multiple_scopes_is_reported(tmp_path):
    project = tmp_path / "work"
    project.mkdir()
    entry = {"mcpServers": {"shared": {"command": "node", "args": ["a.js"]}}}
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(entry), encoding="utf-8")
    second.write_text(json.dumps(entry), encoding="utf-8")
    args = SimpleNamespace(
        config=[first, second],
        project=project,
        host="all",
        server=["shared"],
        live=False,
        encoding="o200k_base",
        context_window=None,
        reserve=0,
        include_names=False,
        fail_on_budget=False,
        format="json",
        output=None,
        timeout=5,
        max_pages=5,
    )
    from mcp_context_doctor.cli import scan

    report = anyio.run(scan, args)
    assert len(report["servers"]) == 2
    assert "name_selector_matched_multiple_scopes" in report["coverage"]


NAMES_ONLY = {
    "instructions": "",
    "tools": [
        {"name": "search", "description": "Search records."},
        {"name": "read", "description": "Read a record."},
    ],
}


def test_names_only_capture_measures_the_floor_and_leaves_the_ceiling_unknown(counter):
    # A catalog reachable only in reduced form still pins the always-loaded cost.
    m = analyze(NAMES_ONLY, {}, counter)
    assert m["detail"] == "names_only"
    assert m["selected_tools"] == 2
    assert m["always_loaded_tokens"] == counter.text("search") + counter.text("read")
    assert "definitions_unavailable_floor_only" in m["findings"]
    # Unmeasured is unknown, never zero: a reduced capture is not a free server.
    for field in ("eager_projection_tokens", "core_tools_tokens", "selected_wire_catalog_tokens"):
        assert m[field] is None, field
    for tool in m["tools"]:
        assert tool["definition_tokens"] is None
        assert tool["input_schema_tokens"] is None
        assert tool["always_loaded_tokens"] > 0


def test_capture_missing_schemas_on_only_some_tools_is_rejected(counter):
    # Reduced is acceptable; inconsistent is malformed and must not be measured.
    mixed = {
        "instructions": "",
        "tools": [
            CAPTURE["tools"][0],
            {"name": "read", "description": "Read a record."},
        ],
    }
    with pytest.raises(ValueError):
        validate_capture(mixed)


def test_floor_is_measured_with_the_host_name_convention(counter):
    bare = analyze(CAPTURE, {}, counter)
    qualified = analyze(CAPTURE, {}, counter, name_prefix="mcp__brain__")
    assert bare["always_loaded_basis"] == "bare_names"
    assert qualified["always_loaded_basis"] == "qualified_names"
    assert qualified["always_loaded_tokens"] > bare["always_loaded_tokens"]
    # The prefix changes only the floor; the definitions are unchanged.
    assert qualified["eager_projection_tokens"] == bare["eager_projection_tokens"]


def test_only_claude_hosts_declare_a_known_name_convention():
    cfg = {"command": "node", "args": ["s.js"]}
    assert Server("brain", cfg, "s", "claude-code").tool_name_prefix == "mcp__brain__"
    assert Server("brain", cfg, "s", "claude-desktop").tool_name_prefix == "mcp__brain__"
    # An unverified convention would put a fabricated number in the floor.
    for host in ("codex", "cursor", "vscode", "generic"):
        assert Server("brain", cfg, "s", host).tool_name_prefix is None


def test_floor_only_server_does_not_silently_complete_a_group_projection(counter):
    args = SimpleNamespace(context_window=100000, reserve=0)
    report = {
        "servers": [
            {
                "group": "g",
                "enabled": True,
                "measurement": analyze(CAPTURE, {}, counter),
            },
            {
                "group": "g",
                "enabled": True,
                "measurement": analyze(NAMES_ONLY, {}, counter),
            },
        ]
    }
    finalize(report, args)
    g = report["groups"][0]
    assert g["measured_servers"] == 2
    assert g["floor_only_servers"] == 1
    # Every measured server contributes a floor, so the floor total is complete.
    assert g["always_loaded_tokens"] > 0
    # The ceiling is a partial sum and must be labelled as one.
    assert g["complete"] is True
    assert g["projection_complete"] is False
    assert g["budget"]["always_loaded_tokens"] == g["always_loaded_tokens"]


def test_diff_falls_back_to_the_floor_when_a_ceiling_is_unmeasured(counter):
    def report(measurement):
        return {
            "schema_version": 1,
            "encoding": "o200k_base",
            "servers": [{"id": "s", "measurement": measurement}],
        }

    full = analyze(CAPTURE, {}, counter)
    reduced = analyze(NAMES_ONLY, {}, counter)
    both_full = compare(report(full), report(full))
    assert both_full["changes"][0]["basis"] == "eager_projection_tokens"
    assert both_full["changes"][0]["token_delta"] == 0
    # Subtracting a measured ceiling from a null one would raise; fall back instead.
    mixed = compare(report(full), report(reduced))
    assert mixed["changes"][0]["basis"] == "always_loaded_tokens"
    assert isinstance(mixed["changes"][0]["token_delta"], int)


def test_report_names_the_floor_and_prints_unknown_for_an_unmeasured_ceiling(counter):
    report = {
        "mode": "offline-capture",
        "encoding": "o200k_base",
        "servers": [
            {
                "id": "server-x",
                "host": "generic",
                "findings": [],
                "measurement": analyze(NAMES_ONLY, {}, counter),
            }
        ],
        "groups": [],
        "methodology": [],
        "sources": [],
    }
    rendered = markdown(report)
    assert "Always loaded" in rendered
    # The ceiling column must read unknown, not 0, for a floor-only measurement.
    assert "| unknown |" in rendered
    assert rendered.isascii()


def test_endpoint_normalization_folds_spellings_of_one_url():
    same = [
        "http://localhost:8765/mcp",
        "http://127.0.0.1:8765/mcp",
        "HTTP://LocalHost:8765/mcp/",
        "http://[::1]:8765/mcp",
    ]
    assert len({normalize_endpoint(u) for u in same}) == 1
    # An explicit default port is the same endpoint; a non-default one is not.
    assert normalize_endpoint("https://x.test:443/mcp") == normalize_endpoint("https://x.test/mcp")
    assert normalize_endpoint("https://x.test:8443/mcp") != normalize_endpoint("https://x.test/mcp")
    # Path and query select a server; they are identity, not noise.
    assert normalize_endpoint("https://x.test/a") != normalize_endpoint("https://x.test/b")
    assert normalize_endpoint("https://x.test/a?t=1") != normalize_endpoint("https://x.test/a")
    for bad in ("", "not a url", "ftp://x.test/mcp", "file:///tmp/x"):
        assert normalize_endpoint(bad) is None


def test_proxy_wrapper_resolves_to_the_url_it_forwards_to():
    argv = ["cmd", "/c", "npx", "-y", "mcp-remote", "http://localhost:8765/mcp"]
    assert proxied_endpoint(argv) == normalize_endpoint("http://localhost:8765/mcp")
    assert proxied_endpoint(["npx", "mcp-remote@0.1.2", "https://x.test/mcp"]) is not None
    # A server that merely takes a URL argument is not a transport wrapper.
    assert proxied_endpoint(["node", "server.js", "https://x.test/api"]) is None


def test_same_backend_through_a_proxy_and_directly_is_reported(tmp_path):
    # The case a config-field fingerprint misses: one endpoint, two spellings.
    direct = write_config(
        tmp_path,
        json.dumps({"mcpServers": {"brain": {"url": "http://127.0.0.1:8765/mcp"}}}),
        "a.json",
    )
    wrapped = write_config(
        tmp_path,
        json.dumps(
            {
                "mcpServers": {
                    "brain": {
                        "command": "cmd",
                        "args": ["/c", "npx", "-y", "mcp-remote", "http://localhost:8765/mcp"],
                    }
                }
            }
        ),
        "b.json",
    )
    servers, _ = inventory([("generic", direct), ("generic", wrapped)], tmp_path)
    assert len(servers) == 2
    for server in servers:
        assert "repeated_endpoint_not_proof_of_duplicate_loading" in server.notes
        assert "same_backend_reached_through_different_transports" in server.notes


def test_distinct_backends_are_not_grouped(tmp_path):
    data = {
        "mcpServers": {
            "a": {"url": "https://one.test/mcp"},
            "b": {"url": "https://two.test/mcp"},
            "c": {"command": "node", "args": ["one.js"]},
            "d": {"command": "node", "args": ["two.js"]},
        }
    }
    servers, _ = inventory([("generic", write_config(tmp_path, json.dumps(data)))], tmp_path)
    assert len(servers) == 4
    for server in servers:
        assert server.notes == [] or "repeated_endpoint" not in " ".join(server.notes)


def test_identical_stdio_commands_still_group_without_a_url(tmp_path):
    data = {
        "mcpServers": {
            "a": {"command": "node", "args": ["same.js"]},
            "b": {"command": "node", "args": ["same.js"]},
        }
    }
    servers, _ = inventory([("generic", write_config(tmp_path, json.dumps(data)))], tmp_path)
    for server in servers:
        assert "repeated_endpoint_not_proof_of_duplicate_loading" in server.notes
        # Same transport, so the proxy-specific note must not be attached.
        assert "same_backend_reached_through_different_transports" not in server.notes


def wide_capture(count, big=0):
    """A catalog of `count` cheap tools, the first `big` of them expensive."""
    tools = []
    for i in range(count):
        words = 200 if i < big else 2
        tools.append(
            {
                "name": f"tool_{i:02d}",
                # Distinct wording per tool: identical descriptions are their own finding.
                "description": f"tool {i} " + " ".join(["word"] * words),
                "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
            }
        )
    return {"instructions": "", "tools": tools}


def row(measurement, **kw):
    base = {
        "id": kw.get("id", "server-x"),
        "host": kw.get("host", "generic"),
        "enabled": kw.get("enabled", True),
        "findings": kw.get("findings", []),
        "measurement": measurement,
    }
    if "name" in kw:
        base["name"] = kw["name"]
    return base


def test_verdict_states_coverage_and_never_implies_more_than_was_measured(counter):
    d = diagnose(
        {
            "servers": [
                row(analyze(CAPTURE, {}, counter), id="a"),
                row({"status": "auth_required"}, id="b"),
            ]
        }
    )
    assert d["verdict"] == "review_recommended"
    assert d["coverage"] == {
        "measured_servers": 1,
        "enabled_servers": 2,
        "unmeasured_enabled_servers": 1,
        "complete": False,
    }
    codes = [i["code"] for i in d["items"]]
    assert "enabled_servers_not_measured" in codes
    # No item may assert an overflow; that is the claim the tool refuses to make.
    assert not any("overflow" in json.dumps(i).lower() for i in d["items"])


def test_nothing_measured_is_not_a_clean_bill_of_health(counter):
    d = diagnose({"servers": [row({"status": "not_probed"}, id="a")]})
    assert d["verdict"] == "nothing_measured"
    assert d["always_loaded_tokens"] == 0
    assert d["coverage"]["complete"] is False


def test_clean_measured_scope_reports_no_findings(counter):
    d = diagnose({"servers": [row(analyze(wide_capture(6), {}, counter), id="a")]})
    assert d["verdict"] == "no_findings_in_measured_scope"
    assert d["items"] == []
    assert d["coverage"]["complete"] is True


def test_concentration_fires_only_when_a_minority_carries_the_cost(counter):
    # 2 of 20 tools hold most of the definitions: a filter can act on exactly those.
    skewed = diagnose({"servers": [row(analyze(wide_capture(20, big=2), {}, counter), id="a")]})
    item = next(
        i for i in skewed["items"] if i["code"] == "definition_cost_concentrated_in_few_tools"
    )
    assert item["evidence"]["tools"] <= 2
    assert item["impact_tokens"] > 0
    assert "tool filter" in item["action"]
    # Cost spread evenly is not a finding: there is no minority to exclude.
    flat = diagnose({"servers": [row(analyze(wide_capture(20), {}, counter), id="a")]})
    assert not any(i["code"] == "definition_cost_concentrated_in_few_tools" for i in flat["items"])


def test_description_outlier_is_relative_to_the_measured_distribution(counter):
    d = diagnose({"servers": [row(analyze(wide_capture(12, big=1), {}, counter), id="a")]})
    item = next(i for i in d["items"] if i["code"] == "tool_description_far_above_median")
    assert item["evidence"]["multiple"] >= 5
    assert item["evidence"]["median_description_tokens"] > 0
    # Too small a sample has no meaningful median, so the rule stays silent.
    tiny = diagnose({"servers": [row(analyze(CAPTURE, {}, counter), id="a")]})
    assert not any(i["code"] == "tool_description_far_above_median" for i in tiny["items"])


def test_repeated_backend_is_reported_once_for_the_configuration(counter):
    shared = ["repeated_endpoint_not_proof_of_duplicate_loading"]
    d = diagnose(
        {
            "servers": [
                row({"status": "not_probed"}, id="a", findings=shared),
                row({"status": "not_probed"}, id="b", findings=shared),
            ]
        }
    )
    items = [i for i in d["items"] if i["code"] == "one_backend_configured_more_than_once"]
    assert len(items) == 1
    assert items[0]["evidence"]["entries"] == 2
    # The wording must not claim one host double-loads it.
    assert "not proof" in items[0]["action"]


def test_unmeasured_advice_matches_the_reason(counter):
    static = diagnose({"servers": [row({"status": "not_probed"}, id="a")]})
    assert "--live" in static["items"][0]["action"]
    assert "token" not in static["items"][0]["action"]
    auth = diagnose({"servers": [row({"status": "auth_required"}, id="a")]})
    assert "token" in auth["items"][0]["action"]
    assert "--live" not in auth["items"][0]["action"]


def test_items_are_ordered_by_measured_impact(counter):
    d = diagnose(
        {
            "servers": [
                row(analyze(wide_capture(20, big=2), {}, counter), id="a"),
                row({"status": "auth_required"}, id="b"),
            ]
        }
    )
    impacts = [i["impact_tokens"] for i in d["items"]]
    assert impacts == sorted(impacts, reverse=True)


def test_report_leads_with_the_verdict(counter):
    report = {
        "mode": "live-discovery",
        "encoding": "o200k_base",
        "servers": [row(analyze(wide_capture(20, big=2), {}, counter), id="a", name="brain")],
        "groups": [],
        "methodology": [],
        "sources": [],
    }
    report["diagnosis"] = diagnose(report)
    rendered = markdown(report)
    head = rendered.splitlines()[2]
    assert head.startswith("## Review recommended")
    assert rendered.index("Review recommended") < rendered.index("Mode:")
    assert rendered.isascii()


def test_floor_line_is_omitted_when_nothing_was_measured(counter):
    report = {
        "mode": "static",
        "encoding": "o200k_base",
        "servers": [row({"status": "not_probed"}, id="a")],
        "groups": [],
        "methodology": [],
        "sources": [],
    }
    report["diagnosis"] = diagnose(report)
    rendered = markdown(report)
    # "0 tokens" next to a floor label reads as a measured zero. Say nothing instead.
    assert "Always loaded across measured servers" not in rendered
    assert "Nothing was measured" in rendered


def test_caveats_are_collapsed_by_default_and_restored_by_verbose(counter):
    report = {
        "mode": "static",
        "encoding": "o200k_base",
        "servers": [row(analyze(CAPTURE, {}, counter), id="a")],
        "groups": [],
        "methodology": ["Method one.", "Method two."],
        "coverage": ["Coverage one."],
        "sources": [],
    }
    report["diagnosis"] = diagnose(report)
    brief = markdown(report)
    full = markdown(report, verbose=True)
    # Correct but long: the default keeps a pointer so the finding stays on screen.
    assert "Method one." not in brief
    assert "methodology and coverage notes apply" in brief
    assert "--verbose" in brief
    # Nothing is deleted, only moved behind a flag.
    for note in ("Method one.", "Method two.", "Coverage one."):
        assert note in full
    assert len(brief.splitlines()) < len(full.splitlines())
    assert brief.isascii() and full.isascii()


def test_json_always_carries_the_full_methodology(tmp_path, capsys):
    capture = write_config(tmp_path, json.dumps(CAPTURE), "capture.json")
    assert main(["analyze", str(capture), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    # A machine reader must never have to ask for the caveats.
    assert len(data["methodology"]) > 1
    assert data["diagnosis"]["verdict"]
    assert data["diagnosis"]["basis"]
