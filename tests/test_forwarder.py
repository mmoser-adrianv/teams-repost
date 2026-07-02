import unittest
import uuid
from base64 import b64encode
from dataclasses import dataclass

from forwarder import (
    AttachmentRepostError,
    DestinationChannel,
    InlineImageRepostError,
    build_reference_attachments,
    forward_message,
    repost_parsed_message,
    repost_translated_message,
)
from graph_client import GraphAPIError
from settings import Settings
from teams_url_parser import TeamsMessageLink


@dataclass
class Request:
    source_message_url: str
    destination_team_id: str | None = None
    destination_channel_id: str | None = None
    mode: str = "post"


ANNOUNCEMENT_BANNER_ATTACHMENT = {
    "id": "announcement-card-1",
    "name": "attachment-1",
    "contentType": "application/vnd.microsoft.teams.messaging-announcementBanner",
}


class FakeGraph:
    def __init__(self, attachment: dict | None = None) -> None:
        self.created_payload = None
        self.file_api_calls = []
        self.attachment = attachment or {
            "id": "file-1",
            "name": "source.docx",
            "contentType": "reference",
            "contentUrl": "https://contoso.sharepoint.com/file.docx",
        }

    async def get_message(self, team_id, channel_id, message_id, parent_message_id=None):
        return {
            "id": message_id,
            "subject": "Source subject",
            "webUrl": "https://teams.microsoft.com/source",
            "body": {
                "contentType": "html",
                "content": '<p>Hello</p><attachment id="file-1"></attachment>',
            },
            "attachments": [self.attachment],
        }

    async def get_message_hosted_contents(self, team_id, channel_id, message_id, parent_message_id=None):
        return []

    async def create_channel_message(self, team_id, channel_id, payload):
        self.created_payload = payload
        return {"id": "new-message", "webUrl": "https://teams.microsoft.com/repost"}

    async def get_channel_files_folder(self, *args, **kwargs):
        self.file_api_calls.append("filesFolder")
        return {"id": "folder-id", "parentReference": {"driveId": "drive-id"}}

    async def upload_file_to_channel_folder(self, files_folder, file_name, content, content_type="application/octet-stream", conflict_behavior="fail"):
        self.file_api_calls.append("upload")
        return {
            "id": "copied-file-1",
            "name": file_name,
            "webUrl": f"https://contoso.sharepoint.com/destination/{file_name}",
        }

    async def get_drive_item_from_share_url(self, content_url):
        self.file_api_calls.append("driveItem")
        return {"id": "source-drive-item", "name": self.attachment.get("name"), "size": 12, "file": {}}

    async def download_drive_item_from_share_url(self, content_url):
        self.file_api_calls.append("download")
        return b"file-content", "application/octet-stream"


class InlineImageGraph:
    def __init__(
        self,
        fail_inline_post: bool = False,
        fail_second_download: bool = False,
        include_attachment: bool = False,
        image_bytes_by_id: dict[str, bytes] | None = None,
        content_type_by_id: dict[str, str] | None = None,
    ) -> None:
        self.fail_inline_post = fail_inline_post
        self.fail_second_download = fail_second_download
        self.include_attachment = include_attachment
        self.image_bytes_by_id = image_bytes_by_id or {}
        self.content_type_by_id = content_type_by_id or {}
        self.created_payloads = []
        self.file_api_calls = []

    async def get_message(self, team_id, channel_id, message_id, parent_message_id=None):
        message = {
            "id": message_id,
            "subject": "Formatted source",
            "webUrl": "https://teams.microsoft.com/source",
            "body": {
                "contentType": "html",
                "content": (
                    '<p><strong>Full formatted body</strong></p>'
                    '<p><img src="../hostedContents/image-1/$value"><img src="../hostedContents/image-2/$value"></p>'
                ),
            },
        }
        if self.include_attachment:
            message["attachments"] = [
                {
                    "id": "file-1",
                    "name": "source.docx",
                    "contentType": "reference",
                    "contentUrl": "https://contoso.sharepoint.com/source.docx",
                }
            ]
        return message

    async def get_message_hosted_contents(self, team_id, channel_id, message_id, parent_message_id=None):
        return [{"id": "image-1"}, {"id": "image-2"}]

    async def download_message_hosted_content(self, team_id, channel_id, message_id, hosted_content_id, parent_message_id=None):
        if hosted_content_id == "image-2" and self.fail_second_download:
            raise GraphAPIError(404, "not found")
        return (
            self.image_bytes_by_id.get(hosted_content_id, f"bytes-{hosted_content_id}".encode("ascii")),
            self.content_type_by_id.get(hosted_content_id, "image/png"),
        )

    async def create_channel_message(self, team_id, channel_id, payload):
        self.created_payloads.append(payload)
        if self.fail_inline_post and "hostedContents" in payload:
            raise GraphAPIError(
                400,
                "bad hosted content",
                {
                    "error": {"message": "Hosted content payload was rejected"},
                    "accessToken": "must-not-be-stored",
                    "contentBytes": "must-not-be-stored",
                },
            )
        return {"id": f"new-message-{len(self.created_payloads)}", "webUrl": "https://teams.microsoft.com/repost"}

    async def get_channel_files_folder(self, *args, **kwargs):
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
        return {
            "id": "copied-file-1",
            "name": file_name,
            "webUrl": f"https://contoso.sharepoint.com/destination/{file_name}",
        }


