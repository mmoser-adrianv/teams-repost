import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

import resource_catalog.router as catalog_router
from resource_catalog.client import (
    ResourceCatalogueClient,
    ResourceCatalogueResponseError,
    ResourceCatalogueTimeout,
)
from resource_catalog.models import (
    ResourceCatalogue,
    ResourceSubmission,
    ResourceSubmissionResult,
)
from resource_catalog.router import create_resource_catalogue_router
from resource_catalog.state import ResourceCatalogueState


RESOURCE = {
    "id": "resource-1",
    "url": "https://example.com/resources/one",
    "name": "Example Agent",
    "description": "A useful example.",
    "type": "workspace_agent",
    "author": "Example Author",
    "submitted_at": "2026-07-20T06:00:00.000Z",
}
CATALOGUE = {
    "schema_version": 1,
    "updated_at": "2026-07-20T06:00:00.000Z",
    "resource_count": 1,
    "resources": [RESOURCE],
}
SUBMISSION = {
    "url": RESOURCE["url"],
    "name": RESOURCE["name"],
    "description": RESOURCE["description"],
    "type": RESOURCE["type"],
    "author": RESOURCE["author"],
}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ResourceModelTests(unittest.TestCase):
    def test_accepts_all_supported_types_without_normalizing_url(self):
        for resource_type in ("workspace_agent", "plugin", "skill"):
            payload = ResourceSubmission(**{**SUBMISSION, "type": resource_type})
            self.assertEqual(payload.type, resource_type)
            self.assertEqual(payload.url, SUBMISSION["url"])

    def test_rejects_invalid_type_missing_fields_and_non_http_url(self):
        with self.assertRaises(ValidationError):
            ResourceSubmission(**{**SUBMISSION, "type": "tool"})
        with self.assertRaises(ValidationError):
            ResourceSubmission(**{key: value for key, value in SUBMISSION.items() if key != "author"})
        with self.assertRaises(ValidationError):
            ResourceSubmission(**{**SUBMISSION, "url": "javascript:alert(1)"})


class ResourceCatalogueClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_validates_catalogue_and_sends_no_authorization(self):
        http = FakeHttpClient([FakeResponse(200, CATALOGUE)])
        client = ResourceCatalogueClient("https://catalog.example", 1, http_client=http)

        result = await client.fetch_catalogue()

        self.assertEqual(result.resource_count, 1)
        self.assertEqual(http.calls[0]["method"], "GET")
        self.assertEqual(http.calls[0]["url"], "https://catalog.example/resources.json")
        self.assertNotIn("headers", http.calls[0])

    async def test_post_sends_exact_payload_and_bearer_token(self):
        response = {"status": "success", "resource": RESOURCE, "feed_url": "/resources.json"}
        http = FakeHttpClient([FakeResponse(200, response)])
        client = ResourceCatalogueClient("https://catalog.example", 1, http_client=http)

        result = await client.submit_resource(ResourceSubmission(**SUBMISSION), "secret-token")

        self.assertEqual(result.status, "success")
        self.assertEqual(http.calls[0]["json"], SUBMISSION)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer secret-token")

    async def test_failed_status_is_parsed_even_when_http_status_is_200(self):
        http = FakeHttpClient([FakeResponse(200, {"status": "failed", "error": "Bad field"})])
        client = ResourceCatalogueClient("https://catalog.example", 1, http_client=http)

        result = await client.submit_resource(ResourceSubmission(**SUBMISSION), "token")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "Bad field")

    async def test_exists_response_accepts_reference_only(self):
        body = {
            "status": "exists",
            "resource": {"id": "resource-1", "url": RESOURCE["url"]},
            "feed_url": "/resources.json",
        }
        http = FakeHttpClient([FakeResponse(200, body)])
        client = ResourceCatalogueClient("https://catalog.example", 1, http_client=http)

        result = await client.submit_resource(ResourceSubmission(**SUBMISSION), "token")

        self.assertEqual(result.status, "exists")
        self.assertEqual(result.resource.id, "resource-1")

    async def test_rejects_count_mismatch_and_invalid_json(self):
        mismatch = {**CATALOGUE, "resource_count": 2}
        for response in (FakeResponse(200, mismatch), FakeResponse(200, ValueError("not json"))):
            client = ResourceCatalogueClient(
                "https://catalog.example", 1, http_client=FakeHttpClient([response])
            )
            with self.assertRaises(ResourceCatalogueResponseError):
                await client.fetch_catalogue()

    async def test_maps_timeout_without_exposing_token(self):
        request = httpx.Request("POST", "https://catalog.example/resources")
        http = FakeHttpClient([httpx.ReadTimeout("secret-token", request=request)])
        client = ResourceCatalogueClient("https://catalog.example", 1, http_client=http)

        with self.assertRaises(ResourceCatalogueTimeout) as context:
            await client.submit_resource(ResourceSubmission(**SUBMISSION), "secret-token")

        self.assertNotIn("secret-token", str(context.exception))


