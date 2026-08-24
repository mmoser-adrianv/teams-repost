from __future__ import annotations

import html
import json
import uuid
from typing import Any

from message_rebuilder import (
    HostedContentUpload,
    ReferenceAttachment,
    append_attachment_placeholders,
    find_hosted_content_refs,
    replace_hosted_content_refs,
)


GRAPH_PAYLOAD_LIMIT_BYTES = 4 * 1024 * 1024
GRAPH_PAYLOAD_TARGET_BYTES = GRAPH_PAYLOAD_LIMIT_BYTES - (64 * 1024)
SUPPORTED_INLINE_TYPES = {"image/jpg", "image/jpeg", "image/png"}
ENGLISH_REPLY_SOURCE_PREFIX = "Reply source:"
CHINESE_REPLY_SOURCE_PREFIX = "回覆來源："
ENGLISH_REPLY_AUTHOR_PREFIX = "Original reply by:"
CHINESE_REPLY_AUTHOR_PREFIX = "原回覆作者："
REPLY_AUTHOR_PREFIXES = (ENGLISH_REPLY_AUTHOR_PREFIX, CHINESE_REPLY_AUTHOR_PREFIX)
REPLY_SOURCE_MARKER_PREFIX = "reply-source:"
LEGACY_REPLY_SOURCE_MARKER_PREFIX = "reply-sync-source:"


class ReplyFidelityError(ValueError):
    pass


