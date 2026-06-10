from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import main as app_main
from auth import PersistentTokenCacheError, acquire_persistent_access_token
from exception_list import ExceptionList
from graph_client import GraphAPIError
from post_cache import PostCache
from repost_history import RepostHistory
from translation_service import TranslationConfigurationError, TranslationError


class AutomationAlreadyRunning(RuntimeError):
    pass


class AutomationLock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "AutomationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise AutomationAlreadyRunning("Automation worker is already running.") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
        return self

    def __exit__(self, *_: object) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()


async def run_once() -> dict[str, Any]:
    settings = app_main.settings
    if not settings.automation_enabled:
        return {"enabled": False, "status": "disabled", "flows": []}

    try:
        with AutomationLock(settings.automation_lock_path):
            token = acquire_persistent_access_token(settings)
            flow_results = []
            for flow_name in settings.automation_flow_list:
                flow_results.append(await process_flow(flow_name, token))
    except AutomationAlreadyRunning:
        return {"enabled": True, "status": "locked", "flows": []}

    return {"enabled": True, "status": "completed", "flows": flow_results}


async def process_flow(flow_name: str, token: str) -> dict[str, Any]:
    settings = app_main.settings
    flow = app_main._repost_flow(flow_name)
    source = flow.source
    target_language = flow.target_language
    cache = PostCache(settings.post_cache_path)
    history = RepostHistory(settings.repost_history_path)
    exceptions = ExceptionList(flow.exception_list_path)

    result: dict[str, Any] = {
        "flow": flow.name,
        "target_language": target_language,
        "checked": 0,
        "translated": 0,
        "reposted": 0,
        "already_reposted": 0,
        "failed": 0,
        "errors": [],
        "refresh": {},
    }

    result["refresh"] = await _refresh_flow_cache(flow, token, cache, exceptions)
    posts = cache.page_posts(
        source.team_id,
        source.channel_id,
        0,
        settings.automation_max_posts_per_flow,
        exceptions.email_set(),
        flow.skipped_body_prefixes,
    )["posts"]

    for post in posts:
        result["checked"] += 1
        message_id = post.get("id") or ""
        if not message_id:
            continue
        if history.get(source.team_id, source.channel_id, message_id, target_language):
            result["already_reposted"] += 1
            continue
        try:
            translated = await _ensure_translation(flow, post, target_language, cache)
            if translated:
                result["translated"] += 1
            repost_result = await app_main._create_repost_with_token(flow, message_id, target_language, token)
            if repost_result["status"] == "already_reposted":
                result["already_reposted"] += 1
            else:
                result["reposted"] += 1
        except (GraphAPIError, HTTPException, TranslationConfigurationError, TranslationError, KeyError, ValueError) as exc:
            result["failed"] += 1
            result["errors"].append({"message_id": message_id, "error": _safe_error(exc)})

    return result


async def _refresh_flow_cache(flow, token: str, cache: PostCache, exceptions: ExceptionList) -> dict[str, Any]:
    image_route_prefix = f"/api/flows/{flow.name}/posts"
    source = flow.source
    try:
        async with app_main._graph(token) as graph:
            return await app_main._refresh_post_cache(graph, flow, cache, exceptions, image_route_prefix)
    except GraphAPIError as exc:
        meta = cache.source_status(source.team_id, source.channel_id)
        meta.update({"refresh_failed": True, "refresh_error": str(exc)})
        return meta


async def _ensure_translation(flow, post: dict[str, Any], target_language: str, cache: PostCache) -> bool:
    if (post.get("translations") or {}).get(target_language):
        return False
    if not app_main.settings.openai_api_key:
        raise TranslationConfigurationError("OPENAI_API_KEY is required to translate posts.")
    translation = await app_main._translate_post(post, target_language, app_main.settings)
    cache.upsert_translation(flow.source.team_id, flow.source.channel_id, post["id"], target_language, translation)
    return True


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, GraphAPIError):
        return f"Microsoft Graph HTTP {exc.status_code}: {exc}"
    return str(exc)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Teams repost automation worker.")
    parser.add_argument("--once", action="store_true", help="Run one automation pass and exit.")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")

    try:
        result = asyncio.run(run_once())
    except PersistentTokenCacheError as exc:
        print(json.dumps({"enabled": True, "status": "auth_required", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    except HTTPException as exc:
        print(json.dumps({"enabled": True, "status": "config_error", "error": str(exc.detail)}, ensure_ascii=True), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
