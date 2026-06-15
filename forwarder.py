from __future__ import annotations

import html
import logging
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from graph_client import GraphAPIError, GraphClient
from message_rebuilder import (
    DisplayImageRef,
    HostedContentRef,
    HostedContentUpload,
    LinkReplacement,
    ReferenceAttachment,
    append_attachment_placeholders,
    build_channel_message_payload,
    build_forward_body,
    build_forward_subject,
    extract_author_display_name,
    find_display_image_refs,
    find_hosted_content_refs,
    normalize_body_to_html,
    replace_hosted_content_refs,
    replace_hosted_content_with_links,
    replace_display_image_refs,
    strip_attachment_placeholders,
)
from settings import Settings
from teams_url_parser import TeamsMessageLink, parse_teams_message_url


logger = logging.getLogger(__name__)

_IMAGE_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_SENSITIVE_GRAPH_RESPONSE_KEYS = ("authorization", "contentbytes", "password", "secret", "token")


class ForwardRequestLike(Protocol):
    source_message_url: str
    destination_team_id: str | None
    destination_channel_id: str | None
    mode: str


@dataclass(frozen=True)
class DestinationChannel:
    team_id: str
    channel_id: str


@dataclass(frozen=True)
class LinkedAttachment:
    name: str
    content_url: str
    status: str = "linked_reference_image"

    def to_status(self) -> dict[str, str]:
        return {
            "name": self.name,
            "content_url": self.content_url,
            "status": self.status,
        }


@dataclass(frozen=True)
class AttachmentPlan:
    reference_attachments: list[ReferenceAttachment]
    linked_attachments: list[LinkedAttachment]

    def statuses(self) -> list[dict[str, str]]:
        return [attachment.to_status() for attachment in self.reference_attachments] + [
            attachment.to_status() for attachment in self.linked_attachments
        ]


class AttachmentRepostError(ValueError):
    pass


async def forward_message(request: ForwardRequestLike, graph: GraphClient, settings: Settings) -> dict[str, Any]:
    parsed_source = parse_teams_message_url(request.source_message_url)
    destination = _resolve_destination(request.destination_team_id, request.destination_channel_id, settings)
    return await repost_parsed_message(parsed_source, destination, graph, settings, request.mode)


