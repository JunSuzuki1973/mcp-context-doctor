"""Bounded discovery using the official MCP SDK. Never calls tools/call."""

from __future__ import annotations

import logging
import os
import re
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import Server, expand
from .measure import canonical


class ProbeLimit(ValueError):
    pass


class ProbeAuth(Exception):
    """The endpoint rejected an unauthenticated request."""


async def collect(
    client, max_pages: int = 50, max_tools: int = 5000, max_bytes: int = 8 * 1024 * 1024
) -> dict:
    tools, cursors = [], set()
    cursor, total_bytes = None, 0
    for _ in range(max_pages):
        result = await client.list_tools(cursor=cursor)
        page = [
            tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in result.tools
        ]
        total_bytes += len(canonical(page).encode())
        if total_bytes > max_bytes or len(tools) + len(page) > max_tools:
            raise ProbeLimit("catalog_limit")
        tools.extend(page)
        cursor = result.next_cursor
        if cursor is None:
            return {"tools": tools, "instructions": client.instructions or ""}
        if cursor in cursors:
            raise ProbeLimit("cursor_cycle")
        cursors.add(cursor)
    raise ProbeLimit("page_limit")


def prepare(server: Server) -> dict:
    cfg = expand(server.config, {**os.environ, **server.variables})
    if "envFile" in cfg:
        raise ValueError("env_file_requires_explicit_resolution")
    if server.transport == "stdio":
        if not isinstance(cfg.get("command"), str) or not cfg["command"]:
            raise ValueError("invalid_command")
        args = cfg.get("args", [])
        env = cfg.get("env", {})
        if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
            raise ValueError("invalid_args")
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ValueError("invalid_env")
        for key in cfg.get("env_vars", []):
            if key not in os.environ:
                raise ValueError("missing_environment")
            env[key] = os.environ[key]
        cfg["env"] = env
    elif server.transport == "http":
        url = urlsplit(cfg["url"])
        if url.scheme not in ("https", "http") or not url.hostname:
            raise ValueError("invalid_url")
        if url.username or url.password or url.fragment:
            raise ValueError("unsupported_url_credentials")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote_cleartext_http")
        headers = dict(cfg.get("headers", cfg.get("http_headers", {})))
        for header, key in cfg.get("env_http_headers", {}).items():
            if key not in os.environ:
                raise ValueError("missing_environment")
            headers[header] = os.environ[key]
        if key := cfg.get("bearer_token_env_var"):
            if key not in os.environ:
                raise ValueError("missing_environment")
            headers["Authorization"] = "Bearer " + os.environ[key]
        if not all(isinstance(v, str) for v in headers.values()):
            raise ValueError("invalid_headers")
        cfg["headers"] = headers
    else:
        raise ValueError("unsupported_transport")
    return cfg


async def unauthorized(cfg: dict, timeout: float) -> bool:
    """Classify an already-failed HTTP attempt. Reads the status line, never the body.

    SDK v2 reports a rejected handshake as an opaque MCPError with no HTTP status, so
    the most common remote failure - an OAuth-protected endpoint - is indistinguishable
    from a broken server. One extra request to the endpoint the caller already selected
    recovers that distinction without widening the trust boundary.
    """
    try:
        async with httpx2.AsyncClient(
            headers=cfg["headers"], timeout=timeout, follow_redirects=False, trust_env=False
        ) as http:
            response = await http.post(
                cfg["url"],
                json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            return response.status_code in (401, 403)
    except Exception:
        return False


async def connect(server: Server, cfg: dict, timeout: float, max_pages: int) -> dict:
    # Third-party logs may contain credentials. Only typed status codes leave this boundary.
    logger_state = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with anyio.fail_after(timeout):
            async with AsyncExitStack() as stack:
                if server.transport == "stdio":
                    errlog = stack.enter_context(open(os.devnull, "w"))
                    params = StdioServerParameters(
                        command=cfg["command"],
                        args=cfg.get("args", []),
                        env=cfg["env"],
                        cwd=cfg.get("cwd"),
                    )
                    transport = stdio_client(params, errlog=errlog)
                else:
                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=cfg["headers"],
                            timeout=timeout,
                            follow_redirects=False,
                            trust_env=False,
                        )
                    )
                    transport = streamable_http_client(cfg["url"], http_client=http)
                client = await stack.enter_async_context(
                    Client(
                        transport,
                        read_timeout_seconds=timeout,
                        cache=None,
                    )
                )
                caps = client.server_capabilities
                if caps and caps.tools is None:
                    capture = {"tools": [], "instructions": client.instructions or ""}
                else:
                    capture = await collect(client, max_pages=max_pages)
                version = client.protocol_version
                # Protocol version comes from an untrusted server; only return its date shape.
                capture["protocol_version"] = (
                    version if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(version)) else "unknown"
                )
                return capture
    finally:
        logging.disable(logger_state)


async def probe(server: Server, timeout: float = 20, max_pages: int = 50) -> dict:
    cfg = prepare(server)
    try:
        return await connect(server, cfg, timeout, max_pages)
    except BaseException as exc:
        # Classification runs after the failed attempt has fully unwound, so it never
        # issues a request inside a cancelled scope. Only the verdict is kept; the
        # original exception text never leaves this boundary.
        if server.transport == "http" and error_code(exc) == "probe_failed":
            if await unauthorized(cfg, timeout):
                raise ProbeAuth from None
        raise


def error_code(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        codes = [error_code(e) for e in exc.exceptions]
        for code in ("timeout", "catalog_limit", "auth_required", "configuration_unresolved"):
            if code in codes:
                return code
        return codes[0] if codes else "probe_failed"
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return "timeout"
    if isinstance(exc, ProbeAuth):
        return "auth_required"
    if isinstance(exc, ProbeLimit):
        return "catalog_limit"
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code in (401, 403):
        return "auth_required"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "configuration_unresolved"
    if isinstance(exc, FileNotFoundError):
        return "command_not_found"
    return "probe_failed"
