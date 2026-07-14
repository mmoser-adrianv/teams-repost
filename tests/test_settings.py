import unittest
from pathlib import Path

from settings import APP_ROOT, Settings, _app_relative_path, parse_automation_flows


class SettingsTests(unittest.TestCase):
    def test_default_runtime_paths_are_repo_local_data_paths(self) -> None:
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client", _env_file=None)

        self.assertEqual(settings.temp_folder, Path(".data") / "temp")
        self.assertEqual(settings.msal_token_cache_path, Path(".data") / "msal-token-cache.json")
        self.assertEqual(settings.repost_history_path, Path(".data") / "repost-history.json")
        self.assertEqual(settings.post_cache_path, Path(".data") / "post-cache.json")
        self.assertEqual(settings.exception_list_path, Path(".data") / "exception-list.json")
        self.assertEqual(settings.automation_lock_path, Path(".data") / "automation.lock")
        self.assertEqual(_app_relative_path(settings.exception_list_path), APP_ROOT / ".data" / "exception-list.json")

    def test_automation_flows_accepts_backward_as_reverse_alias(self) -> None:
        self.assertEqual(parse_automation_flows("forward backward"), ["forward", "reverse"])


if __name__ == "__main__":
    unittest.main()
