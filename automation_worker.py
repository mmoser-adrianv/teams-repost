from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from fastapi import HTTPException

import main as app_main
from auth import PersistentTokenCacheError, acquire_persistent_access_token
from exception_list import ExceptionList
from graph_client import GraphAPIError
from post_cache import PostCache
from repost_history import RepostHistory
from translation_service import TranslationConfigurationError, TranslationError


logger = logging.getLogger(__name__)


class AutomationAlreadyRunning(RuntimeError):
    pass


class AutomationLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> "AutomationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AutomationAlreadyRunning("Automation worker is already running.") from exc
        except Exception:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


async def run_once() -> dict[str, Any]:
    settings = app_main.settings
    if not settings.automation_enabled:
        logger.info("Automation worker skipped because automation is disabled", extra={"status": "disabled"})
        return {"enabled": False, "status": "disabled", "flows": []}

    flow_names = settings.automation_flow_list
    logger.info(
        "Automation worker run started",
        extra={
            "automation_flows": flow_names,
            "max_posts_per_flow": settings.automation_max_posts_per_flow,
            "lock_path": str(settings.automation_lock_path),
        },
    )

    try:
        with AutomationLock(settings.automation_lock_path):
            logger.info(
                "Automation worker lock acquired",
                extra={"lock_path": str(settings.automation_lock_path)},
            )
            token = acquire_persistent_access_token(settings)
            flow_results = []
            for flow_name in flow_names:
                flow_results.append(await process_flow(flow_name, token))
    except AutomationAlreadyRunning:
        logger.warning(
            "Automation worker skipped because another run is active",
            extra={"status": "locked", "lock_path": str(settings.automation_lock_path)},
        )
        return {"enabled": True, "status": "locked", "flows": []}

    logger.info(
        "Automation worker run completed",
        extra={
            "status": "completed",
            "automation_flows": [flow["flow"] for flow in flow_results],
            "checked": sum(flow["checked"] for flow in flow_results),
            "translated": sum(flow["translated"] for flow in flow_results),
            "reposted": sum(flow["reposted"] for flow in flow_results),
            "already_reposted": sum(flow["already_reposted"] for flow in flow_results),
            "failed": sum(flow["failed"] for flow in flow_results),
        },
    )
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

    logger.info(
        "Automation flow started",
        extra={
            "flow": flow.name,
            "target_language": target_language,
            "max_posts": settings.automation_max_posts_per_flow,
        },
    )

    result["refresh"] = await _refresh_flow_cache(flow, token, cache, exceptions)
    _log_refresh_result(flow.name, result["refresh"])
    candidate_page = _automation_candidate_posts(
        cache,
        history,
        flow,
        target_language,
        settings.automation_max_posts_per_flow,
        exceptions,
    )
    posts = candidate_page["posts"]
    result["already_reposted"] += candidate_page["already_reposted"]
    logger.info(
        "Automation flow loaded cached posts",
        extra={
            "flow": flow.name,
            "candidate_posts": len(posts),
            "total_cached_posts": candidate_page["total"],
            "already_reposted": candidate_page["already_reposted"],
            "order": "oldest_to_newest",
        },
    )

    for post in posts:
        result["checked"] += 1
        message_id = post.get("id") or ""
        if not message_id:
            logger.warning("Automation skipped cached post without message id", extra={"flow": flow.name})
            continue
        if history.get(source.team_id, source.channel_id, message_id, target_language):
            result["already_reposted"] += 1
            logger.info(
                "Automation skipped already reposted message",
                extra={"flow": flow.name, "message_id": message_id, "target_language": target_language},
            )
            continue
        if exceptions.matches_post(post):
            logger.info(
                "Automation skipped message from exception list",
                extra={"flow": flow.name, "message_id": message_id, "author": post.get("author")},
            )
            continue
        try:
            translated = await _ensure_translation(flow, post, target_language, cache)
            if translated:
                result["translated"] += 1
                logger.info(
                    "Automation translated message",
                    extra={"flow": flow.name, "message_id": message_id, "target_language": target_language},
                )
            repost_result = await app_main._create_repost_with_token(flow, message_id, target_language, token)
            if repost_result["status"] == "already_reposted":
                result["already_reposted"] += 1
                logger.info(
                    "Automation repost skipped by duplicate check",
                    extra={"flow": flow.name, "message_id": message_id, "target_language": target_language},
                )
            else:
                result["reposted"] += 1
                logger.info(
                    "Automation reposted message",
                    extra={
                        "flow": flow.name,
                        "message_id": message_id,
                        "target_language": target_language,
                        "destination_message_id": _destination_message_id(repost_result),
                    },
                )
        except (GraphAPIError, HTTPException, TranslationConfigurationError, TranslationError, KeyError, ValueError) as exc:
            error = _safe_error(exc)
            result["failed"] += 1
            result["errors"].append({"message_id": message_id, "error": error})
            logger.warning(
                "Automation message failed",
                extra={"flow": flow.name, "message_id": message_id, "target_language": target_language, "error": error},
            )

    logger.info(
        "Automation flow completed",
        extra={
            "flow": flow.name,
            "checked": result["checked"],
            "translated": result["translated"],
            "reposted": result["reposted"],
            "already_reposted": result["already_reposted"],
            "failed": result["failed"],
        },
    )
    return result