async def repost_parsed_message(
    parsed_source: TeamsMessageLink,
    destination: DestinationChannel,
    graph: GraphClient,
    settings: Settings,
    mode: str = "post",
) -> dict[str, Any]:
    source_message = await graph.get_message(
        parsed_source.team_id,
        parsed_source.source_channel_thread_id,
        parsed_source.message_id,
        parsed_source.parent_message_id,
    )
    parsed_source = _with_source_web_url(parsed_source, source_message)
    hosted_contents, hosted_warnings = await _safe_list_hosted_contents(parsed_source, graph)
    original_body_html = strip_attachment_placeholders(normalize_body_to_html(source_message))
    hosted_refs = find_hosted_content_refs(original_body_html)
    warnings = list(hosted_warnings)
    warnings.extend(_hosted_content_warnings(hosted_refs, hosted_contents))

    attachments = source_message.get("attachments") or []
    attachment_links = attachment_metadata(attachments)
    attachment_plan = build_attachment_plan(attachments)
    reference_attachments = attachment_plan.reference_attachments
    linked_attachments = attachment_plan.linked_attachments
    attachment_statuses = attachment_plan.statuses()

    if mode == "dry_run":
        return _dry_run_report(
            parsed_source,
            destination,
            source_message,
            hosted_refs,
            attachment_links,
            warnings,
            settings.try_inline_hosted_contents,
            attachment_statuses,
        )

    subject = build_forward_subject(source_message)

    downloaded_images, image_download_warnings = await _download_hosted_images(parsed_source, hosted_refs, graph)
    warnings.extend(image_download_warnings)

    image_statuses: list[dict[str, Any]] = []
    downloaded_occurrences = {upload.occurrence for upload in downloaded_images}
    failed_download_refs = [ref for ref in hosted_refs if ref.occurrence not in downloaded_occurrences]
    should_try_inline = settings.try_inline_hosted_contents and bool(downloaded_images)
    image_diagnostics: list[dict[str, Any]] = []

    if should_try_inline:
        failed_download_links = _image_placeholder_links(failed_download_refs, parsed_source, settings)
        inline_body = replace_hosted_content_refs(original_body_html, downloaded_images, failed_download_links)
        body = _body_with_reference_attachments(
            build_forward_body(source_message, parsed_source, inline_body, [], warnings),
            reference_attachments,
            linked_attachments,
        )
        payload = build_channel_message_payload(subject, body, downloaded_images, reference_attachments)
        try:
            new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, payload)
            for upload in downloaded_images:
                image_statuses.append({"occurrence": upload.occurrence, "status": "recreated_inline"})
                logger.info("Inline image recreated", extra={"occurrence": upload.occurrence})
            for ref in failed_download_refs:
                image_statuses.append({"occurrence": ref.occurrence, "status": "download_failed_linked"})
                logger.info("Inline image linked after download failure", extra={"occurrence": ref.occurrence})
            return _post_report(
                parsed_source,
                source_message,
                new_message,
                attachment_links,
                warnings,
                image_statuses,
                attachment_statuses,
                image_diagnostics,
            )
        except GraphAPIError as exc:
            diagnostics = _inline_failure_diagnostics(exc, downloaded_images)
            image_diagnostics.append(diagnostics)
            warning = f"Inline image recreation failed with Microsoft Graph HTTP {exc.status_code}; images were replaced with fallback links in the Teams repost."
            warnings.append(warning)
            logger.warning(
                "Inline hosted content post failed, posting without embedded images",
                extra={"inline_image_diagnostics": diagnostics},
            )

    fallback_body_html = original_body_html
    if hosted_refs:
        links = _image_placeholder_links(hosted_refs, parsed_source, settings)
        image_statuses = _fallback_statuses(hosted_refs)
        fallback_body_html = replace_hosted_content_with_links(original_body_html, links)
        for status in image_statuses:
            logger.info("Inline image omitted from repost", extra=status)

    body = _body_with_reference_attachments(
        build_forward_body(source_message, parsed_source, fallback_body_html, [], warnings),
        reference_attachments,
        linked_attachments,
    )
    payload = build_channel_message_payload(subject, body, attachments=reference_attachments)
    new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, payload)
    return _post_report(parsed_source, source_message, new_message, attachment_links, warnings, image_statuses, attachment_statuses, image_diagnostics)


