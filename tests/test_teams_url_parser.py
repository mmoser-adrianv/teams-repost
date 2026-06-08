import unittest

from teams_url_parser import TeamsUrlParseError, parse_teams_message_url


class TeamsUrlParserTests(unittest.TestCase):
    def test_parses_standard_teams_message_deep_link(self) -> None:
        parsed = parse_teams_message_url(
            "https://teams.microsoft.com/l/message/19%3Aabc%40thread.tacv2/1717257000123"
            "?tenantId=tenant-1&groupId=team-1&parentMessageId=1717250000000"
            "&channelName=Testt&teamName=Web%20App%20Ideas&createdTime=1717257000123"
        )

        self.assertEqual(parsed.tenant_id, "tenant-1")
        self.assertEqual(parsed.team_id, "team-1")
        self.assertEqual(parsed.source_channel_thread_id, "19:abc@thread.tacv2")
        self.assertEqual(parsed.message_id, "1717257000123")
        self.assertEqual(parsed.parent_message_id, "1717250000000")
        self.assertEqual(parsed.channel_name, "Testt")
        self.assertEqual(parsed.team_name, "Web App Ideas")

    def test_parses_message_ids_from_fragment_links(self) -> None:
        parsed = parse_teams_message_url(
            "https://teams.microsoft.com/#/l/message/19%3Aabc%40thread.tacv2/42"
            "?tenantId=tenant-1&groupId=team-1"
        )

        self.assertEqual(parsed.source_channel_thread_id, "19:abc@thread.tacv2")
        self.assertEqual(parsed.message_id, "42")

    def test_requires_team_channel_and_message_identifiers(self) -> None:
        with self.assertRaises(TeamsUrlParseError):
            parse_teams_message_url("https://teams.microsoft.com/l/message/19%3Aabc%40thread.tacv2/42")


if __name__ == "__main__":
    unittest.main()
