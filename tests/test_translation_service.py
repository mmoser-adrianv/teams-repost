import json
import types
import unittest

from translation_service import HtmlTextDocument, OpenAITranslationService, TranslationError


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
        client = FakeClient(json.dumps(["only one"]))
        service = OpenAITranslationService(settings(), client=client)

        with self.assertRaises(TranslationError):
            await service.translate_post({"subject": "Subject", "body_html": "<p>Hello</p>"}, "zh-Hans")

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