async def repost_translated_message(
    parsed_source: TeamsMessageLink,
    destination: DestinationChannel,
    graph: GraphClient,
    settings: Settings,
    translation: dict[str, Any],
    target_language: str,
) -> dict[str, Any]:
    source_message = await graph.get_message(
        parsed_source.team_id,
        parsed_source.source_channel_thread_id,
        parsed_source.message_id,
        parsed_source.parent_message_id,
    )
    parsed_source = _with_source_web_url(parsed_source, source_message)
    hosted_contents, hosted_warnings = await _safe_list_hosted_contents(parsed_source, graph)
    original_body_html = strip_attachment_placeholders(normalize_body_to_html(source_message))
    hosted_refs = find_hosted_content_refs(original_body_html)
    warnings = list(hosted_warnings)
    warnings.extend(_hosted_content_warnings(hosted_refs, hosted_contents))

    attachments = source_message.get("attachments") or []
    attachment_links = attachment_metadata(attachments)
    attachment_plan = build_attachment_plan(attachments)
    reference_attachments = attachment_plan.reference_attachments
    linked_attachments = attachment_plan.linked_attachments
    attachment_statuses = attachment_plan.statuses()

    subject = _translated_forward_subject(translation, source_message)
    translated_body_html = translation.get("body_html") or ""
    display_refs = find_display_image_refs(translated_body_html)
    downloaded_images, image_download_warnings = await _download_hosted_images(parsed_source, hosted_refs, graph)
    warnings.extend(image_download_warnings)

    image_statuses: list[dict[str, Any]] = []
    downloaded_occurrences = {upload.occurrence for upload in downloaded_images}
    failed_download_refs = [ref for ref in hosted_refs if ref.occurrence not in downloaded_occurrences]
    should_try_inline = settings.try_inline_hosted_contents and bool(downloaded_images)
    image_diagnostics: list[dict[str, Any]] = []

    if should_try_inline:
        failed_download_links = _image_placeholder_links(failed_download_refs, parsed_source, settings, display_refs)
        inline_body = replace_display_image_refs(translated_body_html, downloaded_images, failed_download_links)
        body = _body_with_reference_attachments(
            build_forward_body(source_message, parsed_source, inline_body, [], warnings),
            reference_attachments,
            linked_attachments,
        )
        payload = build_channel_message_payload(subject, body, downloaded_images, reference_attachments)
        try:
            new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, payload)
            for upload in downloaded_images:
                image_statuses.append({"occurrence": upload.occurrence, "status": "recreated_inline"})
                logger.info("Inline image recreated", extra={"occurrence": upload.occurrence})
            for ref in failed_download_refs:
                image_statuses.append({"occurrence": ref.occurrence, "status": "download_failed_linked"})
                logger.info("Inline image linked after download failure", extra={"occurrence": ref.occurrence})
            report = _post_report(
                parsed_source,
                source_message,
                new_message,
                attachment_links,
                warnings,
                image_statuses,
                attachment_statuses,
                image_diagnostics,
            )
            report["translation_target_language"] = target_language
            return report
        except GraphAPIError as exc:
            diagnostics = _inline_failure_diagnostics(exc, downloaded_images)
            image_diagnostics.append(diagnostics)
            warning = f"Inline image recreation failed with Microsoft Graph HTTP {exc.status_code}; images were replaced with fallback links in the Teams repost."
            warnings.append(warning)
            logger.warning(
                "Inline hosted content post failed, posting without embedded images",
                extra={"inline_image_diagnostics": diagnostics},
            )

    fallback_body_html = translated_body_html
    if hosted_refs:
        links = _image_placeholder_links(hosted_refs, parsed_source, settings, display_refs)
        image_statuses = _fallback_statuses(hosted_refs)
        fallback_body_html = replace_display_image_refs(translated_body_html, [], links)
        for status in image_statuses:
            logger.info("Inline image omitted from repost", extra=status)

    body = _body_with_reference_attachments(
        build_forward_body(source_message, parsed_source, fallback_body_html, [], warnings),
        reference_attachments,
        linked_attachments,
    )
    payload = build_channel_message_payload(subject, body, attachments=reference_attachments)
    new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, payload)
    report = _post_report(parsed_source, source_message, new_message, attachment_links, warnings, image_statuses, attachment_statuses, image_diagnostics)
    report["translation_target_language"] = target_language
    return report


def _resolve_destination(team_id: str | None, channel_id: str | None, settings: Settings) -> DestinationChannel:
    resolved_team_id = team_id or settings.destination_team_id
    resolved_channel_id = channel_id or settings.destination_channel_id
    if not resolved_team_id:
        raise ValueError("destination_team_id or DESTINATION_TEAM_ID is required")
    if not resolved_channel_id:
        raise ValueError("destination_channel_id or DESTINATION_CHANNEL_ID is required")
    return DestinationChannel(resolved_team_id, resolved_channel_id)


def _with_source_web_url(parsed_source: TeamsMessageLink, source_message: dict[str, Any]) -> TeamsMessageLink:
    if parsed_source.raw_url:
        return parsed_source
    source_url = source_message.get("webUrl")
    return replace(parsed_source, raw_url=source_url) if source_url else parsed_source


async def _safe_list_hosted_contents(parsed_source: TeamsMessageLink, graph: GraphClient) -> tuple[list[dict], list[str]]:
    try:
        return (
            await graph.get_message_hosted_contents(
                parsed_source.team_id,
                parsed_source.source_channel_thread_id,
                parsed_source.message_id,
                parsed_source.parent_message_id,
            ),
            [],
        )
    except GraphAPIError as exc:
        return [], [f"Could not list hosted contents: Microsoft Graph HTTP {exc.status_code}."]


def _hosted_content_warnings(refs: list[HostedContentRef], hosted_contents: list[dict]) -> list[str]:
    warnings: list[str] = []
    listed_ids = {item.get("id") for item in hosted_contents}
    for ref in refs:
        if listed_ids and ref.hosted_content_id not in listed_ids:
            warnings.append(f"Inline image {ref.occurrence} references hosted content not returned by Graph list-hostedContents.")
    if refs:
        warnings.append("Inline hosted images will be attempted in the repost; if Graph rejects them, the repost manager will keep download links.")
    return warnings


