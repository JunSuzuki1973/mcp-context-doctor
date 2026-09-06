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
    parse_config,
)
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
    tools, instructions = validate_capture({"result": {"tools": CAPTURE["tools"]}})
    assert len(tools) == 2 and instructions == ""


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
