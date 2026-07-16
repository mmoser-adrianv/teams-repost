from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
    LEGACY_REPLY_SOURCE_MARKER_PREFIX,
    REPLY_AUTHOR_PREFIXES,
    REPLY_SOURCE_MARKER_PREFIX,
    ReplyFidelityError,
    build_degraded_reply_payload,
    build_reply_payload,
    marker_candidates,
)
from .stores import ReplyCache, ReplyHistory, ReturnQueue, ThreadRegistry, utc_now


Translator = Callable[[dict[str, Any], str, Any], Awaitable[dict[str, Any]]]
RETURN_THREAD_SUFFIX = "|reply:return"


logger = logging.getLogger(__name__)


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
        self.return_queue = ReturnQueue(reply_settings.return_queue_path)

    def discover(self) -> dict[str, int]:
        records = RepostHistory(self.core_settings.repost_history_path).list_records()
        result = self.registry.discover(
            self._prepare_discovery_records(records),
            set(self.reply_settings.flow_list),
            self.reply_settings.auto_enroll_new_threads,
        )
        superseded = self._retire_superseded_return_threads()
        result["updated"] += superseded
        result["superseded"] = superseded
        return result

    def list_threads(self) -> dict[str, Any]:
        threads = []
        return_items_by_thread: dict[str, list[dict[str, Any]]] = {}
        for item in self.return_queue.list_items():
            return_items_by_thread.setdefault(str(item.get("thread_key") or ""), []).append(item)
        for thread in self.registry.list_threads():
            cache = self.cache.get_thread(thread["thread_key"])
            records = self.history.list_records(thread["thread_key"])
            completed = {record["source_reply_id"] for record in records if _is_completed(record)}
            baseline = set(thread.get("baseline_reply_ids") or [])
            ordered = cache.get("ordered_reply_ids") or []
            queued = [reply_id for reply_id in ordered if reply_id not in completed and reply_id not in baseline]
            summary = deepcopy(thread)
            return_items = return_items_by_thread.get(thread["thread_key"], [])
            summary.update(
                {
                    "discovered_reply_count": len(ordered),
                    "queued_reply_count": len(queued),
                    "completed_reply_count": len(completed),
                    "stable_scans": int(cache.get("stable_scans") or 0),
                    "return_queue_count": sum(
                        1 for item in return_items if item.get("status") not in ReturnQueue.TERMINAL_STATUSES
                    ),
                    "return_ready_count": sum(1 for item in return_items if item.get("status") == "ready"),
                    "automation_enabled": self.reply_settings.enabled and self._direction_enabled(thread),
                }
            )
            threads.append(summary)
        return_next_send_at = self._return_next_send_at()
        return {
            "enabled": self.reply_settings.enabled,
            "return_enabled": self.reply_settings.return_enabled,
            "return_send_interval_minutes": self.reply_settings.return_send_interval_minutes,
            "return_next_send_at": return_next_send_at.isoformat() if return_next_send_at else None,
            "return_queue": self.return_queue.summary(),
            "threads": threads,
        }

    def activate(self, thread_key: str, start_mode: str) -> dict[str, Any]:
        if start_mode not in {"backfill_all", "future_only"}:
            raise ValueError("start_mode must be backfill_all or future_only")
        thread = self._thread(thread_key)
        self._assert_direction_enabled(thread)
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
        thread = self._thread(thread_key)
        if thread.get("status") == "superseded":
            raise ValueError("Reply-sync thread was superseded by an exact reverse mapping")
        return self.registry.update(thread_key, enabled=False, status="paused")

    def retry(self, thread_key: str) -> dict[str, Any]:
        thread = self._thread(thread_key)
        self._assert_direction_enabled(thread)
        if not thread.get("enabled"):
            raise ValueError("Thread must be active before retrying")
        return self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)

    async def link_destination(self, thread_key: str, destination_url: str, graph: ReplyGraph) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if thread.get("status") == "superseded":
            raise ValueError("Reply-sync thread was superseded by an exact reverse mapping")
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
        linked = self.registry.update(
            thread_key,
            destination=destination,
            origin="manual_link",
            status="preview",
            enabled=False,
            error=None,
        )
        if self.reply_settings.return_enabled and linked.get("direction", "primary") == "primary":
            linked = self._ensure_return_thread(linked)
        return linked

    async def run_all(self, graph: ReplyGraph) -> dict[str, Any]:
        discovery = self.discover()
        remaining = self.reply_settings.max_replies_per_run
        results: list[dict[str, Any]] = []
        threads = self.registry.list_threads()
        for thread in threads:
            if thread.get("direction", "primary") == "return":
                continue
            if not self._direction_enabled(thread) or not thread.get("enabled") or remaining <= 0:
                continue
            result = await self._run_immediate_thread(thread["thread_key"], graph, remaining)
            remaining -= int(result.get("sent") or 0) + int(result.get("recovered") or 0)
            results.append(result)

        if self.reply_settings.return_enabled:
            for thread in threads:
                if thread.get("direction") != "return" or not thread.get("enabled"):
                    continue
                results.append(await self._collect_return_thread(thread["thread_key"], graph))
            results.append(await self._dispatch_return_reply(graph))
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
        if thread.get("direction") != "return":
            return await self._run_immediate_thread(thread_key, graph, limit)
        collection = await self._collect_return_thread(thread_key, graph)
        if collection.get("status") in {"paused", "return_disabled", "superseded", "sequence_conflict"}:
            return collection
        dispatch = await self._dispatch_return_reply(graph)
        return {
            **collection,
            "status": "blocked" if dispatch.get("status") == "blocked" else collection.get("status"),
            "dispatch_status": dispatch.get("status"),
            "sent": int(dispatch.get("sent") or 0),
            "recovered": int(dispatch.get("recovered") or 0),
            "next_send_at": dispatch.get("next_send_at"),
            "blocked_reply_id": dispatch.get("blocked_reply_id") or collection.get("blocked_reply_id"),
            "error": dispatch.get("error") or collection.get("error"),
        }

    async def _run_immediate_thread(
        self,
        thread_key: str,
        graph: ReplyGraph,
        limit: int | None = None,
    ) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if thread.get("status") == "superseded":
            return {"thread_key": thread_key, "status": "superseded", "sent": 0, "recovered": 0}
        if not self._direction_enabled(thread):
            return {"thread_key": thread_key, "status": "return_disabled", "sent": 0, "recovered": 0}
        if not thread.get("enabled"):
            return {"thread_key": thread_key, "status": "paused", "sent": 0, "recovered": 0}
        destination = thread.get("destination") or {}
        if not destination.get("message_id"):
            return self._block(thread_key, None, "Destination post is not linked")

        source = thread["source"]
        counterpart_thread_key = thread.get("counterpart_thread_key")
        paired_destination_ids = (
            self.history.destination_reply_ids(str(counterpart_thread_key)) if counterpart_thread_key else set()
        )
        try:
            listed = await graph.list_replies(source["team_id"], source["channel_id"], source["message_id"])
            normalized = sorted(
                (
                    _normalize_reply(reply, thread["target_language"])
                    for reply in listed
                    if _is_user_reply(reply) and not _is_translated_reply(reply, paired_destination_ids)
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
                payload, fidelity = build_reply_payload(
                    reply,
                    translation,
                    hosted,
                    target_language=thread["target_language"],
                )
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

    async def _collect_return_thread(self, thread_key: str, graph: ReplyGraph) -> dict[str, Any]:
        thread = self._thread(thread_key)
        if thread.get("status") == "superseded":
            return {"thread_key": thread_key, "status": "superseded", "sent": 0, "recovered": 0}
        if not self._direction_enabled(thread):
            return {"thread_key": thread_key, "status": "return_disabled", "sent": 0, "recovered": 0}
        if not thread.get("enabled"):
            return {"thread_key": thread_key, "status": "paused", "sent": 0, "recovered": 0}
        destination = thread.get("destination") or {}
        if not destination.get("message_id"):
            return self._block(thread_key, None, "Destination post is not linked")

        source = thread["source"]
        counterpart_thread_key = thread.get("counterpart_thread_key")
        paired_destination_ids = (
            self.history.destination_reply_ids(str(counterpart_thread_key)) if counterpart_thread_key else set()
        )
        try:
            listed = await graph.list_replies(source["team_id"], source["channel_id"], source["message_id"])
            normalized = sorted(
                (
                    _normalize_reply(reply, thread["target_language"])
                    for reply in listed
                    if _is_user_reply(reply) and not _is_translated_reply(reply, paired_destination_ids)
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
                return {
                    "thread_key": thread_key,
                    "status": "sequence_conflict",
                    "sent": 0,
                    "recovered": 0,
                    "error": str(exc),
                }
            return self._block(thread_key, None, str(exc))

        thread = self._thread(thread_key)
        if thread.get("baseline_pending"):
            self.registry.update(
                thread_key,
                baseline_reply_ids=list(thread_cache["ordered_reply_ids"]),
                baseline_pending=False,
            )
            return {"thread_key": thread_key, "status": "baselined", "sent": 0, "recovered": 0}

        baseline = set(thread.get("baseline_reply_ids") or [])
        ordered_ids = thread_cache.get("ordered_reply_ids") or []
        queue_items: list[dict[str, Any]] = []
        collected = 0
        translated = 0
        for sequence, reply_id in enumerate(ordered_ids):
            if reply_id in baseline:
                continue
            completed = self.history.get(thread_key, reply_id)
            if completed:
                if self.return_queue.get(thread_key, reply_id):
                    self.return_queue.mark(
                        thread_key,
                        reply_id,
                        str(completed.get("status") or "sent"),
                        completed_at=completed.get("completed_at"),
                    )
                continue
            reply = thread_cache["replies"][reply_id]
            queue_item = {
                "thread_key": thread_key,
                "mapping_key": thread.get("mapping_key") or thread_key,
                "source_reply_id": reply_id,
                "source_created_date_time": reply.get("created_date_time"),
                "source_etag": reply.get("etag"),
                "sequence": sequence,
                "status": "collected",
            }
            if int(reply.get("observed_scans") or 0) < self.reply_settings.stability_scans:
                queue_item["status"] = "stabilizing"
                queue_items.append(queue_item)
                self.return_queue.upsert_many(queue_items)
                return {
                    "thread_key": thread_key,
                    "status": "stabilizing",
                    "stable_scans": reply.get("observed_scans"),
                    "collected": collected,
                    "translated": translated,
                    "sent": 0,
                    "recovered": 0,
                }
            collected += 1
            if reply.get("deleted_date_time"):
                completion = self._record_completion(thread, reply, sequence, None, "skipped_deleted", {})
                queue_item.update(status="skipped_deleted", completed_at=completion["completed_at"])
                queue_items.append(queue_item)
                continue
            try:
                translation = reply.get("translation")
                if not translation:
                    translation = await self.translator(reply, thread["target_language"], self.core_settings)
                    self.cache.save_translation(thread_key, reply_id, translation)
                    reply["translation"] = translation
                    translated += 1
                queue_item.update(status="ready", translated_at=translation.get("translated_at") or utc_now())
                queue_items.append(queue_item)
            except (TranslationError, ValueError) as exc:
                queue_item.update(status="blocked", error=str(exc))
                queue_items.append(queue_item)
                self.return_queue.upsert_many(queue_items)
                result = self._block(thread_key, reply_id, str(exc))
                result.update({"collected": collected, "translated": translated})
                return result

        self.return_queue.upsert_many(queue_items)
        self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)
        queued = sum(
            1
            for item in self.return_queue.list_items(thread_key)
            if item.get("status") not in ReturnQueue.TERMINAL_STATUSES
        )
        return {
            "thread_key": thread_key,
            "status": "collected",
            "collected": collected,
            "translated": translated,
            "queued": queued,
            "sent": 0,
            "recovered": 0,
        }

    async def _dispatch_return_reply(self, graph: ReplyGraph) -> dict[str, Any]:
        now = datetime.now(UTC)
        next_send_at = self._return_next_send_at()
        if next_send_at and now < next_send_at:
            return {
                "status": "throttled",
                "sent": 0,
                "recovered": 0,
                "next_send_at": next_send_at.isoformat(),
            }

        recovered = 0
        errors: list[str] = []
        blocked_reply_id: str | None = None
        attempted: set[str] = set()
        while True:
            candidates = [
                item
                for item in self.return_queue.ready_heads()
                if str(item.get("queue_key") or "") not in attempted
            ]
            if not candidates:
                return {
                    "status": "blocked" if errors else "queue_empty",
                    "sent": 0,
                    "recovered": recovered,
                    "errors": errors,
                    "blocked_reply_id": blocked_reply_id,
                    "error": errors[-1] if errors else None,
                }
            item = candidates[0]
            attempted.add(str(item.get("queue_key") or ""))
            thread_key = str(item["thread_key"])
            reply_id = str(item["source_reply_id"])
            thread = self.registry.get(thread_key)
            if (
                not thread
                or thread.get("direction") != "return"
                or not thread.get("enabled")
                or not self._direction_enabled(thread)
            ):
                continue

            completed = self.history.get(thread_key, reply_id)
            if completed:
                self.return_queue.mark(
                    thread_key,
                    reply_id,
                    str(completed.get("status") or "sent"),
                    completed_at=completed.get("completed_at"),
                )
                continue
            cached = self.cache.get_thread(thread_key)
            reply = (cached.get("replies") or {}).get(reply_id)
            if not reply:
                self.return_queue.mark(thread_key, reply_id, "waiting_source")
                continue
            translation = reply.get("translation")
            if not translation or reply.get("etag") != item.get("source_etag"):
                self.return_queue.mark(thread_key, reply_id, "collected")
                continue

            destination = thread.get("destination") or {}
            try:
                destination_replies = await graph.list_replies(
                    destination["team_id"], destination["channel_id"], destination["message_id"]
                )
                matches = _find_destination_matches(destination_replies, reply)
                if len(matches) > 1:
                    raise ReplySequenceConflict(f"Multiple destination replies match source reply {reply_id}")
                if matches:
                    completion = self._record_completion(
                        thread,
                        reply,
                        int(item.get("sequence") or 0),
                        matches[0],
                        "recovered",
                        {"degraded": False},
                    )
                    self.return_queue.mark(
                        thread_key,
                        reply_id,
                        "recovered",
                        completed_at=completion["completed_at"],
                        destination_reply_id=completion.get("destination_reply_id"),
                    )
                    recovered += 1
                    continue

                hosted = await self._download_hosted_contents(graph, thread, reply)
                payload, fidelity = build_reply_payload(
                    reply,
                    translation,
                    hosted,
                    target_language=thread["target_language"],
                )
                created = await graph.create_reply(
                    destination["team_id"], destination["channel_id"], destination["message_id"], payload
                )
                sent_at = self.return_queue.record_send()
                completion = self._record_completion(
                    thread,
                    reply,
                    int(item.get("sequence") or 0),
                    created,
                    "sent",
                    fidelity,
                )
                self.return_queue.mark(
                    thread_key,
                    reply_id,
                    "sent",
                    completed_at=completion["completed_at"],
                    destination_reply_id=completion.get("destination_reply_id"),
                )
                self.registry.update(thread_key, status="active", blocked_reply_id=None, error=None)
                return {
                    "thread_key": thread_key,
                    "source_reply_id": reply_id,
                    "status": "sent",
                    "sent": 1,
                    "recovered": recovered,
                    "sent_at": sent_at,
                    "next_send_at": (datetime.fromisoformat(sent_at) + timedelta(
                        minutes=self.reply_settings.return_send_interval_minutes
                    )).isoformat(),
                }
            except ReplySequenceConflict as exc:
                self.registry.update(thread_key, enabled=False, status="sequence_conflict", error=str(exc))
                self.return_queue.mark(thread_key, reply_id, "blocked", error=str(exc))
                errors.append(str(exc))
                blocked_reply_id = reply_id
            except (GraphAPIError, ReplyFidelityError, ValueError) as exc:
                self.return_queue.mark(thread_key, reply_id, "blocked", error=str(exc))
                self._block(thread_key, reply_id, str(exc))
                errors.append(str(exc))
                blocked_reply_id = reply_id

    def _return_next_send_at(self) -> datetime | None:
        last_sent_at = self.return_queue.last_sent_at()
        if not last_sent_at:
            return None
        parsed = datetime.fromisoformat(last_sent_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC) + timedelta(minutes=self.reply_settings.return_send_interval_minutes)

    async def send_degraded(self, thread_key: str, reply_id: str, graph: ReplyGraph) -> dict[str, Any]:
        thread = self._thread(thread_key)
        self._assert_direction_enabled(thread)
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
            if thread.get("direction") == "return":
                next_send_at = self._return_next_send_at()
                if next_send_at and datetime.now(UTC) < next_send_at:
                    raise ValueError(
                        "The reciprocal reply send interval has not elapsed; next send is allowed at "
                        + next_send_at.isoformat()
                    )
            payload, fidelity = build_degraded_reply_payload(
                reply,
                translation,
                target_language=thread["target_language"],
            )
            created = await graph.create_reply(
                destination["team_id"], destination["channel_id"], destination["message_id"], payload
            )
            if thread.get("direction") == "return":
                self.return_queue.record_send()
            status = "degraded"
        record = self._record_completion(thread, reply, sequence, created, status, fidelity)
        if thread.get("direction") == "return":
            self.return_queue.mark(
                thread_key,
                reply_id,
                status,
                completed_at=record.get("completed_at"),
                destination_reply_id=record.get("destination_reply_id"),
            )
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
            "mapping_key": thread.get("mapping_key") or thread["thread_key"],
            "direction": thread.get("direction", "primary"),
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

    def _prepare_discovery_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        eligible_primary_records: list[dict[str, Any]] = []
        actual_legs: dict[tuple[str, ...], str] = {}
        flows = set(self.reply_settings.flow_list)

        for raw_record in records:
            record = deepcopy(raw_record)
            translation = record.get("translation") or {}
            target_language = str(translation.get("target_language") or "")
            if not target_language or _flow_for_target(target_language) not in flows:
                prepared.append(record)
                continue
            thread_key = _record_thread_key(record, target_language)
            source_language = _source_language_for_record(record, self.core_settings)
            metadata = deepcopy(record.get("reply_sync") or {})
            metadata.update(
                {
                    "mapping_key": metadata.get("mapping_key") or thread_key,
                    "direction": "primary",
                    "source_language": source_language,
                    "auto_enroll": self.reply_settings.auto_enroll_new_threads,
                    "promote_existing": self.reply_settings.auto_enroll_new_threads,
                }
            )
            record["reply_sync"] = metadata
            prepared.append(record)
            eligible_primary_records.append(record)
            identity = _leg_identity(record.get("source") or {}, record.get("destination") or {}, target_language)
            if identity:
                actual_legs.setdefault(identity, thread_key)

        if not self.reply_settings.return_enabled:
            return prepared

        reciprocal_records: list[dict[str, Any]] = []
        for record in eligible_primary_records:
            target_language = str((record.get("translation") or {}).get("target_language") or "")
            source_language = str((record.get("reply_sync") or {}).get("source_language") or "")
            primary_key = _record_thread_key(record, target_language)
            source = record.get("source") or {}
            destination = record.get("destination") or {}
            if not destination.get("message_id"):
                continue
            if not source_language:
                logger.warning(
                    "Skipping reciprocal reply-sync discovery for %s because its source language cannot be inferred",
                    primary_key,
                )
                continue
            if _flow_for_target(source_language) not in flows:
                continue

            reciprocal_identity = _leg_identity(destination, source, source_language)
            actual_counterpart = actual_legs.get(reciprocal_identity) if reciprocal_identity else None
            if actual_counterpart and actual_counterpart != primary_key:
                record["reply_sync"]["counterpart_thread_key"] = actual_counterpart
                continue

            return_record = _build_return_record(
                record,
                primary_key,
                source_language,
                self.reply_settings.return_auto_enroll_new_threads
                or self.reply_settings.return_backfill_existing_threads,
                self.reply_settings.return_backfill_existing_threads,
            )
            if not return_record:
                continue
            return_key = str(return_record["source_key"])
            record["reply_sync"]["counterpart_thread_key"] = return_key
            reciprocal_records.append(return_record)

        return prepared + reciprocal_records

    def _ensure_return_thread(self, primary: dict[str, Any]) -> dict[str, Any]:
        source_language = str(primary.get("source_language") or "")
        if not source_language:
            source_language = _source_language_for_target(str(primary.get("target_language") or ""), self.core_settings)
        if not source_language or _flow_for_target(source_language) not in set(self.reply_settings.flow_list):
            return primary

        desired_identity = _leg_identity(
            primary.get("destination") or {},
            primary.get("source") or {},
            source_language,
        )
        for candidate in self.registry.list_threads():
            if candidate["thread_key"] == primary["thread_key"] or candidate.get("direction", "primary") != "primary":
                continue
            candidate_identity = _leg_identity(
                candidate.get("source") or {},
                candidate.get("destination") or {},
                str(candidate.get("target_language") or ""),
            )
            if desired_identity and candidate_identity == desired_identity:
                self.registry.update(candidate["thread_key"], counterpart_thread_key=primary["thread_key"])
                return self.registry.update(primary["thread_key"], counterpart_thread_key=candidate["thread_key"])

        primary_record = {
            "source_key": primary["thread_key"],
            "source": deepcopy(primary.get("source") or {}),
            "destination": deepcopy(primary.get("destination") or {}),
            "translation": {
                "source_language": source_language,
                "target_language": primary.get("target_language"),
            },
            "reposted_at": primary.get("updated_at"),
        }
        return_record = _build_return_record(
            primary_record,
            primary["thread_key"],
            source_language,
            self.reply_settings.return_auto_enroll_new_threads
            or self.reply_settings.return_backfill_existing_threads,
            self.reply_settings.return_backfill_existing_threads,
        )
        if not return_record:
            return primary
        self.registry.discover([return_record], set(self.reply_settings.flow_list), False)
        return_key = str(return_record["source_key"])
        if not self.registry.get(return_key):
            return primary
        return self.registry.update(primary["thread_key"], counterpart_thread_key=return_key)

    def _retire_superseded_return_threads(self) -> int:
        threads = self.registry.list_threads()
        primaries = {
            thread["thread_key"]: thread
            for thread in threads
            if thread.get("direction", "primary") == "primary"
        }
        retired = 0
        for primary in primaries.values():
            counterpart_key = primary.get("counterpart_thread_key")
            if counterpart_key not in primaries:
                continue
            synthetic_key = primary["thread_key"] + RETURN_THREAD_SUFFIX
            synthetic = self.registry.get(synthetic_key)
            if not synthetic or synthetic.get("direction") != "return":
                continue
            if (
                synthetic.get("status") == "superseded"
                and not synthetic.get("enabled")
                and synthetic.get("superseded_by") == counterpart_key
            ):
                continue
            self.registry.update(
                synthetic_key,
                enabled=False,
                status="superseded",
                superseded_by=counterpart_key,
                error=None,
            )
            retired += 1
        return retired

    def _direction_enabled(self, thread: dict[str, Any]) -> bool:
        return thread.get("status") != "superseded" and (
            thread.get("direction", "primary") != "return" or self.reply_settings.return_enabled
        )

    def _assert_direction_enabled(self, thread: dict[str, Any]) -> None:
        if thread.get("status") == "superseded":
            raise ValueError("Reply-sync thread was superseded by an exact reverse mapping")
        if not self._direction_enabled(thread):
            raise ValueError("Reciprocal reply synchronization is disabled")

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


def _is_translated_reply(message: dict[str, Any], paired_destination_ids: set[str] | None = None) -> bool:
    if str(message.get("id") or "") in (paired_destination_ids or set()):
        return True
    body_text = _visible_text(normalize_body_to_html(message))
    translated_prefixes = (
        ENGLISH_REPLY_SOURCE_PREFIX,
        CHINESE_REPLY_SOURCE_PREFIX,
        *REPLY_AUTHOR_PREFIXES,
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


def _flow_for_target(target_language: str) -> str:
    return "reverse" if _normalized_language(target_language).startswith("en") else "forward"


def _normalized_language(language: str) -> str:
    return str(language or "").strip().lower().replace("_", "-")


def _source_language_for_record(record: dict[str, Any], core_settings: Any) -> str | None:
    translation = record.get("translation") or {}
    explicit = str(translation.get("source_language") or "").strip()
    if explicit:
        return explicit
    return _source_language_for_target(str(translation.get("target_language") or ""), core_settings)


def _source_language_for_target(target_language: str, core_settings: Any) -> str | None:
    target = _normalized_language(target_language)
    configured_target = str(getattr(core_settings, "openai_translation_target", "") or "").strip()
    if target.startswith("en"):
        return configured_target or None
    if configured_target and target == _normalized_language(configured_target):
        return "en"
    return None


def _record_thread_key(record: dict[str, Any], target_language: str) -> str:
    if record.get("source_key"):
        return str(record["source_key"])
    source = record.get("source") or {}
    return "|".join(
        [
            str(source.get("team_id") or ""),
            str(source.get("channel_id") or ""),
            str(source.get("message_id") or ""),
            f"translation:{target_language}",
        ]
    )


def _leg_identity(
    source: dict[str, Any],
    destination: dict[str, Any],
    target_language: str,
) -> tuple[str, ...] | None:
    values = (
        source.get("team_id"),
        source.get("channel_id"),
        source.get("message_id"),
        destination.get("team_id"),
        destination.get("channel_id"),
        destination.get("message_id"),
    )
    if not all(values) or not target_language:
        return None
    return tuple(str(value) for value in values) + (_normalized_language(target_language),)


def _build_return_record(
    primary_record: dict[str, Any],
    primary_key: str,
    source_language: str,
    auto_enroll: bool = False,
    promote_existing: bool = False,
) -> dict[str, Any] | None:
    source = primary_record.get("source") or {}
    destination = primary_record.get("destination") or {}
    target_language = str((primary_record.get("translation") or {}).get("target_language") or "")
    if not source.get("message_id") or not destination.get("message_id") or not target_language:
        return None
    return_key = primary_key + RETURN_THREAD_SUFFIX
    return_source = deepcopy(destination)
    return_source.setdefault("subject", source.get("subject"))
    return_source.setdefault("created_date_time", primary_record.get("reposted_at"))
    return {
        "source_key": return_key,
        "source": return_source,
        "destination": deepcopy(source),
        "translation": {
            "source_language": target_language,
            "target_language": source_language,
        },
        "reposted_at": primary_record.get("reposted_at"),
        "reply_sync": {
            "mapping_key": primary_key,
            "direction": "return",
            "counterpart_thread_key": primary_key,
            "source_language": target_language,
            "auto_enroll": auto_enroll,
            "promote_existing": promote_existing,
            "origin": "reciprocal_repost_history",
        },
    }
