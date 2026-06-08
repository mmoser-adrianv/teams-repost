import json
import tempfile
import unittest
from pathlib import Path

from post_cache import PostCache


class PostCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_path = Path(self.temp_dir.name) / "post-cache.json"
        self.cache = PostCache(self.cache_path)

    def test_upsert_posts_dedupes_and_sorts_newest_first(self) -> None:
        first = {"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z", "subject": "Old"}
        newer = {"id": "msg-2", "created_date_time": "2026-06-05T01:00:00Z", "subject": "New"}

        self.cache.upsert_posts("team-1", "channel-1", [first])
        result = self.cache.upsert_posts("team-1", "channel-1", [newer, first])

        posts = self.cache.list_posts("team-1", "channel-1")
        self.assertEqual(result["new_posts_saved"], 1)
        self.assertEqual([post["id"] for post in posts], ["msg-2", "msg-1"])
        self.assertIn("saved_at", posts[0])

    def test_sources_are_isolated(self) -> None:
        self.cache.upsert_posts("team-1", "channel-1", [{"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z"}])
        self.cache.upsert_posts("team-1", "channel-2", [{"id": "msg-2", "created_date_time": "2026-06-04T02:00:00Z"}])

        self.assertEqual([post["id"] for post in self.cache.list_posts("team-1", "channel-1")], ["msg-1"])
        self.assertEqual([post["id"] for post in self.cache.list_posts("team-1", "channel-2")], ["msg-2"])

    def test_pages_cached_posts(self) -> None:
        self.cache.upsert_posts(
            "team-1",
            "channel-1",
            [
                {"id": "msg-3", "created_date_time": "2026-06-06T01:00:00Z"},
                {"id": "msg-2", "created_date_time": "2026-06-05T01:00:00Z"},
                {"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z"},
            ],
        )

        page = self.cache.page_posts("team-1", "channel-1", offset=1, limit=1)

        self.assertEqual([post["id"] for post in page["posts"]], ["msg-2"])
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["total"], 3)

    def test_invalid_json_fails_clearly(self) -> None:
        self.cache_path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.cache.list_posts("team-1", "channel-1")

    def test_saved_file_is_json_object(self) -> None:
        self.cache.upsert_posts("team-1", "channel-1", [{"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z"}])

        data = json.loads(self.cache_path.read_text(encoding="utf-8"))

        self.assertIn("sources", data)
        self.assertIn("team-1|channel-1", data["sources"])

    def test_upsert_translation_saves_without_overwriting_original_content(self) -> None:
        self.cache.upsert_posts(
            "team-1",
            "channel-1",
            [{"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z", "subject": "Original", "body_html": "<p>Hello</p>"}],
        )

        self.cache.upsert_translation(
            "team-1",
            "channel-1",
            "msg-1",
            "zh-Hans",
            {"subject": "Translated", "body_html": "<p>Ni hao</p>", "body_preview": "Ni hao"},
        )

        post = self.cache.get_post("team-1", "channel-1", "msg-1")
        self.assertEqual(post["subject"], "Original")
        self.assertEqual(post["body_html"], "<p>Hello</p>")
        self.assertEqual(post["translations"]["zh-Hans"]["subject"], "Translated")

    def test_post_refresh_preserves_saved_translations(self) -> None:
        self.cache.upsert_posts(
            "team-1",
            "channel-1",
            [{"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z", "subject": "Original", "body_html": "<p>Hello</p>"}],
        )
        self.cache.upsert_translation(
            "team-1",
            "channel-1",
            "msg-1",
            "zh-Hans",
            {"subject": "Translated", "body_html": "<p>Ni hao</p>", "body_preview": "Ni hao"},
        )

        self.cache.upsert_posts(
            "team-1",
            "channel-1",
            [{"id": "msg-1", "created_date_time": "2026-06-04T01:00:00Z", "subject": "Refreshed", "body_html": "<p>Hello again</p>"}],
        )

        post = self.cache.get_post("team-1", "channel-1", "msg-1")
        self.assertEqual(post["subject"], "Refreshed")
        self.assertEqual(post["translations"]["zh-Hans"]["body_preview"], "Ni hao")


if __name__ == "__main__":
    unittest.main()
