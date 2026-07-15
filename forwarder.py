from __future__ import annotations

import html
import json
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from graph_client import GraphAPIError, GraphClient
from message_rebuilder import (
    HostedContentRef,
    HostedContentUpload,
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
    replace_display_image_refs,
    strip_attachment_placeholders,
)
from settings import Settings
from teams_url_parser import TeamsMessageLink, parse_teams_message_url


logger = logging.getLogger(__name__)

_SENSITIVE_GRAPH_RESPONSE_KEYS = ("authorization", "contentbytes", "password", "secret", "token")
_GRAPH_MESSAGE_PAYLOAD_LIMIT_BYTES = 4 * 1024 * 1024
_GRAPH_MESSAGE_PAYLOAD_SAFETY_BYTES = 64 * 1024
_GRAPH_MESSAGE_PAYLOAD_TARGET_BYTES = _GRAPH_MESSAGE_PAYLOAD_LIMIT_BYTES - _GRAPH_MESSAGE_PAYLOAD_SAFETY_BYTES
_SUPPORTED_INLINE_HOSTED_CONTENT_TYPES = {"image/jpg", "image/jpeg", "image/png"}
_ANNOUNCEMENT_BANNER_CONTENT_TYPE = "application/vnd.microsoft.teams.messaging-announcementbanner"
_OMITTABLE_TEAMS_CARD_CONTENT_TYPES = {
    "application/vnd.microsoft.teams.card.o365connector",
    "tabreference",
}


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
class PreparedAttachments:
    reference_attachments: list[ReferenceAttachment]
    statuses: list[dict[str, Any]]


@dataclass(frozen=True)
class InlinePayloadOmission:
    upload: HostedContentUpload
    reason: str


@dataclass(frozen=True)
class InlinePayloadPlan:
    payload: dict[str, Any]
    included_uploads: list[HostedContentUpload]
    omitted_uploads: list[InlinePayloadOmission]
    warnings: list[str]
    diagnostics: list[dict[str, Any]]


class RepostFidelityError(ValueError):
    pass


class AttachmentRepostError(RepostFidelityError):
    pass


class InlineImageRepostError(RepostFidelityError):
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
    attachment_statuses = _pending_attachment_statuses(attachments)
    warnings.extend(_attachment_warnings(attachments))

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
    prepared_attachments = _prepare_attachments_for_repost(attachments)
    downloaded_images = await _download_required_hosted_images(parsed_source, hosted_refs, graph, settings)
    plan = _build_payload_with_inline_budget(
        subject=subject,
        source_message=source_message,
        parsed_source=parsed_source,
        content_body_html=original_body_html,
        uploads=downloaded_images,
        attachments=prepared_attachments.reference_attachments,
        warnings=warnings,
        image_replacer=replace_hosted_content_refs,
    )
    try:
        new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, plan.payload)
    except GraphAPIError as exc:
        _log_inline_failure_if_present(exc, plan.included_uploads)
        raise

    image_statuses = _recreated_image_statuses(plan.included_uploads) + _omitted_image_statuses(plan.omitted_uploads)
    for status in image_statuses:
        if status["status"] == "recreated_inline":
            logger.info("Inline image recreated", extra=status)
        else:
            logger.warning("Inline image omitted from repost payload", extra=status)
    return _post_report(
        parsed_source,
        source_message,
        new_message,
        attachment_links,
        plan.warnings,
        image_statuses,
        prepared_attachments.statuses,
        plan.diagnostics,
    )


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
    attachment_statuses = _pending_attachment_statuses(attachments)
    warnings.extend(_attachment_warnings(attachments))

    subject = _translated_forward_subject(translation, source_message)
    translated_body_html = translation.get("body_html") or ""
    display_refs = find_display_image_refs(translated_body_html)

    prepared_attachments = _prepare_attachments_for_repost(attachments)
    downloaded_images = await _download_required_hosted_images(parsed_source, hosted_refs, graph, settings)
    plan = _build_payload_with_inline_budget(
        subject=subject,
        source_message=source_message,
        parsed_source=parsed_source,
        content_body_html=translated_body_html,
        uploads=downloaded_images,
        attachments=prepared_attachments.reference_attachments,
        warnings=warnings,
        image_replacer=replace_display_image_refs,
        display_refs=display_refs,
        target_language=target_language,
    )
    try:
        new_message = await graph.create_channel_message(destination.team_id, destination.channel_id, plan.payload)
    except GraphAPIError as exc:
        _log_inline_failure_if_present(exc, plan.included_uploads)
        raise

    image_statuses = _recreated_image_statuses(plan.included_uploads) + _omitted_image_statuses(plan.omitted_uploads)
    for status in image_statuses:
        if status["status"] == "recreated_inline":
            logger.info("Inline image recreated", extra=status)
        else:
            logger.warning("Inline image omitted from repost payload", extra=status)
    report = _post_report(
        parsed_source,
        source_message,
        new_message,
        attachment_links,
        plan.warnings,
        image_statuses,
        prepared_attachments.statuses,
        plan.diagnostics,
    )
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
        warnings.append("Inline hosted images must be recreated in the repost; if Graph rejects them, the repost fails without publishing a degraded message.")
    return warnings


