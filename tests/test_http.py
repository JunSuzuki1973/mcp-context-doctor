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
from mcp_context_doctor.probe import error_code, prepare, probe


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
        # A slow shutdown is not a product defect; the thread is a daemon and cannot
        # outlive the run. Asserting on it made a cold first run flake.
        server.should_exit = True
        thread.join(30)
        sock.close()


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


def test_http_auth_failure_is_classified_without_leaking_response_body():
    async def reject(request):
        return Response("PRIVATE_SERVER_TEXT", status_code=401)

    with serve(Starlette(routes=[Route("/mcp", reject, methods=["POST", "GET"])])) as url:
        with pytest.raises(Exception) as error:
            anyio.run(probe, Server("test", {"url": url}, "test"), 5)
        # SDK v2 wraps this response without retaining its HTTP status, so the failed
        # attempt is re-checked against the same endpoint to recover the distinction.
        assert error_code(error.value) == "auth_required"
        assert "PRIVATE_SERVER_TEXT" not in str(error.value)


def test_http_server_error_is_not_reported_as_auth_required():
    async def broken(request):
        return Response("PRIVATE_SERVER_TEXT", status_code=500)

    with serve(Starlette(routes=[Route("/mcp", broken, methods=["POST", "GET"])])) as url:
        with pytest.raises(Exception) as error:
            anyio.run(probe, Server("test", {"url": url}, "test"), 5)
        assert error_code(error.value) == "probe_failed"
        assert "PRIVATE_SERVER_TEXT" not in str(error.value)


def guard(app, token):
    """Wrap an ASGI app so only the right bearer token gets through."""
    expected = f"Bearer {token}".encode()

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != expected:
            await Response("PRIVATE_SERVER_TEXT", status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)

    return wrapped


def test_a_token_from_the_environment_reaches_an_authenticating_endpoint(monkeypatch):
    mcp = MCPServer("guarded", instructions="Authenticated.")

    @mcp.tool()
    def list_items(limit: int = 10) -> list[str]:
        """List a bounded number of items."""
        pytest.fail("doctor called a tool")

    with serve(guard(mcp.streamable_http_app(), "s3cret")) as url:
        # Without the token the endpoint is measurable only as unreachable.
        with pytest.raises(Exception) as error:
            anyio.run(probe, Server("test", {"url": url}, "test"), 10)
        assert error_code(error.value) == "auth_required"

        # The config names a variable; the token itself never appears in the config.
        monkeypatch.setenv("DOCTOR_TEST_TOKEN", "s3cret")
        cfg = {"url": url, "bearer_token_env_var": "DOCTOR_TEST_TOKEN"}
        capture = anyio.run(probe, Server("test", cfg, "test"), 10)
        assert [t["name"] for t in capture["tools"]] == ["list_items"]


def test_a_missing_token_variable_is_a_configuration_error_not_a_silent_pass(monkeypatch):
    monkeypatch.delenv("DOCTOR_TEST_ABSENT", raising=False)
    cfg = {"url": "https://example.test/mcp", "bearer_token_env_var": "DOCTOR_TEST_ABSENT"}
    with pytest.raises(ValueError):
        prepare(Server("test", cfg, "test"))


def test_header_variables_are_resolved_by_name(monkeypatch):
    monkeypatch.setenv("DOCTOR_TEST_KEY", "abc123")
    cfg = {"url": "https://example.test/mcp", "env_http_headers": {"X-Api-Key": "DOCTOR_TEST_KEY"}}
    assert prepare(Server("test", cfg, "test"))["headers"]["X-Api-Key"] == "abc123"
