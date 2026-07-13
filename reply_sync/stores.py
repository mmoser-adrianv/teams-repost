from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class AtomicJsonStore:
    def __init__(self, path: Path, empty: dict[str, Any]) -> None:
        self.path = path
        self.empty = empty

    def load(self) -> dict[str, Any]:
        with self._lock():
            if not self.path.exists():
                return deepcopy(self.empty)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Reply-sync state is not valid JSON: {self.path}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Reply-sync state must contain a JSON object: {self.path}")
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(self.path.name + ".tmp")
            temp_path.write_text(
                json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.path)

    def _lock(self) -> threading.RLock:
        key = str(self.path.resolve())
        with _PATH_LOCKS_GUARD:
            return _PATH_LOCKS.setdefault(key, threading.RLock())


class ThreadRegistry:
    def __init__(self, path: Path) -> None:
        self.store = AtomicJsonStore(path, {"version": 1, "threads": {}})

    def list_threads(self) -> list[dict[str, Any]]:
        data = self._load()
        return sorted(
            (deepcopy(thread) for thread in data["threads"].values()),
            key=lambda item: (item.get("flow") or "", item.get("source", {}).get("created_date_time") or ""),
            reverse=True,
        )

    def get(self, thread_key: str) -> dict[str, Any] | None:
        thread = self._load()["threads"].get(thread_key)
        return deepcopy(thread) if thread else None

    def discover(self, repost_records: list[dict[str, Any]], flows: set[str], auto_enroll: bool) -> dict[str, int]:
        data = self._load()
        threads = data["threads"]
        added = 0
        updated = 0
        unlinked = 0
        now = utc_now()

        for record in repost_records:
            translation = record.get("translation") or {}
            target_language = str(translation.get("target_language") or "")
            if not target_language:
                continue
            flow = "reverse" if target_language.lower().startswith("en") else "forward"
            if flow not in flows:
                continue
            source = record.get("source") or {}
            destination = record.get("destination") or {}
            source_message_id = source.get("message_id")
            if not source_message_id:
                continue
            thread_key = str(record.get("source_key") or _thread_key(source, target_language))
            destination_message_id = destination.get("message_id")
            existing = threads.get(thread_key)
            if existing is None:
                status = "preview" if destination_message_id else "unlinked"
                threads[thread_key] = {
                    "thread_key": thread_key,
                    "flow": flow,
                    "target_language": target_language,
                    "source": deepcopy(source),
                    "destination": deepcopy(destination),
                    "origin": "repost_history" if destination_message_id else "manual_repost_history",
                    "enabled": bool(auto_enroll and destination_message_id),
                    "start_mode": "backfill_all" if auto_enroll and destination_message_id else None,
                    "baseline_pending": False,
                    "baseline_reply_ids": [],
                    "status": "active" if auto_enroll and destination_message_id else status,
                    "last_scan_at": None,
                    "last_contiguous_sequence": -1,
                    "blocked_reply_id": None,
                    "error": None,
                    "discovered_at": now,
                    "updated_at": now,
                }
                added += 1
            else:
                changed = False
                for field, value in (
                    ("flow", flow),
                    ("target_language", target_language),
                    ("source", deepcopy(source)),
                ):
                    if existing.get(field) != value:
                        existing[field] = value
                        changed = True
                if destination_message_id and existing.get("destination") != destination:
                    existing["destination"] = deepcopy(destination)
                    if existing.get("status") == "unlinked":
                        existing["status"] = "preview"
                    changed = True
                if changed:
                    existing["updated_at"] = now
                    updated += 1
            if not destination_message_id:
                unlinked += 1

        data["last_discovered_at"] = now
        self.store.save(data)
        return {"added": added, "updated": updated, "unlinked": unlinked, "total": len(threads)}

    def update(self, thread_key: str, **changes: Any) -> dict[str, Any]:
        data = self._load()
        thread = data["threads"].get(thread_key)
        if not thread:
            raise KeyError(thread_key)
        thread.update(deepcopy(changes))
        thread["updated_at"] = utc_now()
        self.store.save(data)
        return deepcopy(thread)

    def _load(self) -> dict[str, Any]:
        data = self.store.load()
        if data.get("version") != 1:
            raise ValueError(f"Unsupported reply-sync registry version: {data.get('version')}")
        threads = data.setdefault("threads", {})
        if not isinstance(threads, dict):
            raise ValueError("Reply-sync registry threads must be an object")
        return data


