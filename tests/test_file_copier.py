import unittest

from file_copier import append_short_hash, sanitize_file_name


class FileCopierTests(unittest.TestCase):
    def test_sanitizes_file_names(self) -> None:
        self.assertEqual(sanitize_file_name(' report:Q2?.docx '), "report_Q2_.docx")
        self.assertEqual(sanitize_file_name("..."), "file")

    def test_appends_short_hash_before_extension(self) -> None:
        renamed = append_short_hash("report.docx", "source-url")
        self.assertRegex(renamed, r"^report-[a-f0-9]{8}\.docx$")


if __name__ == "__main__":
    unittest.main()
