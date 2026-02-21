# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for MCP integration in server app wiring."""

import importlib
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest


def _load_app_module(monkeypatch):
    # Avoid importing heavy package-level side effects in this wiring test.
    repo_root = Path(__file__).resolve().parents[2]

    openviking_pkg = types.ModuleType("openviking")
    openviking_pkg.__path__ = [str(repo_root / "openviking")]
    monkeypatch.setitem(sys.modules, "openviking", openviking_pkg)

    server_pkg = types.ModuleType("openviking.server")
    server_pkg.__path__ = [str(repo_root / "openviking" / "server")]
    monkeypatch.setitem(sys.modules, "openviking.server", server_pkg)

    service_pkg = types.ModuleType("openviking.service")
    service_pkg.__path__ = [str(repo_root / "openviking" / "service")]
    monkeypatch.setitem(sys.modules, "openviking.service", service_pkg)

    stub_core = types.ModuleType("openviking.service.core")

    class _StubOpenVikingService:  # pragma: no cover - used only as a type holder
        pass

    stub_core.OpenVikingService = _StubOpenVikingService
    monkeypatch.setitem(sys.modules, "openviking.service.core", stub_core)

    stub_debug = types.ModuleType("openviking.service.debug_service")

    class _StubComponentStatus:  # pragma: no cover - used only for typing in router module
        pass

    class _StubSystemStatus:  # pragma: no cover - used only for typing in router module
        pass

    stub_debug.ComponentStatus = _StubComponentStatus
    stub_debug.SystemStatus = _StubSystemStatus
    monkeypatch.setitem(sys.modules, "openviking.service.debug_service", stub_debug)

    if "openviking.server.dependencies" in sys.modules:
        del sys.modules["openviking.server.dependencies"]
    if "openviking.server.app" in sys.modules:
        del sys.modules["openviking.server.app"]

    return importlib.import_module("openviking.server.app")


class _FakeMCPApp:
    @asynccontextmanager
    async def lifespan(self, _app):
        yield

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


class _FakeMCPServer:
    def __init__(self, captured):
        self._captured = captured

    def http_app(self, path: str = "/mcp"):
        self._captured["http_app_path"] = path
        return _FakeMCPApp()


def _patch_fastmcp(monkeypatch, app_module, captured):
    class _FakeFastMCP:
        @classmethod
        def from_fastapi(
            cls,
            app,
            name=None,
            route_maps=None,
            httpx_client_kwargs=None,
            **_,
        ):
            captured["app"] = app
            captured["name"] = name
            captured["route_maps"] = route_maps
            captured["httpx_client_kwargs"] = httpx_client_kwargs
            return _FakeMCPServer(captured)

    monkeypatch.setattr(app_module, "FastMCP", _FakeFastMCP)


def test_mcp_mount_and_api_key_passthrough(monkeypatch):
    app_module = _load_app_module(monkeypatch)
    captured = {}
    _patch_fastmcp(monkeypatch, app_module, captured)

    app = app_module.create_app(config=app_module.ServerConfig(api_key="test-key"))

    assert captured["app"] is app
    assert captured["name"] == "OpenViking MCP"
    assert captured["httpx_client_kwargs"] is None
    assert isinstance(captured["route_maps"], list)
    expected_tags = {"content", "filesystem", "resources", "search", "sessions", "relations"}
    route_maps = captured["route_maps"]

    for tag in expected_tags:
        assert any(
            route_map.tags == {tag}
            and route_map.methods == ["GET"]
            and route_map.mcp_type.name == "RESOURCE"
            for route_map in route_maps
        )
        assert any(
            route_map.tags == {tag}
            and route_map.methods == ["POST", "DELETE"]
            and route_map.mcp_type.name == "TOOL"
            for route_map in route_maps
        )
    assert route_maps[-1].mcp_type.name == "EXCLUDE"
    assert captured["http_app_path"] == "/"
    assert app.state.mcp_path == "/mcp"


def test_mcp_no_passthrough_header_without_api_key(monkeypatch):
    app_module = _load_app_module(monkeypatch)
    captured = {}
    _patch_fastmcp(monkeypatch, app_module, captured)

    app_module.create_app(config=app_module.ServerConfig(api_key=None))

    assert captured["httpx_client_kwargs"] is None


@pytest.mark.asyncio
async def test_mcp_endpoint_requires_api_key(monkeypatch):
    app_module = _load_app_module(monkeypatch)
    captured = {}
    _patch_fastmcp(monkeypatch, app_module, captured)

    app = app_module.create_app(config=app_module.ServerConfig(api_key="test-key"))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthorized = await client.post("/mcp")
        authorized = await client.post("/mcp/", headers={"X-API-Key": "test-key"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 204


@pytest.mark.asyncio
async def test_mcp_endpoint_without_server_api_key(monkeypatch):
    app_module = _load_app_module(monkeypatch)
    captured = {}
    _patch_fastmcp(monkeypatch, app_module, captured)

    app = app_module.create_app(config=app_module.ServerConfig(api_key=None))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp/")

    assert response.status_code == 204
