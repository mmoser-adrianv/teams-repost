from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import get_access_token
from graph_client import GraphAPIError, GraphClient

from .config import get_reply_sync_settings
from .graph import ReplyGraph
from .locking import ReplySyncAlreadyRunning, ReplySyncLock
from .service import ReplySequenceConflict, ReplySyncService


STATIC_DIR = Path(__file__).resolve().parent / "static"


class ActivateThreadRequest(BaseModel):
    start_mode: str = Field(pattern="^(backfill_all|future_only)$")


class LinkDestinationRequest(BaseModel):
    destination_url: str = Field(min_length=1)


class DegradedSendRequest(BaseModel):
    confirm: bool


def create_reply_sync_router(core_settings: Any) -> APIRouter:
    router = APIRouter()
    reply_settings = get_reply_sync_settings()

    def service() -> ReplySyncService:
        return ReplySyncService(reply_settings, core_settings)

    def require_auth(request: Request) -> None:
        get_access_token(request, core_settings)

    def require_enabled() -> None:
        if not reply_settings.enabled:
            raise HTTPException(status_code=409, detail="Reply synchronization is disabled by REPLY_SYNC_ENABLED")

    @asynccontextmanager
    async def request_graph(request: Request) -> AsyncIterator[ReplyGraph]:
        token = get_access_token(request, core_settings)
        async with GraphClient(
            token,
            base_url=core_settings.graph_base_url,
            timeout_seconds=core_settings.graph_request_timeout_seconds,
            max_retries=core_settings.graph_max_retries,
        ) as client:
            yield ReplyGraph(client)

    @router.get("/reply-sync")
    async def reply_sync_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @router.get("/reply-sync/app.js")
    async def reply_sync_script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")

    @router.get("/reply-sync/styles.css")
    async def reply_sync_styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")

    @router.get("/api/reply-sync/threads")
    async def list_threads(request: Request) -> dict[str, Any]:
        require_auth(request)
        return service().list_threads()

    @router.post("/api/reply-sync/discover")
    async def discover_threads(request: Request) -> dict[str, Any]:
        require_auth(request)
        result = service().discover()
        return {"status": "discovered", **result}

    @router.post("/api/reply-sync/threads/{thread_key}/activate")
    async def activate_thread(thread_key: str, payload: ActivateThreadRequest, request: Request) -> dict[str, Any]:
        require_auth(request)
        return _call(lambda: service().activate(thread_key, payload.start_mode))

    @router.post("/api/reply-sync/threads/{thread_key}/pause")
    async def pause_thread(thread_key: str, request: Request) -> dict[str, Any]:
        require_auth(request)
        return _call(lambda: service().pause(thread_key))

    @router.post("/api/reply-sync/threads/{thread_key}/link")
    async def link_thread(thread_key: str, payload: LinkDestinationRequest, request: Request) -> dict[str, Any]:
        try:
            async with request_graph(request) as graph:
                return await service().link_destination(thread_key, payload.destination_url, graph)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/reply-sync/threads/{thread_key}/run")
    async def run_thread(thread_key: str, request: Request) -> dict[str, Any]:
        require_auth(request)
        require_enabled()
        try:
            with ReplySyncLock(reply_settings.lock_path):
                async with request_graph(request) as graph:
                    return await service().run_thread(thread_key, graph)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/reply-sync/threads/{thread_key}/retry")
    async def retry_thread(thread_key: str, request: Request) -> dict[str, Any]:
        require_auth(request)
        require_enabled()
        try:
            with ReplySyncLock(reply_settings.lock_path):
                sync_service = service()
                sync_service.retry(thread_key)
                async with request_graph(request) as graph:
                    return await sync_service.run_thread(thread_key, graph)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/reply-sync/threads/{thread_key}/replies/{reply_id}/send-degraded")
    async def send_degraded(
        thread_key: str,
        reply_id: str,
        payload: DegradedSendRequest,
        request: Request,
    ) -> dict[str, Any]:
        require_auth(request)
        require_enabled()
        if not payload.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required")
        try:
            with ReplySyncLock(reply_settings.lock_path):
                async with request_graph(request) as graph:
                    return await service().send_degraded(thread_key, reply_id, graph)
        except Exception as exc:
            raise _http_error(exc) from exc

    return router


def _call(operation):
    try:
        return operation()
    except Exception as exc:
        raise _http_error(exc) from exc


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Reply-sync thread or reply was not found")
    if isinstance(exc, ReplySyncAlreadyRunning):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReplySequenceConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, GraphAPIError):
        status = exc.status_code if exc.status_code in {400, 401, 403, 404, 409} else 502
        return HTTPException(status_code=status, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Reply synchronization failed")
