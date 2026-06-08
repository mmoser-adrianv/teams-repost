from __future__ import annotations

import base64
import binascii
import io
import json
import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.sessions import SessionMiddleware

from auth import auth_status, complete_login_flow, create_login_flow, get_access_token, sign_out
from exception_list import ExceptionList, normalize_email
from file_copier import image_extension
from forwarder import (
    AttachmentRepostError,
    DestinationChannel,
    attachment_metadata,
    forward_message,
    repost_parsed_message,
    repost_translated_message,
)
from graph_client import GraphAPIError, GraphClient
from logging_config import configure_logging
from message_rebuilder import (
    extract_author_display_name,
    find_hosted_content_refs,
    normalize_body_to_html,
    sanitize_body_html_for_display,
    strip_attachment_placeholders,
)
from post_cache import PostCache
from repost_history import RepostHistory, build_manual_repost_record, build_repost_record
from settings import get_settings
from teams_url_parser import TeamsMessageLink
from teams_url_parser import TeamsUrlParseError
from translation_service import TranslationConfigurationError, TranslationError, translate_cached_post


settings = get_settings()
configure_logging()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Teams Repost Graph POC", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ForwardMessageRequest(BaseModel):
    source_message_url: str = Field(..., description="Teams channel message deep link")
    destination_team_id: str | None = None
    destination_team_name: str | None = None
    destination_channel_id: str | None = None
    destination_channel_name: str | None = None
    mode: Literal["dry_run", "post"] = "dry_run"

    @model_validator(mode="after")
    def validate_destination(self) -> "ForwardMessageRequest":
        if self.destination_team_name or self.destination_channel_name:
            raise ValueError("Destination name lookup is not supported in the channel-only permission flow; use IDs or env defaults.")
        return self


class CreateRepostRequest(BaseModel):
    source_message_id: str = Field(..., min_length=1)
    target_language: str | None = None


class ManualRepostRequest(BaseModel):
    source_message_id: str = Field(..., min_length=1)
    target_language: str | None = None


class TranslatePostRequest(BaseModel):
    target_language: str | None = None
    force: bool = False


class ExceptionEmailRequest(BaseModel):
    email: str = Field(..., min_length=1)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/login")
async def login(request: Request) -> RedirectResponse:
    return RedirectResponse(create_login_flow(request, settings))


