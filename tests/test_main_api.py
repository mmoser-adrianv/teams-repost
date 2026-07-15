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
        self.created_destinations = []
        self.get_message_calls = []
        self.list_page_calls = []
        self.fail_list = False
        self.fail_create_with_attachments = False
        self.fail_create_with_hosted_contents = False
        self.fail_file_upload = False
        self.fail_get_message_ids = set()
        self.hosted_content_bytes = b"image-bytes"
        self.hosted_content_type = "image/png"
        self.messages = {}
        self.file_api_calls = []
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
        if message_id in self.fail_get_message_ids:
            raise GraphAPIError(500, "UnknownError")
        return self._message(message_id)

    async def get_message_hosted_contents(self, team_id, channel_id, message_id, parent_message_id=None):
        return [{"id": "image-1"}]

    async def download_message_hosted_content(self, team_id, channel_id, message_id, hosted_content_id, parent_message_id=None):
        return self.hosted_content_bytes, self.hosted_content_type

    async def create_channel_message(self, team_id, channel_id, payload):
        self.create_calls += 1
        self.created_destinations.append({"team_id": team_id, "channel_id": channel_id})
        self.created_payloads.append(payload)
        if self.fail_create_with_hosted_contents and payload.get("hostedContents"):
            raise GraphAPIError(
                400,
                "bad hosted content",
                {
                    "error": {"message": "Hosted content payload was rejected"},
                    "accessToken": "must-not-be-stored",
                    "contentBytes": "must-not-be-stored",
                },
            )
        if self.fail_create_with_attachments and payload.get("attachments"):
            raise GraphAPIError(400, "bad attachment cards")
        return {"id": "new-message", "webUrl": "https://teams/repost"}

    async def get_channel_files_folder(self, team_id, channel_id):
        self.file_api_calls.append("filesFolder")
        return {"id": "folder-id", "parentReference": {"driveId": "drive-id"}}

    async def get_drive_item_from_share_url(self, content_url):
        self.file_api_calls.append("driveItem")
        return {"id": "source-drive-item", "name": "source.docx", "size": 12, "file": {}}

    async def download_drive_item_from_share_url(self, content_url):
        self.file_api_calls.append("download")
        return b"file-content", "application/octet-stream"

    async def upload_file_to_channel_folder(self, files_folder, file_name, content, content_type="application/octet-stream", conflict_behavior="fail"):
        self.file_api_calls.append("upload")
        if self.fail_file_upload:
            raise GraphAPIError(403, "file upload denied")
        return {
            "id": "copied-file-1",
            "name": file_name,
            "webUrl": f"https://contoso.sharepoint.com/destination/{file_name}",
        }

    def _message(self, message_id):
        created_times = {
            "msg-new": "2026-06-05T01:02:03Z",
            "msg-3": "2026-06-03T01:02:03Z",
            "msg-2": "2026-06-02T01:02:03Z",
            "msg-1": "2026-06-01T01:02:03Z",
        }
        message = {
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
        message.update(self.messages.get(message_id) or {})
        return message


class MainApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_get_access_token = main.get_access_token
        self.original_complete_login_flow = main.complete_login_flow
        self.original_create_login_flow = main.create_login_flow
        self.original_graph = main._graph
        self.original_history_path = main.settings.repost_history_path
        self.original_post_cache_path = main.settings.post_cache_path
        self.original_msal_token_cache_path = main.settings.msal_token_cache_path
        self.original_exception_list_path = main.settings.exception_list_path
        self.original_reverse_exception_list_path = main.settings.reverse_exception_list_path
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
        main.create_login_flow = lambda request, settings: "https://login.example.com/authorize"
        main._graph = lambda token: FakeGraphContext(self.graph)
        main._translate_post = self._fake_translate_post
        main.settings.source_team_id = "source-team"
        main.settings.source_channel_id = "19:source@thread.tacv2"
        main.settings.destination_team_id = "dest-team"
        main.settings.destination_channel_id = "19:dest@thread.tacv2"
        main.settings.repost_history_path = Path(self.temp_dir.name) / "history.json"
        main.settings.post_cache_path = Path(self.temp_dir.name) / "post-cache.json"
        main.settings.msal_token_cache_path = Path(self.temp_dir.name) / "msal-token-cache.json"
        main.settings.exception_list_path = Path(self.temp_dir.name) / "exception-list.json"
        main.settings.reverse_exception_list_path = Path(self.temp_dir.name) / "exception-list-reverse.json"
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
        main.create_login_flow = self.original_create_login_flow
        main._graph = self.original_graph
        main.settings.repost_history_path = self.original_history_path
        main.settings.post_cache_path = self.original_post_cache_path
        main.settings.msal_token_cache_path = self.original_msal_token_cache_path
        main.settings.exception_list_path = self.original_exception_list_path
        main.settings.reverse_exception_list_path = self.original_reverse_exception_list_path
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

    def test_auth_callback_preserves_reverse_page_return(self) -> None:
        login_response = self.client.get("/auth/login?return_to=/reverse", follow_redirects=False)
        callback_response = self.client.get("/auth/callback?code=ok&state=state", follow_redirects=False)

        self.assertEqual(login_response.status_code, 307)
        self.assertEqual(callback_response.status_code, 307)
        self.assertEqual(callback_response.headers["location"], "/reverse")

    def test_auth_callback_rejects_external_return_path(self) -> None:
        self.client.get("/auth/login?return_to=https%3A%2F%2Fexample.com", follow_redirects=False)

        response = self.client.get("/auth/callback?code=ok&state=state", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/")

    def test_auth_logout_clears_website_session(self) -> None:
        response = self.client.post("/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"signed_in": False})

    def test_reverse_page_is_served_with_flow_config(self) -> None:
        response = self.client.get("/reverse")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Chinese to English Repost Manager", response.text)
        self.assertIn('apiBase: "/api/flows/reverse"', response.text)
        self.assertIn('translationTargetLanguage: "en"', response.text)
        self.assertIn('/auth/login">Sign in</a>', response.text)

    def test_exception_endpoints_require_sign_in(self) -> None:
        main.get_access_token = self.original_get_access_token

        requests = [
            ("get", "/api/exceptions", None),
            ("post", "/api/exceptions", {"email": "alex@example.com"}),
            ("delete", "/api/exceptions/alex%40example.com", None),
            ("get", "/api/flows/reverse/exceptions", None),
            ("post", "/api/flows/reverse/exceptions", {"email": "chen@example.com"}),
            ("delete", "/api/flows/reverse/exceptions/chen%40example.com", None),
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

    def test_flow_forward_posts_api_matches_legacy_source(self) -> None:
        response = self.client.get("/api/flows/forward/posts?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["flow"]["name"], "forward")
        self.assertEqual(payload["flow"]["target_language"], "zh-Hans")
        self.assertEqual(payload["source"], {"team_id": "source-team", "channel_id": "19:source@thread.tacv2"})
        self.assertEqual(payload["destination"], {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2"})
        self.assertEqual(self.graph.list_page_calls[0]["team_id"], "source-team")
        self.assertIn('src="/api/flows/forward/posts/msg-1/images/1"', payload["posts"][0]["body_html"])

    def test_forward_refresh_skips_reverse_repost_body_header(self) -> None:
        self.graph.pages = [
            {
                "messages": [{"id": "reverse-repost-msg", "subject": "English repost"}],
                "next_link": None,
            }
        ]
        self.graph.messages["reverse-repost-msg"] = {
            "body": {
                "contentType": "html",
                "content": "<p><strong>Original author:</strong> Chen<br><strong>Original link:</strong> Source</p><hr><p>English</p>",
            }
        }

        response = self.client.get("/api/flows/forward/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_body_prefix"], 1)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual(cache.list_posts("source-team", "19:source@thread.tacv2"), [])

    def test_forward_refresh_still_skips_legacy_chinese_reverse_repost_body_header(self) -> None:
        self.graph.pages = [
            {
                "messages": [{"id": "legacy-reverse-repost-msg", "subject": "English repost"}],
                "next_link": None,
            }
        ]
        self.graph.messages["legacy-reverse-repost-msg"] = {
            "body": {
                "contentType": "html",
                "content": "<p><strong>原文作者：</strong> Chen<br><strong>原文連結：</strong> Source</p><hr><p>English</p>",
            }
        }

        response = self.client.get("/api/flows/forward/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_body_prefix"], 1)

    def test_reverse_posts_api_reads_destination_channel_as_source(self) -> None:
        response = self.client.get("/api/flows/reverse/posts?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["flow"]["name"], "reverse")
        self.assertEqual(payload["flow"]["target_language"], "en")
        self.assertEqual(payload["source"], {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2"})
        self.assertEqual(payload["destination"], {"team_id": "source-team", "channel_id": "19:source@thread.tacv2"})
        self.assertEqual(self.graph.list_page_calls[0]["team_id"], "dest-team")
        self.assertEqual(self.graph.list_page_calls[0]["channel_id"], "19:dest@thread.tacv2")
        self.assertIn('src="/api/flows/reverse/posts/msg-1/images/1"', payload["posts"][0]["body_html"])
        self.assertEqual(payload["posts"][0]["embedded_images"][0]["download_url"], "/api/flows/reverse/posts/msg-1/images/1")

    def test_reverse_refresh_skips_body_that_starts_with_original_author_header(self) -> None:
        self.graph.pages = [
            {
                "messages": [{"id": "repost-msg", "subject": "普通主题"}],
                "next_link": None,
            }
        ]
        self.graph.messages["repost-msg"] = {
            "body": {
                "contentType": "html",
                "content": "<p><strong>原文作者：</strong> Alex<br><strong>原文連結：</strong> Source</p><hr><p>中文</p>",
            }
        }

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_body_prefix"], 1)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual(cache.list_posts("dest-team", "19:dest@thread.tacv2"), [])

    def test_reverse_refresh_skips_repost_header_before_hydration(self) -> None:
        self.graph.pages = [
            {
                "messages": [
                    {
                        "id": "repost-msg",
                        "subject": "普通主题",
                        "body": {
                            "contentType": "html",
                            "content": "<p><strong>原文作者：</strong> Alex<br><strong>原文連結：</strong> Source</p>",
                        },
                    }
                ],
                "next_link": None,
            }
        ]
        self.graph.fail_get_message_ids.add("repost-msg")

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["posts_skipped_by_body_prefix"], 1)
        self.assertEqual(payload["cache"]["posts_skipped_by_graph_error"], 0)
        self.assertNotIn("repost-msg", self.graph.get_message_calls)

    def test_reverse_refresh_skips_unhydratable_message_and_keeps_loading(self) -> None:
        self.graph.pages = [
            {
                "messages": [
                    {"id": "bad-msg", "body": {"contentType": "html", "content": "<p>Bad</p>"}},
                    {"id": "msg-1", "body": {"contentType": "html", "content": "<p>Good</p>"}},
                ],
                "next_link": None,
            }
        ]
        self.graph.fail_get_message_ids.add("bad-msg")

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([post["id"] for post in payload["posts"]], ["msg-1"])
        self.assertEqual(payload["cache"]["new_posts_saved"], 1)
        self.assertEqual(payload["cache"]["posts_skipped_by_graph_error"], 1)
        self.assertEqual(self.graph.get_message_calls, ["bad-msg", "msg-1"])

    def test_reverse_refresh_does_not_skip_original_author_prefix_in_subject(self) -> None:
        self.graph.pages = [
            {
                "messages": [{"id": "real-cn-msg", "subject": "原文作者：只在主题"}],
                "next_link": None,
            }
        ]
        self.graph.messages["real-cn-msg"] = {
            "subject": "原文作者：只在主题",
            "body": {"contentType": "html", "content": "<p>这是一条真正的中文消息。</p>"},
        }

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([post["id"] for post in payload["posts"]], ["real-cn-msg"])
        self.assertEqual(payload["cache"]["new_posts_saved"], 1)
        self.assertEqual(payload["cache"]["posts_skipped_by_body_prefix"], 0)

    def test_exception_list_can_be_managed_from_api(self) -> None:
        add_response = self.client.post("/api/exceptions", json={"email": " Alex@Example.com "})
        list_response = self.client.get("/api/exceptions")
        remove_response = self.client.delete("/api/exceptions/alex%40example.com")

        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(add_response.json()["emails"], ["alex@example.com"])
        self.assertEqual(list_response.json()["emails"], ["alex@example.com"])
        self.assertEqual(remove_response.json()["emails"], [])

    def test_flow_exception_lists_are_managed_independently(self) -> None:
        forward_add = self.client.post("/api/flows/forward/exceptions", json={"email": " Alex@Example.com "})
        reverse_add = self.client.post("/api/flows/reverse/exceptions", json={"email": " Chen@Example.com "})
        legacy_list = self.client.get("/api/exceptions")
        reverse_list = self.client.get("/api/flows/reverse/exceptions")

        self.assertEqual(forward_add.status_code, 200)
        self.assertEqual(reverse_add.status_code, 200)
        self.assertEqual(forward_add.json()["flow"]["name"], "forward")
        self.assertEqual(reverse_add.json()["flow"]["name"], "reverse")
        self.assertEqual(legacy_list.json()["emails"], ["alex@example.com"])
        self.assertEqual(reverse_list.json()["emails"], ["chen@example.com"])

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

    def test_refresh_skips_posts_with_no_presentable_content(self) -> None:
        self.graph.pages = [{"messages": [{"id": "empty-msg", "subject": "Teams message"}], "next_link": None}]
        self.graph.messages["empty-msg"] = {
            "subject": "Teams message",
            "from": None,
            "body": {"contentType": "html", "content": ""},
            "attachments": [],
        }

        response = self.client.get("/api/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_empty_content"], 1)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual(cache.list_posts("source-team", "19:source@thread.tacv2"), [])

    def test_refresh_keeps_image_only_posts(self) -> None:
        self.graph.pages = [{"messages": [{"id": "image-msg"}], "next_link": None}]
        self.graph.messages["image-msg"] = {
            "subject": None,
            "body": {"contentType": "html", "content": '<p><img src="../hostedContents/image-1/$value"></p>'},
            "attachments": [],
        }

        response = self.client.get("/api/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([post["id"] for post in payload["posts"]], ["image-msg"])
        self.assertEqual(payload["cache"]["posts_skipped_by_empty_content"], 0)

    def test_reverse_refresh_skips_posts_from_reverse_exception_email(self) -> None:
        ExceptionList(main.settings.exception_list_path).add("alex@example.com")
        ExceptionList(main.settings.reverse_exception_list_path).add("chen@example.com")
        self.graph.messages["msg-1"] = {"from": {"user": {"displayName": "Chen", "email": "chen@example.com"}}}

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["cache"]["new_posts_saved"], 0)
        self.assertEqual(payload["cache"]["posts_skipped_by_exception"], 1)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual(cache.list_posts("dest-team", "19:dest@thread.tacv2"), [])

    def test_forward_exception_list_does_not_filter_reverse_side(self) -> None:
        ExceptionList(main.settings.exception_list_path).add("alex@example.com")

        response = self.client.get("/api/flows/reverse/posts?limit=5&refresh=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([post["id"] for post in payload["posts"]], ["msg-1"])
        self.assertEqual(payload["cache"]["posts_skipped_by_exception"], 0)

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

    def test_cached_posts_with_no_presentable_content_are_excluded_from_pages(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                {
                    **self._cached_post("empty-msg", "2026-06-02T01:02:03Z"),
                    "subject": "Teams message",
                    "author": None,
                    "body_html": "",
                    "body_preview": "",
                    "attachments": [],
                    "embedded_images": [],
                },
                self._cached_post("real-msg", "2026-06-01T01:02:03Z"),
            ],
        )

        response = self.client.get("/api/posts?limit=10&refresh=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([post["id"] for post in response.json()["posts"]], ["real-msg"])

    def test_cached_reverse_posts_from_reverse_exception_email_are_excluded_from_pages(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "dest-team",
            "19:dest@thread.tacv2",
            [
                {**self._cached_post("msg-1", "2026-06-01T01:02:03Z"), "author_email": "chen@example.com"},
                {**self._cached_post("msg-2", "2026-06-02T01:02:03Z"), "author_email": "li@example.com"},
            ],
        )
        ExceptionList(main.settings.reverse_exception_list_path).add("chen@example.com")

        response = self.client.get("/api/flows/reverse/posts?limit=10&refresh=false")

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

    def test_cold_cache_refresh_continues_across_pages(self) -> None:
        self.graph.pages = [
            {
                "messages": [{"id": "msg-newer", "body": {"contentType": "html", "content": "<p>Newer</p>"}}],
                "next_link": f"{main.settings.graph_base_url}/next-page",
            },
            {
                "messages": [{"id": "msg-older", "body": {"contentType": "html", "content": "<p>Older</p>"}}],
                "next_link": None,
            },
        ]
        self.graph.messages["msg-newer"] = {"createdDateTime": "2026-06-02T01:02:03Z"}
        self.graph.messages["msg-older"] = {"createdDateTime": "2026-06-01T01:02:03Z"}

        response = self.client.get("/api/posts?limit=10&refresh=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.graph.list_page_calls), 2)
        self.assertEqual(self.graph.list_page_calls[1]["page_url"], f"{main.settings.graph_base_url}/next-page")
        self.assertEqual(self.graph.get_message_calls, ["msg-newer", "msg-older"])
        self.assertEqual(response.json()["cache"]["new_posts_saved"], 2)
        cache = PostCache(main.settings.post_cache_path)
        self.assertEqual([post["id"] for post in cache.list_posts("source-team", "19:source@thread.tacv2")], ["msg-newer", "msg-older"])

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

    def test_cached_reverse_reposts_are_excluded_from_forward_page(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                {
                    **self._cached_post("reverse-repost", "2026-06-05T09:09:25Z"),
                    "subject": None,
                    "author": None,
                    "body_html": "<p><strong>Original author:</strong> Chen<br><strong>Original link:</strong> Source</p><hr><p>English repost</p>",
                    "body_preview": "Original author: Chen Original link: Source English repost",
                },
                self._cached_post("real-msg", "2026-06-04T01:02:03Z"),
            ],
        )
        RepostHistory(main.settings.repost_history_path).upsert(
            {
                "source_key": "source-team|19:source@thread.tacv2|reverse-repost|translation:zh-Hans",
                "source": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "reverse-repost"},
                "destination": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "new-message"},
                "translation": {"target_language": "zh-Hans"},
            }
        )

        response = self.client.get("/api/posts?refresh=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([post["id"] for post in response.json()["posts"]], ["real-msg"])
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

    def test_reverse_translate_defaults_to_english(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "dest-team",
            "19:dest@thread.tacv2",
            [self._cached_post("msg-1", "2026-06-01T01:02:03Z")],
        )

        response = self.client.post("/api/flows/reverse/posts/msg-1/translations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["target_language"], "en")
        self.assertEqual(self.translation_calls[0]["target_language"], "en")
        post = PostCache(main.settings.post_cache_path).get_post("dest-team", "19:dest@thread.tacv2", "msg-1")
        self.assertEqual(post["translations"]["en"]["subject"], "Translated msg-1")

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

    def test_translate_cached_post_with_no_presentable_content_returns_conflict(self) -> None:
        PostCache(main.settings.post_cache_path).upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                {
                    **self._cached_post("empty-msg", "2026-06-01T01:02:03Z"),
                    "body_html": "",
                    "body_preview": "",
                    "attachments": [],
                    "embedded_images": [],
                }
            ],
        )

        response = self.client.post("/api/posts/empty-msg/translations", json={"target_language": "zh-Hans"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.translation_calls, [])

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
        self.assertEqual(response.json()["record"]["translation"]["source_language"], "en")
        self.assertEqual(response.json()["record"]["attachment_statuses"][0]["status"], "attached_reference")
        self.assertEqual(response.json()["record"]["attachment_statuses"][0]["id"], attachment["id"])
        self.assertEqual(self.graph.file_api_calls, [])

    def test_create_repost_omits_announcement_banner_attachment_as_regular_post(self) -> None:
        announcement_attachment = {
            "id": "announcement-card-1",
            "name": "attachment-1",
            "contentType": "application/vnd.microsoft.teams.messaging-announcementBanner",
        }
        cached_post = {
            **self._cached_post("announcement-msg", "2026-06-01T01:02:03Z"),
            "attachments": [
                {
                    "id": announcement_attachment["id"],
                    "name": announcement_attachment["name"],
                    "content_type": announcement_attachment["contentType"],
                    "content_url": None,
                }
            ],
        }
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [cached_post])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "announcement-msg",
            "zh-Hans",
            {
                "subject": "Chinese announcement subject",
                "body_html": "<p>Chinese announcement body</p>",
                "body_preview": "Chinese announcement body",
            },
        )
        self.graph.messages["announcement-msg"] = {
            "subject": "Announcement subject",
            "body": {"contentType": "html", "content": "<p>Announcement body</p>"},
            "attachments": [announcement_attachment],
        }

        response = self.client.post("/api/reposts", json={"source_message_id": "announcement-msg"})

        self.assertEqual(response.status_code, 200)
        payload = self.graph.created_payloads[0]
        self.assertNotIn("attachments", payload)
        self.assertEqual(payload["subject"], "Chinese announcement subject")
        self.assertIn("<p>Chinese announcement body</p>", payload["body"]["content"])
        record = response.json()["record"]
        self.assertEqual(record["destination"]["message_id"], "new-message")
        self.assertEqual(record["attachment_links"][0]["content_type"], "application/vnd.microsoft.teams.messaging-announcementBanner")
        self.assertEqual(record["attachment_statuses"][0]["status"], "omitted_announcement_banner")
        self.assertIsNotNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "announcement-msg",
                "zh-Hans",
            )
        )

    def test_create_repost_omits_o365_connector_card_without_blocking(self) -> None:
        connector_attachment = {
            "id": "connector-card-1",
            "name": "attachment-1",
            "contentType": "application/vnd.microsoft.teams.card.o365connector",
            "content": "{\"title\":\"Legacy connector card\"}",
        }
        cached_post = {
            **self._cached_post("connector-msg", "2026-06-01T01:02:03Z"),
            "attachments": [
                {
                    "id": connector_attachment["id"],
                    "name": connector_attachment["name"],
                    "content_type": connector_attachment["contentType"],
                    "content_url": None,
                }
            ],
        }
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [cached_post])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "connector-msg",
            "zh-Hans",
            {
                "subject": "Chinese connector subject",
                "body_html": "<p>Chinese connector body</p>",
                "body_preview": "Chinese connector body",
            },
        )
        self.graph.messages["connector-msg"] = {
            "subject": "Connector subject",
            "body": {
                "contentType": "html",
                "content": '<p>Connector body</p><attachment id="connector-card-1"></attachment>',
            },
            "attachments": [connector_attachment],
        }

        response = self.client.post("/api/reposts", json={"source_message_id": "connector-msg"})

        self.assertEqual(response.status_code, 200)
        payload = self.graph.created_payloads[0]
        self.assertNotIn("attachments", payload)
        self.assertNotIn("<attachment", payload["body"]["content"])
        record = response.json()["record"]
        self.assertEqual(record["attachment_statuses"][0]["status"], "omitted_nonportable_teams_card")
        self.assertTrue(any("link to the original message" in warning for warning in record["warnings"]))
        self.assertIsNotNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "connector-msg",
                "zh-Hans",
            )
        )

    def test_create_repost_aborts_without_history_when_graph_rejects_hosted_content(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {
                "subject": "Chinese subject",
                "body_html": '<p><strong>你好</strong> <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
        )
        self.graph.fail_create_with_hosted_contents = True

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "bad hosted content")
        self.assertEqual(self.graph.create_calls, 1)
        self.assertIn("hostedContents", self.graph.created_payloads[0])
        self.assertNotIn("Open embedded image", self.graph.created_payloads[0]["body"]["content"])
        self.assertNotIn("Open original message for embedded image", self.graph.created_payloads[0]["body"]["content"])
        self.assertIsNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "msg-1",
                "zh-Hans",
            )
        )

    def test_create_repost_omits_oversized_inline_image_and_writes_history(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {
                "subject": "Chinese subject",
                "body_html": '<p><strong>你好</strong> <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
        )
        self.graph.hosted_content_bytes = b"x" * (3 * 1024 * 1024)
        self.graph.hosted_content_type = "image/png"

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 200)
        payload = self.graph.created_payloads[0]
        self.assertNotIn("hostedContents", payload)
        self.assertIn("Embedded image 1 omitted from this repost", payload["body"]["content"])
        record = response.json()["record"]
        self.assertEqual(record["status"], "reposted")
        self.assertEqual(record["inline_image_statuses"][0]["status"], "omitted_inline_too_large")
        self.assertEqual(record["inline_image_diagnostics"][0]["content_type"], "image/png")
        self.assertIn("Inline image 1 was omitted", record["warnings"][1])
        self.assertIsNotNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "msg-1",
                "zh-Hans",
            )
        )

    def test_create_repost_omits_gif_inline_image_and_writes_history(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {
                "subject": "Chinese subject",
                "body_html": '<p><strong>你好</strong> <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
        )
        self.graph.hosted_content_bytes = b"small-gif"
        self.graph.hosted_content_type = "image/gif"

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 200)
        payload = self.graph.created_payloads[0]
        self.assertNotIn("hostedContents", payload)
        self.assertIn("Microsoft Graph does not accept image/gif", payload["body"]["content"])
        record = response.json()["record"]
        self.assertEqual(record["inline_image_statuses"][0]["status"], "omitted_inline_unsupported_content_type")
        self.assertEqual(record["inline_image_diagnostics"][0]["reason"], "unsupported_content_type")
        self.assertIn("only accepts image/jpg, image/jpeg, and image/png", record["warnings"][1])
        self.assertIsNotNone(
            RepostHistory(main.settings.repost_history_path).get(
                "source-team",
                "19:source@thread.tacv2",
                "msg-1",
                "zh-Hans",
            )
        )

    def test_reverse_repost_uses_cached_english_translation_and_posts_to_original_source_channel(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("dest-team", "19:dest@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "dest-team",
            "19:dest@thread.tacv2",
            "msg-1",
            "en",
            {
                "subject": "English subject",
                "body_html": '<p><strong>Hello</strong> <img src="/api/flows/reverse/posts/msg-1/images/1"></p>',
                "body_preview": "Hello",
            },
        )
        RepostHistory(main.settings.repost_history_path).upsert(
            {
                "source_key": "dest-team|19:dest@thread.tacv2|msg-1|translation:zh-Hans",
                "source": {"team_id": "dest-team", "channel_id": "19:dest@thread.tacv2", "message_id": "msg-1"},
                "destination": {"team_id": "source-team", "channel_id": "19:source@thread.tacv2", "message_id": "old-chinese"},
                "translation": {"target_language": "zh-Hans"},
            }
        )

        response = self.client.post("/api/flows/reverse/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.graph.created_destinations[0], {"team_id": "source-team", "channel_id": "19:source@thread.tacv2"})
        payload = self.graph.created_payloads[0]
        self.assertEqual(payload["subject"], "English subject")
        self.assertIn("<strong>Hello</strong>", payload["body"]["content"])
        self.assertIn('src="../hostedContents/1/$value"', payload["body"]["content"])
        self.assertEqual(response.json()["record"]["source_key"], "dest-team|19:dest@thread.tacv2|msg-1|translation:en")
        self.assertEqual(response.json()["record"]["translation"]["target_language"], "en")
        self.assertEqual(response.json()["record"]["translation"]["source_language"], "zh-Hans")

    def test_successful_create_repost_is_saved_into_post_status(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts("source-team", "19:source@thread.tacv2", [self._cached_post("msg-1", "2026-06-01T01:02:03Z")])
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "msg-1",
            "zh-Hans",
            {
                "subject": "Chinese subject",
                "body_html": '<p>你好 <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
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
        self.assertEqual(record["translation"]["source_language"], "en")

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
            {
                "subject": "Chinese subject",
                "body_html": '<p>你好 <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
        )
        self.graph.fail_create_with_attachments = True

        response = self.client.post("/api/reposts", json={"source_message_id": "msg-1"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "bad attachment cards")
        self.assertEqual(self.graph.create_calls, 1)
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

    def test_create_repost_with_no_presentable_content_returns_conflict(self) -> None:
        cache = PostCache(main.settings.post_cache_path)
        cache.upsert_posts(
            "source-team",
            "19:source@thread.tacv2",
            [
                {
                    **self._cached_post("empty-msg", "2026-06-01T01:02:03Z"),
                    "body_html": "",
                    "body_preview": "",
                    "attachments": [],
                    "embedded_images": [],
                }
            ],
        )
        cache.upsert_translation(
            "source-team",
            "19:source@thread.tacv2",
            "empty-msg",
            "zh-Hans",
            {"subject": "Teams message", "body_html": "", "body_preview": ""},
        )

        response = self.client.post("/api/reposts", json={"source_message_id": "empty-msg"})

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
            {
                "subject": "Chinese subject",
                "body_html": '<p>你好 <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "你好",
            },
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
            {
                "subject": "Translated msg-1",
                "body_html": '<p>Ni hao <img src="/api/flows/forward/posts/msg-1/images/1"></p>',
                "body_preview": "Ni hao",
            },
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
