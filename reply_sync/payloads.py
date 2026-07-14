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
LEGACY_REPLY_AUTHOR_PREFIXES = ("Original reply by:", "原回覆作者：")
REPLY_SOURCE_MARKER_PREFIX = "reply-source:"
LEGACY_REPLY_SOURCE_MARKER_PREFIX = "reply-sync-source:"


class ReplyFidelityError(ValueError):
    pass


def build_reply_payload(
    reply: dict[str, Any],
    translation: dict[str, Any],
    hosted_downloads: dict[str, tuple[bytes, str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    translated_body = str(translation.get("body_html") or "")
    refs = find_hosted_content_refs(str(reply.get("body_html") or ""))
    uploads: list[HostedContentUpload] = []
    for ref in refs:
        download = hosted_downloads.get(ref.hosted_content_id)
        if download is None:
            raise ReplyFidelityError(f"Inline image {ref.occurrence} was not returned by Microsoft Graph")
        content, content_type = download
        normalized_type = (content_type or "application/octet-stream").split(";", 1)[0].lower()
        if normalized_type not in SUPPORTED_INLINE_TYPES:
            raise ReplyFidelityError(
                f"Inline image {ref.occurrence} has unsupported content type '{normalized_type}'"
            )
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
    body = _audit_line(reply) + replace_hosted_content_refs(translated_body, uploads)
    body = append_attachment_placeholders(body, attachments)
    payload: dict[str, Any] = {"body": {"contentType": "html", "content": body}}
    if uploads:
        payload["hostedContents"] = [upload.to_graph_payload() for upload in uploads]
    if attachments:
        payload["attachments"] = [attachment.to_graph_payload() for attachment in attachments]
    payload_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_size > GRAPH_PAYLOAD_TARGET_BYTES:
        raise ReplyFidelityError(
            f"Reply payload is {payload_size} bytes and exceeds the safe Microsoft Graph payload budget"
        )
    return payload, {
        "degraded": False,
        "payload_bytes": payload_size,
        "inline_images": len(uploads),
        "attachments": len(attachments),
    }


def build_degraded_reply_payload(reply: dict[str, Any], translation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    translated_body = str(translation.get("body_html") or "")
    refs = find_hosted_content_refs(str(reply.get("body_html") or ""))
    source_url = str(reply.get("web_url") or "")
    placeholders = {
        ref.occurrence: _source_link(source_url, f"Inline image {ref.occurrence} in original reply")
        for ref in refs
    }
    body = _audit_line(reply) + replace_hosted_content_refs(translated_body, [], placeholders)
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


def _audit_line(reply: dict[str, Any]) -> str:
    target_language = str(reply.get("target_language") or "")
    author = html.escape(str(reply.get("author") or "Unknown"))
    source_url = str(reply.get("web_url") or "")
    reply_id = html.escape(str(reply.get("id") or ""))
    if target_language.lower().startswith("en"):
        source_label = ENGLISH_REPLY_SOURCE_PREFIX
        author_label = "Original reply by:"
        link_label = "Original reply"
    else:
        source_label = CHINESE_REPLY_SOURCE_PREFIX
        author_label = "原回覆作者："
        link_label = "原回覆"
    marker = f"{REPLY_SOURCE_MARKER_PREFIX}{reply_id}"
    link = _source_link(source_url, link_label) if source_url else f"{link_label} ({marker})"
    return (
        f"<p><strong>{source_label}</strong> {link}<br>"
        f"<strong>{author_label}</strong> {author}</p>"
    )


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
