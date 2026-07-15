import tempfile
import unittest
from pathlib import Path

from repost_history import RepostHistory, build_manual_repost_record, build_repost_record


class RepostHistoryTests(unittest.TestCase):
    def test_upsert_and_get_repost_record(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            history = RepostHistory(Path(folder) / "history.json")
            report = {
                "source_message_id": "msg-1",
                "source_message_web_url": "https://teams/source",
                "source_subject": "Subject",
                "source_author": "Alex",
                "source_created_date_time": "2026-06-04T01:02:03Z",
                "new_message_id": "new-1",
                "new_message_web_url": "https://teams/repost",
                "attachment_links": [{"name": "file.docx"}],
                "attachment_statuses": [{"id": "attachment-1", "status": "attached_reference"}],
                "inline_image_statuses": [{"occurrence": 1, "status": "recreated_inline"}],
                "warnings": ["note"],
            }

            record = build_repost_record("source-team", "source-channel", "dest-team", "dest-channel", report)
            history.upsert(record)

            loaded = history.get("source-team", "source-channel", "msg-1")
            self.assertEqual(loaded["destination"]["web_url"], "https://teams/repost")
            self.assertEqual(loaded["status"], "reposted")
            self.assertFalse(loaded["manual"])
            self.assertEqual(loaded["attachment_statuses"][0]["status"], "attached_reference")
            self.assertEqual(len(history.list_records()), 1)

            updated = dict(record)
            updated["warnings"] = ["updated"]
            history.upsert(updated)
            self.assertEqual(history.get("source-team", "source-channel", "msg-1")["warnings"], ["updated"])
            self.assertEqual(len(history.list_records()), 1)

    def test_translation_repost_records_are_keyed_by_target_language(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            history = RepostHistory(Path(folder) / "history.json")
            report = {
                "source_message_id": "msg-1",
                "new_message_id": "new-1",
                "new_message_web_url": "https://teams/repost",
            }

            english_record = build_repost_record("source-team", "source-channel", "dest-team", "dest-channel", report)
            chinese_record = build_repost_record(
                "source-team",
                "source-channel",
                "dest-team",
                "dest-channel",
                report,
                "zh-Hans",
                "en",
            )
            history.upsert(english_record)
            history.upsert(chinese_record)

            self.assertEqual(len(history.list_records()), 2)
            self.assertIsNone(history.get("source-team", "source-channel", "msg-2", "zh-Hans"))
            self.assertIsNotNone(history.get("source-team", "source-channel", "msg-1"))
            self.assertEqual(history.get("source-team", "source-channel", "msg-1", "zh-Hans")["translation"]["target_language"], "zh-Hans")
            self.assertEqual(history.get("source-team", "source-channel", "msg-1", "zh-Hans")["translation"]["source_language"], "en")

    def test_build_manual_repost_record(self) -> None:
        record = build_manual_repost_record(
            "source-team",
            "source-channel",
            "dest-team",
            "dest-channel",
            {
                "id": "msg-1",
                "web_url": "https://teams/source",
                "subject": "Subject",
                "author": "Alex",
                "created_date_time": "2026-06-04T01:02:03Z",
                "attachments": [{"name": "file.docx"}],
            },
            "zh-Hans",
            "en",
        )

        self.assertEqual(record["source_key"], "source-team|source-channel|msg-1|translation:zh-Hans")
        self.assertEqual(record["status"], "manually_marked")
        self.assertTrue(record["manual"])
        self.assertEqual(record["translation"]["source_language"], "en")
        self.assertIsNone(record["destination"]["message_id"])
        self.assertEqual(record["attachment_links"][0]["name"], "file.docx")


if __name__ == "__main__":
    unittest.main()