async def _download_hosted_images(
    parsed_source: TeamsMessageLink,
    refs: list[HostedContentRef],
    graph: GraphClient,
) -> tuple[list[HostedContentUpload], list[str]]:
    uploads: list[HostedContentUpload] = []
    warnings: list[str] = []
    for ref in refs:
        try:
            content, content_type = await graph.download_message_hosted_content(
                parsed_source.team_id,
                parsed_source.source_channel_thread_id,
                parsed_source.message_id,
                ref.hosted_content_id,
                parsed_source.parent_message_id,
            )
            uploads.append(
                HostedContentUpload(
                    occurrence=ref.occurrence,
                    original_id=ref.hosted_content_id,
                    temporary_id=str(ref.occurrence),
                    content_type=content_type or "application/octet-stream",
                    content_bytes=content,
                )
            )
        except GraphAPIError as exc:
            warnings.append(f"Inline image {ref.occurrence} could not be downloaded: Microsoft Graph HTTP {exc.status_code}.")
    return uploads, warnings


def _image_placeholder_links(
    refs: list[HostedContentRef],
    parsed_source: TeamsMessageLink,
    settings: Settings,
    display_refs: list[DisplayImageRef] | None = None,
) -> list[LinkReplacement]:
    manager_links = _manager_image_links(settings, display_refs or [])
    links: list[LinkReplacement] = []
    for ref in refs:
        manager_link = manager_links.get(ref.occurrence)
        if manager_link:
            links.append(LinkReplacement(ref.occurrence, manager_link, f"Open embedded image {ref.occurrence} in repost manager"))
            continue
        if parsed_source.raw_url:
            links.append(LinkReplacement(ref.occurrence, parsed_source.raw_url, f"Open original message for embedded image {ref.occurrence}"))
            continue
        links.append(LinkReplacement(ref.occurrence, "", f"Embedded image {ref.occurrence} could not be recreated"))
    return links


def _manager_image_links(settings: Settings, display_refs: list[DisplayImageRef]) -> dict[int, str]:
    if not settings.app_base_url:
        return {}
    links: dict[int, str] = {}
    for ref in display_refs:
        if not ref.src.startswith("/"):
            continue
        links[ref.occurrence] = settings.app_base_url.rstrip("/") + ref.src
    return links


def _fallback_statuses(refs: list[HostedContentRef]) -> list[dict[str, Any]]:
    return [
        {
            "occurrence": ref.occurrence,
            "status": "omitted_from_repost",
        }
        for ref in refs
    ]


def build_reference_attachments(attachments: list[dict]) -> list[ReferenceAttachment]:
    return build_attachment_plan(attachments).reference_attachments


def build_attachment_plan(attachments: list[dict]) -> AttachmentPlan:
    reference_attachments: list[ReferenceAttachment] = []
    linked_attachments: list[LinkedAttachment] = []
    for index, attachment in enumerate(attachments, start=1):
        name = attachment.get("name") or f"attachment-{index}"
        content_type = (attachment.get("contentType") or "").lower()
        content_url = attachment.get("contentUrl")
        if content_type != "reference":
            raise AttachmentRepostError(
                f"Attachment '{name}' cannot be reposted as a native Teams attachment card "
                f"because its contentType is '{attachment.get('contentType') or 'missing'}', not 'reference'."
            )
        if not content_url:
            raise AttachmentRepostError(
                f"Attachment '{name}' cannot be reposted as a native Teams attachment card because it has no contentUrl."
            )
        if _is_image_reference_attachment(name, content_url):
            linked_attachments.append(LinkedAttachment(name=name, content_url=content_url))
            continue
        reference_attachments.append(
            ReferenceAttachment(
                id=str(uuid.uuid4()),
                name=name,
                content_url=content_url,
            )
        )
    return AttachmentPlan(reference_attachments, linked_attachments)


def _body_with_reference_attachments(
    body_html: str,
    attachments: list[ReferenceAttachment],
    linked_attachments: list[LinkedAttachment],
) -> str:
    return append_attachment_placeholders(_append_linked_attachment_links(body_html, linked_attachments), attachments)


