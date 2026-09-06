"""Real HTTP transport tests against isolated local official-SDK servers."""

import socket
import threading
import time
from contextlib import contextmanager

import anyio
import pytest
import uvicorn
from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from mcp_context_doctor.config import Server
from mcp_context_doctor.probe import error_code, probe


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("test_server_failed")
            time.sleep(0.01)
        else:
            raise RuntimeError("test_server_start_timeout")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_official_sdk_http_discovery_does_not_call_tools():
    mcp = MCPServer("local-test", instructions="List items before selecting.")

    @mcp.tool()
    def list_items(limit: int = 10) -> list[str]:
        """List a bounded number of items."""
        pytest.fail("doctor called a tool")

    with serve(mcp.streamable_http_app()) as url:
        capture = anyio.run(probe, Server("test", {"url": url}, "test"), 10)
    assert len(capture["tools"]) == 1
    assert capture["tools"][0]["name"] == "list_items"
    assert capture["protocol_version"] == "2026-07-28"


def test_http_auth_failure_is_incomplete_without_leaking_response_body():
    async def unauthorized(request):
        return Response("PRIVATE_SERVER_TEXT", status_code=401)

    with serve(Starlette(routes=[Route("/mcp", unauthorized, methods=["POST", "GET"])])) as url:
        with pytest.raises(Exception) as error:
            anyio.run(probe, Server("test", {"url": url}, "test"), 5)
        # SDK v2 deliberately wraps this response without retaining its HTTP status.
        # Treating the opaque failure as authentication would be an unsupported inference.
        assert error_code(error.value) == "probe_failed"
        assert "PRIVATE_SERVER_TEXT" not in str(error.value)
