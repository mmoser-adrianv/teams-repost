from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any


VISIBLE_TEXT_SKIP_TAGS = {"script", "style", "noscript"}
TAG_NAME_RE = re.compile(r"^<\s*(/)?\s*([a-zA-Z][\w:.-]*)")
MAX_TRANSLATION_SEGMENTS_PER_REQUEST = 12


class TranslationError(Exception):
    """Raised when a translation cannot be produced safely."""


class TranslationConfigurationError(TranslationError):
    """Raised when the translation provider is not configured."""


class HtmlTextDocument:
    def __init__(self, html: str) -> None:
        self.html = html or ""
        self.parts: list[str | dict[str, Any]] = []
        self.text_segments: list[str] = []
        self._scan()

    def render(self, translated_segments: list[str]) -> str:
        if len(translated_segments) != len(self.text_segments):
            raise TranslationError("Translated segment count does not match source segment count.")

        rendered: list[str] = []
        for part in self.parts:
            if isinstance(part, str):
                rendered.append(part)
                continue
            translated = translated_segments[part["index"]]
            rendered.append(f"{part['prefix']}{escape(translated, quote=False)}{part['suffix']}")
        return "".join(rendered)

    def _scan(self) -> None:
        index = 0
        skip_stack: list[str] = []
        html = self.html
        while index < len(html):
            if html[index] == "<":
                tag_end = _find_tag_end(html, index)
                if tag_end is not None:
                    tag = html[index : tag_end + 1]
                    self.parts.append(tag)
                    _update_skip_stack(skip_stack, tag)
                    index = tag_end + 1
                    continue

            next_tag = html.find("<", index)
            if next_tag == -1:
                next_tag = len(html)
            self._append_text(html[index:next_tag], bool(skip_stack))
            index = next_tag

    def _append_text(self, raw_text: str, skip: bool) -> None:
        if not raw_text:
            return
        decoded = unescape(raw_text)
        if skip or not decoded.strip():
            self.parts.append(raw_text)
            return

        match = re.match(r"^(\s*)(.*?)(\s*)$", decoded, flags=re.DOTALL)
        if not match or not match.group(2):
            self.parts.append(raw_text)
            return

        segment_index = len(self.text_segments)
        self.text_segments.append(match.group(2))
        self.parts.append({"index": segment_index, "prefix": match.group(1), "suffix": match.group(3)})


class OpenAITranslationService:
    def __init__(self, settings: Any, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client
        self.model = settings.openai_translation_model

    async def translate_post(self, post: dict[str, Any], target_language: str) -> dict[str, Any]:
        document = HtmlTextDocument(post.get("body_html") or "")
        subject = post.get("subject") or ""
        translate_subject = bool(subject.strip())
        source_segments = ([subject] if translate_subject else []) + document.text_segments

        translated_segments = await self._translate_segments(source_segments, target_language) if source_segments else []
        translated_subject = translated_segments[0] if translate_subject else subject
        body_segments = translated_segments[1:] if translate_subject else translated_segments
        body_html = document.render(body_segments)

        return {
            "subject": translated_subject,
            "body_html": body_html,
            "body_preview": html_preview(body_html),
            "translated_at": datetime.now(UTC).isoformat(),
            "model": self.model,
        }

    async def _translate_segments(self, segments: list[str], target_language: str) -> list[str]:
        translated: list[str] = []
        for start in range(0, len(segments), MAX_TRANSLATION_SEGMENTS_PER_REQUEST):
            batch = segments[start : start + MAX_TRANSLATION_SEGMENTS_PER_REQUEST]
            translated.extend(await self._translate_batch(batch, target_language))
        return translated

    async def _translate_batch(self, segments: list[str], target_language: str) -> list[str]:
        response = await self._request_translation(segments, target_language)
        try:
            return _parse_translation_array(_response_output_text(response), len(segments))
        except TranslationError:
            if len(segments) == 1:
                raise
            midpoint = len(segments) // 2
            left = await self._translate_batch(segments[:midpoint], target_language)
            right = await self._translate_batch(segments[midpoint:], target_language)
            return left + right

    async def _request_translation(self, segments: list[str], target_language: str) -> Any:
        try:
            response = await self._client().responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "Translate each provided text segment directly into the requested language. "
                            "Return only a JSON array of strings with exactly the same number of items. "
                            "Do not add explanations, markdown, HTML tags, or extra fields."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"target_language": target_language, "segments": segments},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
            )
        except Exception as exc:
            raise TranslationError(f"OpenAI translation request failed: {exc}") from exc
        return response

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        if not self.settings.openai_api_key:
            raise TranslationConfigurationError("OPENAI_API_KEY is required to translate posts.")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise TranslationConfigurationError("The openai package is not installed. Run pip install -r requirements.txt.") from exc

        self.client = AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            timeout=self.settings.openai_request_timeout_seconds,
        )
        return self.client


async def translate_cached_post(post: dict[str, Any], target_language: str, settings: Any) -> dict[str, Any]:
    return await OpenAITranslationService(settings).translate_post(post, target_language)


def html_preview(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()[:240]


def _find_tag_end(html: str, start: int) -> int | None:
    if html.startswith("<!--", start):
        comment_end = html.find("-->", start + 4)
        return comment_end + 2 if comment_end != -1 else None

    quote_char: str | None = None
    index = start + 1
    while index < len(html):
        char = html[index]
        if quote_char:
            if char == quote_char:
                quote_char = None
        elif char in {"'", '"'}:
            quote_char = char
        elif char == ">":
            return index
        index += 1
    return None


def _update_skip_stack(skip_stack: list[str], tag: str) -> None:
    match = TAG_NAME_RE.match(tag)
    if not match:
        return

    closing = bool(match.group(1))
    name = match.group(2).lower()
    if name not in VISIBLE_TEXT_SKIP_TAGS:
        return

    if closing:
        for index in range(len(skip_stack) - 1, -1, -1):
            if skip_stack[index] == name:
                del skip_stack[index:]
                break
    elif not tag.rstrip().endswith("/>"):
        skip_stack.append(name)


def _response_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    data: dict[str, Any] | None = None
    if isinstance(response, dict):
        data = response
    elif hasattr(response, "model_dump"):
        data = response.model_dump()

    if data:
        fragments: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str):
                    fragments.append(text)
        if fragments:
            return "".join(fragments)

    raise TranslationError("OpenAI translation response did not contain text output.")


def _parse_translation_array(output_text: str, expected_count: int) -> list[str]:
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise TranslationError("OpenAI translation response was not valid JSON.") from exc

    if not isinstance(payload, list) or len(payload) != expected_count or not all(isinstance(item, str) for item in payload):
        raise TranslationError("OpenAI translation response did not match the requested segment count.")
    return payload
