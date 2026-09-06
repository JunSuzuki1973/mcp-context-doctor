"""Cross-check the same harmless fixture with an already installed official Inspector."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import anyio

from mcp_context_doctor.config import Server
from mcp_context_doctor.measure import Counter, analyze
from mcp_context_doctor.probe import probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspector-js", required=True, type=Path)
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/server.py"
    inspector = subprocess.run(
        [
            "node",
            str(args.inspector_js),
            "--cli",
            sys.executable,
            str(fixture),
            "--method",
            "tools/list",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    imported = json.loads(inspector.stdout)
    own = anyio.run(
        probe, Server("fixture", {"command": sys.executable, "args": [str(fixture)]}, "fixture")
    )
    external = imported["result"]["tools"]
    assert sorted(own["tools"], key=lambda x: x["name"]) == sorted(
        external, key=lambda x: x["name"]
    )
    counter = Counter()
    # tools/list does not include initialize instructions: compare only the same surface.
    a = analyze({"tools": own["tools"]}, {}, counter)
    b = analyze(imported, {}, counter)
    assert a["core_tools_tokens"] == b["core_tools_tokens"]
    print(
        json.dumps(
            {
                "result": "PASS",
                "tools": len(external),
                "core_tokens": a["core_tools_tokens"],
                "encoding": counter.encoding,
                "business_tool_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
