from __future__ import annotations

import asyncio
import email.utils
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx


logger = logging.getLogger(__name__)


class GraphAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, response_body: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GraphClient:
    def __init__(
        self,
        access_token: str,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._access_token = access_token
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_team_by_name(self, display_name: str) -> dict:
        filter_value = f"displayName eq '{_odata_string(display_name)}'"
        data = await self._get_collection("/teams", params={"$filter": filter_value, "$select": "id,displayName,description,visibility"})
        return _single_named_match(data, display_name, "team")

    async def get_channel_by_name(self, team_id: str, display_name: str) -> dict:
        path = f"/teams/{_quote_segment(team_id)}/allChannels"
        data = await self._get_collection(
            path,
            params={"$filter": f"displayName eq '{_odata_string(display_name)}'", "$select": "id,displayName,membershipType,webUrl"},
        )
        return _single_named_match(data, display_name, "channel")

    async def get_channel_files_folder(self, team_id: str, channel_id: str) -> dict:
        path = f"/teams/{_quote_segment(team_id)}/channels/{_quote_segment(channel_id)}/filesFolder"
        return await self.get_json(path)

    async def get_message(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        parent_message_id: str | None = None,
    ) -> dict:
        path = self._message_path(team_id, channel_id, message_id, parent_message_id)
        return await self.get_json(path)

    async def list_channel_messages_page(self, team_id: str, channel_id: str, top: int = 25, page_url: str | None = None) -> dict:
        if page_url:
            data = await self.get_json(page_url)
        else:
            path = f"/teams/{_quote_segment(team_id)}/channels/{_quote_segment(channel_id)}/messages"
            data = await self.get_json(path, params={"$top": str(max(1, min(top, 100)))})
        return {"messages": data.get("value", []), "next_link": data.get("@odata.nextLink")}

    async def list_channel_messages(self, team_id: str, channel_id: str, top: int = 25) -> list[dict]:
        path = f"/teams/{_quote_segment(team_id)}/channels/{_quote_segment(channel_id)}/messages"
        data = await self.get_json(path, params={"$top": str(max(1, min(top, 100)))})
        return data.get("value", [])

    async def get_channel_messages(self, team_id: str, channel_id: str, message_ids: list[str]) -> list[dict]:
        messages: list[dict] = []
        for message_id in message_ids:
            messages.append(await self.get_message(team_id, channel_id, message_id))
        return messages

    async def get_message_hosted_contents(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        parent_message_id: str | None = None,
    ) -> list[dict]:
        path = self._message_path(team_id, channel_id, message_id, parent_message_id) + "/hostedContents"
        return await self._get_collection(path)

    async def download_message_hosted_content(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        hosted_content_id: str,
        parent_message_id: str | None = None,
    ) -> tuple[bytes, str]:
        path = (
            self._message_path(team_id, channel_id, message_id, parent_message_id)
            + f"/hostedContents/{_quote_segment(hosted_content_id)}/$value"
        )
        response = await self.request("GET", path)
        return response.content, response.headers.get("content-type", "application/octet-stream").split(";")[0]

    async def create_channel_message(self, team_id: str, channel_id: str, payload: dict) -> dict:
        path = f"/teams/{_quote_segment(team_id)}/channels/{_quote_segment(channel_id)}/messages"
        return await self.post_json(path, payload)

    async def get_drive_item_from_share_url(self, content_url: str) -> dict:
        share_id = encode_sharing_url(content_url)
        return await self.get_json(f"/shares/{share_id}/driveItem")

    async def download_drive_item_from_share_url(self, content_url: str) -> tuple[bytes, str]:
        share_id = encode_sharing_url(content_url)
        response = await self.request("GET", f"/shares/{share_id}/driveItem/content")
        return response.content, response.headers.get("content-type", "application/octet-stream").split(";")[0]

    async def upload_file_to_channel_folder(
        self,
        files_folder: dict,
        file_name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        conflict_behavior: str = "fail",
    ) -> dict:
        parent = files_folder.get("parentReference") or {}
        drive_id = parent.get("driveId")
        folder_id = files_folder.get("id")
        if not drive_id or not folder_id:
            raise GraphAPIError(400, "Destination channel filesFolder did not include driveId and folder id")

        encoded_name = quote(file_name, safe="")
        path = (
            f"/drives/{_quote_segment(drive_id)}/items/{_quote_segment(folder_id)}:"
            f"/{encoded_name}:/content?@microsoft.graph.conflictBehavior={quote(conflict_behavior, safe='')}"
        )
        response = await self.request(
            "PUT",
            path,
            content=content,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )
        return response.json()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = await self.request("GET", path, params=params)
        return response.json()

    async def post_json(self, path: str, payload: dict) -> dict:
        response = await self.request("POST", path, json=payload, headers={"Content-Type": "application/json"})
        return response.json()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {}) or {}
        headers = {"Authorization": f"Bearer {self._access_token}", **headers}
        url = path if path.startswith("https://") else self.base_url + path

        attempt = 0
        while True:
            response = await self._client.request(method, url, headers=headers, **kwargs)
            if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= self.max_retries:
                break
            delay = _retry_delay(response, attempt)
            logger.info("Retrying Microsoft Graph request", extra={"method": method, "status_code": response.status_code, "delay_seconds": delay})
            await asyncio.sleep(delay)
            attempt += 1

        if response.status_code >= 400:
            raise GraphAPIError(response.status_code, _graph_error_message(response), _safe_response_body(response))
        return response

    async def _get_collection(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        values: list[dict] = []
        next_path: str | None = path
        next_params = params
        while next_path:
            page = await self.get_json(next_path, params=next_params)
            values.extend(page.get("value", []))
            next_path = page.get("@odata.nextLink")
            next_params = None
        return values

    def _message_path(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        parent_message_id: str | None = None,
    ) -> str:
        base = f"/teams/{_quote_segment(team_id)}/channels/{_quote_segment(channel_id)}/messages"
        if parent_message_id and parent_message_id != message_id:
            return f"{base}/{_quote_segment(parent_message_id)}/replies/{_quote_segment(message_id)}"
        return f"{base}/{_quote_segment(message_id)}"


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def encode_sharing_url(url: str) -> str:
    import base64

    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=").replace("/", "_").replace("+", "-")


def _single_named_match(items: list[dict], display_name: str, resource_name: str) -> dict:
    matches = [item for item in items if (item.get("displayName") or "").casefold() == display_name.casefold()]
    if not matches:
        raise GraphAPIError(404, f"No {resource_name} found with displayName '{display_name}'")
    if len(matches) > 1:
        raise GraphAPIError(409, f"Multiple {resource_name}s found with displayName '{display_name}'")
    return matches[0]


def _quote_segment(value: str) -> str:
    return quote(value, safe="")


def _odata_string(value: str) -> str:
    return value.replace("'", "''")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        if retry_after.isdigit():
            return max(float(retry_after), 0.0)
        try:
            retry_time = email.utils.parsedate_to_datetime(retry_after)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=UTC)
            return max((retry_time - datetime.now(UTC)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, 8)


def _graph_error_message(response: httpx.Response) -> str:
    body = _safe_response_body(response)
    if isinstance(body, dict):
        error = body.get("error") or {}
        if error.get("message"):
            return str(error["message"])
    return f"Microsoft Graph returned HTTP {response.status_code}"


def _safe_response_body(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        text = response.text
        return text[:1000] if text else None
