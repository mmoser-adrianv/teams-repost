from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from graph_client import GraphAPIError, GraphClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedUpload:
    source_name: str
    destination_name: str
    content_url: str | None
    supported: bool
    warning: str | None = None


@dataclass(frozen=True)
class CopiedFile:
    name: str
    web_url: str
    drive_item_id: str | None
    source_content_url: str | None = None

    def to_report(self) -> dict:
        return {
            "name": self.name,
            "web_url": self.web_url,
            "drive_item_id": self.drive_item_id,
            "source_content_url": self.source_content_url,
        }


class FileCopier:
    def __init__(
        self,
        graph: GraphClient,
        files_folder: dict,
        temp_folder: Path,
        max_file_size_bytes: int,
    ) -> None:
        self.graph = graph
        self.files_folder = files_folder
        self.temp_folder = temp_folder
        self.max_file_size_bytes = max_file_size_bytes
        self.temp_folder.mkdir(parents=True, exist_ok=True)

    def plan_attachment_uploads(self, attachments: list[dict]) -> tuple[list[PlannedUpload], list[str]]:
        plans: list[PlannedUpload] = []
        warnings: list[str] = []
        for index, attachment in enumerate(attachments, start=1):
            content_type = (attachment.get("contentType") or "").lower()
            content_url = attachment.get("contentUrl")
            name = attachment.get("name") or _name_from_url(content_url) or f"attachment-{index}"
            destination_name = sanitize_file_name(name)
            if content_type != "reference" or not content_url:
                warning = f"Attachment {name} has unsupported contentType '{content_type or 'unknown'}' and will not be copied."
                warnings.append(warning)
                plans.append(PlannedUpload(name, destination_name, content_url, supported=False, warning=warning))
                continue
            plans.append(PlannedUpload(name, destination_name, content_url, supported=True))
        return plans, warnings

    async def copy_attachments(self, attachments: list[dict]) -> tuple[list[CopiedFile], list[str]]:
        copied: list[CopiedFile] = []
        warnings: list[str] = []
        for plan in self.plan_attachment_uploads(attachments)[0]:
            if not plan.supported or not plan.content_url:
                if plan.warning:
                    warnings.append(plan.warning)
                continue
            try:
                copied.append(await self.copy_reference_url(plan.content_url, plan.destination_name))
            except Exception as exc:
                warning = f"Attachment {plan.source_name} could not be copied: {exc}"
                logger.warning("Attachment copy failed", extra={"attachment_name": plan.source_name, "error": str(exc)})
                warnings.append(warning)
        return copied, warnings

    async def copy_reference_url(self, content_url: str, destination_name: str) -> CopiedFile:
        drive_item = await self.graph.get_drive_item_from_share_url(content_url)
        name = sanitize_file_name(drive_item.get("name") or destination_name)
        size = drive_item.get("size")
        if size is not None and int(size) > self.max_file_size_bytes:
            raise ValueError(f"file is {size} bytes, above configured limit of {self.max_file_size_bytes} bytes")
        if "file" not in drive_item or drive_item.get("file") is None:
            raise ValueError("Graph driveItem is not a file")

        content, content_type = await self.graph.download_drive_item_from_share_url(content_url)
        if len(content) > self.max_file_size_bytes:
            raise ValueError(f"downloaded file is {len(content)} bytes, above configured limit of {self.max_file_size_bytes} bytes")

        return await self.upload_bytes_with_collision_retry(name, content, content_type, source_content_url=content_url)

    async def upload_bytes_with_collision_retry(
        self,
        name: str,
        content: bytes,
        content_type: str,
        source_content_url: str | None = None,
    ) -> CopiedFile:
        if len(content) > self.max_file_size_bytes:
            raise ValueError(f"upload is {len(content)} bytes, above configured limit of {self.max_file_size_bytes} bytes")

        safe_name = sanitize_file_name(name)
        temp_path = self._write_temp_file(safe_name, content)
        try:
            try:
                uploaded = await self.graph.upload_file_to_channel_folder(self.files_folder, safe_name, content, content_type, conflict_behavior="fail")
            except GraphAPIError as exc:
                if exc.status_code != 409:
                    raise
                safe_name = append_short_hash(safe_name, source_content_url or content)
                uploaded = await self.graph.upload_file_to_channel_folder(self.files_folder, safe_name, content, content_type, conflict_behavior="fail")
            web_url = uploaded.get("webUrl")
            if not web_url:
                raise ValueError(f"uploaded file {uploaded.get('name') or safe_name} did not return a webUrl")
            logger.info("Uploaded file to destination channel folder", extra={"file_name": uploaded.get("name") or safe_name})
            return CopiedFile(
                name=uploaded.get("name") or safe_name,
                web_url=web_url,
                drive_item_id=uploaded.get("id"),
                source_content_url=source_content_url,
            )
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Temporary file cleanup failed", extra={"temp_path": str(temp_path)})

    def _write_temp_file(self, name: str, content: bytes) -> Path:
        suffix = Path(name).suffix
        handle = tempfile.NamedTemporaryFile(prefix="teams-repost-", suffix=suffix, dir=self.temp_folder, delete=False)
        with handle:
            handle.write(content)
        return Path(handle.name)


def sanitize_file_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    if not name:
        return "file"
    if len(name) <= 180:
        return name
    path = Path(name)
    suffix = path.suffix[:20]
    stem = path.stem[: 180 - len(suffix) - 1]
    return (stem + suffix).strip(" .") or "file"


def append_short_hash(name: str, seed: str | bytes) -> str:
    seed_bytes = seed if isinstance(seed, bytes) else seed.encode("utf-8")
    digest = hashlib.sha256(seed_bytes).hexdigest()[:8]
    path = Path(name)
    stem = path.stem or "file"
    return sanitize_file_name(f"{stem}-{digest}{path.suffix}")


def image_extension(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get((content_type or "").lower(), ".bin")


def _name_from_url(content_url: str | None) -> str | None:
    if not content_url:
        return None
    path = urlparse(content_url).path
    name = Path(path).name
    return name or None