async def _download_required_hosted_images(
    parsed_source: TeamsMessageLink,
    refs: list[HostedContentRef],
    graph: GraphClient,
    settings: Settings,
) -> list[HostedContentUpload]:
    if refs and not settings.try_inline_hosted_contents:
        raise InlineImageRepostError(
            "Cannot repost this message at native fidelity because it contains inline Teams hosted images "
            "and TRY_INLINE_HOSTED_CONTENTS=false."
        )
    uploads: list[HostedContentUpload] = []
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
            raise InlineImageRepostError(
                f"Cannot repost this message at native fidelity because inline image {ref.occurrence} "
                f"could not be downloaded from Microsoft Graph HTTP {exc.status_code}."
            ) from exc
    return uploads


def _prepare_attachments_for_repost(attachments: list[dict]) -> PreparedAttachments:
    if not attachments:
        return PreparedAttachments([], [])
    reference_attachments: list[ReferenceAttachment] = []
    statuses: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        if _is_announcement_banner_attachment(attachment):
            statuses.append(_omitted_announcement_banner_status(attachment, index))
            continue
        if _is_omittable_teams_card_attachment(attachment):
            statuses.append(_omitted_teams_card_status(attachment, index))
            continue
        reference_attachment = _build_reference_attachment(attachment, index)
        reference_attachments.append(reference_attachment)
        statuses.append(reference_attachment.to_status())
    return PreparedAttachments(reference_attachments, statuses)


def build_reference_attachments(attachments: list[dict]) -> list[ReferenceAttachment]:
    return [
        _build_reference_attachment(attachment, index)
        for index, attachment in enumerate(attachments, start=1)
        if not _is_announcement_banner_attachment(attachment) and not _is_omittable_teams_card_attachment(attachment)
    ]


def _build_reference_attachment(attachment: dict, index: int) -> ReferenceAttachment:
    name = _attachment_name(attachment, index)
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
    return ReferenceAttachment(
        id=str(uuid.uuid4()),
        name=name,
        content_url=content_url,
    )


def _is_announcement_banner_attachment(attachment: dict) -> bool:
    return _normalized_attachment_content_type(attachment) == _ANNOUNCEMENT_BANNER_CONTENT_TYPE


def _is_omittable_teams_card_attachment(attachment: dict) -> bool:
    return _normalized_attachment_content_type(attachment) in _OMITTABLE_TEAMS_CARD_CONTENT_TYPES


def _normalized_attachment_content_type(attachment: dict) -> str:
    return (attachment.get("contentType") or "").split(";", 1)[0].strip().lower()


def _omitted_announcement_banner_status(attachment: dict, index: int) -> dict[str, Any]:
    return {
        "id": attachment.get("id"),
        "name": _attachment_name(attachment, index),
        "content_type": attachment.get("contentType"),
        "status": "omitted_announcement_banner",
    }


def _omitted_teams_card_status(attachment: dict, index: int) -> dict[str, Any]:
    return {
        "id": attachment.get("id"),
        "name": _attachment_name(attachment, index),
        "content_type": attachment.get("contentType"),
        "status": "omitted_nonportable_teams_card",
    }