def _automation_candidate_posts(
    cache: PostCache,
    history: RepostHistory,
    flow,
    target_language: str,
    max_posts: int,
    exceptions: ExceptionList,
) -> dict[str, Any]:
    source = flow.source
    page = cache.page_posts(
        source.team_id,
        source.channel_id,
        0,
        sys.maxsize,
        exceptions.email_set(),
        flow.skipped_body_prefixes,
        excluded_author_matcher=exceptions.matches_post,
    )
    posts: list[dict[str, Any]] = []
    already_reposted = 0

    for post in sorted(page["posts"], key=_automation_post_sort_key):
        message_id = post.get("id") or ""
        if message_id and history.get(source.team_id, source.channel_id, message_id, target_language):
            already_reposted += 1
            continue
        posts.append(post)
        if len(posts) >= max_posts:
            break

    return {"posts": posts, "total": page["total"], "already_reposted": already_reposted}


def _automation_post_sort_key(post: dict[str, Any]) -> str:
    return str(post.get("created_date_time") or post.get("saved_at") or "")


def _destination_message_id(repost_result: dict[str, Any]) -> str | None:
    record = repost_result.get("record") or {}
    destination = record.get("destination") or {}
    return destination.get("message_id") or (repost_result.get("report") or {}).get("new_message_id")


def _log_refresh_result(flow_name: str, refresh: dict[str, Any]) -> None:
    payload = {
        "flow": flow_name,
        "refresh_failed": refresh.get("refresh_failed"),
        "new_posts_saved": refresh.get("new_posts_saved"),
        "posts_skipped_by_exception": refresh.get("posts_skipped_by_exception"),
        "posts_skipped_by_body_prefix": refresh.get("posts_skipped_by_body_prefix"),
        "posts_skipped_by_graph_error": refresh.get("posts_skipped_by_graph_error"),
        "posts_skipped_by_empty_content": refresh.get("posts_skipped_by_empty_content"),
        "partial_refresh": refresh.get("partial_refresh"),
        "last_refreshed_at": refresh.get("last_refreshed_at"),
    }
    if refresh.get("refresh_failed"):
        logger.warning(
            "Automation flow cache refresh failed",
            extra={**payload, "refresh_error": refresh.get("refresh_error")},
        )
        return
    logger.info("Automation flow cache refreshed", extra=payload)


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
        logger.error(
            "Automation worker authentication failed",
            extra={"status": "auth_required", "error": str(exc)},
        )
        print(json.dumps({"enabled": True, "status": "auth_required", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    except HTTPException as exc:
        logger.error(
            "Automation worker configuration failed",
            extra={"status": "config_error", "error": str(exc.detail)},
        )
        print(json.dumps({"enabled": True, "status": "config_error", "error": str(exc.detail)}, ensure_ascii=True), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
