from __future__ import annotations

from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from .models import ResourceCatalogue, ResourceSubmission, ResourceSubmissionResult


class ResourceCatalogueError(RuntimeError):
    """Base class for errors safe to translate at the HTTP boundary."""


class ResourceCatalogueTimeout(ResourceCatalogueError):
    pass


class ResourceCatalogueTransportError(ResourceCatalogueError):
    pass


class ResourceCatalogueResponseError(ResourceCatalogueError):
    pass


class ResourceCatalogueClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "ResourceCatalogueClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_catalogue(self) -> ResourceCatalogue:
        response = await self._request("GET", f"{self.base_url}/resources.json")
        payload = self._response_json(response)
        try:
            return ResourceCatalogue.model_validate(payload)
        except ValidationError as exc:
            raise ResourceCatalogueResponseError("The resource catalogue response did not match schema version 1") from exc

    async def submit_resource(
        self,
        resource: ResourceSubmission,
        bearer_token: str,
    ) -> ResourceSubmissionResult:
        token = bearer_token.strip()
        if not token:
            raise ResourceCatalogueResponseError("The resource catalogue bearer token is not configured")
        response = await self._request(
            "POST",
            f"{self.base_url}/resources",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=resource.model_dump(mode="json"),
        )
        payload = self._response_json(response)
        try:
            return TypeAdapter(ResourceSubmissionResult).validate_python(payload)
        except ValidationError as exc:
            raise ResourceCatalogueResponseError("The resource submission response was not recognized") from exc

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ResourceCatalogueTimeout("The resource catalogue request timed out") from exc
        except httpx.RequestError as exc:
            raise ResourceCatalogueTransportError("The resource catalogue could not be reached") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ResourceCatalogueResponseError(
                f"The resource catalogue returned HTTP {response.status_code}"
            )
        return response

    @staticmethod
    def _response_json(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ResourceCatalogueResponseError("The resource catalogue returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ResourceCatalogueResponseError("The resource catalogue returned an invalid JSON document")
        return payload
