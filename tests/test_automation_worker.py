import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AZURE_TENANT_ID", "tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "client")
os.environ.setdefault("SOURCE_TEAM_ID", "source-team")
os.environ.setdefault("SOURCE_CHANNEL_ID", "19:source@thread.tacv2")
os.environ.setdefault("DESTINATION_TEAM_ID", "dest-team")
os.environ.setdefault("DESTINATION_CHANNEL_ID", "19:dest@thread.tacv2")

import automation_worker  # noqa: E402
import main as app_main  # noqa: E402
from exception_list import ExceptionList  # noqa: E402
from graph_client import GraphAPIError  # noqa: E402
from post_cache import PostCache  # noqa: E402
from repost_history import RepostHistory  # noqa: E402


class FakeGraphContext:
    def __init__(self, graph) -> None:
        self.graph = graph

    async def __aenter__(self):
        return self.graph

    async def __aexit__(self, *_):
        return None


class FakeGraph:
    def __init__(self) -> None:
        self.create_calls = 0
        self.created_destinations = []
        self.get_message_calls = []
        self.list_page_calls = []
        self.fail_create = False
        self.pages_by_team = {
            "source-team": [{"id": "forward-msg", "body": {"contentType": "html", "content": "<p>Hello</p>"}}],
            "dest-team": [{"id": "reverse-msg", "body": {"contentType": "html", "content": "<p>你好</p>"}}],
        }
        self.messages = {}

    async def list_channel_messages_page(self, team_id, channel_id, top, page_url=None):
        self.list_page_calls.append({"team_id": team_id, "channel_id": channel_id, "top": top, "page_url": page_url})
        return {"messages": self.pages_by_team.get(team_id, []), "next_link": None}

    async def get_message(self, team_id, channel_id, message_id, parent_message_id=None):
        self.get_message_calls.append(message_id)
        message = {
            "id": message_id,
            "subject": f"Subject {message_id}",
            "createdDateTime": "2026-06-10T01:02:03Z",
            "webUrl": f"https://teams/source/{message_id}",
            "from": {"user": {"displayName": "Alex", "email": "alex@example.com"}},
            "body": {"contentType": "html", "content": f"<p>Body {message_id}</p>"},
            "attachments": [],
        }
        message.update(self.messages.get(message_id) or {})
        return message

    async def get_message_hosted_contents(self, team_id, channel_id, message_id, parent_message_id=None):
        return []

    async def download_message_hosted_content(self, team_id, channel_id, message_id, hosted_content_id, parent_message_id=None):
        return b"image", "image/png"

    async def create_channel_message(self, team_id, channel_id, payload):
        if self.fail_create:
            raise GraphAPIError(503, "Graph create failed")
        self.create_calls += 1
        self.created_destinations.append({"team_id": team_id, "channel_id": channel_id})
        return {"id": f"new-message-{self.create_calls}", "webUrl": f"https://teams/repost/{self.create_calls}"}


class AutomationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.graph = FakeGraph()
        self.translation_calls = []
        self.original_graph = app_main._graph
        self.original_translate_post = app_main._translate_post
        self.original_acquire_token = automation_worker.acquire_persistent_access_token
        self.original_settings = {
            "source_team_id": app_main.settings.source_team_id,
            "source_channel_id": app_main.settings.source_channel_id,
            "destination_team_id": app_main.settings.destination_team_id,
            "destination_channel_id": app_main.settings.destination_channel_id,
            "repost_history_path": app_main.settings.repost_history_path,
            "post_cache_path": app_main.settings.post_cache_path,
            "exception_list_path": app_main.settings.exception_list_path,
            "reverse_exception_list_path": app_main.settings.reverse_exception_list_path,
            "automation_enabled": app_main.settings.automation_enabled,
            "automation_flows": app_main.settings.automation_flows,
            "automation_max_posts_per_flow": app_main.settings.automation_max_posts_per_flow,
            "automation_lock_path": app_main.settings.automation_lock_path,
            "openai_api_key": app_main.settings.openai_api_key,
            "openai_translation_model": app_main.settings.openai_translation_model,
            "openai_translation_target": app_main.settings.openai_translation_target,
        }
        app_main._graph = lambda token: FakeGraphContext(self.graph)
        app_main._translate_post = self._fake_translate_post
        automation_worker.acquire_persistent_access_token = lambda settings: "token"
        app_main.settings.source_team_id = "source-team"
        app_main.settings.source_channel_id = "19:source@thread.tacv2"
        app_main.settings.destination_team_id = "dest-team"
        app_main.settings.destination_channel_id = "19:dest@thread.tacv2"
        app_main.settings.repost_history_path = Path(self.temp_dir.name) / "history.json"
        app_main.settings.post_cache_path = Path(self.temp_dir.name) / "post-cache.json"
        app_main.settings.exception_list_path = Path(self.temp_dir.name) / "exceptions.json"
        app_main.settings.reverse_exception_list_path = Path(self.temp_dir.name) / "exceptions-reverse.json"
        app_main.settings.automation_enabled = True
        app_main.settings.automation_flows = "forward,reverse"
        app_main.settings.automation_max_posts_per_flow = 10
        app_main.settings.automation_lock_path = Path(self.temp_dir.name) / "automation.lock"
        app_main.settings.openai_api_key = "openai-key"
        app_main.settings.openai_translation_model = "gpt-5.5"
        app_main.settings.openai_translation_target = "zh-Hans"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        app_main._graph = self.original_graph
        app_main._translate_post = self.original_translate_post
        automation_worker.acquire_persistent_access_token = self.original_acquire_token
        for key, value in self.original_settings.items():
            setattr(app_main.settings, key, value)

    async def _fake_translate_post(self, post, target_language, settings):
        self.translation_calls.append({"message_id": post["id"], "target_language": target_language})
        return {
            "subject": f"Translated {post['id']}",
            "body_html": f"<p>Translated {target_language}</p>",
            "body_preview": f"Translated {target_language}",
            "translated_at": "2026-06-10T01:02:03+00:00",
            "model": settings.openai_translation_model,
        }

    def test_automation_disabled_exits_without_graph_or_auth(self) -> None:
        app_main.settings.automation_enabled = False
        automation_worker.acquire_persistent_access_token = lambda settings: self.fail("auth should not be called")

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.graph.list_page_calls, [])
        self.assertEqual(self.graph.create_calls, 0)

    def test_run_once_processes_forward_and_reverse_flows(self) -> None:
        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["status"], "completed")
        self.assertEqual([flow["flow"] for flow in result["flows"]], ["forward", "reverse"])
        self.assertEqual(self.graph.create_calls, 2)
        self.assertEqual(
            self.graph.created_destinations,
            [
                {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2"},
                {"team_id": "source-team", "channel_id": "19:source@thread.tacv2"},
            ],
        )
        self.assertEqual(
            self.translation_calls,
            [
                {"message_id": "forward-msg", "target_language": "zh-Hans"},
                {"message_id": "reverse-msg", "target_language": "en"},
            ],
        )
        self.assertEqual(len(RepostHistory(app_main.settings.repost_history_path).list_records()), 2)

    def test_already_reposted_messages_are_skipped(self) -> None:
        cache = PostCache(app_main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [_cached_post("forward-msg")])
        RepostHistory(app_main.settings.repost_history_path).upsert(
            {
                "source_key": "source-team|19:source@thread.tacv2|forward-msg|translation:zh-Hans",
                "source": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "forward-msg"},
                "destination": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "old"},
                "translation": {"target_language": "zh-Hans"},
            }
        )
        app_main.settings.automation_flows = "forward"

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["already_reposted"], 1)
        self.assertEqual(self.graph.create_calls, 0)

    def test_cached_posts_are_automated_oldest_to_newest(self) -> None:
        cache = PostCache(app_main.settings.post_cache_path)
        cache.upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                _cached_post("newest-msg", "2026-06-10T03:00:00Z"),
                _cached_post("oldest-msg", "2026-06-10T01:00:00Z"),
                _cached_post("middle-msg", "2026-06-10T02:00:00Z"),
            ],
        )
        app_main.settings.automation_flows = "forward"
        app_main.settings.automation_max_posts_per_flow = 2
        self.graph.pages_by_team = {"source-team": []}

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["checked"], 2)
        self.assertEqual(
            self.translation_calls,
            [
                {"message_id": "oldest-msg", "target_language": "zh-Hans"},
                {"message_id": "middle-msg", "target_language": "zh-Hans"},
            ],
        )

    def test_already_reposted_oldest_posts_do_not_consume_automation_limit(self) -> None:
        cache = PostCache(app_main.settings.post_cache_path)
        cache.upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                _cached_post("newest-msg", "2026-06-10T03:00:00Z"),
                _cached_post("oldest-msg", "2026-06-10T01:00:00Z"),
                _cached_post("middle-msg", "2026-06-10T02:00:00Z"),
            ],
        )
        RepostHistory(app_main.settings.repost_history_path).upsert(
            {
                "source_key": "source-team|19:source@thread.tacv2|oldest-msg|translation:zh-Hans",
                "source": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "oldest-msg"},
                "destination": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "old"},
                "translation": {"target_language": "zh-Hans"},
            }
        )
        app_main.settings.automation_flows = "forward"
        app_main.settings.automation_max_posts_per_flow = 2
        self.graph.pages_by_team = {"source-team": []}

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["already_reposted"], 1)
        self.assertEqual(
            self.translation_calls,
            [
                {"message_id": "middle-msg", "target_language": "zh-Hans"},
                {"message_id": "newest-msg", "target_language": "zh-Hans"},
            ],
        )

    def test_cached_posts_with_no_presentable_content_are_not_automated(self) -> None:
        cache = PostCache(app_main.settings.post_cache_path)
        empty_post = {
            **_cached_post("empty-msg"),
            "subject": "Teams message",
            "author": None,
            "body_html": "",
            "body_preview": "",
            "attachments": [],
            "embedded_images": [],
        }
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [empty_post])
        app_main.settings.automation_flows = "forward"
        self.graph.pages_by_team = {"source-team": []}

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["checked"], 0)
        self.assertEqual(self.translation_calls, [])
        self.assertEqual(self.graph.create_calls, 0)

    def test_real_teams_sender_without_email_is_skipped_by_exception_alias(self) -> None:
        app_main.settings.automation_flows = "forward"
        ExceptionList(app_main.settings.exception_list_path).add("laceyl@mmoser.com")
        self.graph.messages["forward-msg"] = {
            "from": {
                "user": {
                    "id": "0ce5a181-9a6c-425e-b6f3-8cd7866be8e6",
                    "displayName": "LaceyLi - M Moser Associates",
                    "userIdentityType": "aadUser",
                }
            }
        }

        result = asyncio.run(automation_worker.run_once())

        flow = result["flows"][0]
        self.assertEqual(flow["refresh"]["posts_skipped_by_exception"], 1)
        self.assertEqual(flow["checked"], 0)
        self.assertEqual(self.translation_calls, [])
        self.assertEqual(self.graph.create_calls, 0)

    def test_legacy_cached_sender_without_email_is_not_automated(self) -> None:
        excluded_post = {
            **_cached_post("forward-msg"),
            "author": "LaceyLi - M Moser Associates",
            "author_email": None,
        }
        PostCache(app_main.settings.post_cache_path).upsert_posts(
            "source-team", "19:source@thread.tacv2", [excluded_post]
        )
        ExceptionList(app_main.settings.exception_list_path).add("laceyl@mmoser.com")
        app_main.settings.automation_flows = "forward"
        self.graph.pages_by_team = {"source-team": []}

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["checked"], 0)
        self.assertEqual(self.translation_calls, [])
        self.assertEqual(self.graph.create_calls, 0)

    def test_repost_failure_does_not_write_success_history(self) -> None:
        cache = PostCache(app_main.settings.post_cache_path)
        post = _cached_post("forward-msg")
        post["translations"] = {"zh-Hans": {"subject": "Saved", "body_html": "<p>Saved</p>", "body_preview": "Saved"}}
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [post])
        app_main.settings.automation_flows = "forward"
        self.graph.pages_by_team = {"source-team": []}
        self.graph.fail_create = True

        result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["flows"][0]["failed"], 1)
        self.assertIsNone(
            RepostHistory(app_main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "forward-msg",
                "zh-Hans",
            )
        )

    def test_active_lock_prevents_overlapping_run(self) -> None:
        automation_worker.acquire_persistent_access_token = lambda settings: self.fail("auth should not be called")

        with automation_worker.AutomationLock(app_main.settings.automation_lock_path):
            result = asyncio.run(automation_worker.run_once())

        self.assertEqual(result["status"], "locked")
        self.assertTrue(app_main.settings.automation_lock_path.exists())

    def test_stale_lock_filename_does_not_prevent_a_new_lock(self) -> None:
        app_main.settings.automation_lock_path.write_text("pid=1\n", encoding="utf-8")

        with automation_worker.AutomationLock(app_main.settings.automation_lock_path):
            self.assertEqual(
                app_main.settings.automation_lock_path.read_text(encoding="utf-8"),
                f"pid={os.getpid()}\n",
            )

        self.assertTrue(app_main.settings.automation_lock_path.exists())


def _cached_post(message_id: str, created_date_time: str = "2026-06-10T01:02:03Z") -> dict:
    return {
        "id": message_id,
        "subject": f"Cached {message_id}",
        "author": "Alex",
        "author_email": "alex@example.com",
        "created_date_time": created_date_time,
        "web_url": f"https://teams/source/{message_id}",
        "body_html": "<p>Cached body</p>",
        "body_preview": "Cached body",
        "attachments": [],
        "embedded_images": [],
        "embedded_images_zip_url": None,
    }


if __name__ == "__main__":
    unittest.main()
