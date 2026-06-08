import json
import tempfile
import unittest
from pathlib import Path

from exception_list import ExceptionList, normalize_email


class ExceptionListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "exceptions.json"
        self.exceptions = ExceptionList(self.path)

    def test_add_normalizes_and_dedupes_email_addresses(self) -> None:
        self.exceptions.add(" Alex@Example.com ")
        emails = self.exceptions.add("alex@example.com")

        self.assertEqual(emails, ["alex@example.com"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["emails"], ["alex@example.com"])

    def test_remove_email(self) -> None:
        self.exceptions.add("alex@example.com")

        emails = self.exceptions.remove("ALEX@example.com")

        self.assertEqual(emails, [])

    def test_invalid_email_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.exceptions.add("not an email")

    def test_legacy_array_file_is_supported(self) -> None:
        self.path.write_text(json.dumps(["Alex@Example.com"]), encoding="utf-8")

        self.assertEqual(self.exceptions.list_emails(), ["alex@example.com"])

    def test_normalize_email_rejects_empty_values(self) -> None:
        self.assertIsNone(normalize_email(""))


if __name__ == "__main__":
    unittest.main()
