# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""FastAPI application for OpenViking HTTP Server."""

import hmac
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

from openviking.server.config import ServerConfig, load_server_config
from openviking.server.dependencies import set_service
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS, ErrorInfo, Response
from openviking.server.routers import (
    content_router,
    debug_router,
    filesystem_router,
    observer_router,
    pack_router,
    relations_router,
    resources_router,
    search_router,
    sessions_router,
    system_router,
)
from openviking.service.core import OpenVikingService
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


def create_app(
    config: Optional[ServerConfig] = None,
    service: Optional[OpenVikingService] = None,
) -> FastAPI:
    """Create FastAPI application.

    Args:
        config: Server configuration. If None, loads from default location.
        service: Pre-initialized OpenVikingService (optional).

    Returns:
        FastAPI application instance
    """
    if config is None:
        config = load_server_config()

    mcp_path = config.mcp_path.rstrip("/") or "/mcp"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan handler."""
        nonlocal service
        if service is None:
            # Create and initialize service (reads config from ov.conf singleton)
            service = OpenVikingService()
            await service.initialize()
            logger.info("OpenVikingService initialized")

        set_service(service)
        if config.enable_mcp:
            async with app.state.mcp_lifespan(app):
                yield
        else:
            yield

        # Cleanup
        if service:
            await service.close()
            logger.info("OpenVikingService closed")

    app = FastAPI(
        title="OpenViking API",
        description="OpenViking HTTP Server - Agent-native context database",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store API key in app state for authentication
    app.state.api_key = config.api_key
    app.state.config = config

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add request timing middleware
    @app.middleware("http")
    async def add_timing(request: Request, call_next: Callable):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response

    @app.middleware("http")
    async def protect_mcp_endpoint(request: Request, call_next: Callable):
        is_mcp_request = request.url.path == mcp_path or request.url.path.startswith(f"{mcp_path}/")
        if config.enable_mcp and config.api_key and is_mcp_request:
            request_api_key = request.headers.get("X-API-Key")
            if not request_api_key:
                authorization = request.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    request_api_key = authorization[7:]

            if not request_api_key or not hmac.compare_digest(request_api_key, config.api_key):
                return JSONResponse(
                    status_code=401,
                    content=Response(
                        status="error",
                        error=ErrorInfo(
                            code="UNAUTHENTICATED",
                            message="Invalid API Key",
                        ),
                    ).model_dump(),
                )

        return await call_next(request)

    # Add exception handler for OpenVikingError
    @app.exception_handler(OpenVikingError)
    async def openviking_error_handler(request: Request, exc: OpenVikingError):
        http_status = ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500)
        return JSONResponse(
            status_code=http_status,
            content=Response(
                status="error",
                error=ErrorInfo(
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                ),
            ).model_dump(),
        )

    # Catch-all for unhandled exceptions so clients always get JSON
    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception in request handler")
        return JSONResponse(
            status_code=500,
            content=Response(
                status="error",
                error=ErrorInfo(
                    code="INTERNAL",
                    message=str(exc),
                ),
            ).model_dump(),
        )

    # Register routers
    app.include_router(system_router)
    app.include_router(resources_router)
    app.include_router(filesystem_router)
    app.include_router(content_router)
    app.include_router(search_router)
    app.include_router(relations_router)
    app.include_router(sessions_router)
    app.include_router(pack_router)
    app.include_router(debug_router)
    app.include_router(observer_router)

    # Expose the existing HTTP API as MCP tools on the configured MCP path.
    mcp_route_maps = []
    for tag in ("content", "filesystem", "resources", "search", "sessions", "relations"):
        mcp_route_maps.append(
            RouteMap(methods=["GET"], tags={tag}, mcp_type=MCPType.RESOURCE)
        )
        mcp_route_maps.append(
            RouteMap(
                methods=["POST", "DELETE"],
                tags={tag},
                mcp_type=MCPType.TOOL,
            )
        )
    mcp_route_maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))

    if config.enable_mcp:
        mcp_server = FastMCP.from_fastapi(
            app=app,
            name="OpenViking MCP",
            route_maps=mcp_route_maps,
        )
        mcp_app = mcp_server.http_app(path="/")
        app.mount(mcp_path, mcp_app)
        app.state.mcp_path = mcp_path
        app.state.mcp_lifespan = mcp_app.lifespan

    return app
