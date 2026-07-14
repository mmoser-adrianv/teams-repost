from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from html import unescape
from typing import Any, Awaitable, Callable

from graph_client import GraphAPIError
from message_rebuilder import extract_author_display_name, find_hosted_content_refs, normalize_body_to_html, strip_attachment_placeholders
from repost_history import RepostHistory
from teams_url_parser import TeamsUrlParseError, parse_teams_message_url
from translation_service import TranslationError, translate_cached_post

from .config import ReplySyncSettings
from .graph import ReplyGraph
from .payloads import (
    CHINESE_REPLY_SOURCE_PREFIX,
    ENGLISH_REPLY_SOURCE_PREFIX,
    LEGACY_REPLY_AUTHOR_PREFIXES,
    LEGACY_REPLY_SOURCE_MARKER_PREFIX,
    REPLY_SOURCE_MARKER_PREFIX,
    ReplyFidelityError,
    build_degraded_reply_payload,
    build_reply_payload,
    marker_candidates,
)
from .stores import ReplyCache, ReplyHistory, ThreadRegistry, utc_now


Translator = Callable[[dict[str, Any], str, Any], Awaitable[dict[str, Any]]]


class ReplySyncError(RuntimeError):
    pass


class ReplySequenceConflict(ReplySyncError):
    pass


