import unittest

from message_rebuilder import (
    HostedContentUpload,
    ReferenceAttachment,
    append_attachment_placeholders,
    build_channel_message_payload,
    build_forward_body,
    find_hosted_content_refs,
    replace_hosted_content_refs,
    replace_display_image_refs,
    replace_hosted_content_with_temporary_refs,
    sanitize_body_html_for_display,
    strip_attachment_placeholders,
)
from teams_url_parser import TeamsMessageLink


class MessageRebuilderTests(unittest.TestCase):
    def test_builds_chinese_repost_header_with_original_title_link(self) -> None:
        body = build_forward_body(
            {
                "subject": "What should AI not change? 🧩",
                "webUrl": "https://teams/source",
                "from": {"user": {"displayName": "RuchiraSrivastava - M Moser Associates"}},
            },
            TeamsMessageLink(
                tenant_id=None,
                team_id="team-1",
                source_channel_thread_id="channel-1",
                message_id="msg-1",
                parent_message_id=None,
            ),
            "<p>Translated body</p>",
            [],
            ["Inline hosted images will be attempted in the repost."],
        )

        self.assertIn("<strong>原文作者：</strong> RuchiraSrivastava - M Moser Associates", body)
        self.assertIn('<strong>原文連結：</strong> <a href="https://teams/source">What should AI not change? 🧩</a>', body)
        self.assertNotIn("Forwarded from", body)
        self.assertNotIn("Original time", body)
        self.assertNotIn("Open in Teams", body)
        self.assertNotIn("Forwarding notes", body)
        self.assertNotIn("Inline hosted images", body)
        self.assertIn("<hr><p>Translated body</p>", body)

    def test_finds_hosted_content_refs_in_order(self) -> None:
        refs = find_hosted_content_refs(
            '<p>one<img src="https://graph.microsoft.com/v1.0/teams/t/channels/c/messages/m/hostedContents/id-1/$value">'
            '<img alt="two" src="../hostedContents/id-2/$value"></p>'
        )

        self.assertEqual([ref.occurrence for ref in refs], [1, 2])
        self.assertEqual([ref.hosted_content_id for ref in refs], ["id-1", "id-2"])

    def test_replaces_hosted_content_refs_with_temporary_ids_and_plain_mentions(self) -> None:
        rewritten = replace_hosted_content_with_temporary_refs(
            '<p><at id="0">Alex</at><img width="10" src="../hostedContents/original-id/$value"></p>',
            [
                HostedContentUpload(
                    occurrence=1,
                    original_id="original-id",
                    temporary_id="1",
                    content_type="image/png",
                    content_bytes=b"png",
                )
            ],
        )

        self.assertIn("<span>Alex</span>", rewritten)
        self.assertIn('src="../hostedContents/1/$value"', rewritten)
        self.assertNotIn("<at", rewritten)

    def test_replaces_hosted_content_refs_with_uploads(self) -> None:
        rewritten = replace_hosted_content_refs(
            '<p><img src="../hostedContents/image-1/$value"><img src="../hostedContents/image-2/$value"></p>',
            [
                HostedContentUpload(
                    occurrence=1,
                    original_id="image-1",
                    temporary_id="1",
                    content_type="image/png",
                    content_bytes=b"png",
                )
            ],
        )

        self.assertIn('src="../hostedContents/1/$value"', rewritten)
        self.assertIn('src="../hostedContents/image-2/$value"', rewritten)

    def test_replaces_cached_display_image_refs_with_uploads(self) -> None:
        rewritten = replace_display_image_refs(
            '<p><img src="/api/posts/msg-1/images/1"><img src="/api/posts/msg-1/images/2"></p>',
            [
                HostedContentUpload(
                    occurrence=1,
                    original_id="image-1",
                    temporary_id="1",
                    content_type="image/png",
                    content_bytes=b"png",
                )
            ],
        )

        self.assertIn('src="../hostedContents/1/$value"', rewritten)
        self.assertIn('src="/api/posts/msg-1/images/2"', rewritten)

    def test_replaces_flow_display_image_refs(self) -> None:
        rewritten = replace_display_image_refs(
            '<p><img src="/api/flows/reverse/posts/msg-1/images/1"></p>',
            [
                HostedContentUpload(
                    occurrence=1,
                    original_id="image-1",
                    temporary_id="1",
                    content_type="image/png",
                    content_bytes=b"png",
                )
            ],
        )

        self.assertIn('src="../hostedContents/1/$value"', rewritten)

    def test_strips_attachment_placeholders(self) -> None:
        rewritten = strip_attachment_placeholders('<p>before</p><attachment id="file-1"></attachment><p>after</p>')

        self.assertEqual(rewritten, "<p>before</p><p>after</p>")

    def test_appends_native_attachment_placeholders_and_payload(self) -> None:
        attachments = [
            ReferenceAttachment(
                id="attachment-1",
                name="source.docx",
                content_url="https://contoso.sharepoint.com/source.docx",
            )
        ]

        body = append_attachment_placeholders("<p>Translated body</p>", attachments)
        payload = build_channel_message_payload("Forwarded: Source", body, attachments=attachments)

        self.assertIn('<attachment id="attachment-1"></attachment>', body)
        self.assertEqual(
            payload["attachments"],
            [
                {
                    "id": "attachment-1",
                    "contentType": "reference",
                    "contentUrl": "https://contoso.sharepoint.com/source.docx",
                    "name": "source.docx",
                }
            ],
        )

    def test_sanitizes_display_html_and_rewrites_hosted_image_sources(self) -> None:
        sanitized = sanitize_body_html_for_display(
            '<script>alert(1)</script><p onclick="bad()"><strong>Hello</strong> '
            '<a href="javascript:alert(1)">bad</a>'
            '<img onerror="bad()" width="240" style="width: 240px; position: absolute" '
            'src="../hostedContents/image-1/$value"></p>'
            '<attachment id="file-1"></attachment>',
            lambda ref: f"/api/posts/msg-1/images/{ref.occurrence}",
        )

        self.assertIn("<strong>Hello</strong>", sanitized)
        self.assertIn('src="/api/posts/msg-1/images/1"', sanitized)
        self.assertIn('width="240"', sanitized)
        self.assertIn('style="width: 240px"', sanitized)
        self.assertNotIn("script", sanitized)
        self.assertNotIn("onclick", sanitized)
        self.assertNotIn("javascript", sanitized)
        self.assertNotIn("attachment", sanitized)


if __name__ == "__main__":
    unittest.main()
