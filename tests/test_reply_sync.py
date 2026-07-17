from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from graph_client import GraphAPIError
from repost_history import RepostHistory
from reply_sync.config import ReplySyncSettings, parse_reply_sync_flows
from reply_sync.graph import ReplyGraph
from reply_sync.locking import ReplySyncAlreadyRunning, ReplySyncLock
from reply_sync.payloads import ReplyFidelityError, build_degraded_reply_payload, build_reply_payload
import reply_sync.router as reply_router_module
import reply_sync.worker as reply_worker_module
from reply_sync.service import ReplySyncService
from reply_sync.stores import ReplyCache, ReturnQueue, ThreadRegistry


def source_reply(
    reply_id: str,
    created: str,
    *,
    body: str | None = None,
    attachments: list[dict] | None = None,
    etag: str | None = None,
    deleted: str | None = None,
) -> dict:
    return {
        "id": reply_id,
        "replyToId": "source-parent",
        "messageType": "message",
        "createdDateTime": created,
        "lastModifiedDateTime": created,
        "deletedDateTime": deleted,
        "etag": etag or reply_id,
        "webUrl": f"https://teams.example/source/{reply_id}",
        "from": {"user": {"displayName": f"Author {reply_id}"}},
        "body": {"contentType": "html", "content": body or f"<p>Body {reply_id}</p>"},
        "attachments": attachments or [],
    }


class FakeReplyGraph:
    def __init__(self, replies: list[dict] | None = None) -> None:
        self.source_replies = replies or []
        self.destination_replies: list[dict] = []
        self.created: list[dict] = []
        self.hosted: dict[str, tuple[bytes, str]] = {}
        self.fail_create = False

    async def list_replies(self, team_id, channel_id, parent_message_id):
        if parent_message_id == "source-parent":
            return list(self.source_replies)
        return list(self.destination_replies)

    async def create_reply(self, team_id, channel_id, parent_message_id, payload):
        if self.fail_create:
            raise GraphAPIError(400, "Graph rejected reply")
        created = {
            "id": f"destination-{len(self.created) + 1}",
            "replyToId": parent_message_id,
            "webUrl": f"https://teams.example/destination/{len(self.created) + 1}",
            "body": payload["body"],
        }
        self.created.append(payload)
        self.destination_replies.append(created)
        return created

    async def get_reply(self, team_id, channel_id, parent_message_id, reply_id):
        for reply in await self.list_replies(team_id, channel_id, parent_message_id):
            if str(reply.get("id")) == str(reply_id):
                return reply
        raise GraphAPIError(404, "Reply not found")

    async def list_hosted_contents(self, team_id, channel_id, parent_message_id, reply_id):
        return [{"id": hosted_id} for hosted_id in self.hosted]

    async def download_hosted_content(self, team_id, channel_id, parent_message_id, reply_id, hosted_content_id):
        return self.hosted[hosted_content_id]

    async def get_root_message(self, team_id, channel_id, message_id):
        return {"id": message_id, "webUrl": f"https://teams.example/destination/{message_id}"}


class PairedReplyGraph(FakeReplyGraph):
    def __init__(self, replies_by_parent: dict[str, list[dict]]) -> None:
        super().__init__()
        self.replies_by_parent = replies_by_parent
        self.created_with_parent: list[tuple[str, dict]] = []

    async def list_replies(self, team_id, channel_id, parent_message_id):
        return list(self.replies_by_parent.get(parent_message_id, []))

    async def create_reply(self, team_id, channel_id, parent_message_id, payload):
        created_number = len(self.created_with_parent) + 1
        created = {
            "id": f"generated-{created_number}",
            "replyToId": parent_message_id,
            "messageType": "message",
            "createdDateTime": f"2026-07-13T00:00:{created_number + 10:02d}Z",
            "lastModifiedDateTime": f"2026-07-13T00:00:{created_number + 10:02d}Z",
            "etag": f"generated-{created_number}",
            "webUrl": f"https://teams.example/generated/{created_number}",
            "from": {"user": {"displayName": "Reply translator"}},
            "body": payload["body"],
            "attachments": payload.get("attachments") or [],
        }
        self.created.append(payload)
        self.created_with_parent.append((parent_message_id, created))
        self.replies_by_parent.setdefault(parent_message_id, []).append(created)
        return created


class ReplySyncServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.reply_settings = ReplySyncSettings(_env_file=None)
        self.reply_settings.enabled = True
        self.reply_settings.registry_path = root / "reply-sync" / "registry.json"
        self.reply_settings.cache_path = root / "reply-sync" / "cache.json"
        self.reply_settings.history_path = root / "reply-sync" / "history.json"
        self.reply_settings.queue_path = root / "reply-sync" / "return-queue.json"
        self.reply_settings.lock_path = root / "reply-sync" / "lock"
        self.reply_settings.temp_folder = root / "reply-sync" / "temp"
        self.reply_settings.stability_scans = 2
        self.repost_history_path = root / "main-history.json"
        self.core_settings = SimpleNamespace(
            repost_history_path=self.repost_history_path,
            openai_translation_target="zh-Hans",
        )
        self.translation_calls: list[str] = []
        self.service = ReplySyncService(self.reply_settings, self.core_settings, self.translate)
        self.thread_key = "source-team|source-channel|source-parent|translation:zh-Hans"
        self._write_mapping()
        self.service.discover()

    async def translate(self, reply, target_language, settings):
        self.translation_calls.append(reply["id"])
        return {
            "subject": "",
            "body_html": reply["body_html"].replace("Body", "Translated"),
            "body_preview": "Translated",
            "translated_at": "2026-07-13T00:00:00Z",
            "model": "fake",
        }

    def allow_next_send(self) -> None:
        self.service.return_queue.record_send((datetime.now(UTC) - timedelta(minutes=11)).isoformat())

    def test_auto_enroll_activates_existing_preview_for_backfill(self) -> None:
        preview = self.service.registry.get(self.thread_key)
        self.assertFalse(preview["enabled"])
        self.assertEqual(preview["status"], "preview")

        self.reply_settings.auto_enroll_new_threads = True
        result = self.service.discover()

        thread = self.service.registry.get(self.thread_key)
        self.assertEqual(result["updated"], 1)
        self.assertTrue(thread["enabled"])
        self.assertEqual(thread["start_mode"], "backfill_all")
        self.assertEqual(thread["status"], "active")
        self.assertEqual(thread["baseline_reply_ids"], [])

    def test_auto_enroll_does_not_reactivate_paused_thread(self) -> None:
        self.service.activate(self.thread_key, "backfill_all")
        self.service.pause(self.thread_key)

        self.reply_settings.auto_enroll_new_threads = True
        self.service.discover()

        thread = self.service.registry.get(self.thread_key)
        self.assertFalse(thread["enabled"])
        self.assertEqual(thread["status"], "paused")

    def test_reciprocal_discovery_swaps_roots_and_starts_as_preview(self) -> None:
        self.reply_settings.return_enabled = True

        result = self.service.discover()

        return_key = self.thread_key + "|reply:return"
        primary = self.service.registry.get(self.thread_key)
        reciprocal = self.service.registry.get(return_key)
        self.assertEqual(result["added"], 1)
        self.assertEqual(primary["counterpart_thread_key"], return_key)
        self.assertEqual(reciprocal["counterpart_thread_key"], self.thread_key)
        self.assertEqual(reciprocal["mapping_key"], self.thread_key)
        self.assertEqual(reciprocal["direction"], "return")
        self.assertEqual(reciprocal["source"]["message_id"], "destination-parent")
        self.assertEqual(reciprocal["destination"]["message_id"], "source-parent")
        self.assertEqual(reciprocal["source_language"], "zh-Hans")
        self.assertEqual(reciprocal["target_language"], "en")
        self.assertFalse(reciprocal["enabled"])
        self.assertEqual(reciprocal["status"], "preview")
        self.assertEqual(
            [thread["direction"] for thread in self.service.registry.list_threads()],
            ["primary", "return"],
        )

    def test_return_auto_enroll_only_applies_when_return_thread_is_created(self) -> None:
        self.reply_settings.return_enabled = True
        self.service.discover()
        existing_return_key = self.thread_key + "|reply:return"

        self.reply_settings.return_auto_enroll_new_threads = True
        self.service.discover()

        existing = self.service.registry.get(existing_return_key)
        self.assertFalse(existing["enabled"])
        self.assertEqual(existing["status"], "preview")

        new_key = "source-team|source-channel|source-parent-2|translation:zh-Hans"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": new_key,
                "source": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "source-parent-2",
                },
                "destination": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": "destination-parent-2",
                },
                "translation": {"source_language": "en", "target_language": "zh-Hans"},
            }
        )
        self.service.discover()

        newly_created = self.service.registry.get(new_key + "|reply:return")
        self.assertTrue(newly_created["enabled"])
        self.assertEqual(newly_created["start_mode"], "backfill_all")
        self.assertEqual(newly_created["status"], "active")

    def test_controlled_backfill_activates_existing_return_previews(self) -> None:
        self.reply_settings.return_enabled = True
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.assertFalse(self.service.registry.get(return_key)["enabled"])

        self.reply_settings.return_backfill_existing_threads = True
        result = self.service.discover()

        reciprocal = self.service.registry.get(return_key)
        self.assertGreaterEqual(result["updated"], 1)
        self.assertTrue(reciprocal["enabled"])
        self.assertEqual(reciprocal["start_mode"], "backfill_all")
        self.assertEqual(reciprocal["status"], "active")

    def test_reverse_mapping_gets_a_chinese_return_thread(self) -> None:
        reverse_key = "destination-team|destination-channel|reverse-source|translation:en"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": reverse_key,
                "source": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": "reverse-source",
                },
                "destination": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "reverse-destination",
                },
                "translation": {"source_language": "zh-Hans", "target_language": "en"},
            }
        )
        self.reply_settings.return_enabled = True

        self.service.discover()

        reciprocal = self.service.registry.get(reverse_key + "|reply:return")
        self.assertEqual(reciprocal["source"]["message_id"], "reverse-destination")
        self.assertEqual(reciprocal["destination"]["message_id"], "reverse-source")
        self.assertEqual(reciprocal["target_language"], "zh-Hans")
        self.assertEqual(reciprocal["flow"], "forward")

    def test_exact_reverse_mapping_is_paired_without_synthetic_threads(self) -> None:
        reverse_key = "destination-team|destination-channel|destination-parent|translation:en"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": reverse_key,
                "source": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": "destination-parent",
                },
                "destination": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "source-parent",
                },
                "translation": {"source_language": "zh-Hans", "target_language": "en"},
            }
        )
        self.reply_settings.return_enabled = True

        self.service.discover()

        threads = self.service.registry.list_threads()
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread["direction"] == "primary" for thread in threads))
        self.assertEqual(self.service.registry.get(self.thread_key)["counterpart_thread_key"], reverse_key)
        self.assertEqual(self.service.registry.get(reverse_key)["counterpart_thread_key"], self.thread_key)

    def test_existing_synthetic_return_is_retired_when_an_exact_reverse_mapping_appears(self) -> None:
        self.reply_settings.return_enabled = True
        self.service.discover()
        synthetic_key = self.thread_key + "|reply:return"
        self.service.activate(synthetic_key, "backfill_all")
        reverse_key = "destination-team|destination-channel|destination-parent|translation:en"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": reverse_key,
                "source": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": "destination-parent",
                },
                "destination": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "source-parent",
                },
                "translation": {"source_language": "zh-Hans", "target_language": "en"},
            }
        )

        result = self.service.discover()

        synthetic = self.service.registry.get(synthetic_key)
        self.assertEqual(result["superseded"], 1)
        self.assertFalse(synthetic["enabled"])
        self.assertEqual(synthetic["status"], "superseded")
        self.assertEqual(synthetic["superseded_by"], reverse_key)
        self.assertEqual(self.service.registry.get(self.thread_key)["counterpart_thread_key"], reverse_key)
        with self.assertRaisesRegex(ValueError, "superseded"):
            self.service.activate(synthetic_key, "backfill_all")

    async def test_return_thread_is_dormant_when_feature_flag_is_disabled(self) -> None:
        self.reply_settings.return_enabled = True
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.reply_settings.return_enabled = False

        with self.assertRaisesRegex(ValueError, "Reciprocal reply synchronization is disabled"):
            self.service.activate(return_key, "backfill_all")
        result = await self.service.run_thread(return_key, FakeReplyGraph())
        self.assertEqual(result["status"], "return_disabled")

    def _write_mapping(self, *, destination_message_id: str | None = "destination-parent", target_language="zh-Hans"):
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": self.thread_key,
                "source": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "source-parent",
                    "web_url": "https://teams.example/source-parent",
                    "subject": "Source post",
                    "created_date_time": "2026-07-13T00:00:00Z",
                },
                "destination": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": destination_message_id,
                    "web_url": "https://teams.example/destination-parent" if destination_message_id else None,
                },
                "translation": {"target_language": target_language},
                "status": "reposted" if destination_message_id else "manually_marked",
                "manual": destination_message_id is None,
            }
        )

    async def test_primary_backlog_sends_oldest_one_per_interval(self) -> None:
        graph = FakeReplyGraph(
            [
                source_reply("3", "2026-07-13T00:00:03Z"),
                source_reply("1", "2026-07-13T00:00:01Z"),
                source_reply("2", "2026-07-13T00:00:02Z"),
            ]
        )
        self.service.activate(self.thread_key, "backfill_all")

        first = await self.service.run_thread(self.thread_key, graph)
        second = await self.service.run_thread(self.thread_key, graph)
        immediate = await self.service.run_thread(self.thread_key, graph)
        self.allow_next_send()
        third = await self.service.run_thread(self.thread_key, graph)
        self.allow_next_send()
        fourth = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(first["status"], "stabilizing")
        self.assertEqual(second["sent"], 1)
        self.assertEqual(immediate["dispatch_status"], "throttled")
        self.assertEqual(immediate["sent"], 0)
        self.assertEqual(third["sent"], 1)
        self.assertEqual(fourth["sent"], 1)
        bodies = [payload["body"]["content"] for payload in graph.created]
        self.assertIn("<strong>原回覆作者：</strong> Author 1", bodies[0])
        self.assertIn('<a href="https://teams.example/source/1">link</a></p><hr>', bodies[0])
        self.assertNotIn("回覆來源", bodies[0])
        self.assertIn("https://teams.example/source/1", bodies[0])
        self.assertIn("https://teams.example/source/2", bodies[1])
        self.assertIn("https://teams.example/source/3", bodies[2])
        self.assertNotIn("reply-sync", "".join(bodies))
        self.assertEqual(self.translation_calls, ["1", "2", "3"])

    async def test_english_thread_uses_compact_english_header(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        self.service.registry.update(self.thread_key, target_language="en")
        self.service.activate(self.thread_key, "backfill_all")

        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(result["sent"], 1)
        body = graph.created[0]["body"]["content"]
        self.assertIn("<strong>Original reply by:</strong> Author 1", body)
        self.assertIn('<a href="https://teams.example/source/1">link</a></p><hr>', body)
        self.assertNotIn("原回覆作者", body)
        self.assertNotIn("Reply source", body)

    async def test_identical_timestamps_use_numeric_message_id_order(self) -> None:
        created = "2026-07-13T00:00:01Z"
        graph = FakeReplyGraph([source_reply("10", created), source_reply("2", created), source_reply("1", created)])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        await self.service.run_thread(self.thread_key, graph)
        self.allow_next_send()
        await self.service.run_thread(self.thread_key, graph)
        self.allow_next_send()
        await self.service.run_thread(self.thread_key, graph)
        bodies = [payload["body"]["content"] for payload in graph.created]
        self.assertIn("https://teams.example/source/1", bodies[0])
        self.assertIn("https://teams.example/source/2", bodies[1])
        self.assertIn("https://teams.example/source/10", bodies[2])

    async def test_stable_prefix_progresses_while_new_reply_is_still_stabilizing(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        graph.source_replies.append(source_reply("2", "2026-07-13T00:00:02Z"))

        second = await self.service.run_thread(self.thread_key, graph)
        third = await self.service.run_thread(self.thread_key, graph)
        self.allow_next_send()
        fourth = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(second["status"], "stabilizing")
        self.assertEqual(second["sent"], 1)
        self.assertEqual(third["dispatch_status"], "throttled")
        self.assertEqual(third["sent"], 0)
        self.assertEqual(fourth["sent"], 1)
        self.assertIn("https://teams.example/source/1", graph.created[0]["body"]["content"])
        self.assertIn("https://teams.example/source/2", graph.created[1]["body"]["content"])

    async def test_unsupported_first_reply_blocks_later_reply(self) -> None:
        graph = FakeReplyGraph(
            [
                source_reply(
                    "1",
                    "2026-07-13T00:00:01Z",
                    attachments=[{"id": "a", "name": "card", "contentType": "adaptive-card"}],
                ),
                source_reply("2", "2026-07-13T00:00:02Z"),
            ]
        )
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_reply_id"], "1")
        self.assertEqual(graph.created, [])
        self.assertIsNone(self.service.history.get(self.thread_key, "2"))

        degraded = await self.service.send_degraded(self.thread_key, "1", graph)
        self.allow_next_send()
        continued = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(continued["sent"], 1)
        self.assertIn("https://teams.example/source/1", graph.created[0]["body"]["content"])
        self.assertIn("https://teams.example/source/2", graph.created[1]["body"]["content"])

    async def test_graph_create_failure_keeps_head_of_line_blocked(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z"), source_reply("2", "2026-07-13T00:00:02Z")])
        graph.fail_create = True
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(result["blocked_reply_id"], "1")
        self.assertIsNone(self.service.history.get(self.thread_key, "1"))
        self.assertIsNone(self.service.history.get(self.thread_key, "2"))

        graph.fail_create = False
        immediate = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(immediate["dispatch_status"], "throttled")
        self.assertEqual(graph.created, [])

        self.allow_next_send()
        retry = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(retry["sent"], 1)

    async def test_existing_destination_marker_recovers_without_duplicate(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        graph.destination_replies.append(
            {
                "id": "already-there",
                "webUrl": "https://teams.example/destination/already-there",
                "body": {"content": "<p>reply-sync-source:1</p>"},
            }
        )
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(graph.created, [])
        self.assertEqual(self.service.history.get(self.thread_key, "1")["destination_reply_id"], "already-there")

    async def test_generated_reply_headers_are_skipped_in_both_languages(self) -> None:
        graph = FakeReplyGraph(
            [
                source_reply(
                    "1",
                    "2026-07-13T00:00:01Z",
                    body="<p><strong>Reply source:</strong> Original reply</p>",
                ),
                source_reply(
                    "2",
                    "2026-07-13T00:00:02Z",
                    body="<p><strong>回覆來源：</strong> 原回覆</p>",
                ),
                source_reply(
                    "3",
                    "2026-07-13T00:00:03Z",
                    body="<p><strong>Original reply by:</strong> Generated author · link</p><hr><p>Body</p>",
                ),
                source_reply(
                    "4",
                    "2026-07-13T00:00:04Z",
                    body="<p><strong>原回覆作者：</strong> Generated author · link</p><hr><p>Body</p>",
                ),
                source_reply("5", "2026-07-13T00:00:05Z"),
            ]
        )
        self.service.activate(self.thread_key, "backfill_all")

        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.translation_calls, ["5"])
        self.assertIn("https://teams.example/source/5", graph.created[0]["body"]["content"])

    async def test_paired_threads_sync_human_replies_both_ways_without_ping_pong(self) -> None:
        self.reply_settings.return_enabled = True
        self.reply_settings.stability_scans = 1
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.service.activate(self.thread_key, "backfill_all")
        self.service.activate(return_key, "backfill_all")
        graph = PairedReplyGraph(
            {
                "source-parent": [source_reply("original-human", "2026-07-13T00:00:01Z")],
                "destination-parent": [
                    source_reply(
                        "translated-human",
                        "2026-07-13T00:00:02Z",
                        body="<p>Translated-side human reply</p>",
                    )
                ],
            }
        )

        primary_result = await self.service.run_thread(self.thread_key, graph)
        return_throttled = await self.service.run_thread(return_key, graph)
        self.allow_next_send()
        return_result = await self.service.run_thread(return_key, graph)
        primary_repeat = await self.service.run_thread(self.thread_key, graph)
        return_repeat = await self.service.run_thread(return_key, graph)

        self.assertEqual(primary_result["sent"], 1)
        self.assertEqual(return_throttled["dispatch_status"], "throttled")
        self.assertEqual(return_throttled["sent"], 0)
        self.assertEqual(return_result["sent"], 1)
        self.assertEqual(primary_repeat["sent"], 0)
        self.assertEqual(return_repeat["sent"], 0)
        self.assertEqual([parent for parent, _ in graph.created_with_parent], ["destination-parent", "source-parent"])
        self.assertIn("<strong>Original reply by:</strong>", graph.created[1]["body"]["content"])
        self.assertEqual(self.translation_calls, ["original-human", "translated-human"])
        self.assertEqual(len(graph.created), 2)

    async def test_return_backlog_is_translated_and_dispatched_one_per_interval(self) -> None:
        self.reply_settings.return_enabled = True
        self.reply_settings.stability_scans = 1
        self.reply_settings.send_interval_minutes = 10
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.service.activate(return_key, "backfill_all")
        graph = PairedReplyGraph(
            {
                "source-parent": [],
                "destination-parent": [
                    source_reply("return-1", "2026-07-13T00:00:01Z"),
                    source_reply("return-2", "2026-07-13T00:00:02Z"),
                    source_reply("return-3", "2026-07-13T00:00:03Z"),
                ],
            }
        )

        first = await self.service.run_thread(return_key, graph)
        immediate = await self.service.run_thread(return_key, graph)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["dispatch_status"], "sent")
        self.assertEqual(immediate["sent"], 0)
        self.assertEqual(immediate["dispatch_status"], "throttled")
        self.assertEqual(self.translation_calls, ["return-1"])
        self.assertEqual(len(graph.created), 1)
        self.assertEqual(self.service.return_queue.summary()["counts"]["collected"], 2)

        self.service.return_queue.record_send((datetime.now(UTC) - timedelta(minutes=11)).isoformat())
        second = await self.service.run_thread(return_key, graph)
        self.service.return_queue.record_send((datetime.now(UTC) - timedelta(minutes=11)).isoformat())
        third = await self.service.run_thread(return_key, graph)

        self.assertEqual(second["sent"], 1)
        self.assertEqual(third["sent"], 1)
        self.assertEqual(self.translation_calls, ["return-1", "return-2", "return-3"])
        self.assertEqual([parent for parent, _ in graph.created_with_parent], ["source-parent"] * 3)
        bodies = [payload["body"]["content"] for payload in graph.created]
        self.assertIn("/return-1", bodies[0])
        self.assertIn("/return-2", bodies[1])
        self.assertIn("/return-3", bodies[2])

    async def test_return_throttle_survives_service_recreation(self) -> None:
        self.reply_settings.return_enabled = True
        self.reply_settings.stability_scans = 1
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.service.activate(return_key, "backfill_all")
        graph = PairedReplyGraph(
            {
                "source-parent": [],
                "destination-parent": [
                    source_reply("return-1", "2026-07-13T00:00:01Z"),
                    source_reply("return-2", "2026-07-13T00:00:02Z"),
                ],
            }
        )
        await self.service.run_thread(return_key, graph)

        restarted = ReplySyncService(self.reply_settings, self.core_settings, self.translate)
        result = await restarted.run_thread(return_key, graph)

        self.assertEqual(result["dispatch_status"], "throttled")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(graph.created), 1)

    async def test_run_all_collects_multiple_return_threads_but_sends_only_one(self) -> None:
        second_key = "source-team|source-channel|source-parent-2|translation:zh-Hans"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": second_key,
                "source": {
                    "team_id": "source-team",
                    "channel_id": "source-channel",
                    "message_id": "source-parent-2",
                },
                "destination": {
                    "team_id": "destination-team",
                    "channel_id": "destination-channel",
                    "message_id": "destination-parent-2",
                },
                "translation": {"source_language": "en", "target_language": "zh-Hans"},
            }
        )
        self.reply_settings.return_enabled = True
        self.reply_settings.return_backfill_existing_threads = True
        self.reply_settings.stability_scans = 1
        graph = PairedReplyGraph(
            {
                "source-parent": [],
                "source-parent-2": [],
                "destination-parent": [source_reply("return-1", "2026-07-13T00:00:01Z")],
                "destination-parent-2": [source_reply("return-2", "2026-07-13T00:00:02Z")],
            }
        )

        first = await self.service.run_all(graph)
        immediate = await self.service.run_all(graph)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(immediate["sent"], 0)
        self.assertEqual(self.translation_calls, ["return-1"])
        self.assertEqual(len(graph.created), 1)
        self.assertIn("/return-1", graph.created[0]["body"]["content"])
        self.assertEqual(self.service.return_queue.summary()["counts"]["collected"], 1)

    async def test_run_all_dispatches_before_and_during_full_thread_scan(self) -> None:
        events: list[str] = []
        self.service.discover = lambda: {"added": 0, "updated": 0, "unlinked": 0, "total": 2}
        self.service.return_queue.dispatchable_heads = lambda: [{"queue_key": "queued-at-start"}]
        self.service.registry.list_threads = lambda: [
            {"thread_key": "thread-1", "enabled": True},
            {"thread_key": "thread-2", "enabled": True},
        ]
        self.service._direction_enabled = lambda thread: True

        async def collect(thread_key: str, graph: FakeReplyGraph) -> dict[str, object]:
            events.append(f"collect:{thread_key}")
            return {"thread_key": thread_key, "status": "collected", "sent": 0, "recovered": 0}

        dispatch_count = 0

        async def dispatch(
            graph: FakeReplyGraph,
            allowed_queue_keys: set[str] | None = None,
        ) -> dict[str, object]:
            nonlocal dispatch_count
            dispatch_count += 1
            events.append("dispatch")
            if dispatch_count in {1, 3}:
                return {"status": "sent", "sent": 1, "recovered": 0}
            return {"status": "throttled", "sent": 0, "recovered": 0}

        self.service._collect_thread = collect
        self.service._dispatch_reply = dispatch

        result = await self.service.run_all(FakeReplyGraph())

        self.assertEqual(
            events,
            [
                "dispatch",
                "collect:thread-1",
                "dispatch",
                "collect:thread-2",
                "dispatch",
                "dispatch",
            ],
        )
        self.assertEqual(result["sent"], 2)

    async def test_dispatch_defers_a_source_reply_edited_after_collection(self) -> None:
        self.reply_settings.stability_scans = 1
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service._collect_thread(self.thread_key, graph)
        graph.source_replies[0]["etag"] = "edited-after-collection"

        result = await self.service._dispatch_reply(graph)

        self.assertEqual(result["status"], "queue_empty")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(graph.created), 0)
        queued = self.service.return_queue.get(self.thread_key, "1")
        self.assertEqual(queued["status"], "collected")
        self.assertEqual(queued["source_etag"], "edited-after-collection")

    async def test_return_failure_does_not_pause_primary_thread(self) -> None:
        self.reply_settings.return_enabled = True
        self.reply_settings.stability_scans = 1
        self.service.discover()
        return_key = self.thread_key + "|reply:return"
        self.service.activate(self.thread_key, "backfill_all")
        self.service.activate(return_key, "backfill_all")
        graph = FakeReplyGraph()
        graph.destination_replies.append(
            source_reply(
                "translated-human",
                "2026-07-13T00:00:02Z",
                body="<p>Translated-side human reply</p>",
            )
        )
        graph.fail_create = True

        result = await self.service.run_thread(return_key, graph)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_reply_id"], "translated-human")
        primary = self.service.registry.get(self.thread_key)
        self.assertTrue(primary["enabled"])
        self.assertEqual(primary["status"], "active")

    async def test_late_earlier_reply_pauses_with_sequence_conflict(self) -> None:
        graph = FakeReplyGraph([source_reply("2", "2026-07-13T00:00:02Z")])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        await self.service.run_thread(self.thread_key, graph)
        graph.source_replies.insert(0, source_reply("1", "2026-07-13T00:00:01Z"))

        result = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(result["status"], "sequence_conflict")
        self.assertFalse(self.service.registry.get(self.thread_key)["enabled"])
        self.assertEqual(len(graph.created), 1)

    async def test_future_only_baselines_existing_then_sends_new_reply(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        self.service.activate(self.thread_key, "future_only")
        baseline = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(baseline["status"], "baselined")
        await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(graph.created, [])

        graph.source_replies.append(source_reply("2", "2026-07-13T00:00:02Z"))
        first = await self.service.run_thread(self.thread_key, graph)
        second = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(first["status"], "stabilizing")
        self.assertEqual(second["sent"], 1)
        self.assertIn("https://teams.example/source/2", graph.created[0]["body"]["content"])

    async def test_synced_edit_is_reported_as_drift_without_new_reply(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z", etag="v1")])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        await self.service.run_thread(self.thread_key, graph)
        graph.source_replies[0] = source_reply("1", "2026-07-13T00:00:01Z", etag="v2", body="<p>Edited</p>")
        await self.service.run_thread(self.thread_key, graph)
        cached = self.service.cache.get_thread(self.thread_key)["replies"]["1"]
        self.assertEqual(cached["drift"], "edited_after_sync")
        self.assertEqual(len(graph.created), 1)

    async def test_manual_record_can_be_linked_without_editing_main_history(self) -> None:
        other_key = "source-team|source-channel|manual-parent|translation:zh-Hans"
        RepostHistory(self.repost_history_path).upsert(
            {
                "source_key": other_key,
                "source": {"team_id": "source-team", "channel_id": "source-channel", "message_id": "manual-parent"},
                "destination": {"team_id": "destination-team", "channel_id": "destination-channel", "message_id": None},
                "translation": {"target_language": "zh-Hans"},
                "status": "manually_marked",
            }
        )
        self.service.discover()
        self.reply_settings.return_enabled = True
        graph = FakeReplyGraph()
        linked = await self.service.link_destination(
            other_key,
            "https://teams.microsoft.com/l/message/destination-channel/destination-manual"
            "?groupId=destination-team&tenantId=tenant-1",
            graph,
        )
        self.assertEqual(linked["destination"]["message_id"], "destination-manual")
        main_record = RepostHistory(self.repost_history_path).get(
            "source-team", "source-channel", "manual-parent", "zh-Hans"
        )
        self.assertIsNone(main_record["destination"]["message_id"])
        reciprocal = self.service.registry.get(other_key + "|reply:return")
        self.assertEqual(reciprocal["source"]["message_id"], "destination-manual")
        self.assertEqual(reciprocal["destination"]["message_id"], "manual-parent")
        self.assertFalse(reciprocal["enabled"])


class PayloadTests(unittest.TestCase):
    def test_explicit_english_target_uses_compact_english_header(self) -> None:
        reply = {
            "id": "1",
            "author": "Alex",
            "web_url": "https://teams.example/source/1",
            "target_language": "zh-Hans",
            "body_html": "<p>Hello</p>",
            "attachments": [],
        }

        payload, _ = build_reply_payload(reply, {"body_html": "<p>Hello</p>"}, {}, target_language="en")

        self.assertIn("<strong>Original reply by:</strong> Alex", payload["body"]["content"])
        self.assertIn('<a href="https://teams.example/source/1">link</a></p><hr>', payload["body"]["content"])
        self.assertNotIn("原回覆作者", payload["body"]["content"])
        self.assertNotIn("Reply source", payload["body"]["content"])
        self.assertNotIn("reply-sync", payload["body"]["content"])

    def test_missing_target_language_does_not_fall_back_to_chinese_header(self) -> None:
        reply = {
            "id": "1",
            "author": "Alex",
            "web_url": "https://teams.example/source/1",
            "body_html": "<p>Hello</p>",
            "attachments": [],
        }

        with self.assertRaisesRegex(ReplyFidelityError, "target language is missing"):
            build_reply_payload(reply, {"body_html": "<p>Hello</p>"}, {})

    def test_builds_reply_with_hosted_image_and_reference_attachment(self) -> None:
        reply = {
            "id": "1",
            "author": "Alex",
            "web_url": "https://teams.example/source/1",
            "target_language": "zh-Hans",
            "body_html": '<p>Hello<img src="../hostedContents/image-1/$value"></p>',
            "attachments": [
                {"name": "file.docx", "content_type": "reference", "content_url": "https://sharepoint/file.docx"}
            ],
        }
        translation = {"body_html": '<p>你好<img src="../hostedContents/image-1/$value"></p>'}
        payload, fidelity = build_reply_payload(reply, translation, {"image-1": (b"png", "image/png")})
        self.assertEqual(len(payload["hostedContents"]), 1)
        self.assertEqual(len(payload["attachments"]), 1)
        self.assertIn('../hostedContents/1/$value', payload["body"]["content"])
        self.assertIn("<strong>原回覆作者：</strong> Alex", payload["body"]["content"])
        self.assertIn('<a href="https://teams.example/source/1">link</a></p><hr>', payload["body"]["content"])
        self.assertNotIn("Original reply by", payload["body"]["content"])
        self.assertNotIn("回覆來源", payload["body"]["content"])
        self.assertNotIn("reply-sync", payload["body"]["content"])
        self.assertFalse(fidelity["degraded"])

    def test_unsupported_inline_type_blocks_full_fidelity(self) -> None:
        reply = {
            "id": "1",
            "body_html": '<p><img src="../hostedContents/image-1/$value"></p>',
            "attachments": [],
        }
        with self.assertRaises(ReplyFidelityError):
            build_reply_payload(reply, {"body_html": reply["body_html"]}, {"image-1": (b"gif", "image/gif")})

    def test_degraded_payload_contains_source_links_without_native_media(self) -> None:
        reply = {
            "id": "1",
            "web_url": "https://teams.example/source/1",
            "target_language": "zh-Hans",
            "body_html": '<p><img src="../hostedContents/image-1/$value"></p>',
            "attachments": [{"name": "file", "content_type": "card", "content_url": None}],
        }
        payload, fidelity = build_degraded_reply_payload(reply, {"body_html": reply["body_html"]})
        self.assertNotIn("hostedContents", payload)
        self.assertNotIn("attachments", payload)
        self.assertIn("https://teams.example/source/1", payload["body"]["content"])
        self.assertTrue(fidelity["degraded"])


class StoreAndGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_return_queue_preserves_per_thread_heads_and_send_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = ReturnQueue(Path(folder) / "return-queue.json")
            queue.upsert_many(
                [
                    {
                        "thread_key": "thread-a",
                        "source_reply_id": "a-2",
                        "source_created_date_time": "2026-07-13T00:00:01Z",
                        "sequence": 1,
                        "status": "ready",
                    },
                    {
                        "thread_key": "thread-a",
                        "source_reply_id": "a-1",
                        "source_created_date_time": "2026-07-13T00:00:02Z",
                        "sequence": 0,
                        "status": "blocked",
                    },
                    {
                        "thread_key": "thread-b",
                        "source_reply_id": "b-1",
                        "source_created_date_time": "2026-07-13T00:00:03Z",
                        "sequence": 0,
                        "status": "ready",
                    },
                ]
            )

            heads = queue.dispatchable_heads()
            sent_at = queue.record_send("2026-07-13T00:10:00+00:00")

            self.assertEqual([item["source_reply_id"] for item in heads], ["b-1"])
            self.assertEqual(sent_at, "2026-07-13T00:10:00+00:00")
            self.assertEqual(ReturnQueue(Path(folder) / "return-queue.json").last_sent_at(), sent_at)

    async def test_reply_graph_follows_all_pages(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def get_json(self, path, params=None):
                self.calls.append((path, params))
                if len(self.calls) == 1:
                    return {"value": [{"id": "2"}], "@odata.nextLink": "https://graph.example/page-2"}
                return {"value": [{"id": "1"}]}

        client = FakeClient()
        replies = await ReplyGraph(client).list_replies("team", "channel", "parent")
        self.assertEqual([reply["id"] for reply in replies], ["2", "1"])
        self.assertEqual(client.calls[0][1], {"$top": "50"})
        self.assertIsNone(client.calls[1][1])

    async def test_reply_graph_rejects_repeated_next_link(self) -> None:
        class FakeClient:
            async def get_json(self, path, params=None):
                return {"value": [], "@odata.nextLink": path}

        with self.assertRaises(ValueError):
            await ReplyGraph(FakeClient()).list_replies("team", "channel", "parent")

    async def test_corrupt_registry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "registry.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ValueError):
                ThreadRegistry(path).list_threads()

    async def test_lock_is_separate_and_prevents_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reply-sync.lock"
            with ReplySyncLock(path):
                with self.assertRaises(ReplySyncAlreadyRunning):
                    with ReplySyncLock(path):
                        pass
            self.assertTrue(path.exists())
            with ReplySyncLock(path):
                pass

    async def test_cache_does_not_persist_raw_image_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = ReplyCache(Path(folder) / "cache.json")
            cache.record_scan(
                "thread",
                [
                    {
                        "id": "1",
                        "etag": "v1",
                        "hosted_content_refs": [{"occurrence": 1, "hosted_content_id": "image-1"}],
                    }
                ],
            )
            raw = (Path(folder) / "cache.json").read_text(encoding="utf-8")
            self.assertNotIn("contentBytes", raw)
            self.assertNotIn("cG5n", raw)


class ReplySyncConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_and_isolated(self) -> None:
        settings = ReplySyncSettings(_env_file=None)
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.return_enabled)
        self.assertFalse(settings.return_auto_enroll_new_threads)
        self.assertFalse(settings.return_backfill_existing_threads)
        self.assertEqual(settings.send_interval_minutes, 1)
        self.assertEqual(settings.queue_path, Path(".data/reply-sync/return-queue.json"))
        self.assertEqual(settings.registry_path, Path(".data/reply-sync/thread-registry.json"))
        self.assertEqual(settings.flow_list, ["forward", "reverse"])

    def test_legacy_return_queue_settings_remain_compatible(self) -> None:
        settings = ReplySyncSettings(
            _env_file=None,
            REPLY_SYNC_RETURN_SEND_INTERVAL_MINUTES=12,
            REPLY_SYNC_RETURN_QUEUE_PATH="legacy-return-queue.json",
        )
        self.assertEqual(settings.send_interval_minutes, 12)
        self.assertEqual(settings.queue_path, Path("legacy-return-queue.json"))

    def test_rejects_unknown_flow(self) -> None:
        with self.assertRaises(ValueError):
            parse_reply_sync_flows("forward,sideways")


class ReplySyncWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_worker_stops_before_authentication_or_state_access(self) -> None:
        original_reply_settings = reply_worker_module.get_reply_sync_settings
        original_core_settings = reply_worker_module.get_settings
        original_token = reply_worker_module.acquire_persistent_access_token
        reply_worker_module.get_reply_sync_settings = lambda: ReplySyncSettings(_env_file=None)
        reply_worker_module.get_settings = lambda: self.fail("core settings should not be loaded")
        reply_worker_module.acquire_persistent_access_token = lambda settings: self.fail(
            "authentication should not be attempted"
        )
        try:
            result = await reply_worker_module.run_once()
        finally:
            reply_worker_module.get_reply_sync_settings = original_reply_settings
            reply_worker_module.get_settings = original_core_settings
            reply_worker_module.acquire_persistent_access_token = original_token

        self.assertEqual(result, {"enabled": False, "status": "disabled", "threads": []})


class ReplySyncRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.reply_settings = ReplySyncSettings(_env_file=None)
        self.reply_settings.registry_path = root / "registry.json"
        self.reply_settings.cache_path = root / "cache.json"
        self.reply_settings.history_path = root / "history.json"
        self.reply_settings.queue_path = root / "return-queue.json"
        self.reply_settings.lock_path = root / "lock"
        self.reply_settings.temp_folder = root / "temp"
        self.core_settings = SimpleNamespace(
            repost_history_path=root / "main-history.json",
            graph_base_url="https://graph.microsoft.com/v1.0",
            graph_request_timeout_seconds=1,
            graph_max_retries=0,
        )
        self.original_settings = reply_router_module.get_reply_sync_settings
        self.original_token = reply_router_module.get_access_token
        reply_router_module.get_reply_sync_settings = lambda: self.reply_settings
        self.app = FastAPI()
        self.app.include_router(reply_router_module.create_reply_sync_router(self.core_settings))
        self.client = TestClient(self.app)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        reply_router_module.get_reply_sync_settings = self.original_settings
        reply_router_module.get_access_token = self.original_token

    def test_frontend_is_served_without_touching_state(self) -> None:
        response = self.client.get("/reply-sync")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Translated Reply Sync", response.text)
        self.assertFalse(self.reply_settings.registry_path.exists())

    def test_api_requires_existing_authentication(self) -> None:
        def unauthenticated(request, settings):
            raise HTTPException(status_code=401, detail="Not signed in")

        reply_router_module.get_access_token = unauthenticated
        response = self.client.get("/api/reply-sync/threads")
        self.assertEqual(response.status_code, 401)

    def test_sending_stays_blocked_while_module_disabled(self) -> None:
        reply_router_module.get_access_token = lambda request, settings: "token"
        response = self.client.post("/api/reply-sync/threads/missing/run")
        self.assertEqual(response.status_code, 409)
        self.assertIn("REPLY_SYNC_ENABLED", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
