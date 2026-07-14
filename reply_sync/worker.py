from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from auth import PersistentTokenCacheError, acquire_persistent_access_token
from graph_client import GraphClient
from logging_config import configure_logging
from settings import get_settings

from .config import get_reply_sync_settings
from .graph import ReplyGraph
from .locking import ReplySyncAlreadyRunning, ReplySyncLock
from .service import ReplySyncService


logger = logging.getLogger(__name__)


async def run_once() -> dict[str, Any]:
    reply_settings = get_reply_sync_settings()
    if not reply_settings.enabled:
        logger.info("Reply-sync worker skipped because automation is disabled", extra={"status": "disabled"})
        return {"enabled": False, "status": "disabled", "threads": []}

    core_settings = get_settings()
    try:
        with ReplySyncLock(reply_settings.lock_path):
            token = acquire_persistent_access_token(core_settings)
            async with GraphClient(
                token,
                base_url=core_settings.graph_base_url,
                timeout_seconds=core_settings.graph_request_timeout_seconds,
                max_retries=core_settings.graph_max_retries,
            ) as client:
                result = await ReplySyncService(reply_settings, core_settings).run_all(ReplyGraph(client))
    except ReplySyncAlreadyRunning:
        logger.warning("Reply-sync worker skipped because another run is active", extra={"status": "locked"})
        return {"enabled": True, "status": "locked", "threads": []}

    logger.info(
        "Reply-sync worker run completed",
        extra={
            "status": result.get("status"),
            "sent": result.get("sent"),
            "recovered": result.get("recovered"),
            "blocked": result.get("blocked"),
        },
    )
    return {"enabled": True, **result}


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Teams translated-reply synchronization worker")
    parser.add_argument("--once", action="store_true", help="Run one reply synchronization pass and exit")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")

    configure_logging()
    try:
        result = asyncio.run(run_once())
    except PersistentTokenCacheError as exc:
        logger.error("Reply-sync worker authentication failed", extra={"status": "auth_required", "error": str(exc)})
        result = {"enabled": True, "status": "auth_required", "error": str(exc)}
    except Exception as exc:
        logger.exception("Reply-sync worker failed", extra={"status": "failed", "error": str(exc)})
        result = {"enabled": True, "status": "failed", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") not in {"failed", "auth_required"} else 1


if __name__ == "__main__":
    raise SystemExit(cli())
