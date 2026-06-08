import unittest
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

import auth
from auth import AUTH_DOMAIN_HINT, GRAPH_SCOPES, _AUTH_FLOWS, _TOKEN_CACHES, create_login_flow, sign_out
from settings import Settings


class AuthScopeTests(unittest.TestCase):
    def test_channel_only_graph_scopes(self) -> None:
        self.assertEqual(GRAPH_SCOPES, ["ChannelMessage.Read.All", "ChannelMessage.Send"])

    def test_graph_scopes_can_be_configured_from_env_string(self) -> None:
        settings = Settings(
            AZURE_TENANT_ID="tenant",
            AZURE_CLIENT_ID="client",
            GRAPH_SCOPES="ChannelMessage.Read.All, ChannelMessage.Send",
        )

        self.assertEqual(settings.graph_scope_list, ["ChannelMessage.Read.All", "ChannelMessage.Send"])


class FakeMsalApp:
    def initiate_auth_code_flow(self, scopes, **kwargs):
        return {
            "auth_uri": (
                "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?"
                f"domain_hint={kwargs.get('domain_hint')}"
            )
        }


class LoginFlowTests(unittest.TestCase):
    def tearDown(self) -> None:
        _AUTH_FLOWS.clear()

    def test_login_flow_adds_mmoser_domain_hint(self) -> None:
        original_build_msal_app = auth.build_msal_app
        auth.build_msal_app = lambda settings, cache=None: FakeMsalApp()
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
        _AUTH_FLOWS["session-1"] = {"flow": {}, "cache": "{}"}
        _TOKEN_CACHES["session-1"] = {"cache": "{}", "expires_at": 123}

        result = sign_out(request)

        self.assertEqual(result, {"signed_in": False})
        self.assertEqual(request.session, {})
        self.assertNotIn("session-1", _AUTH_FLOWS)
        self.assertNotIn("session-1", _TOKEN_CACHES)


if __name__ == "__main__":
    unittest.main()
