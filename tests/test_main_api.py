import os
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("AZURE_TENANT_ID", "tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "client")
os.environ.setdefault("SOURCE_TEAM_ID", "source-team")
os.environ.setdefault("SOURCE_CHANNEL_ID", "19:source@thread.tacv2")
os.environ.setdefault("DESTINATION_TEAM_ID", "dest-team")
os.environ.setdefault("DESTINATION_CHANNEL_ID", "19:dest@thread.tacv2")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
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
        self.created_payloads = []
        self.get_message_calls = []
        self.list_page_calls = []
        self.fail_list = False
        self.fail_create_with_attachments = False
        self.pages = [
            {
                "messages": [
                    {
                        "id": "msg-1",
                        "subject": "Source subject",
                        "body": {"contentType": "html", "content": "<p>Truncated list body</p>"},
                    }
                ],
                "next_link": None,
            }
        ]

    async def list_channel_messages(self, team_id, channel_id, top):
        return [
            {
                "id": "msg-1",
                "subject": "Source subject",
                "body": {"contentType": "html", "content": "<p>Truncated list body</p>"},
            }
        ]

    async def list_channel_messages_page(self, team_id, channel_id, top, page_url=None):
        if self.fail_list:
            raise GraphAPIError(503, "Graph unavailable")
        self.list_page_calls.append({"team_id": team_id, "channel_id": channel_id, "top": top, "page_url": page_url})
        page_index = min(len(self.list_page_calls) - 1, len(self.pages) - 1)
        return self.pages[page_index]

    async def get_channel_messages(self, team_id, channel_id, message_ids):
        return [await self.get_message(team_id, channel_id, message_id) for message_id in message_ids]

    async def get_message(self, team_id, channel_id, message_id, parent_message_id=None):
        self.get_message_calls.append(message_id)
        return self._message(message_id)

    async def get_message_hosted_contents(self, team_id, channel_id, message_id, parent_message_id=None):
        return [{"id": "image-1"}]

    async def download_message_hosted_content(self, team_id, channel_id, message_id, hosted_content_id, parent_message_id=None):
        return b"image-bytes", "image/png"

    async def create_channel_message(self, team_id, channel_id, payload):
        self.create_calls += 1
        self.created_payloads.append(payload)
        if self.fail_create_with_attachments and payload.get("attachments"):
            raise GraphAPIError(400, "bad attachment cards")
        return {"id": "new-message", "webUrl": "https://teams/repost"}

    def _message(self, message_id):
        created_times = {
            "msg-new": "2026-06-05T01:02:03Z",
            "msg-3": "2026-06-03T01:02:03Z",
            "msg-2": "2026-06-02T01:02:03Z",
            "msg-1": "2026-06-01T01:02:03Z",
        }
        return {
            "id": message_id,
            "subject": f"Source subject {message_id}",
            "createdDateTime": created_times.get(message_id, "2026-06-04T01:02:03Z"),
            "webUrl": f"https://teams/source/{message_id}",
            "from": {"user": {"displayName": "Alex", "email": "alex@example.com"}},
            "body": {
                "contentType": "html",
                "content": (
                    '<p><strong>Hello formatted full body</strong> '
                    '<img width="320" src="../hostedContents/image-1/$value"></p>'
                    '<attachment id="file-1"></attachment>'
                ),
            },
            "attachments": [
                {
                    "id": "file-1",
                    "name": "source.docx",
                    "contentType": "reference",
                    "contentUrl": "https://contoso.sharepoint.com/source.docx",
                }
            ],
        }


class MainApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_get_access_token = main.get_access_token
        self.original_complete_login_flow = main.complete_login_flow
        self.original_graph = main._graph
        self.original_history_path = main.settings.repost_history_path
        self.original_post_cache_path = main.settings.post_cache_path
        self.original_exception_list_path = main.settings.exception_list_path
        self.original_graph_base_url = main.settings.graph_base_url
        self.original_post_list_limit = main.settings.post_list_limit
        self.original_post_cache_max_refresh_pages = main.settings.post_cache_max_refresh_pages
        self.original_openai_api_key = main.settings.openai_api_key
        self.original_openai_translation_model = main.settings.openai_translation_model
        self.original_openai_translation_target = main.settings.openai_translation_target
        self.original_translate_post = main._translate_post
        self.graph = FakeGraph()
        self.translation_calls = []
        main.get_access_token = lambda request, settings: "token"
        main.complete_login_flow = lambda request, settings: {"access_token": "token"}
        main._graph = lambda token: FakeGraphContext(self.graph)
        main._translate_post = self._fake_translate_post
        main.settings.source_team_id = "source-team"
        main.settings.source_channel_id = "19:source@thread.tacv2"
        main.settings.destination_team_id = "dest-team"
        main.settings.destination_channel_id = "19:dest@thread.tacv2"
        main.settings.repost_history_path = Path(self.temp_dir.name) / "history.json"
        main.settings.post_cache_path = Path(self.temp_dir.name) / "post-cache.json"
        main.settings.exception_list_path = Path(self.temp_dir.name) / "exception-list.json"
        main.settings.graph_base_url = "https://graph.microsoft.com/v1.0"
        main.settings.post_list_limit = 25
        main.settings.post_cache_max_refresh_pages = 3
        main.settings.openai_api_key = "openai-key"
        main.settings.openai_translation_model = "gpt-5.5"
        main.settings.openai_translation_target = "zh-Hans"
        self.addCleanup(self._restore_main)

    def _restore_main(self) -> None:
        main.get_access_token = self.original_get_access_token
        main.complete_login_flow = self.original_complete_login_flow
        main._graph = self.original_graph
        main.settings.repost_history_path = self.original_history_path
        main.settings.post_cache_path = self.original_post_cache_path
        main.settings.exception_list_path = self.original_exception_list_path
        main.settings.graph_base_url = self.original_graph_base_url
        main.settings.post_list_limit = self.original_post_list_limit
        main.settings.post_cache_max_refresh_pages = self.original_post_cache_max_refresh_pages
        main.settings.openai_api_key = self.original_openai_api_key
        main.settings.openai_translation_model = self.original_openai_translation_model
        main.settings.openai_translation_target = self.original_openai_translation_target
        main._translate_post = self.original_translate_post

    async def _fake_translate_post(self, post, target_language, settings):
        self.translation_calls.append({"post": post, "target_language": target_language, "model": settings.openai_translation_model})
        return {
            "subject": f"Translated {post['id']}",
            "body_html": "<p>Ni hao</p>",
            "body_preview": "Ni hao",
            "translated_at": "2026-06-08T01:02:03+00:00",
            "model": settings.openai_translation_model,
        }

    def _cached_post(self, message_id: str, created_date_time: str) -> dict:
        return {
            "id": message_id,
            "subject": f"Cached {message_id}",
            "author": "Alex",
            "created_date_time": created_date_time,
            "web_url": f"https://teams/source/{message_id}",
            "body_html": "<p>Cached body</p>",
            "body_preview": "Cached body",
            "attachments": [],
            "embedded_images": [],
            "embedded_images_zip_url": None,
        }

    def test_auth_callback_redirects_to_landing_page(self) -> None:
        response = self.client.get("/auth/callback?code=ok&state=state", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/")

    def test_auth_logout_clears_website_session(self) -> None:
        response = self.client.post("/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"signed_in": False})

    def test_exception_endpoints_require_sign_in(self) -> None:
        main.get_access_token = self.original_get_access_token

        requests = [
            ("get", "/api/exceptions", None),
            ("post", "/api/exceptions", {"email": "alex@example.com"}),
            ("delete", "/api/exceptions/alex%40example.com", None),
        ]
        for method, url, payload in requests:
            with self.subTest(method=method, url=url):
                if payload is None:
                    response = getattr(self.client, method)(url)
                else:
                    response = getattr(self.client, method)(url, json=payload)
                self.assertEqual(response.status_code, 401)

    def test_lists_posts_with_attachment_and_image_links(self) -> None:
        response = self.client.get("/api/posts?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        post = payload["posts"][0]
        self.assertEqual(self.graph.list_page_calls[0]["top"], 25)
        self.assertIsNone(self.graph.list_page_calls[0]["page_url"])
        self.assertFalse(payload["pagination"]["has_next"])
        self.assertIsNone(payload["pagination"]["next_cursor"])
        self.assertEqual(payload["pagination"]["limit"], 5)
        self.assertEqual(payload["cache"]["new_posts_saved"], 1)
        self.assertEqual(payload["cache"]["posts_skipped_by_exception"], 0)
        self.assertFalse(payload["cache"]["partial_refresh"])
        self.assertFalse(payload["cache"]["refresh_failed"])
        self.assertTrue(main.settings.post_cache_path.exists())
        self.assertEqual(self.graph.get_message_calls, ["msg-1"])
        self.assertIn("<strong>Hello formatted full body</strong>", post["body_html"])
        self.assertIn('src="/api/posts/msg-1/images/1"', post["body_html"])
        self.assertNotIn("Truncated list body", post["body_html"])
        self.assertNotIn("attachment", post["body_html"])
        self.assertEqual(post["attachments"][0]["content_url"], "https://contoso.sharepoint.com/source.docx")
        self.assertEqual(post["embedded_images"][0]["download_url"], "/api/posts/msg-1/images/1")
        self.assertEqual(post["author_email"], "alex@example.com")
        self.assertFalse(post["reposted"])

    def test_exception_list_can_be_managed_from_api(self) -> None:
        add_response = self.client.post("/api/exceptions", json={"email": " Alex@Example.com "})
        list_response = self.client.get("/api/exceptions")
        remove_response = self.client.delete("/api/exceptions/alex%40example.com")

        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(add_response.json()["emails"], ["alex@example.com"])
        self.assertEqual(list_response.json()["emails"], ["alex@example.com"])
        self.assertEqual(remove_response.json()["emails"], [])

    def test_invalid_exception_email_returns_bad_request(self) -> None:
        response = self.client.post("/api/exceptions", json={"email": "not an email"})

        self.assertEqual(response.status_code, 400)

    def test_refresh_skips_posts_from_exception_email(self) -> None:
        ExceptionList(main.settings.exception_list_path).add("alex@example.com")

        response = self.client.get("/api/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_exception"], 1)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual(cache.list_posts("source-team", "19:source@thread.tacv2"), [])

    def test_cached_posts_from_exception_email_are_excluded_from_pages(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                {**self._cached_post("msg-1", "2026-06-01T01:02:03Z"), "author_email": "alex@example.com"},
                {**self._cached_post("msg-2", "2026-06-02T01:02:03Z"), "author_email": "jamie@example.com"},
            ],
        )
        ExceptionList(main.settings.exception_list_path).add("alex@example.com")

        response = self.client.get("/api/posts?limit=10&refresh=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([post["id"] for post in response.json()["posts"]], ["msg-2"])

    def test_lists_posts_from_cursor(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                self._cached_post("msg-1", "2026-06-03T01:02:03Z"),
                self._cached_post("msg-2", "2026-06-02T01:02:03Z"),
            ],
        )
        cursor = main._encode_posts_cursor(1)

        response = self.client.get(f"/api/posts?limit=1&refresh=false&cursor={cursor}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["posts"][0]["id"], "msg-2")
        self.assertEqual(self.graph.list_page_calls, [])

    def test_rejects_invalid_posts_cursor(self) -> None:
        response = self.client.get("/api/posts?refresh=false&cursor=not-a-valid-cursor")

        self.assertEqual(response.status_code, 400)

    def test_refresh_stops_when_cached_newest_post_is_reached(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        self.graph.pages = [
            {
                "messages": [
                    {"id": "msg-new", "body": {"contentType": "html", "content": "<p>New</p>"}},
                    {"id": "msg-1", "body": {"contentType": "html", "content": "<p>Cached</p>"}},
                ],
                "next_link": f"{main.settings.graph_base_url}/next-page",
            }
        ]

        response = self.client.get("/api/posts?limit=10&refresh=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.graph.get_message_calls, ["msg-new"])
        self.assertEqual(len(self.graph.list_page_calls), 1)
        self.assertEqual(response.json()["cache"]["new_posts_saved"], 1)
        self.assertEqual([post["id"] for post in cache.list_posts("source-team", "19:source@thread.tacv2")], ["msg-new", "msg-1"])

    def test_cache_only_posts_still_merge_repost_status(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )
        RepostHistory(main.settings.repost_history_path).upsert(
            {
                "source_key": "source-team|19:source@thread.tacv2|msg-1|translation:zh-Hans",
                "source": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "msg-1"},
                "destination": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "new-message"},
                "translation": {"target_language": "zh-Hans"},
                "warnings": ["Copied with warning"],
            }
        )

        response = self.client.get("/api/posts?refresh=false")

        post = response.json()["posts"][0]
        self.assertTrue(post["reposted"])
        self.assertEqual(post["repost"]["message_id"], "new-message")
        self.assertEqual(post["warnings"], ["Copied with warning"])
        self.assertEqual(self.graph.list_page_calls, [])

    def test_graph_failure_returns_cached_posts_with_warning(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )
        self.graph.fail_list = True

        response = self.client.get("/api/posts?refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["cache"]["refresh_failed"])
        self.assertEqual(payload["cache"]["refresh_error"], "Graph unavailable")
        self.assertEqual(payload["posts"][0]["id"], "msg-1")

    def test_graph_failure_without_cache_returns_error(self) -> None:
        self.graph.fail_list = True

        response = self.client.get("/api/posts?refresh=true")

        self.assertEqual(response.status_code, 502)

    def test_translate_cached_post_saves_translation(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])

        response = self.client.post("/api/posts/msg-1/translations", json={"target_language": "zh-Hans"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["translation"]["body_html"], "<p>Ni hao</p>")
        self.assertEqual(len(self.translation_calls), 1)
        post = cache.get_post("source-team", "19:source@thread.tacv2", "msg-1")
        self.assertEqual(post["translations"]["zh-Hans"]["subject"], "Translated msg-1")

    def test_translate_cached_post_defaults_body_when_request_body_is_omitted(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )

        response = self.client.post("/api/posts/msg-1/translations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["target_language"], "zh-Hans")
        self.assertEqual(self.translation_calls[0]["target_language"], "zh-Hans")

    def test_translate_cached_post_returns_saved_translation_without_provider_call(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Saved", "body_html": "<p>Saved</p>", "body_preview": "Saved", "translated_at": "now", "model": "old"},
        )

        response = self.client.post("/api/posts/msg-1/translations", json={"target_language": "zh-Hans"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cached"])
        self.assertEqual(response.json()["translation"]["subject"], "Saved")
        self.assertEqual(self.translation_calls, [])

    def test_force_translate_overwrites_saved_translation(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Saved", "body_html": "<p>Saved</p>", "body_preview": "Saved", "translated_at": "now", "model": "old"},
        )

        response = self.client.post("/api/posts/msg-1/translations", json={"target_language": "zh-Hans", "force": True})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cached"])
        self.assertEqual(len(self.translation_calls), 1)
        post = cache.get_post("source-team", "19:source@thread.tacv2", "msg-1")
        self.assertEqual(post["translations"]["zh-Hans"]["subject"], "Translated msg-1")

    def test_translate_missing_cached_post_returns_not_found(self) -> None:
        response = self.client.post("/api/posts/missing/translations", json={"target_language": "zh-Hans"})

        self.assertEqual(response.status_code, 404)

    def test_translate_missing_openai_api_key_returns_service_unavailable(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )
        main.settings.openai_api_key = None

        response = self.client.post("/api/posts/msg-1/translations", json={"target_language": "zh-Hans"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.translation_calls, [])

    def test_posts_response_includes_saved_translations(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Saved", "body_html": "<p>Saved</p>", "body_preview": "Saved", "translated_at": "now", "model": "gpt-5.5"},
        )

        response = self.client.get("/api/posts?refresh=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["posts"][0]["translations"]["zh-Hans"]["body_preview"], "Saved")

    def test_create_repost_uses_cached_chinese_translation(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {
                "subject": "Chinese subject",
                "body_html": '<p><strong>你好</strong> <img src="/api/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
        )

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 200)
        payload = self.graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(payload["subject"], "Chinese subject")
        self.assertIn("<strong>你好</strong>", body)
        self.assertNotIn("Hello formatted full body", body)
        self.assertIn('src="../hostedContents/1/$value"', body)
        attachment = payload["attachments"][0]
        uuid.UUID(attachment["id"])
        self.assertIn(f'<attachment id="{attachment["id"]}"></attachment>', body)
        self.assertEqual(attachment["contentType"], "reference")
        self.assertEqual(attachment["contentUrl"], "https://contoso.sharepoint.com/source.docx")
        self.assertEqual(response.json()["record"]["source_key"], "source-team|19:source@thread.tacv2|msg-1|translation:zh-Hans")
        self.assertEqual(response.json()["record"]["attachment_statuses"][0]["status"], "attached_reference")
        self.assertEqual(response.json()["record"]["attachment_statuses"][0]["id"], attachment["id"])

    def test_successful_create_repost_is_saved_into_post_status(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Chinese subject", "body_html": "<p>你好</p>", "body_preview": "你好"},
        )

        repost_response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})
        posts_response = self.client.get("/api/posts?refresh=false")

        self.assertEqual(repost_response.status_code, 200)
        post = posts_response.json()["posts"][0]
        self.assertTrue(post["reposted"])
        self.assertEqual(post["repost"]["message_id"], "new-message")
        self.assertIsNotNone(post["reposted_at"])
        self.assertEqual(post["repost_status"], "reposted")
        self.assertFalse(post["manual_repost"])

    def test_manual_repost_status_is_saved_into_post_status(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )

        mark_response = self.client.post("/api/reposts/manual", json={"source_message_id": "msg-1"})
        posts_response = self.client.get("/api/posts?refresh=false")

        self.assertEqual(mark_response.status_code, 200)
        record = mark_response.json()["record"]
        self.assertEqual(mark_response.json()["status"], "marked_reposted")
        self.assertEqual(record["status"], "manually_marked")
        self.assertTrue(record["manual"])

        post = posts_response.json()["posts"][0]
        self.assertTrue(post["reposted"])
        self.assertEqual(post["repost_status"], "manually_marked")
        self.assertTrue(post["manual_repost"])
        self.assertIsNone(post["repost"]["web_url"])

    def test_manual_repost_status_is_idempotent(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )

        first = self.client.post("/api/reposts/manual", json={"source_message_id": "msg-1"})
        second = self.client.post("/api/reposts/manual", json={"source_message_id": "msg-1"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "marked_reposted")
        self.assertEqual(second.json()["status"], "already_reposted")
        self.assertEqual(RepostHistory(main.settings.repost_history_path).list_records()[0]["status"], "manually_marked")

    def test_create_repost_graph_attachment_failure_does_not_write_history(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Chinese subject", "body_html": "<p>你好</p>", "body_preview": "你好"},
        )
        self.graph.fail_create_with_attachments = True

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "bad attachment cards")
        self.assertEqual(self.graph.create_calls, 2)
        self.assertIsNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "msg-1",
                "zh-Hans",
            )
        )

    def test_create_repost_requires_cached_translation(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.graph.create_calls, 0)

    def test_english_repost_history_does_not_block_chinese_repost(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Chinese subject", "body_html": "<p>你好</p>", "body_preview": "你好"},
        )
        RepostHistory(main.settings.repost_history_path).upsert(
            {
                "source_key": "source-team|19:source@thread.tacv2|msg-1",
                "source": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "msg-1"},
                "destination": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "old-english"},
            }
        )

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reposted")
        self.assertEqual(self.graph.create_calls, 1)

    def test_create_repost_is_idempotent_from_history(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {"subject": "Translated msg-1", "body_html": "<p>Ni hao</p>", "body_preview": "Ni hao"},
        )

        first = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})
        second = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "reposted")
        self.assertEqual(second.json()["status"], "already_reposted")
        self.assertEqual(self.graph.create_calls, 1)
        self.assertEqual(first.json()["record"]["translation"]["target_language"], "zh-Hans")


if __name__ == "__main__":
    unittest.main()