def build_reply_payload(
    reply: dict[str, Any],
    translation: dict[str, Any],
    hosted_downloads: dict[str, tuple[bytes, str]],
    *,
    target_language: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    translated_body = str(translation.get("body_html") or "")
    refs = find_hosted_content_refs(str(reply.get("body_html") or ""))
    uploads: list[HostedContentUpload] = []
    omissions: list[dict[str, Any]] = []
    for ref in refs:
        download = hosted_downloads.get(ref.hosted_content_id)
        if download is None:
            raise ReplyFidelityError(f"Inline image {ref.occurrence} was not returned by Microsoft Graph")
        content, content_type = download
        normalized_type = (content_type or "application/octet-stream").split(";", 1)[0].lower()
        if normalized_type not in SUPPORTED_INLINE_TYPES:
            omissions.append(
                {
                    "occurrence": ref.occurrence,
                    "content_type": normalized_type,
                    "byte_size": len(content),
                    "reason": "unsupported_content_type",
                }
            )
        else:
            uploads.append(
                HostedContentUpload(
                    occurrence=ref.occurrence,
                    original_id=ref.hosted_content_id,
                    temporary_id=str(ref.occurrence),
                    content_type=normalized_type,
                    content_bytes=content,
                )
            )

    attachments = _reference_attachments(reply.get("attachments") or [])
    while True:
        placeholders = {
            int(omission["occurrence"]): _inline_omission_placeholder(reply, omission)
            for omission in omissions
        }
        body = _audit_line(reply, target_language) + replace_hosted_content_refs(
            translated_body,
            uploads,
            placeholders,
        )
        body = append_attachment_placeholders(body, attachments)
        payload: dict[str, Any] = {"body": {"contentType": "html", "content": body}}
        if uploads:
            payload["hostedContents"] = [upload.to_graph_payload() for upload in uploads]
        if attachments:
            payload["attachments"] = [attachment.to_graph_payload() for attachment in attachments]
        payload_size = _payload_size(payload)
        if payload_size <= GRAPH_PAYLOAD_TARGET_BYTES:
            inline_image_statuses = [
                {
                    "occurrence": upload.occurrence,
                    "status": "recreated_inline",
                    "content_type": upload.content_type,
                    "byte_size": len(upload.content_bytes),
                }
                for upload in uploads
            ] + [
                {
                    "occurrence": int(omission["occurrence"]),
                    "status": _inline_omission_status(omission),
                    "content_type": str(omission["content_type"]),
                    "byte_size": int(omission["byte_size"]),
                }
                for omission in omissions
            ]
            return payload, {
                "degraded": bool(omissions),
                "payload_bytes": payload_size,
                "inline_images": len(uploads),
                "attachments": len(attachments),
                "inline_image_statuses": sorted(
                    inline_image_statuses,
                    key=lambda item: int(item["occurrence"]),
                ),
                "warnings": [_inline_omission_warning(omission) for omission in omissions],
            }

        if not uploads:
            raise ReplyFidelityError(
                f"Reply payload is {payload_size} bytes and remains over the safe Microsoft Graph payload budget "
                "after omitting inline images"
            )

        drop = max(uploads, key=lambda upload: len(upload.content_bytes))
        uploads = [upload for upload in uploads if upload.occurrence != drop.occurrence]
        omissions.append(
            {
                "occurrence": drop.occurrence,
                "content_type": drop.content_type,
                "byte_size": len(drop.content_bytes),
                "reason": "payload_size_limit",
            }
        )


def build_degraded_reply_payload(
    reply: dict[str, Any],
    translation: dict[str, Any],
    *,
    target_language: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    translated_body = str(translation.get("body_html") or "")
    refs = find_hosted_content_refs(str(reply.get("body_html") or ""))
    source_url = str(reply.get("web_url") or "")
    placeholders = {
        ref.occurrence: _source_link(source_url, f"Inline image {ref.occurrence} in original reply")
        for ref in refs
    }
    body = _audit_line(reply, target_language) + replace_hosted_content_refs(translated_body, [], placeholders)
    attachment_links: list[str] = []
    for index, attachment in enumerate(reply.get("attachments") or [], start=1):
        name = html.escape(str(attachment.get("name") or f"Attachment {index}"))
        url = str(attachment.get("content_url") or source_url)
        attachment_links.append("<li>" + _source_link(url, name) + "</li>")
    if attachment_links:
        body += "<p><strong>Attachments from original reply:</strong></p><ul>" + "".join(attachment_links) + "</ul>"
    payload = {"body": {"contentType": "html", "content": body}}
    return payload, {
        "degraded": True,
        "payload_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "inline_images": 0,
        "attachments": 0,
    }


def marker_candidates(reply: dict[str, Any]) -> tuple[str, ...]:
    reply_id = str(reply.get("id") or "")
    source_url = str(reply.get("web_url") or "")
    return tuple(
        value
        for value in (
            source_url,
            f"{REPLY_SOURCE_MARKER_PREFIX}{reply_id}" if reply_id else "",
            f"{LEGACY_REPLY_SOURCE_MARKER_PREFIX}{reply_id}" if reply_id else "",
        )
        if value
    )


def _audit_line(reply: dict[str, Any], target_language: str | None = None) -> str:
    language_value = reply.get("target_language") if target_language is None else target_language
    resolved_language = str(language_value or "").strip()
    if not resolved_language:
        raise ReplyFidelityError("Reply target language is missing; refusing to choose a header language")
    author = html.escape(str(reply.get("author") or "Unknown"))
    source_url = str(reply.get("web_url") or "")
    reply_id = html.escape(str(reply.get("id") or ""))
    language_code = resolved_language.lower().replace("_", "-").split("-", 1)[0]
    if language_code == "en":
        author_label = ENGLISH_REPLY_AUTHOR_PREFIX
    else:
        author_label = CHINESE_REPLY_AUTHOR_PREFIX
    marker = f"{REPLY_SOURCE_MARKER_PREFIX}{reply_id}"
    link = _source_link(source_url, "link") if source_url else f"link ({marker})"
    return f"<p><strong>{author_label}</strong> {author} · {link}</p><hr>"


def _reference_attachments(attachments: list[dict[str, Any]]) -> list[ReferenceAttachment]:
    output: list[ReferenceAttachment] = []
    for index, attachment in enumerate(attachments, start=1):
        name = str(attachment.get("name") or f"attachment-{index}")
        content_type = str(attachment.get("content_type") or attachment.get("contentType") or "").lower()
        content_url = attachment.get("content_url") or attachment.get("contentUrl")
        if content_type != "reference":
            raise ReplyFidelityError(
                f"Attachment '{name}' has unsupported content type '{content_type or 'missing'}'"
            )
        if not content_url:
            raise ReplyFidelityError(f"Attachment '{name}' has no content URL")
        output.append(ReferenceAttachment(str(uuid.uuid4()), name, str(content_url)))
    return output


def _source_link(url: str, label: str) -> str:
    escaped_label = html.escape(label)
    if not url:
        return escaped_label
    return f'<a href="{html.escape(url, quote=True)}">{escaped_label}</a>'


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _inline_omission_status(omission: dict[str, Any]) -> str:
    if omission["reason"] == "unsupported_content_type":
        return "omitted_inline_unsupported_content_type"
    return "omitted_inline_too_large"


def _inline_omission_warning(omission: dict[str, Any]) -> str:
    occurrence = int(omission["occurrence"])
    if omission["reason"] == "unsupported_content_type":
        return (
            f"Inline image {occurrence} was omitted because Microsoft Graph does not accept "
            f"{omission['content_type']} as native inline hosted content."
        )
    return (
        f"Inline image {occurrence} was omitted because embedding it would exceed the safe Microsoft Graph "
        f"payload budget (downloaded size {omission['byte_size']} bytes)."
    )


def _inline_omission_placeholder(reply: dict[str, Any], omission: dict[str, Any]) -> str:
    warning = html.escape(_inline_omission_warning(omission))
    source_link = _source_link(str(reply.get("web_url") or ""), "Open original reply")
    return f"<span><strong>{warning}</strong> {source_link}.</span>"
