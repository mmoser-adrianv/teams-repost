from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Iterable

from teams_url_parser import TeamsMessageLink


HOSTED_CONTENT_SRC_RE = re.compile(r"hostedContents/([^/?#]+)/\$value", re.IGNORECASE)
DISPLAY_IMAGE_SRC_RE = re.compile(
    r"(?:^|/)(?:api/posts|api/flows/[^/]+/posts)/[^/?#]+/images/(\d+)(?:$|[?#])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HostedContentRef:
    occurrence: int
    hosted_content_id: str
    src: str


@dataclass(frozen=True)
class DisplayImageRef:
    occurrence: int
    src: str


@dataclass(frozen=True)
class HostedContentUpload:
    occurrence: int
    original_id: str
    temporary_id: str
    content_type: str
    content_bytes: bytes

    def to_graph_payload(self) -> dict[str, str]:
        return {
            "@microsoft.graph.temporaryId": self.temporary_id,
            "contentBytes": base64.b64encode(self.content_bytes).decode("ascii"),
            "contentType": self.content_type,
        }


@dataclass(frozen=True)
class ReferenceAttachment:
    id: str
    name: str
    content_url: str
    content_type: str = "reference"

    def to_graph_payload(self) -> dict[str, str]:
        return {
            "id": self.id,
            "contentType": self.content_type,
            "contentUrl": self.content_url,
            "name": self.name,
        }

    def to_status(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "content_url": self.content_url,
            "status": "attached_reference",
        }


def find_hosted_content_refs(body_html: str) -> list[HostedContentRef]:
    parser = _HostedContentParser()
    parser.feed(body_html or "")
    parser.close()
    return parser.refs


def find_display_image_refs(body_html: str) -> list[DisplayImageRef]:
    parser = _DisplayImageRefParser()
    parser.feed(body_html or "")
    parser.close()
    return parser.refs


def replace_hosted_content_with_temporary_refs(
    body_html: str,
    uploads: Iterable[HostedContentUpload],
) -> str:
    replacements = {upload.occurrence: f"../hostedContents/{upload.temporary_id}/$value" for upload in uploads}
    return _rewrite_html(body_html, img_src_replacements=replacements)


def replace_hosted_content_refs(
    body_html: str,
    uploads: Iterable[HostedContentUpload],
) -> str:
    img_replacements = {upload.occurrence: f"../hostedContents/{upload.temporary_id}/$value" for upload in uploads}
    return _rewrite_html(body_html, img_src_replacements=img_replacements)


def replace_display_image_refs(
    body_html: str,
    uploads: Iterable[HostedContentUpload],
) -> str:
    img_replacements = {upload.occurrence: f"../hostedContents/{upload.temporary_id}/$value" for upload in uploads}
    return _rewrite_html(
        body_html,
        img_src_replacements=img_replacements,
        image_ref_detector=_display_image_occurrence_from_src,
    )


def strip_attachment_placeholders(body_html: str) -> str:
    return _rewrite_html(body_html, img_src_replacements={}, strip_attachments=True)


def append_attachment_placeholders(body_html: str, attachments: Iterable[ReferenceAttachment]) -> str:
    attachment_list = list(attachments)
    if not attachment_list:
        return body_html
    placeholders = "".join(
        '<attachment id="' + html.escape(attachment.id, quote=True) + '"></attachment>'
        for attachment in attachment_list
    )
    return (body_html or "<p></p>") + "<p>" + placeholders + "</p>"


def sanitize_body_html_for_display(
    body_html: str,
    hosted_image_src: Callable[[HostedContentRef], str | None] | None = None,
) -> str:
    parser = _DisplaySanitizingParser(hosted_image_src)
    parser.feed(body_html or "")
    parser.close()
    return "".join(parser.output)


def normalize_body_to_html(message: dict) -> str:
    body = message.get("body") or {}
    content = body.get("content") or ""
    content_type = (body.get("contentType") or "html").lower()
    if content_type == "text":
        return html.escape(content).replace("\n", "<br>")
    return content


def build_forward_subject(message: dict) -> str:
    subject = _clean_text(message.get("subject") or "")
    if not subject:
        subject = _first_line_from_body(normalize_body_to_html(message))
    if not subject:
        subject = "Teams message"
    return subject[:100]


def build_forward_body(
    original_message: dict,
    parsed_source: TeamsMessageLink,
    original_body_html: str,
    copied_file_links: list[dict],
    warnings: list[str],
) -> str:
    author = extract_author_display_name(original_message) or "Unknown"
    original_link = original_message.get("webUrl") or parsed_source.raw_url
    original_link_text = _original_link_text(original_message)

    parts = [
        "<p><strong>原文作者：</strong> " + html.escape(author) + "<br>",
    ]
    if original_link:
        parts.append(
            '<strong>原文連結：</strong> <a href="'
            + html.escape(original_link, quote=True)
            + '">'
            + html.escape(original_link_text)
            + "</a></p>"
        )
    else:
        parts.append("<strong>原文連結：</strong> " + html.escape(original_link_text) + "</p>")

    if original_message.get("mentions"):
        warning = "Mentions are rendered as plain text and are not recreated as Teams mentions."
        if warning not in warnings:
            warnings.append(warning)

    parts.append("<hr>")
    parts.append(original_body_html or "<p></p>")

    if copied_file_links:
        parts.append("<p><strong>Attachments copied to this channel:</strong></p><ul>")
        for item in copied_file_links:
            parts.append(
                '<li><a href="'
                + html.escape(item["web_url"], quote=True)
                + '">'
                + html.escape(item["name"])
                + "</a></li>"
            )
        parts.append("</ul>")

    return "".join(parts)


def build_channel_message_payload(
    subject: str,
    body_html: str,
    hosted_uploads: Iterable[HostedContentUpload] | None = None,
    attachments: Iterable[ReferenceAttachment] | None = None,
) -> dict:
    payload = {
        "subject": subject,
        "body": {
            "contentType": "html",
            "content": body_html,
        },
    }
    hosted_payload = [upload.to_graph_payload() for upload in hosted_uploads or []]
    if hosted_payload:
        payload["hostedContents"] = hosted_payload
    attachment_payload = [attachment.to_graph_payload() for attachment in attachments or []]
    if attachment_payload:
        payload["attachments"] = attachment_payload
    return payload


def extract_author_display_name(message: dict) -> str | None:
    sender = message.get("from") or {}
    for key in ("user", "application", "conversation"):
        identity = sender.get(key)
        if identity and identity.get("displayName"):
            return identity["displayName"]
    return None


def _source_label(parsed_source: TeamsMessageLink) -> str:
    labels = []
    if parsed_source.team_name:
        labels.append(parsed_source.team_name)
    if parsed_source.channel_name:
        labels.append(parsed_source.channel_name)
    if labels:
        return " / ".join(labels)
    return f"{parsed_source.team_id} / {parsed_source.source_channel_thread_id}"


def _first_line_from_body(body_html: str) -> str:
    text = _clean_text(re.sub(r"<[^>]+>", " ", body_html or ""))
    return text.splitlines()[0][:100] if text else ""


def _original_link_text(message: dict) -> str:
    subject = _clean_text(message.get("subject") or "")
    if subject:
        return subject
    return _first_line_from_body(normalize_body_to_html(message)) or "Open in Teams"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


class _HostedContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[HostedContentRef] = []
        self._occurrence = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        src = _attr_value(attrs, "src")
        hosted_id = _hosted_content_id_from_src(src)
        if hosted_id:
            self._occurrence += 1
            self.refs.append(HostedContentRef(self._occurrence, hosted_id, src or ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class _DisplayImageRefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[DisplayImageRef] = []
        self._seen: set[int] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        src = _attr_value(attrs, "src")
        occurrence = _display_image_occurrence_from_src(src)
        if occurrence and occurrence not in self._seen:
            self._seen.add(occurrence)
            self.refs.append(DisplayImageRef(occurrence, src or ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class _RewritingParser(HTMLParser):
    def __init__(
        self,
        img_src_replacements: dict[int, str],
        strip_attachments: bool = False,
        image_ref_detector: Callable[[str | None], int | None] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self._occurrence = 0
        self._img_src_replacements = img_src_replacements
        self._strip_attachments = strip_attachments
        self._image_ref_detector = image_ref_detector
        self._suppressed_attachment_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        if self._suppressed_attachment_depth:
            if tag.lower() == "attachment":
                self._suppressed_attachment_depth -= 1
            return
        if tag.lower() == "at":
            self.output.append("</span>")
            return
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppressed_attachment_depth:
            return
        self.output.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if self._suppressed_attachment_depth:
            return
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._suppressed_attachment_depth:
            return
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self._suppressed_attachment_depth:
            return
        self.output.append(f"<!--{data}-->")

    def _handle_tag(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        lowered = tag.lower()
        if self._suppressed_attachment_depth:
            if lowered == "attachment" and not self_closing:
                self._suppressed_attachment_depth += 1
            return
        if self._strip_attachments and lowered == "attachment":
            if not self_closing:
                self._suppressed_attachment_depth = 1
            return
        if lowered == "at":
            self.output.append("<span>")
            return
        if lowered == "img":
            src = _attr_value(attrs, "src")
            occurrence = None
            if _hosted_content_id_from_src(src):
                self._occurrence += 1
                occurrence = self._occurrence
            elif self._image_ref_detector:
                occurrence = self._image_ref_detector(src)
            if occurrence:
                new_src = self._img_src_replacements.get(occurrence)
                if new_src:
                    attrs = _replace_attr(attrs, "src", new_src)
        self.output.append(_render_tag(tag, attrs, self_closing))


class _DisplaySanitizingParser(HTMLParser):
    def __init__(self, hosted_image_src: Callable[[HostedContentRef], str | None] | None) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self._hosted_image_src = hosted_image_src
        self._hosted_occurrence = 0
        self._suppressed_depth = 0
        self._suppressed_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._suppressed_depth:
            if lowered == self._suppressed_tag:
                self._suppressed_depth -= 1
                if not self._suppressed_depth:
                    self._suppressed_tag = None
            return
        if lowered == "at":
            self.output.append("</span>")
            return
        if lowered in _DISPLAY_VOID_TAGS:
            return
        if lowered in _DISPLAY_ALLOWED_TAGS:
            self.output.append(f"</{lowered}>")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.output.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if not self._suppressed_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._suppressed_depth:
            self.output.append(f"&#{name};")

    def _handle_tag(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        lowered = tag.lower()
        if self._suppressed_depth:
            if lowered == self._suppressed_tag and not self_closing:
                self._suppressed_depth += 1
            return
        if lowered in _DISPLAY_SUPPRESSED_TAGS:
            if not self_closing:
                self._suppressed_depth = 1
                self._suppressed_tag = lowered
            return
        if lowered == "attachment":
            return
        if lowered == "at":
            self.output.append("<span>")
            return
        if lowered == "emoji":
            alt = _attr_value(attrs, "alt") or _attr_value(attrs, "title") or ""
            self.output.append(html.escape(alt))
            return
        if lowered == "customemoji":
            alt = _attr_value(attrs, "alt") or _attr_value(attrs, "title") or ""
            self.output.append(html.escape(alt))
            return
        if lowered not in _DISPLAY_ALLOWED_TAGS:
            return

        safe_attrs = self._safe_attrs(lowered, attrs)
        if lowered == "img":
            src = _attr_value(attrs, "src")
            hosted_id = _hosted_content_id_from_src(src)
            if hosted_id:
                self._hosted_occurrence += 1
                replacement = self._hosted_image_src(
                    HostedContentRef(self._hosted_occurrence, hosted_id, src or "")
                ) if self._hosted_image_src else None
                if replacement:
                    safe_attrs = _replace_attr(safe_attrs, "src", replacement)
            if not _attr_value(safe_attrs, "src"):
                return

        self.output.append(_render_tag(lowered, safe_attrs, self_closing or lowered in _DISPLAY_VOID_TAGS))

    def _safe_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
        allowed = _DISPLAY_ALLOWED_ATTRS.get(tag, set()) | _DISPLAY_ALLOWED_ATTRS.get("*", set())
        output: list[tuple[str, str | None]] = []
        for name, value in attrs:
            lowered = name.lower()
            if lowered.startswith("on") or lowered not in allowed or value is None:
                continue
            if lowered in {"href", "src"}:
                if not _safe_url(value):
                    continue
            elif lowered in {"width", "height"}:
                if not _safe_dimension(value):
                    continue
            elif lowered == "style":
                value = _safe_style(value)
                if not value:
                    continue
            output.append((lowered, value))
        return output


def _rewrite_html(
    body_html: str,
    img_src_replacements: dict[int, str],
    strip_attachments: bool = False,
    image_ref_detector: Callable[[str | None], int | None] | None = None,
) -> str:
    parser = _RewritingParser(img_src_replacements, strip_attachments, image_ref_detector)
    parser.feed(body_html or "")
    parser.close()
    return "".join(parser.output)


def _render_tag(tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> str:
    rendered_attrs = []
    for name, value in attrs:
        if value is None:
            rendered_attrs.append(html.escape(name))
        else:
            rendered_attrs.append(f'{html.escape(name)}="{html.escape(value, quote=True)}"')
    attr_text = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""
    closing = " /" if self_closing else ""
    return f"<{tag}{attr_text}{closing}>"


def _attr_value(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    for attr_name, value in attrs:
        if attr_name.lower() == name.lower():
            return value
    return None


def _replace_attr(attrs: list[tuple[str, str | None]], name: str, value: str) -> list[tuple[str, str | None]]:
    replaced = False
    output = []
    for attr_name, attr_value in attrs:
        if attr_name.lower() == name.lower():
            output.append((attr_name, value))
            replaced = True
        else:
            output.append((attr_name, attr_value))
    if not replaced:
        output.append((name, value))
    return output


def _hosted_content_id_from_src(src: str | None) -> str | None:
    if not src:
        return None
    match = HOSTED_CONTENT_SRC_RE.search(src)
    return html.unescape(match.group(1)) if match else None


def _display_image_occurrence_from_src(src: str | None) -> int | None:
    if not src:
        return None
    match = DISPLAY_IMAGE_SRC_RE.search(html.unescape(src))
    return int(match.group(1)) if match else None


_DISPLAY_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "codeblock",
    "del",
    "div",
    "em",
    "font",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strike",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_DISPLAY_VOID_TAGS = {"br", "hr", "img"}
_DISPLAY_SUPPRESSED_TAGS = {"script", "style", "iframe", "object", "embed", "form"}
_DISPLAY_ALLOWED_ATTRS = {
    "*": {"class", "style", "title"},
    "a": {"href", "title"},
    "font": {"color"},
    "img": {"alt", "height", "src", "style", "title", "width"},
    "td": {"colspan", "rowspan", "style"},
    "th": {"colspan", "rowspan", "style"},
}
_SAFE_STYLE_PROPERTIES = {
    "background-color",
    "color",
    "font-style",
    "font-weight",
    "height",
    "max-height",
    "max-width",
    "min-height",
    "min-width",
    "text-align",
    "text-decoration",
    "vertical-align",
    "width",
}


def _safe_url(value: str) -> bool:
    stripped = html.unescape(value).strip().lower()
    return stripped.startswith(("http://", "https://", "mailto:", "/"))


def _safe_dimension(value: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+(?:\.\d+)?(?:px|%)?\s*", value))


def _safe_style(value: str) -> str:
    safe_parts: list[str] = []
    for part in value.split(";"):
        if ":" not in part:
            continue
        name, raw_value = part.split(":", 1)
        name = name.strip().lower()
        raw_value = raw_value.strip()
        lowered_value = raw_value.lower()
        if name not in _SAFE_STYLE_PROPERTIES:
            continue
        if any(blocked in lowered_value for blocked in ("expression", "javascript:", "url(")):
            continue
        safe_parts.append(f"{name}: {raw_value}")
    return "; ".join(safe_parts)