class ForwarderTests(unittest.IsolatedAsyncioTestCase):
    async def test_repost_attaches_reference_attachments_without_file_copy(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            DESTINATION_TEAM_ID="dest-team",
            DESTINATION_CHANNEL_ID="dest-channel",
        )
        graph = FakeGraph()
        request = Request(
            source_message_url=(
                "https://teams.microsoft.com/l/message/19%3Asource%40thread.tacv2/msg-1"
                "?groupId=source-team"
            )
        )

        report = await forward_message(request, graph, settings)

        self.assertEqual(report["new_message_id"], "new-message")
        self.assertEqual(report["attachment_links"][0]["content_url"], "https://contoso.sharepoint.com/file.docx")
        attachment = graph.created_payload["attachments"][0]
        uuid.UUID(attachment["id"])
        self.assertIn(f'<attachment id="{attachment["id"]}"></attachment>', graph.created_payload["body"]["content"])
        self.assertEqual(attachment["contentType"], "reference")
        self.assertEqual(attachment["contentUrl"], "https://contoso.sharepoint.com/file.docx")
        self.assertEqual(attachment["name"], "source.docx")
        self.assertEqual(report["attachment_statuses"][0]["status"], "attached_reference")
        self.assertEqual(report["attachment_statuses"][0]["id"], attachment["id"])
        self.assertEqual(graph.file_api_calls, [])

    async def test_repost_attaches_image_reference_attachments_as_native_cards(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            DESTINATION_TEAM_ID="dest-team",
            DESTINATION_CHANNEL_ID="dest-channel",
        )
        graph = FakeGraph(
            {
                "id": "file-1",
                "name": "screenshot.png",
                "contentType": "reference",
                "contentUrl": "https://contoso.sharepoint.com/screenshot.png",
            }
        )
        request = Request(
            source_message_url=(
                "https://teams.microsoft.com/l/message/19%3Asource%40thread.tacv2/msg-1"
                "?groupId=source-team"
            )
        )

        report = await forward_message(request, graph, settings)

        body = graph.created_payload["body"]["content"]
        attachment = graph.created_payload["attachments"][0]
        self.assertIn(f'<attachment id="{attachment["id"]}"></attachment>', body)
        self.assertNotIn("Open embedded image", body)
        self.assertNotIn("Open original message for embedded image", body)
        self.assertEqual(attachment["contentUrl"], "https://contoso.sharepoint.com/screenshot.png")
        self.assertEqual(report["attachment_statuses"][0]["status"], "attached_reference")
        self.assertEqual(graph.file_api_calls, [])

    async def test_repost_omits_announcement_banner_attachment_as_regular_post(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            DESTINATION_TEAM_ID="dest-team",
            DESTINATION_CHANNEL_ID="dest-channel",
        )
        graph = FakeGraph(ANNOUNCEMENT_BANNER_ATTACHMENT)
        request = Request(
            source_message_url=(
                "https://teams.microsoft.com/l/message/19%3Asource%40thread.tacv2/msg-1"
                "?groupId=source-team"
            )
        )

        report = await forward_message(request, graph, settings)

        self.assertEqual(report["new_message_id"], "new-message")
        self.assertEqual(report["attachment_links"][0]["content_type"], "application/vnd.microsoft.teams.messaging-announcementBanner")
        self.assertEqual(report["attachment_statuses"][0]["status"], "omitted_announcement_banner")
        self.assertNotIn("attachments", graph.created_payload)
        self.assertIn("<p>Hello</p>", graph.created_payload["body"]["content"])
        self.assertNotIn("<attachment", graph.created_payload["body"]["content"])

    async def test_dry_run_reports_announcement_banner_as_omitted(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            DESTINATION_TEAM_ID="dest-team",
            DESTINATION_CHANNEL_ID="dest-channel",
        )
        graph = FakeGraph(ANNOUNCEMENT_BANNER_ATTACHMENT)
        request = Request(
            source_message_url=(
                "https://teams.microsoft.com/l/message/19%3Asource%40thread.tacv2/msg-1"
                "?groupId=source-team"
            ),
            mode="dry_run",
        )

        report = await forward_message(request, graph, settings)

        self.assertFalse(report["would_post"])
        self.assertEqual(report["detected_attachment_count"], 1)
        self.assertEqual(report["attachment_statuses"][0]["status"], "omitted_announcement_banner")
        self.assertIsNone(graph.created_payload)

    async def test_repost_rejects_unsupported_attachments_before_posting(self) -> None:
        with self.assertRaises(AttachmentRepostError):
            build_reference_attachments(
                [
                    {
                        "id": "file-1",
                        "name": "source.docx",
                        "contentType": "file",
                        "contentUrl": "https://contoso.sharepoint.com/file.docx",
                    }
                ]
            )

        with self.assertRaises(AttachmentRepostError):
            build_reference_attachments(
                [
                    {
                        "id": "file-1",
                        "name": "source.docx",
                        "contentType": "reference",
                    }
                ]
            )

    async def test_repost_embeds_downloaded_hosted_images_with_full_html(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph()
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
        )

        report = await repost_parsed_message(
            parsed,
            DestinationChannel("dest-team", "dest-channel"),
            graph,
            settings,
            mode="post",
        )

        payload = graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(report["new_message_id"], "new-message-1")
        self.assertIn("<strong>Full formatted body</strong>", body)
        self.assertIn('src="../hostedContents/1/$value"', body)
        self.assertIn('src="../hostedContents/2/$value"', body)
        self.assertEqual(len(payload["hostedContents"]), 2)
        self.assertEqual(payload["hostedContents"][0]["@microsoft.graph.temporaryId"], "1")
        self.assertEqual(payload["hostedContents"][0]["contentBytes"], b64encode(b"bytes-image-1").decode("ascii"))
        self.assertEqual([item["status"] for item in report["inline_image_statuses"]], ["recreated_inline", "recreated_inline"])

    async def test_repost_aborts_when_any_inline_image_download_fails(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(fail_second_download=True)
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        with self.assertRaises(InlineImageRepostError):
            await repost_parsed_message(parsed, DestinationChannel("dest-team", "dest-channel"), graph, settings)

        self.assertEqual(graph.created_payloads, [])

    async def test_repost_aborts_without_fallback_when_graph_rejects_hosted_content(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(fail_inline_post=True, include_attachment=True)
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        with self.assertRaises(GraphAPIError):
            await repost_parsed_message(parsed, DestinationChannel("dest-team", "dest-channel"), graph, settings)

        self.assertEqual(len(graph.created_payloads), 1)
        attempted_payload = graph.created_payloads[0]
        self.assertIn("hostedContents", attempted_payload)
        self.assertIn("attachments", attempted_payload)
        attachment_id = attempted_payload["attachments"][0]["id"]
        uuid.UUID(attachment_id)
        self.assertIn(f'<attachment id="{attachment_id}"></attachment>', attempted_payload["body"]["content"])
        self.assertNotIn("Open embedded image", attempted_payload["body"]["content"])
        self.assertNotIn("Open original message for embedded image", attempted_payload["body"]["content"])

    async def test_repost_omits_inline_image_when_payload_would_exceed_graph_limit(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(image_bytes_by_id={"image-1": b"x" * (3 * 1024 * 1024)})
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        report = await repost_parsed_message(parsed, DestinationChannel("dest-team", "dest-channel"), graph, settings)

        payload = graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(report["new_message_id"], "new-message-1")
        self.assertEqual(len(payload["hostedContents"]), 1)
        self.assertEqual(payload["hostedContents"][0]["@microsoft.graph.temporaryId"], "2")
        self.assertIn("Embedded image 1 omitted from this repost", body)
        self.assertIn("Open original message", body)
        self.assertNotIn("image-1/$value", body)
        self.assertIn('src="../hostedContents/2/$value"', body)
        self.assertEqual([item["status"] for item in report["inline_image_statuses"]], ["recreated_inline", "omitted_inline_too_large"])
        self.assertEqual(report["inline_image_diagnostics"][0]["status"], "omitted_inline_too_large")

    async def test_repost_omits_gif_inline_image_because_graph_does_not_accept_gif_hosted_content(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(content_type_by_id={"image-2": "image/gif"})
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        report = await repost_parsed_message(parsed, DestinationChannel("dest-team", "dest-channel"), graph, settings)

        payload = graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(len(payload["hostedContents"]), 1)
        self.assertEqual(payload["hostedContents"][0]["@microsoft.graph.temporaryId"], "1")
        self.assertIn('src="../hostedContents/1/$value"', body)
        self.assertIn("Embedded image 2 omitted from this repost", body)
        self.assertIn("Microsoft Graph does not accept image/gif", body)
        self.assertEqual(report["inline_image_statuses"][1]["status"], "omitted_inline_unsupported_content_type")
        self.assertEqual(report["inline_image_diagnostics"][0]["reason"], "unsupported_content_type")

    async def test_translated_repost_uses_translated_body_and_recreates_images(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(include_attachment=True)
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        report = await repost_translated_message(
            parsed,
            DestinationChannel("dest-team", "dest-channel"),
            graph,
            settings,
            {
                "subject": "Chinese subject",
                "body_html": (
                    '<p><strong>你好，团队</strong></p>'
                    '<p><img src="/api/posts/msg-1/images/1"><img src="/api/posts/msg-1/images/2"></p>'
                ),
            },
            "zh-Hans",
        )

        payload = graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(report["translation_target_language"], "zh-Hans")
        self.assertEqual(payload["subject"], "Chinese subject")
        self.assertIn("<strong>你好，团队</strong>", body)
        self.assertNotIn("<strong>Full formatted body</strong>", body)
        self.assertIn('src="../hostedContents/1/$value"', body)
        self.assertIn('src="../hostedContents/2/$value"', body)
        attachment_id = payload["attachments"][0]["id"]
        uuid.UUID(attachment_id)
        self.assertIn(f'<attachment id="{attachment_id}"></attachment>', body)
        self.assertEqual(len(payload["hostedContents"]), 2)
        self.assertEqual(payload["attachments"][0]["contentType"], "reference")

    async def test_translated_repost_aborts_without_fallback_when_graph_rejects_hosted_content(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(fail_inline_post=True)
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        with self.assertRaises(GraphAPIError):
            await repost_translated_message(
                parsed,
                DestinationChannel("dest-team", "dest-channel"),
                graph,
                settings,
                {
                    "subject": "Chinese subject",
                    "body_html": (
                        '<p><img src="/api/flows/forward/posts/msg-1/images/1">'
                        '<img src="/api/flows/forward/posts/msg-1/images/2"></p>'
                    ),
                },
                "zh-Hans",
            )

        self.assertEqual(len(graph.created_payloads), 1)
        self.assertIn("hostedContents", graph.created_payloads[0])
        self.assertNotIn("Open embedded image", graph.created_payloads[0]["body"]["content"])
        self.assertNotIn("Open original message for embedded image", graph.created_payloads[0]["body"]["content"])

    async def test_translated_repost_omits_oversized_inline_image_and_posts_degraded_body(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")
        graph = InlineImageGraph(image_bytes_by_id={"image-1": b"x" * (3 * 1024 * 1024)})
        parsed = TeamsMessageLink(
            tenant_id=None,
            team_id="source-team",
            source_channel_thread_id="source-channel",
            message_id="msg-1",
            parent_message_id=None,
            raw_url="https://teams.microsoft.com/source",
        )

        report = await repost_translated_message(
            parsed,
            DestinationChannel("dest-team", "dest-channel"),
            graph,
            settings,
            {
                "subject": "Chinese subject",
                "body_html": (
                    '<p><img src="/api/flows/forward/posts/msg-1/images/1">'
                    '<img src="/api/flows/forward/posts/msg-1/images/2"></p>'
                ),
            },
            "zh-Hans",
        )

        payload = graph.created_payloads[0]
        body = payload["body"]["content"]
        self.assertEqual(report["translation_target_language"], "zh-Hans")
        self.assertEqual(len(payload["hostedContents"]), 1)
        self.assertEqual(payload["hostedContents"][0]["@microsoft.graph.temporaryId"], "2")
        self.assertIn("Embedded image 1 omitted from this repost", body)
        self.assertIn('src="../hostedContents/2/$value"', body)
        self.assertEqual(report["inline_image_statuses"][1]["status"], "omitted_inline_too_large")


if __name__ == "__main__":
    unittest.main()