class ReplySyncService:
    def __init__(
        self,
        reply_settings: ReplySyncSettings,
        core_settings: Any,
        translator: Translator = translate_cached_post,
    ) -> None:
        self.reply_settings = reply_settings
        self.core_settings = core_settings
        self.translator = translator
        self.registry = ThreadRegistry(reply_settings.registry_path)
        self.cache = ReplyCache(reply_settings.cache_path)
        self.history = ReplyHistory(reply_settings.history_path)

    def discover(self) -> dict[str, int]:
        records = RepostHistory(self.core_settings.repost_history_path).list_records()
        return self.registry.discover(
            records,
            set(self.reply_settings.flow_list),
            self.reply_settings.auto_enroll_new_threads,
        )

    def list_threads(self) -> dict[str, Any]:
        threads = []
        for thread in self.registry.list_threads():
            cache = self.cache.get_thread(thread["thread_key"])
            records = self.history.list_records(thread["thread_key"])
            completed = {record["source_reply_id"] for record in records if _is_completed(record)}
            baseline = set(thread.get("baseline_reply_ids") or [])
            ordered = cache.get("ordered_reply_ids") or []
            queued = [reply_id for reply_id in ordered if reply_id not in completed and reply_id not in baseline]
            summary = deepcopy(thread)
            summary.update(
                {
                    "discovered_reply_count": len(ordered),
                    "queued_reply_count": len(queued),
                    "completed_reply_count": len(completed),
                    "stable_scans": int(cache.get("stable_scans") or 0),
                    "automation_enabled": self.reply_settings.enabled,
                }
            )
            threads.append(summary)
        return {"enabled": self.reply_settings.enabled, "threads": threads}

    def activate(self, thread_key: str, start_mode: str) -> dict[str, Any]:
        if start_mode not in {"backfill_all", "future_only"}:
            raise ValueError("start_mode must be backfill_all or future_only")
        thread = self._thread(thread_key)
        if not (thread.get("destination") or {}).get("message_id"):
            raise ValueError("Thread must be linked to a destination post before activation")
        changes: dict[str, Any] = {
            "enabled": True,
            "start_mode": start_mode,
            "status": "active",
            "blocked_reply_id": None,
            "error": None,
        }
        if start_mode == "future_only":
            cached = self.cache.get_thread(thread_key)
            ordered = cached.get("ordered_reply_ids") or []
            changes["baseline_reply_ids"] = list(ordered)
            changes["baseline_pending"] = not bool(cached.get("last_scan_at"))
        else:
            changes["baseline_reply_ids"] = []
            changes["baseline_pending"] = False
        return self.registry.update(thread_key, **changes)

    def pause(self, thread_key: str) -> dict[str, Any]:
        self._thread(thread_key)
        return self.registry.update(thread_key, enabled=False, status="paused")

    def retry(self, thread_key: str) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if not thread.get("enabled"):
            raise ValueError("Thread must be active before retrying")
        return self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)

    async def link_destination(self, thread_key: str, destination_url: str, graph: ReplyGraph) -> dict[str, Any]:
        thread = self._thread(thread_key)
        try:
            parsed = parse_teams_message_url(destination_url)
        except TeamsUrlParseError as exc:
            raise ValueError(str(exc)) from exc
        if parsed.parent_message_id and parsed.parent_message_id != parsed.message_id:
            raise ValueError("Destination URL must point to a root channel post, not a reply")
        expected = thread.get("destination") or {}
        if expected.get("team_id") and expected["team_id"] != parsed.team_id:
            raise ValueError("Destination post belongs to a different team")
        if expected.get("channel_id") and expected["channel_id"] != parsed.source_channel_thread_id:
            raise ValueError("Destination post belongs to a different channel")
        message = await graph.get_root_message(parsed.team_id, parsed.source_channel_thread_id, parsed.message_id)
        destination = {
            "team_id": parsed.team_id,
            "channel_id": parsed.source_channel_thread_id,
            "message_id": parsed.message_id,
            "web_url": message.get("webUrl") or destination_url,
        }
        return self.registry.update(
            thread_key,
            destination=destination,
            origin="manual_link",
            status="preview",
            enabled=False,
            error=None,
        )

    async def run_all(self, graph: ReplyGraph) -> dict[str, Any]:
        discovery = self.discover()
        remaining = self.reply_settings.max_replies_per_run
        results: list[dict[str, Any]] = []
        for thread in self.registry.list_threads():
            if not thread.get("enabled") or remaining <= 0:
                continue
            result = await self.run_thread(thread["thread_key"], graph, remaining)
            remaining -= int(result.get("sent") or 0) + int(result.get("recovered") or 0)
            results.append(result)
        return {
            "status": "completed",
            "discovery": discovery,
            "threads": results,
            "sent": sum(int(result.get("sent") or 0) for result in results),
            "recovered": sum(int(result.get("recovered") or 0) for result in results),
            "blocked": sum(1 for result in results if result.get("status") == "blocked"),
        }

    async def run_thread(self, thread_key: str, graph: ReplyGraph, limit: int | None = None) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if not thread.get("enabled"):
            return {"thread_key": thread_key, "status": "paused", "sent": 0, "recovered": 0}
        destination = thread.get("destination") or {}
        if not destination.get("message_id"):
            return self._block(thread_key, None, "Destination post is not linked")

        source = thread["source"]
        try:
            listed = await graph.list_replies(source["team_id"], source["channel_id"], source["message_id"])
            normalized = sorted(
                (
                    _normalize_reply(reply, thread["target_language"])
                    for reply in listed
                    if _is_user_reply(reply) and not _is_translated_reply(reply)
                ),
                key=_reply_sort_key,
            )
            thread_cache = self.cache.record_scan(thread_key, normalized)
            self.registry.update(thread_key, last_scan_at=thread_cache["last_scan_at"])
            self._record_drift(thread_key, thread_cache)
            self._assert_no_late_reply(thread, thread_cache)
        except (GraphAPIError, ValueError, ReplySequenceConflict) as exc:
            if isinstance(exc, ReplySequenceConflict):
                self.registry.update(thread_key, enabled=False, status="sequence_conflict", error=str(exc))
                return {"thread_key": thread_key, "status": "sequence_conflict", "sent": 0, "recovered": 0, "error": str(exc)}
            return self._block(thread_key, None, str(exc))

        thread = self._thread(thread_key)
        if thread.get("baseline_pending"):
            self.registry.update(
                thread_key,
                baseline_reply_ids=list(thread_cache["ordered_reply_ids"]),
                baseline_pending=False,
            )
            return {"thread_key": thread_key, "status": "baselined", "sent": 0, "recovered": 0}
        sent = 0
        recovered = 0
        max_items = limit if limit is not None else self.reply_settings.max_replies_per_run
        destination_replies: list[dict[str, Any]] | None = None
        baseline = set(thread.get("baseline_reply_ids") or [])
        ordered_ids = thread_cache.get("ordered_reply_ids") or []
        for sequence, reply_id in enumerate(ordered_ids):
            if sent + recovered >= max_items:
                break
            if reply_id in baseline or self.history.get(thread_key, reply_id):
                continue
            reply = thread_cache["replies"][reply_id]
            if int(reply.get("observed_scans") or 0) < self.reply_settings.stability_scans:
                return {
                    "thread_key": thread_key,
                    "status": "stabilizing",
                    "stable_scans": reply.get("observed_scans"),
                    "sent": sent,
                    "recovered": recovered,
                }
            if reply.get("deleted_date_time"):
                self._record_completion(thread, reply, sequence, None, "skipped_deleted", {})
                continue
            try:
                translation = reply.get("translation")
                if not translation:
                    translation = await self.translator(reply, thread["target_language"], self.core_settings)
                    self.cache.save_translation(thread_key, reply_id, translation)
                    reply["translation"] = translation

                if destination_replies is None:
                    destination_replies = await graph.list_replies(
                        destination["team_id"], destination["channel_id"], destination["message_id"]
                    )
                matches = _find_destination_matches(destination_replies, reply)
                if len(matches) > 1:
                    raise ReplySequenceConflict(f"Multiple destination replies match source reply {reply_id}")
                if len(matches) == 1:
                    self._record_completion(thread, reply, sequence, matches[0], "recovered", {"degraded": False})
                    recovered += 1
                    continue

                hosted = await self._download_hosted_contents(graph, thread, reply)
                payload, fidelity = build_reply_payload(reply, translation, hosted)
                created = await graph.create_reply(
                    destination["team_id"], destination["channel_id"], destination["message_id"], payload
                )
                self._record_completion(thread, reply, sequence, created, "sent", fidelity)
                destination_replies.append(created)
                sent += 1
            except ReplySequenceConflict as exc:
                self.registry.update(thread_key, enabled=False, status="sequence_conflict", error=str(exc))
                return {
                    "thread_key": thread_key,
                    "status": "sequence_conflict",
                    "sent": sent,
                    "recovered": recovered,
                    "error": str(exc),
                }
            except (GraphAPIError, TranslationError, ReplyFidelityError, ValueError) as exc:
                return self._block(thread_key, reply_id, str(exc), sent, recovered)

        self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)
        return {"thread_key": thread_key, "status": "completed", "sent": sent, "recovered": recovered}

    async def send_degraded(self, thread_key: str, reply_id: str, graph: ReplyGraph) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if not thread.get("enabled"):
            raise ValueError("Thread must be active before sending a degraded reply")
        if thread.get("blocked_reply_id") != reply_id:
            raise ValueError("Only the currently blocked reply can be sent degraded")
        cached = self.cache.get_thread(thread_key)
        reply = (cached.get("replies") or {}).get(reply_id)
        if not reply:
            raise KeyError(reply_id)
        translation = reply.get("translation")
        if not translation:
            raise ValueError("Blocked reply has no completed translation to send")
        ordered_ids = cached.get("ordered_reply_ids") or []
        sequence = ordered_ids.index(reply_id)
        destination = thread["destination"]
        destination_replies = await graph.list_replies(
            destination["team_id"], destination["channel_id"], destination["message_id"]
        )
        matches = _find_destination_matches(destination_replies, reply)
        if len(matches) > 1:
            raise ReplySequenceConflict(f"Multiple destination replies match source reply {reply_id}")
        if matches:
            created = matches[0]
            status = "recovered"
            fidelity = {"degraded": True, "reconciled": True}
        else:
            payload, fidelity = build_degraded_reply_payload(reply, translation)
            created = await graph.create_reply(
                destination["team_id"], destination["channel_id"], destination["message_id"], payload
            )
            status = "degraded"
        record = self._record_completion(thread, reply, sequence, created, status, fidelity)
        self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)
        return {"status": status, "record": record}

    async def _download_hosted_contents(
        self,
        graph: ReplyGraph,
        thread: dict[str, Any],
        reply: dict[str, Any],
    ) -> dict[str, tuple[bytes, str]]:
        refs = reply.get("hosted_content_refs") or []
        if not refs:
            return {}
        source = thread["source"]
        listed = await graph.list_hosted_contents(
            source["team_id"], source["channel_id"], source["message_id"], reply["id"]
        )
        listed_ids = {str(item.get("id")) for item in listed if item.get("id")}
        downloads: dict[str, tuple[bytes, str]] = {}
        for ref in refs:
            hosted_id = str(ref["hosted_content_id"])
            if hosted_id not in listed_ids:
                raise ReplyFidelityError(f"Inline image {ref['occurrence']} was not listed by Microsoft Graph")
            downloads[hosted_id] = await graph.download_hosted_content(
                source["team_id"], source["channel_id"], source["message_id"], reply["id"], hosted_id
            )
        return downloads

    def _record_completion(
        self,
        thread: dict[str, Any],
        reply: dict[str, Any],
        sequence: int,
        destination_reply: dict[str, Any] | None,
        status: str,
        fidelity: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "thread_key": thread["thread_key"],
            "source_reply_id": reply["id"],
            "source_created_date_time": reply.get("created_date_time"),
            "source_etag": reply.get("etag"),
            "source_web_url": reply.get("web_url"),
            "destination_reply_id": (destination_reply or {}).get("id"),
            "destination_web_url": (destination_reply or {}).get("webUrl"),
            "sequence": sequence,
            "status": status,
            "fidelity": deepcopy(fidelity),
            "completed_at": utc_now(),
        }
        self.history.upsert(record)
        self.registry.update(thread["thread_key"], last_contiguous_sequence=sequence)
        return record

    def _record_drift(self, thread_key: str, thread_cache: dict[str, Any]) -> None:
        replies = thread_cache.get("replies") or {}
        for record in self.history.list_records(thread_key):
            reply = replies.get(record.get("source_reply_id"))
            if not reply:
                continue
            drift = None
            if reply.get("deleted_date_time"):
                drift = "deleted_after_sync"
            elif record.get("source_etag") and record.get("source_etag") != reply.get("etag"):
                drift = "edited_after_sync"
            if drift:
                self.cache.annotate(thread_key, reply["id"], drift=drift)

    def _assert_no_late_reply(self, thread: dict[str, Any], thread_cache: dict[str, Any]) -> None:
        ordered = thread_cache.get("ordered_reply_ids") or []
        completed = {record["source_reply_id"] for record in self.history.list_records(thread["thread_key"]) if _is_completed(record)}
        baseline = set(thread.get("baseline_reply_ids") or [])
        completed_positions = [index for index, reply_id in enumerate(ordered) if reply_id in completed]
        if not completed_positions:
            return
        last_completed_position = max(completed_positions)
        late = [
            reply_id
            for index, reply_id in enumerate(ordered)
            if index < last_completed_position and reply_id not in completed and reply_id not in baseline
        ]
        if late:
            raise ReplySequenceConflict(
                "Earlier source replies appeared after later replies were already synchronized: " + ", ".join(late)
            )

    def _block(
        self,
        thread_key: str,
        reply_id: str | None,
        error: str,
        sent: int = 0,
        recovered: int = 0,
    ) -> dict[str, Any]:
        self.registry.update(thread_key, status="blocked", blocked_reply_id=reply_id, error=error)
        if reply_id:
            self.cache.annotate(thread_key, reply_id, blocked_error=error, blocked_at=utc_now())
        return {
            "thread_key": thread_key,
            "status": "blocked",
            "blocked_reply_id": reply_id,
            "sent": sent,
            "recovered": recovered,
            "error": error,
        }

    def _thread(self, thread_key: str) -> dict[str, Any]:
        thread = self.registry.get(thread_key)
        if not thread:
            raise KeyError(thread_key)
        return thread


