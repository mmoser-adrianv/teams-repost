from __future__ import annotations

from typing import Any
from urllib.parse import quote

from graph_client import GraphClient


class ReplyGraph:
    def __init__(self, graph: GraphClient) -> None:
        self.graph = graph

    async def list_replies(self, team_id: str, channel_id: str, parent_message_id: str) -> list[dict[str, Any]]:
        path: str | None = self._replies_path(team_id, channel_id, parent_message_id)
        params: dict[str, str] | None = {"$top": "50"}
        replies: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        while path:
            if path in seen_pages:
                raise ValueError("Microsoft Graph returned a repeated replies nextLink")
            seen_pages.add(path)
            page = await self.graph.get_json(path, params=params)
            values = page.get("value", [])
            if not isinstance(values, list):
                raise ValueError("Microsoft Graph replies response did not contain an array")
            replies.extend(value for value in values if isinstance(value, dict))
            path = page.get("@odata.nextLink")
            params = None
        return replies

    async def create_reply(
        self,
        team_id: str,
        channel_id: str,
        parent_message_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.graph.post_json(self._replies_path(team_id, channel_id, parent_message_id), payload)

    async def get_reply(
        self,
        team_id: str,
        channel_id: str,
        parent_message_id: str,
        reply_id: str,
    ) -> dict[str, Any]:
        return await self.graph.get_message(team_id, channel_id, reply_id, parent_message_id)

    async def get_root_message(self, team_id: str, channel_id: str, message_id: str) -> dict[str, Any]:
        return await self.graph.get_message(team_id, channel_id, message_id)

    async def list_hosted_contents(
        self,
        team_id: str,
        channel_id: str,
        parent_message_id: str,
        reply_id: str,
    ) -> list[dict[str, Any]]:
        return await self.graph.get_message_hosted_contents(team_id, channel_id, reply_id, parent_message_id)

    async def download_hosted_content(
        self,
        team_id: str,
        channel_id: str,
        parent_message_id: str,
        reply_id: str,
        hosted_content_id: str,
    ) -> tuple[bytes, str]:
        return await self.graph.download_message_hosted_content(
            team_id,
            channel_id,
            reply_id,
            hosted_content_id,
            parent_message_id,
        )

    @staticmethod
    def _replies_path(team_id: str, channel_id: str, parent_message_id: str) -> str:
        return (
            f"/teams/{quote(team_id, safe='')}/channels/{quote(channel_id, safe='')}"
            f"/messages/{quote(parent_message_id, safe='')}/replies"
        )
