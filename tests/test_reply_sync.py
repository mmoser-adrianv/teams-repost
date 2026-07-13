from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
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
from reply_sync.stores import ReplyCache, ThreadRegistry


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

    async def list_hosted_contents(self, team_id, channel_id, parent_message_id, reply_id):
        return [{"id": hosted_id} for hosted_id in self.hosted]

    async def download_hosted_content(self, team_id, channel_id, parent_message_id, reply_id, hosted_content_id):
        return self.hosted[hosted_content_id]

    async def get_root_message(self, team_id, channel_id, message_id):
        return {"id": message_id, "webUrl": f"https://teams.example/destination/{message_id}"}


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
        self.reply_settings.lock_path = root / "reply-sync" / "lock"
        self.reply_settings.temp_folder = root / "reply-sync" / "temp"
        self.reply_settings.stability_scans = 2
        self.reply_settings.max_replies_per_run = 50
        self.repost_history_path = root / "main-history.json"
        self.core_settings = SimpleNamespace(repost_history_path=self.repost_history_path)
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

    async def test_two_stable_scans_then_sends_oldest_first(self) -> None:
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

        self.assertEqual(first["status"], "stabilizing")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["sent"], 3)
        bodies = [payload["body"]["content"] for payload in graph.created]
        self.assertIn("reply-sync-source:1", bodies[0])
        self.assertIn("reply-sync-source:2", bodies[1])
        self.assertIn("reply-sync-source:3", bodies[2])
        self.assertEqual(self.translation_calls, ["1", "2", "3"])

    async def test_identical_timestamps_use_numeric_message_id_order(self) -> None:
        created = "2026-07-13T00:00:01Z"
        graph = FakeReplyGraph([source_reply("10", created), source_reply("2", created), source_reply("1", created)])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        await self.service.run_thread(self.thread_key, graph)
        bodies = [payload["body"]["content"] for payload in graph.created]
        self.assertIn("reply-sync-source:1", bodies[0])
        self.assertIn("reply-sync-source:2", bodies[1])
        self.assertIn("reply-sync-source:10", bodies[2])

    async def test_stable_prefix_progresses_while_new_reply_is_still_stabilizing(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z")])
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        graph.source_replies.append(source_reply("2", "2026-07-13T00:00:02Z"))

        second = await self.service.run_thread(self.thread_key, graph)
        third = await self.service.run_thread(self.thread_key, graph)

        self.assertEqual(second["status"], "stabilizing")
        self.assertEqual(second["sent"], 1)
        self.assertEqual(third["sent"], 1)
        self.assertIn("reply-sync-source:1", graph.created[0]["body"]["content"])
        self.assertIn("reply-sync-source:2", graph.created[1]["body"]["content"])

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
        continued = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(continued["sent"], 1)
        self.assertIn("reply-sync-source:1", graph.created[0]["body"]["content"])
        self.assertIn("reply-sync-source:2", graph.created[1]["body"]["content"])

    async def test_graph_create_failure_keeps_head_of_line_blocked(self) -> None:
        graph = FakeReplyGraph([source_reply("1", "2026-07-13T00:00:01Z"), source_reply("2", "2026-07-13T00:00:02Z")])
        graph.fail_create = True
        self.service.activate(self.thread_key, "backfill_all")
        await self.service.run_thread(self.thread_key, graph)
        result = await self.service.run_thread(self.thread_key, graph)
        self.assertEqual(result["blocked_reply_id"], "1")
        self.assertIsNone(self.service.history.get(self.thread_key, "1"))
        self.assertIsNone(self.service.history.get(self.thread_key, "2"))

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
        self.assertIn("reply-sync-source:2", graph.created[0]["body"]["content"])

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


class PayloadTests(unittest.TestCase):
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
        self.assertIn("reply-sync-source:1", payload["body"]["content"])
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
            "body_html": '<p><img src="../hostedContents/image-1/$value"></p>',
            "attachments": [{"name": "file", "content_type": "card", "content_url": None}],
        }
        payload, fidelity = build_degraded_reply_payload(reply, {"body_html": reply["body_html"]})
        self.assertNotIn("hostedContents", payload)
        self.assertNotIn("attachments", payload)
        self.assertIn("https://teams.example/source/1", payload["body"]["content"])
        self.assertTrue(fidelity["degraded"])


class StoreAndGraphTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertFalse(path.exists())

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
        self.assertEqual(settings.registry_path, Path(".data/reply-sync/thread-registry.json"))
        self.assertEqual(settings.flow_list, ["forward", "reverse"])

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
