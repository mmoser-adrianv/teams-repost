from __future__ import annotations

import os
import time
import uuid
from contextlib import suppress
from typing import Any

import msal
from fastapi import HTTPException, Request

from settings import DEFAULT_GRAPH_SCOPES, Settings


GRAPH_SCOPES = list(DEFAULT_GRAPH_SCOPES)
AUTH_DOMAIN_HINT = "mmoser.com"

_AUTH_FLOWS: dict[str, dict[str, Any]] = {}
_TOKEN_CACHES: dict[str, dict[str, Any]] = {}


class PersistentTokenCacheError(RuntimeError):
    pass


class PersistentTokenCacheMissing(PersistentTokenCacheError):
    pass


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


def load_persistent_token_cache(settings: Settings) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = settings.msal_token_cache_path
    if not path.exists():
        raise PersistentTokenCacheMissing("Microsoft token cache is missing. Sign in again.")
    try:
        cache.deserialize(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PersistentTokenCacheError("Microsoft token cache is invalid. Sign in again.") from exc
    return cache


def save_persistent_token_cache(settings: Settings, cache: msal.SerializableTokenCache) -> None:
    path = settings.msal_token_cache_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with suppress(FileNotFoundError):
        temp_path.unlink()
    descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(cache.serialize())
    temp_path.replace(path)
    _restrict_owner_only(path)


def delete_persistent_token_cache(settings: Settings) -> None:
    with suppress(FileNotFoundError):
        settings.msal_token_cache_path.unlink()
    with suppress(FileNotFoundError):
        settings.msal_token_cache_path.with_name(settings.msal_token_cache_path.name + ".tmp").unlink()


def has_persistent_account(settings: Settings) -> bool:
    try:
        cache = load_persistent_token_cache(settings)
        return bool(build_msal_app(settings, cache).get_accounts())
    except PersistentTokenCacheError:
        return False


def acquire_persistent_access_token(settings: Settings) -> str:
    cache = load_persistent_token_cache(settings)
    app = build_msal_app(settings, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise PersistentTokenCacheMissing("No Microsoft account found in token cache. Sign in again.")

    result = app.acquire_token_silent(settings.graph_scope_list, account=accounts[0])
    if "access_token" not in result:
        detail = result.get("error_description") if isinstance(result, dict) else None
        raise PersistentTokenCacheMissing(detail or "Microsoft token could not be refreshed. Sign in again.")

    if cache.has_state_changed:
        save_persistent_token_cache(settings, cache)
    return result["access_token"]


def _restrict_owner_only(path) -> None:
    if os.name != "posix":
        return
    with suppress(OSError):
        os.chmod(path, 0o600)


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

    save_persistent_token_cache(settings, cache)
    _TOKEN_CACHES[session_id] = {
        "cache": cache.serialize(),
        "expires_at": time.time() + int(result.get("expires_in") or 0) - 60,
    }
    return result


def get_access_token(request: Request, settings: Settings) -> str:
    session_id = request.session.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not signed in. Open /auth/login first.")

    cache = msal.SerializableTokenCache()
    stored = _TOKEN_CACHES.get(session_id)
    if stored:
        cache.deserialize(stored["cache"])
    else:
        try:
            cache = load_persistent_token_cache(settings)
        except PersistentTokenCacheError as exc:
            raise HTTPException(status_code=401, detail="Not signed in. Open /auth/login first.") from exc

    app = build_msal_app(settings, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise HTTPException(status_code=401, detail="No Microsoft account found in token cache. Open /auth/login again.")

    result = app.acquire_token_silent(settings.graph_scope_list, account=accounts[0])
    if "access_token" not in result:
        raise HTTPException(status_code=401, detail="Microsoft token expired or could not be refreshed. Open /auth/login again.")

    if cache.has_state_changed:
        save_persistent_token_cache(settings, cache)
    _TOKEN_CACHES[session_id] = {
        "cache": cache.serialize(),
        "expires_at": time.time() + int(result.get("expires_in") or 0) - 60,
    }
    return result["access_token"]


def sign_out(request: Request, settings: Settings) -> dict[str, bool]:
    session_id = request.session.get("session_id")
    if session_id:
        _AUTH_FLOWS.pop(session_id, None)
        _TOKEN_CACHES.pop(session_id, None)
    delete_persistent_token_cache(settings)
    request.session.clear()
    return {"signed_in": False}


def auth_status(request: Request, settings: Settings) -> dict[str, Any]:
    session_id = request.session.get("session_id")
    signed_in = bool(session_id and (session_id in _TOKEN_CACHES or has_persistent_account(settings)))
    return {"signed_in": signed_in}
