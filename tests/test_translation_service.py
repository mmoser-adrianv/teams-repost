import json
import types
import unittest

from translation_service import (
    MAX_TRANSLATION_SEGMENTS_PER_REQUEST,
    HtmlTextDocument,
    OpenAITranslationService,
    TranslationError,
)


class FakeResponse:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text


class FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.output_text, Exception):
            raise self.output_text
        return FakeResponse(self.output_text)


class FakeClient:
    def __init__(self, output_text: str) -> None:
        self.responses = FakeResponses(output_text)


class EchoResponses:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["input"][1]["content"])
        return FakeResponse(json.dumps([f"translated:{segment}" for segment in payload["segments"]]))


class EchoClient:
    def __init__(self) -> None:
        self.responses = EchoResponses()


class SplitRetryResponses(EchoResponses):
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["input"][1]["content"])
        segments = payload["segments"]
        if len(segments) > 1:
            return FakeResponse(json.dumps(["wrong count"]))
        return FakeResponse(json.dumps([f"translated:{segments[0]}"]))


class SplitRetryClient:
    def __init__(self) -> None:
        self.responses = SplitRetryResponses()


def settings():
    return types.SimpleNamespace(
        openai_api_key="key",
        openai_translation_model="gpt-5.5",
        openai_request_timeout_seconds=60.0,
    )


class HtmlTextDocumentTests(unittest.TestCase):
    def test_reconstructs_html_without_changing_tags_links_or_images(self) -> None:
        source = '<p><strong>Hello</strong> <a href="https://example.com?a=1&b=2">Open link</a><img src="/image.png"></p>'
        document = HtmlTextDocument(source)

        html = document.render(["Ni hao", "Da kai lian jie"])

        self.assertEqual(document.text_segments, ["Hello", "Open link"])
        self.assertIn('<strong>Ni hao</strong>', html)
        self.assertIn('<a href="https://example.com?a=1&b=2">Da kai lian jie</a>', html)
        self.assertIn('<img src="/image.png">', html)

    def test_escapes_translated_text_so_model_cannot_insert_tags(self) -> None:
        document = HtmlTextDocument("<p>Hello</p>")

        html = document.render(["<b>Ni hao</b>"])

        self.assertEqual(html, "<p>&lt;b&gt;Ni hao&lt;/b&gt;</p>")


class OpenAITranslationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_translates_subject_and_body_segments_as_json_array(self) -> None:
        client = FakeClient(json.dumps(["Zhu ti", "Ni hao", "Da kai lian jie"]))
        service = OpenAITranslationService(settings(), client=client)
        post = {
            "subject": "Subject",
            "body_html": '<p><strong>Hello</strong> <a href="https://example.com">Open link</a><img src="/image.png"></p>',
        }

        translation = await service.translate_post(post, "zh-Hans")

        self.assertEqual(translation["subject"], "Zhu ti")
        self.assertIn("<strong>Ni hao</strong>", translation["body_html"])
        self.assertIn('<a href="https://example.com">Da kai lian jie</a>', translation["body_html"])
        self.assertIn('<img src="/image.png">', translation["body_html"])
        self.assertEqual(translation["body_preview"], "Ni hao Da kai lian jie")
        self.assertEqual(translation["model"], "gpt-5.5")
        self.assertEqual(len(client.responses.calls), 1)

    async def test_rejects_mismatched_segment_count(self) -> None:
        client = FakeClient(json.dumps([]))
        service = OpenAITranslationService(settings(), client=client)

        with self.assertRaises(TranslationError):
            await service.translate_post({"subject": "Subject", "body_html": "<p>Hello</p>"}, "zh-Hans")

    async def test_batches_large_segment_sets_without_changing_order(self) -> None:
        client = EchoClient()
        service = OpenAITranslationService(settings(), client=client)
        segments = [f"segment-{index}" for index in range(MAX_TRANSLATION_SEGMENTS_PER_REQUEST * 2 + 5)]

        translated = await service._translate_segments(segments, "zh-Hans")

        self.assertEqual(translated, [f"translated:{segment}" for segment in segments])
        self.assertEqual(len(client.responses.calls), 3)
        request_sizes = [
            len(json.loads(call["input"][1]["content"])["segments"])
            for call in client.responses.calls
        ]
        self.assertEqual(request_sizes, [12, 12, 5])

    async def test_retries_mismatched_batches_in_smaller_validated_groups(self) -> None:
        client = SplitRetryClient()
        service = OpenAITranslationService(settings(), client=client)

        translated = await service._translate_segments(["one", "two"], "zh-Hans")

        self.assertEqual(translated, ["translated:one", "translated:two"])
        self.assertEqual(len(client.responses.calls), 3)

    async def test_rejects_invalid_json(self) -> None:
        client = FakeClient("not json")
        service = OpenAITranslationService(settings(), client=client)

        with self.assertRaises(TranslationError):
            await service.translate_post({"subject": "Subject", "body_html": "<p>Hello</p>"}, "zh-Hans")

    async def test_wraps_provider_errors(self) -> None:
        client = FakeClient(RuntimeError("provider down"))
        service = OpenAITranslationService(settings(), client=client)

        with self.assertRaises(TranslationError) as context:
            await service.translate_post({"subject": "Subject", "body_html": "<p>Hello</p>"}, "zh-Hans")
        self.assertIn("OpenAI translation request failed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