class ReplyCache:
    def __init__(self, path: Path) -> None:
        self.store = AtomicJsonStore(path, {"version": 1, "threads": {}})

    def get_thread(self, thread_key: str) -> dict[str, Any]:
        data = self._load()
        return deepcopy(data["threads"].get(thread_key) or {"replies": {}, "ordered_reply_ids": [], "stable_scans": 0})

    def record_scan(self, thread_key: str, replies: list[dict[str, Any]]) -> dict[str, Any]:
        data = self._load()
        threads = data["threads"]
        previous = threads.get(thread_key) or {"replies": {}, "ordered_reply_ids": [], "stable_scans": 0}
        ordered_ids = [str(reply["id"]) for reply in replies]
        previous_order = previous.get("ordered_reply_ids") or []
        previous_replies = previous.get("replies") or {}
        cached_replies: dict[str, dict[str, Any]] = {}
        observation_counts: list[int] = []
        for index, reply in enumerate(replies):
            reply_id = str(reply["id"])
            cached = deepcopy(reply)
            existing = previous_replies.get(reply_id) or {}
            same_position = index < len(previous_order) and previous_order[index] == reply_id
            same_version = existing.get("etag") == cached.get("etag")
            observed_scans = int(existing.get("observed_scans") or 0) + 1 if same_position and same_version else 1
            cached["observed_scans"] = observed_scans
            observation_counts.append(observed_scans)
            if existing.get("etag") == cached.get("etag") and existing.get("translation"):
                cached["translation"] = deepcopy(existing["translation"])
            cached_replies[reply_id] = cached
        thread_cache = {
            "ordered_reply_ids": ordered_ids,
            "stable_scans": min(observation_counts) if observation_counts else 0,
            "last_scan_at": utc_now(),
            "replies": cached_replies,
        }
        threads[thread_key] = thread_cache
        self.store.save(data)
        return deepcopy(thread_cache)

    def save_translation(self, thread_key: str, reply_id: str, translation: dict[str, Any]) -> None:
        data = self._load()
        try:
            reply = data["threads"][thread_key]["replies"][reply_id]
        except KeyError as exc:
            raise KeyError(reply_id) from exc
        reply["translation"] = deepcopy(translation)
        self.store.save(data)

    def annotate(self, thread_key: str, reply_id: str, **changes: Any) -> None:
        data = self._load()
        try:
            reply = data["threads"][thread_key]["replies"][reply_id]
        except KeyError as exc:
            raise KeyError(reply_id) from exc
        reply.update(deepcopy(changes))
        self.store.save(data)

    def _load(self) -> dict[str, Any]:
        data = self.store.load()
        if data.get("version") != 1:
            raise ValueError(f"Unsupported reply-sync cache version: {data.get('version')}")
        threads = data.setdefault("threads", {})
        if not isinstance(threads, dict):
            raise ValueError("Reply-sync cache threads must be an object")
        return data


class ReplyHistory:
    def __init__(self, path: Path) -> None:
        self.store = AtomicJsonStore(path, {"version": 1, "records": []})

    def list_records(self, thread_key: str | None = None) -> list[dict[str, Any]]:
        records = self._load()["records"]
        if thread_key is not None:
            records = [record for record in records if record.get("thread_key") == thread_key]
        return deepcopy(records)

    def get(self, thread_key: str, source_reply_id: str) -> dict[str, Any] | None:
        for record in self.list_records(thread_key):
            if record.get("source_reply_id") == source_reply_id:
                return record
        return None

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        records = data["records"]
        for index, existing in enumerate(records):
            if (
                existing.get("thread_key") == record.get("thread_key")
                and existing.get("source_reply_id") == record.get("source_reply_id")
            ):
                records[index] = deepcopy(record)
                self.store.save(data)
                return deepcopy(record)
        records.append(deepcopy(record))
        self.store.save(data)
        return deepcopy(record)

    def _load(self) -> dict[str, Any]:
        data = self.store.load()
        if data.get("version") != 1:
            raise ValueError(f"Unsupported reply-sync history version: {data.get('version')}")
        records = data.setdefault("records", [])
        if not isinstance(records, list):
            raise ValueError("Reply-sync history records must be an array")
        return data


def _thread_key(source: dict[str, Any], target_language: str) -> str:
    return "|".join(
        [
            str(source.get("team_id") or ""),
            str(source.get("channel_id") or ""),
            str(source.get("message_id") or ""),
            f"translation:{target_language}",
        ]
    )