def _attachment_name(attachment: dict, index: int) -> str:
    return attachment.get("name") or f"attachment-{index}"


def _body_with_reference_attachments(
    body_html: str,
    attachments: list[ReferenceAttachment],
) -> str:
    return append_attachment_placeholders(body_html, attachments)


def _pending_attachment_statuses(attachments: list[dict]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        if _is_announcement_banner_attachment(attachment):
            statuses.append(_omitted_announcement_banner_status(attachment, index))
            continue
        if _is_omittable_teams_card_attachment(attachment):
            statuses.append(_omitted_teams_card_status(attachment, index))
            continue
        _build_reference_attachment(attachment, index)
        statuses.append(
            {
                "name": _attachment_name(attachment, index),
                "content_url": attachment.get("contentUrl"),
                "status": "will_attach_reference",
            }
        )
    return statuses


def _recreated_image_statuses(uploads: list[HostedContentUpload]) -> list[dict[str, Any]]:
    return [{"occurrence": upload.occurrence, "status": "recreated_inline"} for upload in uploads]


def _omitted_image_statuses(omissions: list[InlinePayloadOmission]) -> list[dict[str, Any]]:
    return [
        {
            "occurrence": omission.upload.occurrence,
            "status": _omitted_inline_status(omission),
            "content_type": omission.upload.content_type,
            "byte_size": len(omission.upload.content_bytes),
        }
        for omission in omissions
    ]


def _ensure_translated_body_references_images(
    body_html: str,
    uploads: list[HostedContentUpload],
    display_refs: list[Any],
) -> None:
    missing = [upload.occurrence for upload in uploads if f'../hostedContents/{upload.temporary_id}/$value' not in body_html]
    if missing:
        display_occurrences = sorted(ref.occurrence for ref in display_refs)
        raise InlineImageRepostError(
            "Cannot repost this translated message at native fidelity because the translated body is missing "
            f"embedded image placeholder(s): {missing}. Detected translated image placeholders: {display_occurrences}."
        )


def _build_payload_with_inline_budget(
    *,
    subject: str,
    source_message: dict[str, Any],
    parsed_source: TeamsMessageLink,
    content_body_html: str,
    uploads: list[HostedContentUpload],
    attachments: list[ReferenceAttachment],
    warnings: list[str],
    image_replacer: Callable[[str, list[HostedContentUpload], dict[int, str]], str],
    display_refs: list[Any] | None = None,
    target_language: str | None = None,
) -> InlinePayloadPlan:
    included: list[HostedContentUpload] = []
    omitted: list[InlinePayloadOmission] = []
    for upload in uploads:
        if _can_embed_inline_content_type(upload.content_type):
            included.append(upload)
        else:
            omitted.append(InlinePayloadOmission(upload, "unsupported_content_type"))

    while True:
        placeholders = _omitted_inline_image_placeholders(omitted, source_message, parsed_source)
        inline_body = image_replacer(content_body_html, included, placeholders)
        if display_refs is not None:
            _ensure_translated_body_references_images(inline_body, included, display_refs)

        plan_warnings = [*warnings, *_omitted_inline_image_warnings(omitted)]
        body = _body_with_reference_attachments(
            build_forward_body(source_message, parsed_source, inline_body, [], plan_warnings, target_language),
            attachments,
        )
        payload = build_channel_message_payload(subject, body, included, attachments)
        payload_size = _graph_payload_size(payload)
        if payload_size <= _GRAPH_MESSAGE_PAYLOAD_TARGET_BYTES:
            return InlinePayloadPlan(
                payload=payload,
                included_uploads=included,
                omitted_uploads=omitted,
                warnings=plan_warnings,
                diagnostics=_omitted_inline_image_diagnostics(omitted, payload_size),
            )

        if not included:
            raise InlineImageRepostError(
                "Cannot repost this message because the Microsoft Graph channel-message payload remains larger than "
                f"{_format_bytes(_GRAPH_MESSAGE_PAYLOAD_LIMIT_BYTES)} after omitting inline images."
            )

        drop = max(included, key=lambda upload: len(upload.content_bytes))
        included = [upload for upload in included if upload.occurrence != drop.occurrence]
        omitted.append(InlinePayloadOmission(drop, "payload_size_limit"))


def _can_embed_inline_content_type(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in _SUPPORTED_INLINE_HOSTED_CONTENT_TYPES


def _graph_payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload).encode("utf-8"))


