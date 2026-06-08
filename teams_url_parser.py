from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class TeamsUrlParseError(ValueError):
    pass


@dataclass(frozen=True)
class TeamsMessageLink:
    tenant_id: str | None
    team_id: str
    source_channel_thread_id: str
    message_id: str
    parent_message_id: str | None
    channel_name: str | None = None
    team_name: str | None = None
    created_time: str | None = None
    raw_url: str | None = None

    def to_report(self) -> dict[str, Any]:
        return asdict(self)


def parse_teams_message_url(url: str) -> TeamsMessageLink:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise TeamsUrlParseError("source_message_url must be an absolute Teams message URL")

    query = parse_qs(parsed.query, keep_blank_values=False)
    path_segments = _message_path_segments(parsed.path)
    if not path_segments and parsed.fragment:
        fragment = parsed.fragment
        if "?" in fragment:
            fragment_path, fragment_query = fragment.split("?", 1)
            query.update(parse_qs(fragment_query, keep_blank_values=False))
        else:
            fragment_path = fragment
        path_segments = _message_path_segments(fragment_path)

    channel_thread_id, message_id = _extract_message_path_ids(path_segments, query)
    team_id = _first_query(query, "groupId", "teamId")
    tenant_id = _first_query(query, "tenantId")
    parent_message_id = _first_query(query, "parentMessageId", "rootMessageId")

    if not team_id:
        raise TeamsUrlParseError("Teams message URL is missing groupId/teamId")
    if not channel_thread_id:
        raise TeamsUrlParseError("Teams message URL is missing the channel thread ID")
    if not message_id:
        raise TeamsUrlParseError("Teams message URL is missing the message ID")

    return TeamsMessageLink(
        tenant_id=tenant_id,
        team_id=team_id,
        source_channel_thread_id=channel_thread_id,
        message_id=message_id,
        parent_message_id=parent_message_id,
        channel_name=_first_query(query, "channelName"),
        team_name=_first_query(query, "teamName"),
        created_time=_first_query(query, "createdTime"),
        raw_url=url,
    )


def _message_path_segments(path: str) -> list[str]:
    return [unquote(segment) for segment in path.split("/") if segment]


def _extract_message_path_ids(segments: list[str], query: dict[str, list[str]]) -> tuple[str | None, str | None]:
    channel_thread_id = _first_query(query, "threadId", "channelId")
    message_id = _first_query(query, "messageId")

    for index, segment in enumerate(segments):
        if segment.lower() == "message":
            if index + 1 < len(segments):
                channel_thread_id = channel_thread_id or segments[index + 1]
            if index + 2 < len(segments):
                message_id = message_id or segments[index + 2]
            break

    return channel_thread_id, message_id


def _first_query(query: dict[str, list[str]], *keys: str) -> str | None:
    lowered = {key.lower(): value for key, value in query.items()}
    for key in keys:
        values = lowered.get(key.lower())
        if values:
            return unquote(values[0])
    return None
