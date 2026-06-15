import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

import auth
from auth import (
    AUTH_DOMAIN_HINT,
    GRAPH_SCOPES,
    PersistentTokenCacheError,
    PersistentTokenCacheMissing,
    _AUTH_FLOWS,
    _TOKEN_CACHES,
    acquire_persistent_access_token,
    complete_login_flow,
    create_login_flow,
    load_persistent_token_cache,
    sign_out,
)
from settings import Settings


class AuthScopeTests(unittest.TestCase):
    def test_channel_only_graph_scopes(self) -> None:
        self.assertEqual(GRAPH_SCOPES, ["offline_access", "ChannelMessage.Read.All", "ChannelMessage.Send"])

    def test_graph_scopes_can_be_configured_from_env_string(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            GRAPH_SCOPES="offline_access, ChannelMessage.Read.All, ChannelMessage.Send",
        )

        self.assertEqual(settings.graph_scope_list, ["offline_access", "ChannelMessage.Read.All", "ChannelMessage.Send"])


class FakeMsalApp:
    def __init__(self, cache=None) -> None:
        self.cache = cache
        self.initiated_scopes = None

    def initiate_auth_code_flow(self, scopes, **kwargs):
        self.initiated_scopes = scopes
        return {
            "auth_uri": (
                "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?"
                f"domain_hint={kwargs.get('domain_hint')}"
            )
        }

    def acquire_token_by_auth_code_flow(self, flow, params):
        return {"access_token": "token", "expires_in": 3600}


class FakeSilentApp:
    def __init__(self, result) -> None:
        self.result = result
        self.silent_scopes = None

    def get_accounts(self):
        return [{"home_account_id": "account-1"}]

    def acquire_token_silent(self, scopes, account):
        self.silent_scopes = scopes
        return self.result


class FakeCache:
    has_state_changed = True

    def serialize(self):
        return '{"changed": true}'


class LoginFlowTests(unittest.TestCase):
    def tearDown(self) -> None:
        _AUTH_FLOWS.clear()

    def test_login_flow_adds_mmoser_domain_hint(self) -> None:
        original_build_msal_app = auth.build_msal_app
        auth.build_msal_app = lambda settings, cache=None: FakeMsalApp(cache)
        self.addCleanup(lambda: setattr(auth, "build_msal_app", original_build_msal_app))
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/auth/login",
                "headers": [],
                "session": {},
            }
        )
        settings = Settings(AZURE_TENANT_ID="tenant", AZURE_CLIENT_ID="client")

        auth_uri = create_login_flow(request, settings)

        params = parse_qs(urlparse(auth_uri).query)
        self.assertEqual(params["domain_hint"], [AUTH_DOMAIN_HINT])

    def test_login_flow_filters_msal_reserved_scopes(self) -> None:
        original_build_msal_app = auth.build_msal_app
        fake_app = FakeMsalApp()
        auth.build_msal_app = lambda settings, cache=None: fake_app
        self.addCleanup(lambda: setattr(auth, "build_msal_app", original_build_msal_app))
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/auth/login",
                "headers": [],
                "session": {},
            }
        )
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            GRAPH_SCOPES="offline_access ChannelMessage.Read.All ChannelMessage.Send",
        )

        create_login_flow(request, settings)

        self.assertEqual(fake_app.initiated_scopes, ["ChannelMessage.Read.All", "ChannelMessage.Send"])


class PersistentTokenCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            GRAPH_SCOPES="offline_access ChannelMessage.Read.All ChannelMessage.Send",
            MSAL_TOKEN_CACHE_PATH=Path(self.temp_dir.name) / "msal-cache.json",
        )
        _AUTH_FLOWS.clear()
        _TOKEN_CACHES.clear()

    def tearDown(self) -> None:
        _AUTH_FLOWS.clear()
        _TOKEN_CACHES.clear()

    def test_complete_login_flow_writes_persistent_cache_file(self) -> None:
        original_build_msal_app = auth.build_msal_app
        auth.build_msal_app = lambda settings, cache=None: FakeMsalApp(cache)
        self.addCleanup(lambda: setattr(auth, "build_msal_app", original_build_msal_app))
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/auth/callback",
                "headers": [],
                "query_string": b"code=ok&state=state",
                "session": {"session_id": "session-1"},
            }
        )
        _AUTH_FLOWS["session-1"] = {"flow": {}, "cache": "{}"}

        complete_login_flow(request, self.settings)

        self.assertTrue(self.settings.msal_token_cache_path.exists())
        self.assertIn("session-1", _TOKEN_CACHES)

    def test_acquire_persistent_access_token_saves_changed_cache(self) -> None:
        saved = []
        original_load = auth.load_persistent_token_cache
        original_save = auth.save_persistent_token_cache
        original_build = auth.build_msal_app
        fake_app = FakeSilentApp({"access_token": "token", "expires_in": 3600})
        auth.load_persistent_token_cache = lambda settings: FakeCache()
        auth.save_persistent_token_cache = lambda settings, cache: saved.append(cache.serialize())
        auth.build_msal_app = lambda settings, cache=None: fake_app
        self.addCleanup(lambda: setattr(auth, "load_persistent_token_cache", original_load))
        self.addCleanup(lambda: setattr(auth, "save_persistent_token_cache", original_save))
        self.addCleanup(lambda: setattr(auth, "build_msal_app", original_build))

        token = acquire_persistent_access_token(self.settings)

        self.assertEqual(token, "token")
        self.assertEqual(fake_app.silent_scopes, ["ChannelMessage.Read.All", "ChannelMessage.Send"])
        self.assertEqual(saved, ['{"changed": true}'])

    def test_missing_persistent_cache_requires_reauth(self) -> None:
        with self.assertRaises(PersistentTokenCacheMissing):
            load_persistent_token_cache(self.settings)

    def test_invalid_persistent_cache_requires_reauth(self) -> None:
        self.settings.msal_token_cache_path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(PersistentTokenCacheError):
            load_persistent_token_cache(self.settings)


class SignOutTests(unittest.TestCase):
    def tearDown(self) -> None:
        _AUTH_FLOWS.clear()
        _TOKEN_CACHES.clear()

    def test_sign_out_clears_local_session_and_cached_auth(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/logout",
                "headers": [],
                "session": {"session_id": "session-1"},
            }
        )
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            MSAL_TOKEN_CACHE_PATH=Path(temp_dir.name) / "msal-cache.json",
        )
        settings.msal_token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        settings.msal_token_cache_path.write_text("{}", encoding="utf-8")
        _AUTH_FLOWS["session-1"] = {"flow": {}, "cache": "{}"}
        _TOKEN_CACHES["session-1"] = {"cache": "{}", "expires_at": 123}

        result = sign_out(request, settings)

        self.assertEqual(result, {"signed_in": False})
        self.assertEqual(request.session, {})
        self.assertNotIn("session-1", _AUTH_FLOWS)
        self.assertNotIn("session-1", _TOKEN_CACHES)
        self.assertFalse(settings.msal_token_cache_path.exists())


if __name__ == "__main__":
    unittest.main()