def _omitted_inline_image_placeholders(
    omissions: list[InlinePayloadOmission],
    source_message: dict[str, Any],
    parsed_source: TeamsMessageLink,
) -> dict[int, str]:
    return {
        omission.upload.occurrence: _omitted_inline_image_placeholder(omission, source_message, parsed_source)
        for omission in omissions
    }


def _omitted_inline_image_placeholder(
    omission: InlinePayloadOmission,
    source_message: dict[str, Any],
    parsed_source: TeamsMessageLink,
) -> str:
    upload = omission.upload
    source_url = source_message.get("webUrl") or parsed_source.raw_url
    if omission.reason == "unsupported_content_type":
        text = (
            f"Embedded image {upload.occurrence} omitted from this repost because Microsoft Graph does not accept "
            f"{upload.content_type} as native inline hosted content."
        )
    else:
        text = (
            f"Embedded image {upload.occurrence} omitted from this repost because it is too large "
            f"to embed through Microsoft Graph ({_format_bytes(len(upload.content_bytes))})."
        )
    if source_url:
        return (
            '<span><strong>'
            + html.escape(text)
            + '</strong> <a href="'
            + html.escape(source_url, quote=True)
            + '">Open original message</a>.</span>'
        )
    return "<span><strong>" + html.escape(text) + "</strong></span>"


def _omitted_inline_image_warnings(omissions: list[InlinePayloadOmission]) -> list[str]:
    return [_omitted_inline_image_warning(omission) for omission in omissions]


def _omitted_inline_image_warning(omission: InlinePayloadOmission) -> str:
    upload = omission.upload
    if omission.reason == "unsupported_content_type":
        return (
            f"Inline image {upload.occurrence} was omitted because Microsoft Graph only accepts "
            "image/jpg, image/jpeg, and image/png as native inline hosted content; "
            f"the downloaded content type was {upload.content_type}."
        )
    return (
        f"Inline image {upload.occurrence} was omitted because embedding it would exceed the "
        f"Microsoft Graph {_format_bytes(_GRAPH_MESSAGE_PAYLOAD_LIMIT_BYTES)} channel-message payload limit "
        f"(downloaded size {_format_bytes(len(upload.content_bytes))})."
    )


def _omitted_inline_image_diagnostics(
    omissions: list[InlinePayloadOmission],
    final_payload_size: int,
) -> list[dict[str, Any]]:
    return [
        {
            "status": _omitted_inline_status(omission),
            "occurrence": omission.upload.occurrence,
            "content_type": omission.upload.content_type,
            "byte_size": len(omission.upload.content_bytes),
            "reason": omission.reason,
            "payload_limit_bytes": _GRAPH_MESSAGE_PAYLOAD_LIMIT_BYTES,
            "payload_target_bytes": _GRAPH_MESSAGE_PAYLOAD_TARGET_BYTES,
            "final_payload_size_bytes": final_payload_size,
        }
        for omission in omissions
    ]


def _omitted_inline_status(omission: InlinePayloadOmission) -> str:
    if omission.reason == "unsupported_content_type":
        return "omitted_inline_unsupported_content_type"
    return "omitted_inline_too_large"


def _format_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} bytes"


def _log_inline_failure_if_present(exc: GraphAPIError, uploads: list[HostedContentUpload]) -> None:
    if not uploads:
        return
    diagnostics = _inline_failure_diagnostics(exc, uploads)
    logger.warning(
        "Native inline hosted-content post failed; repost was not degraded or retried without images",
        extra={"inline_image_diagnostics": diagnostics},
    )


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
    return [
        (
            f"Attachment '{_attachment_name(attachment, index)}' was omitted because its Teams card type "
            f"'{attachment.get('contentType') or 'missing'}' cannot be reliably copied to another channel. "
            "The repost includes a link to the original message."
        )
        for index, attachment in enumerate(attachments, start=1)
        if _is_omittable_teams_card_attachment(attachment)
    ]
