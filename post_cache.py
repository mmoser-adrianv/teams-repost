from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any


class PostCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_posts(self, source_team_id: str, source_channel_id: str) -> list[dict[str, Any]]:
        source = self._source(self._load(), source_team_id, source_channel_id, create=False)
        return deepcopy(source.get("posts", [])) if source else []

    def get_post(self, source_team_id: str, source_channel_id: str, message_id: str) -> dict[str, Any] | None:
        for post in self.list_posts(source_team_id, source_channel_id):
            if post.get("id") == message_id:
                return post
        return None

    def has_posts(self, source_team_id: str, source_channel_id: str) -> bool:
        return bool(self.list_posts(source_team_id, source_channel_id))

    def newest_message_id(self, source_team_id: str, source_channel_id: str) -> str | None:
        posts = self.list_posts(source_team_id, source_channel_id)
        return posts[0].get("id") if posts else None

    def source_status(self, source_team_id: str, source_channel_id: str) -> dict[str, Any]:
        source = self._source(self._load(), source_team_id, source_channel_id, create=False) or {}
        return {
            "last_refreshed_at": source.get("last_refreshed_at"),
            "post_count": len(source.get("posts", [])),
        }

    def upsert_posts(self, source_team_id: str, source_channel_id: str, posts: list[dict[str, Any]]) -> dict[str, Any]:
        data = self._load()
        source = self._source(data, source_team_id, source_channel_id, create=True)
        refreshed_at = datetime.now(UTC).isoformat()
        existing_by_id = {post.get("id"): post for post in source.get("posts", []) if post.get("id")}
        new_posts_saved = 0

        for post in posts:
            message_id = post.get("id")
            if not message_id:
                continue
            existing = existing_by_id.get(message_id)
            cached_post = deepcopy(post)
            cached_post["saved_at"] = (existing or {}).get("saved_at") or refreshed_at
            if existing and existing.get("translations") and not cached_post.get("translations"):
                cached_post["translations"] = deepcopy(existing["translations"])
            if existing is None:
                new_posts_saved += 1
            existing_by_id[message_id] = cached_post

        source["posts"] = sorted(existing_by_id.values(), key=_post_sort_key, reverse=True)
        source["last_refreshed_at"] = refreshed_at
        self._save(data)
        return {"new_posts_saved": new_posts_saved, "last_refreshed_at": refreshed_at}

    def page_posts(
        self,
        source_team_id: str,
        source_channel_id: str,
        offset: int,
        limit: int,
        excluded_author_emails: set[str] | None = None,
        excluded_body_prefixes: tuple[str, ...] = (),
        exclude_unpresentable: bool = True,
    ) -> dict[str, Any]:
        posts = self.list_posts(source_team_id, source_channel_id)
        if exclude_unpresentable:
            posts = [post for post in posts if is_presentable_post(post)]
        if excluded_author_emails:
            posts = [post for post in posts if (post.get("author_email") or "").lower() not in excluded_author_emails]
        if excluded_body_prefixes:
            posts = [post for post in posts if not _cached_body_text(post).startswith(excluded_body_prefixes)]
        safe_offset = max(0, offset)
        safe_limit = max(1, limit)
        page = posts[safe_offset : safe_offset + safe_limit]
        next_offset = safe_offset + safe_limit
        return {
            "posts": page,
            "total": len(posts),
            "next_offset": next_offset if next_offset < len(posts) else None,
        }

    def upsert_translation(
        self,
        source_team_id: str,
        source_channel_id: str,
        message_id: str,
        target_language: str,
        translation: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._load()
        source = self._source(data, source_team_id, source_channel_id, create=False)
        if not source:
            raise KeyError(message_id)

        for post in source["posts"]:
            if post.get("id") != message_id:
                continue
            translations = post.setdefault("translations", {})
            if not isinstance(translations, dict):
                translations = {}
                post["translations"] = translations
            translations[target_language] = deepcopy(translation)
            self._save(data)
            return deepcopy(post)

        raise KeyError(message_id)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sources": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Post cache file is not valid JSON: {self.path}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Post cache file must contain a JSON object: {self.path}")
        sources = data.get("sources")
        if sources is None:
            data["sources"] = {}
        elif not isinstance(sources, dict):
            raise ValueError(f"Post cache 'sources' must be an object: {self.path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)

    def _source(self, data: dict[str, Any], source_team_id: str, source_channel_id: str, create: bool) -> dict[str, Any] | None:
        key = source_key(source_team_id, source_channel_id)
        sources = data.setdefault("sources", {})
        source = sources.get(key)
        if source is None and create:
            source = {
                "source": {"team_id": source_team_id, "channel_id": source_channel_id},
                "last_refreshed_at": None,
                "posts": [],
            }
            sources[key] = source
        if source is not None:
            posts = source.get("posts")
            if posts is None:
                source["posts"] = []
            elif not isinstance(posts, list):
                raise ValueError(f"Post cache source posts must be an array: {self.path}")
        return source


def source_key(source_team_id: str, source_channel_id: str) -> str:
    return f"{source_team_id}|{source_channel_id}"


def _post_sort_key(post: dict[str, Any]) -> str:
    return str(post.get("created_date_time") or post.get("saved_at") or "")


def _cached_body_text(post: dict[str, Any]) -> str:
    body_html = str(post.get("body_html") or "")
    body_preview = str(post.get("body_preview") or "")
    text = re.sub(r"<[^>]+>", " ", body_html) if body_html else body_preview
    return re.sub(r"[\s\u00a0]+", " ", unescape(text)).strip()


def is_presentable_post(post: dict[str, Any]) -> bool:
    return bool(_cached_body_text(post) or post.get("attachments") or post.get("embedded_images"))