def _normalize_reply(message: dict[str, Any], target_language: str) -> dict[str, Any]:
    body_html = strip_attachment_placeholders(normalize_body_to_html(message))
    refs = find_hosted_content_refs(body_html)
    return {
        "id": str(message.get("id") or ""),
        "reply_to_id": message.get("replyToId"),
        "created_date_time": message.get("createdDateTime"),
        "last_modified_date_time": message.get("lastModifiedDateTime"),
        "deleted_date_time": message.get("deletedDateTime"),
        "etag": message.get("etag"),
        "web_url": message.get("webUrl"),
        "author": extract_author_display_name(message),
        "body_html": body_html,
        "body_preview": _visible_text(body_html)[:240],
        "attachments": [
            {
                "id": attachment.get("id"),
                "name": attachment.get("name") or f"attachment-{index}",
                "content_type": attachment.get("contentType"),
                "content_url": attachment.get("contentUrl"),
            }
            for index, attachment in enumerate(message.get("attachments") or [], start=1)
        ],
        "hosted_content_refs": [
            {
                "occurrence": ref.occurrence,
                "hosted_content_id": ref.hosted_content_id,
                "src": ref.src,
            }
            for ref in refs
        ],
        "target_language": target_language,
    }


def _is_user_reply(message: dict[str, Any]) -> bool:
    return bool(message.get("id")) and str(message.get("messageType") or "message") == "message"


