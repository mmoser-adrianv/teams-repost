from __future__ import annotations

import time
import uuid
from typing import Any

import msal
from fastapi import HTTPException, Request

from settings import DEFAULT_GRAPH_SCOPES, Settings


GRAPH_SCOPES = list(DEFAULT_GRAPH_SCOPES)
AUTH_DOMAIN_HINT = "mmoser.com"

_AUTH_FLOWS: dict[str, dict[str, Any]] = {}
_TOKEN_CACHES: dict[str, dict[str, Any]] = {}


def get_or_create_session_id(request: Request) -> str:
    session_id = request.session.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        request.session["session_id"] = session_id
    return session_id


def build_msal_app(settings: Settings, cache: msal.SerializableTokenCache | None = None) -> msal.ClientApplication:
    if settings.azure_client_secret:
        return msal.ConfidentialClientApplication(
            settings.azure_client_id,
            authority=settings.authority,
            client_credential=settings.azure_client_secret,
            token_cache=cache,
        )
    return msal.PublicClientApplication(settings.azure_client_id, authority=settings.authority, token_cache=cache)


def create_login_flow(request: Request, settings: Settings) -> str:
    session_id = get_or_create_session_id(request)
    cache = msal.SerializableTokenCache()
    app = build_msal_app(settings, cache)
    flow = app.initiate_auth_code_flow(
        settings.graph_scope_list,
        redirect_uri=settings.redirect_uri,
        domain_hint=AUTH_DOMAIN_HINT,
    )
    _AUTH_FLOWS[session_id] = {"flow": flow, "cache": cache.serialize()}
    return flow["auth_uri"]


def complete_login_flow(request: Request, settings: Settings) -> dict[str, Any]:
    session_id = get_or_create_session_id(request)
    stored = _AUTH_FLOWS.pop(session_id, None)
    if not stored:
        raise HTTPException(status_code=400, detail="No pending Microsoft login flow found for this session")

    cache = msal.SerializableTokenCache()
    cache.deserialize(stored["cache"])
    app = build_msal_app(settings, cache)
    result = app.acquire_token_by_auth_code_flow(stored["flow"], dict(request.query_params))
    if "access_token" not in result:
        raise HTTPException(status_code=401, detail=result.get("error_description") or "Microsoft login failed")

    _TOKEN_CACHES[session_id] = {
        "cache": cache.serialize(),
        "expires_at": time.time() + int(result.get("expires_in", 0)) - 60,
    }
    return result


def get_access_token(request: Request, settings: Settings) -> str:
    session_id = request.session.get("session_id")
    if not session_id or session_id not in _TOKEN_CACHES:
        raise HTTPException(status_code=401, detail="Not signed in. Open /auth/login first.")

    stored = _TOKEN_CACHES[session_id]
    cache = msal.SerializableTokenCache()
    cache.deserialize(stored["cache"])
    app = build_msal_app(settings, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise HTTPException(status_code=401, detail="No Microsoft account found in token cache. Open /auth/login again.")

    result = app.acquire_token_silent(settings.graph_scope_list, account=accounts[0])
    if "access_token" not in result:
        raise HTTPException(status_code=401, detail="Microsoft token expired or could not be refreshed. Open /auth/login again.")

    if cache.has_state_changed:
        _TOKEN_CACHES[session_id]["cache"] = cache.serialize()
    return result["access_token"]


def sign_out(request: Request) -> dict[str, bool]:
    session_id = request.session.get("session_id")
    if session_id:
        _AUTH_FLOWS.pop(session_id, None)
        _TOKEN_CACHES.pop(session_id, None)
    request.session.clear()
    return {"signed_in": False}


def auth_status(request: Request) -> dict[str, Any]:
    session_id = request.session.get("session_id")
    return {"signed_in": bool(session_id and session_id in _TOKEN_CACHES)}
