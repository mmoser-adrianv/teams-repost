from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RepostHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_records(self) -> list[dict[str, Any]]:
        return list(self._load().get("records", []))

    def get(
        self,
        source_team_id: str,
        source_channel_id: str,
        source_message_id: str,
        target_language: str | None = None,
    ) -> dict[str, Any] | None:
        key = _source_key(source_team_id, source_channel_id, source_message_id, target_language)
        for record in self.list_records():
            if record.get("source_key") == key:
                return record
        return None

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        records = data.setdefault("records", [])
        key = record["source_key"]
        for index, existing in enumerate(records):
            if existing.get("source_key") == key:
                records[index] = record
                self._save(data)
                return record
        records.append(record)
        self._save(data)
        return record

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"records": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Repost history file is not valid JSON: {self.path}") from exc
        if isinstance(data, list):
            return {"records": data}
        if not isinstance(data, dict):
            raise ValueError(f"Repost history file must contain a JSON object: {self.path}")
        records = data.get("records")
        if records is None:
            data["records"] = []
        elif not isinstance(records, list):
            raise ValueError(f"Repost history 'records' must be an array: {self.path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


def build_repost_record(
    source_team_id: str,
    source_channel_id: str,
    destination_team_id: str,
    destination_channel_id: str,
    report: dict[str, Any],
    target_language: str | None = None,
) -> dict[str, Any]:
    source_message_id = report["source_message_id"]
    record = {
        "source_key": _source_key(source_team_id, source_channel_id, source_message_id, target_language),
        "source": {
            "team_id": source_team_id,
            "channel_id": source_channel_id,
            "message_id": source_message_id,
            "web_url": report.get("source_message_web_url"),
            "subject": report.get("source_subject"),
            "author": report.get("source_author"),
            "created_date_time": report.get("source_created_date_time"),
        },
        "destination": {
            "team_id": destination_team_id,
            "channel_id": destination_channel_id,
            "message_id": report.get("new_message_id"),
            "web_url": report.get("new_message_web_url"),
        },
        "attachment_links": report.get("attachment_links") or [],
        "attachment_statuses": report.get("attachment_statuses") or [],
        "inline_image_statuses": report.get("inline_image_statuses") or [],
        "status": "reposted",
        "manual": False,
        "warnings": report.get("warnings") or [],
        "reposted_at": datetime.now(UTC).isoformat(),
    }
    if target_language:
        record["translation"] = {
            "target_language": target_language,
        }
    return record


def build_manual_repost_record(
    source_team_id: str,
    source_channel_id: str,
    destination_team_id: str,
    destination_channel_id: str,
    cached_post: dict[str, Any],
    target_language: str | None = None,
) -> dict[str, Any]:
    source_message_id = cached_post["id"]
    record = {
        "source_key": _source_key(source_team_id, source_channel_id, source_message_id, target_language),
        "source": {
            "team_id": source_team_id,
            "channel_id": source_channel_id,
            "message_id": source_message_id,
            "web_url": cached_post.get("web_url"),
            "subject": cached_post.get("subject"),
            "author": cached_post.get("author"),
            "created_date_time": cached_post.get("created_date_time"),
        },
        "destination": {
            "team_id": destination_team_id,
            "channel_id": destination_channel_id,
            "message_id": None,
            "web_url": None,
        },
        "attachment_links": cached_post.get("attachments") or [],
        "attachment_statuses": [],
        "inline_image_statuses": [],
        "status": "manually_marked",
        "manual": True,
        "warnings": [],
        "reposted_at": datetime.now(UTC).isoformat(),
    }
    if target_language:
        record["translation"] = {
            "target_language": target_language,
        }
    return record


def _source_key(source_team_id: str, source_channel_id: str, source_message_id: str, target_language: str | None = None) -> str:
    key = f"{source_team_id}|{source_channel_id}|{source_message_id}"
    return f"{key}|translation:{target_language}" if target_language else key