def _is_translated_reply(message: dict[str, Any]) -> bool:
    body_text = _visible_text(normalize_body_to_html(message))
    translated_prefixes = (
        ENGLISH_REPLY_SOURCE_PREFIX,
        CHINESE_REPLY_SOURCE_PREFIX,
        *LEGACY_REPLY_AUTHOR_PREFIXES,
    )
    return body_text.startswith(translated_prefixes) or any(
        marker in body_text
        for marker in (REPLY_SOURCE_MARKER_PREFIX, LEGACY_REPLY_SOURCE_MARKER_PREFIX)
    )


def _reply_sort_key(reply: dict[str, Any]) -> tuple[str, tuple[int, str]]:
    reply_id = str(reply.get("id") or "")
    return str(reply.get("created_date_time") or ""), (int(reply_id), "") if reply_id.isdigit() else (0, reply_id)


def _visible_text(body_html: str) -> str:
    return re.sub(r"[\s\u00a0]+", " ", unescape(re.sub(r"<[^>]+>", " ", body_html or ""))).strip()


def _find_destination_matches(destination_replies: list[dict[str, Any]], source_reply: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = marker_candidates(source_reply)
    matches = []
    for reply in destination_replies:
        body = str((reply.get("body") or {}).get("content") or "")
        if any(candidate in body or candidate.replace("&", "&amp;") in body for candidate in candidates):
            matches.append(reply)
    return matches


def _is_completed(record: dict[str, Any]) -> bool:
    return record.get("status") in {"sent", "degraded", "recovered", "skipped_deleted"}