class ResourceCatalogueStateTests(unittest.TestCase):
    def test_compares_updated_at_and_persists_change(self):
        with tempfile.TemporaryDirectory() as folder:
            state = ResourceCatalogueState(Path(folder) / "state.json")
            first = ResourceCatalogue.model_validate(CATALOGUE)
            changed = ResourceCatalogue.model_validate(
                {**CATALOGUE, "updated_at": "2026-07-20T07:00:00.000Z"}
            )

            self.assertTrue(state.record(first))
            self.assertFalse(state.record(first))
            self.assertTrue(state.record(changed))


class FakeCatalogueClient:
    def __init__(self, catalogue, submission):
        self.catalogue = catalogue
        self.submission = submission
        self.submissions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def fetch_catalogue(self):
        return self.catalogue

    async def submit_resource(self, payload, token):
        self.submissions.append((payload, token))
        return self.submission


class ResourceCatalogueRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_auth = catalog_router.get_access_token
        self.addCleanup(lambda: setattr(catalog_router, "get_access_token", self.original_auth))
        self.settings = SimpleNamespace(
            resource_catalog_base_url="https://catalog.example",
            resource_catalog_request_timeout_seconds=1,
            resource_catalog_poll_interval_seconds=60,
            resource_catalog_api_token="server-token",
            resource_catalog_state_path=Path(self.temp.name) / "state.json",
        )
        self.catalogue = ResourceCatalogue.model_validate(CATALOGUE)
        failed_adapter = TypeAdapter(ResourceSubmissionResult)
        self.fake = FakeCatalogueClient(
            self.catalogue,
            failed_adapter.validate_python({"status": "failed", "error": "Rejected by upstream"}),
        )
        app = FastAPI()
        app.include_router(
            create_resource_catalogue_router(self.settings, client_factory=lambda: self.fake)
        )
        self.client = TestClient(app)

    def test_page_and_get_endpoint_work_without_auth(self):
        catalog_router.get_access_token = lambda request, settings: self.fail("GET should not authenticate")

        page = self.client.get("/resources")
        first = self.client.get("/api/resources")
        second = self.client.get("/api/resources")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Agents, Plugins", page.text)
        self.assertTrue(first.json()["changed"])
        self.assertFalse(second.json()["changed"])
        self.assertEqual(first.headers["cache-control"], "no-store")

    def test_post_requires_auth_and_configured_token(self):
        def unauthorized(request, settings):
            raise HTTPException(status_code=401, detail="Not signed in")

        catalog_router.get_access_token = unauthorized
        self.assertEqual(self.client.post("/api/resources", json=SUBMISSION).status_code, 401)

        catalog_router.get_access_token = lambda request, settings: "graph-token"
        self.settings.resource_catalog_api_token = None
        self.assertEqual(self.client.post("/api/resources", json=SUBMISSION).status_code, 503)
        self.assertEqual(self.fake.submissions, [])

    def test_post_preserves_failed_status_from_http_200_contract(self):
        catalog_router.get_access_token = lambda request, settings: "graph-token"

        response = self.client.post("/api/resources", json=SUBMISSION)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "failed", "error": "Rejected by upstream"})
        self.assertEqual(self.fake.submissions[0][1], "server-token")


if __name__ == "__main__":
    unittest.main()
