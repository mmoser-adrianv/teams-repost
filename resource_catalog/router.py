from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from auth import get_access_token

from .client import (
    ResourceCatalogueClient,
    ResourceCatalogueError,
    ResourceCatalogueResponseError,
    ResourceCatalogueTimeout,
)
from .models import ResourceSubmission
from .state import ResourceCatalogueState


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_resource_catalogue_router(
    settings: Any,
    *,
    client_factory: Callable[[], Any] | None = None,
    state: ResourceCatalogueState | None = None,
) -> APIRouter:
    router = APIRouter()
    catalogue_state = state or ResourceCatalogueState(settings.resource_catalog_state_path)
    state_lock = asyncio.Lock()

    def make_client() -> ResourceCatalogueClient:
        if client_factory is not None:
            return client_factory()
        return ResourceCatalogueClient(
            settings.resource_catalog_base_url,
            settings.resource_catalog_request_timeout_seconds,
        )

    def bearer_token() -> str | None:
        configured = settings.resource_catalog_api_token
        if configured is None:
            return None
        if hasattr(configured, "get_secret_value"):
            configured = configured.get_secret_value()
        configured = str(configured).strip()
        return configured or None

    @router.get("/resources")
    async def resources_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @router.get("/resources/app.js")
    async def resources_app() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")

    @router.get("/resources/styles.css")
    async def resources_styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")

    @router.get("/api/resources")
    async def list_resources(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            async with make_client() as client:
                catalogue = await client.fetch_catalogue()
            async with state_lock:
                changed = catalogue_state.record(catalogue)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {
            "catalogue": catalogue.model_dump(mode="json"),
            "changed": changed,
            "checked_at": _utc_now(),
            "poll_interval_seconds": settings.resource_catalog_poll_interval_seconds,
            "submission_enabled": bearer_token() is not None,
        }

    @router.post("/api/resources")
    async def create_resource(payload: ResourceSubmission, request: Request, response: Response) -> dict:
        get_access_token(request, settings)
        token = bearer_token()
        if token is None:
            raise HTTPException(status_code=503, detail="Resource catalogue submissions are not configured")
        response.headers["Cache-Control"] = "no-store"
        try:
            async with make_client() as client:
                result = await client.submit_resource(payload, token)
        except Exception as exc:
            raise _http_error(exc) from exc
        return result.model_dump(mode="json")

    return router


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ResourceCatalogueTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    if isinstance(exc, (ResourceCatalogueResponseError, ResourceCatalogueError)):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="The resource catalogue request could not be completed")