def _append_linked_attachment_links(body_html: str, linked_attachments: list[LinkedAttachment]) -> str:
    if not linked_attachments:
        return body_html
    parts = [body_html or "<p></p>", "<p><strong>Image attachments:</strong></p><ul>"]
    for attachment in linked_attachments:
        parts.append(
            '<li><a href="'
            + html.escape(attachment.content_url, quote=True)
            + '">'
            + html.escape(attachment.name)
            + "</a></li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def _is_image_reference_attachment(name: str, content_url: str) -> bool:
    suffixes = (Path(name).suffix.lower(), Path(urlparse(content_url).path).suffix.lower())
    return any(suffix in _IMAGE_ATTACHMENT_EXTENSIONS for suffix in suffixes)


def _translated_forward_subject(translation: dict[str, Any], source_message: dict) -> str:
    subject = str(translation.get("subject") or "").strip()
    if not subject:
        subject = build_forward_subject(source_message).strip()
    if not subject:
        subject = "Teams message"
    return subject[:100]


def _dry_run_report(
    parsed_source: TeamsMessageLink,
    destination: DestinationChannel,
    source_message: dict,
    hosted_refs: list[HostedContentRef],
    attachment_links: list[dict[str, Any]],
    warnings: list[str],
    try_inline_hosted_contents: bool,
    attachment_statuses: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "would_post": False,
        "parsed_source_identifiers": parsed_source.to_report(),
        "destination": {
            "team": {"id": destination.team_id},
            "channel": {"id": destination.channel_id},
        },
        "original_subject": source_message.get("subject"),
        "original_author": extract_author_display_name(source_message),
        "detected_inline_image_count": len(hosted_refs),
        "detected_attachment_count": len(attachment_links),
        "attachment_links": attachment_links,
        "attachment_statuses": attachment_statuses,
        "warnings": warnings,
        "inline_images_can_be_recreated": bool(try_inline_hosted_contents and hosted_refs),
    }


def _inline_failure_diagnostics(exc: GraphAPIError, uploads: list[HostedContentUpload]) -> dict[str, Any]:
    return {
        "status": "graph_rejected_hosted_contents",
        "status_code": exc.status_code,
        "message": str(exc)[:500],
        "response_body": _sanitize_graph_response(exc.response_body),
        "image_count": len(uploads),
        "content_types": sorted({upload.content_type for upload in uploads}),
        "byte_sizes": [len(upload.content_bytes) for upload in uploads],
        "occurrences": [upload.occurrence for upload in uploads],
    }


def _sanitize_graph_response(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(sensitive in lowered for sensitive in _SENSITIVE_GRAPH_RESPONSE_KEYS):
                output[key_text] = "[redacted]"
            else:
                output[key_text] = _sanitize_graph_response(item, depth + 1)
        return output
    if isinstance(value, list):
        return [_sanitize_graph_response(item, depth + 1) for item in value[:25]]
    if isinstance(value, bytes):
        return f"[{len(value)} bytes redacted]"
    if isinstance(value, str):
        return value[:1000]
    return value


def _post_report(
    parsed_source: TeamsMessageLink,
    source_message: dict,
    new_message: dict,
    attachment_links: list[dict[str, Any]],
    warnings: list[str],
    image_statuses: list[dict[str, Any]],
    attachment_statuses: list[dict[str, Any]],
    image_diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "post",
        "source_message_id": parsed_source.message_id,
        "source_message_web_url": source_message.get("webUrl") or parsed_source.raw_url,
        "source_subject": source_message.get("subject"),
        "source_author": extract_author_display_name(source_message),
        "source_created_date_time": source_message.get("createdDateTime"),
        "new_message_id": new_message.get("id"),
        "new_message_web_url": new_message.get("webUrl"),
        "attachment_links": attachment_links,
        "attachment_statuses": attachment_statuses,
        "inline_image_statuses": image_statuses,
        "inline_image_diagnostics": image_diagnostics or [],
        "warnings": warnings,
    }


def attachment_metadata(attachments: list[dict]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        output.append(
            {
                "id": attachment.get("id"),
                "name": attachment.get("name") or f"attachment-{index}",
                "content_type": attachment.get("contentType"),
                "content_url": attachment.get("contentUrl"),
            }
        )
    return output


def _attachment_warnings(attachments: list[dict[str, Any]]) -> list[str]:
    return []
