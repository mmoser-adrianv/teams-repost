import unittest

import httpx

from graph_client import GraphAPIError, GraphClient, encode_sharing_url


class FakeResponse:
    def __init__(self, status_code: int, json_body=None, headers=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}
        self.content = content
        self.text = "" if json_body is not None else content.decode("utf-8", errors="replace")

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class FakeAsyncClient:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers, "kwargs": kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        pass


class GraphClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_retryable_graph_response(self) -> None:
        fake = FakeAsyncClient(
            [
                FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}),
                FakeResponse(200, {"value": [{"id": "team-1", "displayName": "Web App Ideas"}]}),
            ]
        )
        client = GraphClient("secret-token", http_client=fake, max_retries=1)

        team = await client.get_team_by_name("Web App Ideas")

        self.assertEqual(team["id"], "team-1")
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("Bearer secret-token", fake.calls[0]["headers"]["Authorization"])

    async def test_retries_network_timeout(self) -> None:
        request = httpx.Request("GET", "https://graph.example/replies")
        fake = FakeAsyncClient(
            [
                httpx.ReadTimeout("timed out", request=request),
                FakeResponse(200, {"value": []}),
            ]
        )
        client = GraphClient("token", http_client=fake, max_retries=1)

        result = await client.get_json("/replies")

        self.assertEqual(result, {"value": []})
        self.assertEqual(len(fake.calls), 2)

    async def test_exhausted_network_timeout_becomes_graph_error(self) -> None:
        request = httpx.Request("GET", "https://graph.example/replies")
        fake = FakeAsyncClient(
            [
                httpx.ReadTimeout("timed out", request=request),
                httpx.ReadTimeout("timed out", request=request),
            ]
        )
        client = GraphClient("token", http_client=fake, max_retries=1)

        with self.assertRaises(GraphAPIError) as context:
            await client.get_json("/replies")

        self.assertEqual(context.exception.status_code, 503)
        self.assertIn("ReadTimeout", str(context.exception))

    async def test_encodes_channel_path_when_creating_message(self) -> None:
        fake = FakeAsyncClient([FakeResponse(201, {"id": "new-message"})])
        client = GraphClient("token", http_client=fake)

        await client.create_channel_message("team-1", "19:abc@thread.tacv2", {"body": {"content": "Hello"}})

        self.assertIn("/teams/team-1/channels/19%3Aabc%40thread.tacv2/messages", fake.calls[0]["url"])

    async def test_lists_channel_messages_with_top_limit(self) -> None:
        fake = FakeAsyncClient([FakeResponse(200, {"value": [{"id": "msg-1"}]})])
        client = GraphClient("token", http_client=fake)

        messages = await client.list_channel_messages("team-1", "19:abc@thread.tacv2", top=10)

        self.assertEqual(messages, [{"id": "msg-1"}])
        self.assertEqual(fake.calls[0]["kwargs"]["params"], {"$top": "10"})

    async def test_lists_channel_messages_page_with_next_link(self) -> None:
        fake = FakeAsyncClient([FakeResponse(200, {"value": [{"id": "msg-1"}], "@odata.nextLink": "https://graph.example/next"})])
        client = GraphClient("token", http_client=fake)

        page = await client.list_channel_messages_page("team-1", "19:abc@thread.tacv2", top=10)

        self.assertEqual(page, {"messages": [{"id": "msg-1"}], "next_link": "https://graph.example/next"})
        self.assertEqual(fake.calls[0]["kwargs"]["params"], {"$top": "10"})

    async def test_lists_channel_messages_page_from_next_url(self) -> None:
        fake = FakeAsyncClient([FakeResponse(200, {"value": [{"id": "msg-2"}]})])
        client = GraphClient("token", http_client=fake)

        page = await client.list_channel_messages_page("team-1", "19:abc@thread.tacv2", top=10, page_url="https://graph.example/next")

        self.assertEqual(page, {"messages": [{"id": "msg-2"}], "next_link": None})
        self.assertEqual(fake.calls[0]["url"], "https://graph.example/next")
        self.assertIsNone(fake.calls[0]["kwargs"]["params"])

    async def test_gets_full_channel_messages_by_id(self) -> None:
        fake = FakeAsyncClient(
            [
                FakeResponse(200, {"id": "msg-1", "body": {"content": "<p>Full one</p>"}}),
                FakeResponse(200, {"id": "msg-2", "body": {"content": "<p>Full two</p>"}}),
            ]
        )
        client = GraphClient("token", http_client=fake)

        messages = await client.get_channel_messages("team-1", "19:abc@thread.tacv2", ["msg-1", "msg-2"])

        self.assertEqual([message["id"] for message in messages], ["msg-1", "msg-2"])
        self.assertIn("/messages/msg-1", fake.calls[0]["url"])
        self.assertIn("/messages/msg-2", fake.calls[1]["url"])

    async def test_multiple_named_matches_fail_safely(self) -> None:
        fake = FakeAsyncClient(
            [
                FakeResponse(
                    200,
                    {"value": [{"id": "1", "displayName": "Test"}, {"id": "2", "displayName": "Test"}]},
                )
            ]
        )
        client = GraphClient("token", http_client=fake)

        with self.assertRaises(GraphAPIError) as context:
            await client.get_team_by_name("Test")
        self.assertEqual(context.exception.status_code, 409)

    def test_encodes_sharing_urls(self) -> None:
        encoded = encode_sharing_url("https://contoso.sharepoint.com/:w:/r/sites/site/file.docx?d=abc")
        self.assertTrue(encoded.startswith("u!"))
        self.assertNotIn("=", encoded)
        self.assertNotIn("/", encoded)


if __name__ == "__main__":
    unittest.main()