@app.get("/auth/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    complete_login_flow(request, settings)
    return RedirectResponse("/")


@app.get("/auth/status")
async def status(request: Request) -> dict:
    return auth_status(request)


@app.post("/auth/logout")
async def logout(request: Request) -> dict:
    return sign_out(request)


@app.get("/api/posts")
async def list_posts(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    cursor: str | None = None,
    refresh: bool = True,
) -> dict:
    source = _configured_source()
    token = get_access_token(request, settings)
    history = RepostHistory(settings.repost_history_path)
    cache = PostCache(settings.post_cache_path)
    exceptions = ExceptionList(settings.exception_list_path)
    exception_emails = exceptions.email_set()
    page_size = min(limit, settings.post_list_limit)
    offset = 0 if refresh else (_decode_posts_cursor(cursor) if cursor else 0)
    cache_meta = {
        "last_refreshed_at": cache.source_status(source.team_id, source.channel_id)["last_refreshed_at"],
        "new_posts_saved": 0,
        "posts_skipped_by_exception": 0,
        "partial_refresh": False,
        "refresh_failed": False,
        "refresh_error": None,
    }

    if refresh:
        try:
            async with _graph(token) as graph:
                cache_meta.update(await _refresh_post_cache(graph, source, cache, exceptions))
        except GraphAPIError as exc:
            if not cache.has_posts(source.team_id, source.channel_id):
                raise HTTPException(status_code=_graph_http_status(exc.status_code), detail=str(exc)) from exc
            cache_meta.update(cache.source_status(source.team_id, source.channel_id))
            cache_meta["refresh_failed"] = True
            cache_meta["refresh_error"] = str(exc)

    page = cache.page_posts(source.team_id, source.channel_id, offset, page_size, exception_emails)
    return {
        "source": {"team_id": source.team_id, "channel_id": source.channel_id},
        "posts": [_with_repost_status(post, source, history, settings.openai_translation_target) for post in page["posts"]],
        "pagination": {
            "limit": page_size,
            "next_cursor": _encode_posts_cursor(page["next_offset"]) if page["next_offset"] is not None else None,
            "has_next": page["next_offset"] is not None,
        },
        "cache": cache_meta,
        "exceptions": {"emails": sorted(exception_emails)},
    }


@app.get("/api/exceptions")
async def list_exceptions(request: Request) -> dict:
    get_access_token(request, settings)
    return {"emails": ExceptionList(settings.exception_list_path).list_emails()}


@app.post("/api/exceptions")
async def add_exception(payload: ExceptionEmailRequest, request: Request) -> dict:
    get_access_token(request, settings)
    try:
        emails = ExceptionList(settings.exception_list_path).add(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"emails": emails}


@app.delete("/api/exceptions/{email}")
async def remove_exception(email: str, request: Request) -> dict:
    get_access_token(request, settings)
    try:
        emails = ExceptionList(settings.exception_list_path).remove(email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"emails": emails}


@app.post("/api/reposts")
async def create_repost(payload: CreateRepostRequest, request: Request) -> dict:
    source = _configured_source()
    destination = _configured_destination()
    history = RepostHistory(settings.repost_history_path)
    target_language = payload.target_language or settings.openai_translation_target
    existing = history.get(source.team_id, source.channel_id, payload.source_message_id, target_language)
    if existing:
        return {"status": "already_reposted", "record": existing}

    token = get_access_token(request, settings)
    cache = PostCache(settings.post_cache_path)
    cached_post = cache.get_post(source.team_id, source.channel_id, payload.source_message_id)
    if cached_post is None:
        raise HTTPException(status_code=404, detail="Cached post was not found")
    translation = (cached_post.get("translations") or {}).get(target_language)
    if translation is None:
        raise HTTPException(status_code=409, detail=f"Translate this post to {target_language} before reposting")

    parsed_source = TeamsMessageLink(
        tenant_id=None,
        team_id=source.team_id,
        source_channel_thread_id=source.channel_id,
        message_id=payload.source_message_id,
        parent_message_id=None,
    )
    async with _graph(token) as graph:
        try:
            report = await repost_translated_message(parsed_source, destination, graph, settings, translation, target_language)
        except AttachmentRepostError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except GraphAPIError as exc:
            raise HTTPException(status_code=_graph_http_status(exc.status_code), detail=str(exc)) from exc

    record = build_repost_record(source.team_id, source.channel_id, destination.team_id, destination.channel_id, report, target_language)
    history.upsert(record)
    return {"status": "reposted", "record": record, "report": report}


@app.post("/api/reposts/manual")
async def mark_repost_manually(payload: ManualRepostRequest, request: Request) -> dict:
    source = _configured_source()
    destination = _configured_destination()
    target_language = payload.target_language or settings.openai_translation_target
    get_access_token(request, settings)

    history = RepostHistory(settings.repost_history_path)
    existing = history.get(source.team_id, source.channel_id, payload.source_message_id, target_language)
    if existing:
        return {"status": "already_reposted", "record": existing}

    cache = PostCache(settings.post_cache_path)
    cached_post = cache.get_post(source.team_id, source.channel_id, payload.source_message_id)
    if cached_post is None:
        raise HTTPException(status_code=404, detail="Cached post was not found")

    record = build_manual_repost_record(
        source.team_id,
        source.channel_id,
        destination.team_id,
        destination.channel_id,
        cached_post,
        target_language,
    )
    history.upsert(record)
    return {"status": "marked_reposted", "record": record}


@app.post("/api/posts/{source_message_id}/translations")
async def translate_post(source_message_id: str, request: Request, payload: TranslatePostRequest | None = None) -> dict:
    payload = payload or TranslatePostRequest()
    source = _configured_source()
    get_access_token(request, settings)
    cache = PostCache(settings.post_cache_path)
    post = cache.get_post(source.team_id, source.channel_id, source_message_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Cached post was not found")

    target_language = payload.target_language or settings.openai_translation_target
    existing = (post.get("translations") or {}).get(target_language)
    if existing and not payload.force:
        return {"cached": True, "target_language": target_language, "translation": existing}

    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required to translate posts")

    try:
        translation = await _translate_post(post, target_language, settings)
    except TranslationConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TranslationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    cache.upsert_translation(source.team_id, source.channel_id, source_message_id, target_language, translation)
    return {"cached": False, "target_language": target_language, "translation": translation}


@app.get("/api/posts/{source_message_id}/images.zip")
async def download_all_embedded_images(source_message_id: str, request: Request) -> Response:
    source = _configured_source()
    token = get_access_token(request, settings)
    async with _graph(token) as graph:
        message = await graph.get_message(source.team_id, source.channel_id, source_message_id)
        refs = _hosted_image_refs(message)
        archive = io.BytesIO()
        errors: list[str] = []
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for ref in refs:
                try:
                    content, content_type = await graph.download_message_hosted_content(
                        source.team_id,
                        source.channel_id,
                        source_message_id,
                        ref.hosted_content_id,
                    )
                    zip_file.writestr(_image_file_name(ref.occurrence, content_type), content)
                except GraphAPIError as exc:
                    errors.append(f"Embedded image {ref.occurrence}: Microsoft Graph HTTP {exc.status_code}")
            if errors:
                zip_file.writestr("download-errors.txt", "\n".join(errors) + "\n")
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="teams-message-{source_message_id}-embedded-images.zip"'},
    )


@app.get("/api/posts/{source_message_id}/images/{occurrence}")
async def download_embedded_image(source_message_id: str, occurrence: int, request: Request) -> Response:
    source = _configured_source()
    token = get_access_token(request, settings)
    async with _graph(token) as graph:
        message = await graph.get_message(source.team_id, source.channel_id, source_message_id)
        ref = _hosted_image_ref(message, occurrence)
        content, content_type = await graph.download_message_hosted_content(
            source.team_id,
            source.channel_id,
            source_message_id,
            ref.hosted_content_id,
        )
    return Response(
        content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{_image_file_name(occurrence, content_type)}"'},
    )


@app.post("/forward-message")
async def forward_message_route(payload: ForwardMessageRequest, request: Request) -> dict:
    token = get_access_token(request, settings)
    async with _graph(token) as graph:
        try:
            return await forward_message(payload, graph, settings)
        except TeamsUrlParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except GraphAPIError as exc:
            raise HTTPException(status_code=_graph_http_status(exc.status_code), detail=str(exc)) from exc


def _graph_http_status(status_code: int) -> int:
    if status_code in {400, 401, 403, 404, 409}:
        return status_code
    return 502


def _graph(token: str) -> GraphClient:
    return GraphClient(
        token,
        base_url=settings.graph_base_url,
        timeout_seconds=settings.graph_request_timeout_seconds,
        max_retries=settings.graph_max_retries,
    )


async def _translate_post(post: dict, target_language: str, settings) -> dict:
    return await translate_cached_post(post, target_language, settings)


def _encode_posts_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_posts_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        offset = payload["offset"]
    except (KeyError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid posts cursor") from exc

    if not isinstance(offset, int) or offset < 0:
        raise HTTPException(status_code=400, detail="Invalid posts cursor")
    return offset


async def _refresh_post_cache(graph: GraphClient, source: DestinationChannel, cache: PostCache, exceptions: ExceptionList) -> dict:
    newest_cached_id = cache.newest_message_id(source.team_id, source.channel_id)
    new_posts: list[dict] = []
    posts_skipped_by_exception = 0
    next_url: str | None = None
    pages_checked = 0
    reached_cached_post = False
    partial_refresh = False

    while pages_checked < settings.post_cache_max_refresh_pages:
        page = await graph.list_channel_messages_page(source.team_id, source.channel_id, settings.post_list_limit, next_url)
        pages_checked += 1
        new_summaries: list[dict] = []

        for summary in page["messages"]:
            if newest_cached_id and summary.get("id") == newest_cached_id:
                reached_cached_post = True
                break
            new_summaries.append(summary)

        if new_summaries:
            hydrated = await _hydrate_messages(graph, source, new_summaries)
            for message in hydrated:
                if exceptions.contains(extract_author_email(message)):
                    posts_skipped_by_exception += 1
                    continue
                new_posts.append(_cached_post_summary(message))

        if reached_cached_post or not newest_cached_id or not page["next_link"]:
            break
        next_url = page["next_link"]
    else:
        partial_refresh = True

    result = cache.upsert_posts(source.team_id, source.channel_id, new_posts)
    return {
        "last_refreshed_at": result["last_refreshed_at"],
        "new_posts_saved": result["new_posts_saved"],
        "posts_skipped_by_exception": posts_skipped_by_exception,
        "partial_refresh": partial_refresh and not reached_cached_post,
        "refresh_failed": False,
        "refresh_error": None,
    }


async def _hydrate_messages(graph: GraphClient, source: DestinationChannel, messages: list[dict]) -> list[dict]:
    message_ids = [message["id"] for message in messages if message.get("id")]
    hydrated = await graph.get_channel_messages(source.team_id, source.channel_id, message_ids)
    if len(hydrated) == len(messages):
        return hydrated

    by_id = {message.get("id"): message for message in hydrated}
    return [by_id.get(message.get("id"), message) for message in messages]


def _configured_source() -> DestinationChannel:
    if not settings.source_team_id:
        raise HTTPException(status_code=400, detail="SOURCE_TEAM_ID is required")
    if not settings.source_channel_id:
        raise HTTPException(status_code=400, detail="SOURCE_CHANNEL_ID is required")
    return DestinationChannel(settings.source_team_id, settings.source_channel_id)


def _configured_destination() -> DestinationChannel:
    if not settings.destination_team_id:
        raise HTTPException(status_code=400, detail="DESTINATION_TEAM_ID is required")
    if not settings.destination_channel_id:
        raise HTTPException(status_code=400, detail="DESTINATION_CHANNEL_ID is required")
    return DestinationChannel(settings.destination_team_id, settings.destination_channel_id)


def _cached_post_summary(message: dict) -> dict:
    message_id = message.get("id") or ""
    refs = _hosted_image_refs(message)
    return {
        "id": message_id,
        "subject": message.get("subject"),
        "author": extract_author_display_name(message),
        "author_email": extract_author_email(message),
        "created_date_time": message.get("createdDateTime"),
        "web_url": message.get("webUrl"),
        "body_html": _body_html(message, message_id),
        "body_preview": _body_preview(message),
        "attachments": attachment_metadata(message.get("attachments") or []),
        "embedded_images": [
            {
                "occurrence": ref.occurrence,
                "hosted_content_id": ref.hosted_content_id,
                "download_url": f"/api/posts/{quote(message_id, safe='')}/images/{ref.occurrence}",
            }
            for ref in refs
        ],
        "embedded_images_zip_url": f"/api/posts/{quote(message_id, safe='')}/images.zip" if refs else None,
    }


def _post_summary(message: dict, source: DestinationChannel, history: RepostHistory) -> dict:
    return _with_repost_status(_cached_post_summary(message), source, history)


def _with_repost_status(post: dict, source: DestinationChannel, history: RepostHistory, target_language: str | None = None) -> dict:
    message_id = post.get("id") or ""
    record = history.get(source.team_id, source.channel_id, message_id, target_language) if message_id else None
    summary = dict(post)
    summary["reposted"] = bool(record)
    summary["repost"] = record.get("destination") if record else None
    summary["reposted_at"] = record.get("reposted_at") if record else None
    summary["repost_status"] = record.get("status") if record else None
    summary["manual_repost"] = bool(record.get("manual")) if record else False
    summary["warnings"] = record.get("warnings", []) if record else []
    return summary


def _hosted_image_refs(message: dict):
    return find_hosted_content_refs(strip_attachment_placeholders(normalize_body_to_html(message)))


def _hosted_image_ref(message: dict, occurrence: int):
    for ref in _hosted_image_refs(message):
        if ref.occurrence == occurrence:
            return ref
    raise HTTPException(status_code=404, detail=f"Embedded image {occurrence} was not found")


def _body_preview(message: dict) -> str:
    body_html = strip_attachment_placeholders(normalize_body_to_html(message))
    text = re.sub(r"<[^>]+>", " ", body_html)
    return re.sub(r"\s+", " ", unescape(text)).strip()[:240]


def extract_author_email(message: dict) -> str | None:
    sender = message.get("from") or {}
    for container_key in ("user", "application", "conversation"):
        container = sender.get(container_key) or {}
        for key in ("email", "mail", "userPrincipalName"):
            value = normalize_email(container.get(key))
            if value:
                return value
    for path in (("from", "emailAddress", "address"), ("sender", "emailAddress", "address")):
        value = message
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        normalized = normalize_email(value if isinstance(value, str) else None)
        if normalized:
            return normalized
    return None


def _body_html(message: dict, message_id: str) -> str:
    encoded_message_id = quote(message_id, safe="")

    def image_src(ref) -> str:
        return f"/api/posts/{encoded_message_id}/images/{ref.occurrence}"

    return sanitize_body_html_for_display(normalize_body_to_html(message), image_src)


def _image_file_name(occurrence: int, content_type: str) -> str:
    return f"embedded-image-{occurrence}{image_extension(content_type)}"
