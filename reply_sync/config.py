from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from settings import APP_ROOT


VALID_REPLY_SYNC_FLOWS = {"forward", "reverse"}


class ReplySyncSettings(BaseSettings):
    enabled: bool = Field(default=False, alias="REPLY_SYNC_ENABLED")
    flows: str = Field(default="forward,reverse", alias="REPLY_SYNC_FLOWS")
    auto_enroll_new_threads: bool = Field(default=False, alias="REPLY_SYNC_AUTO_ENROLL_NEW_THREADS")
    stability_scans: int = Field(default=2, alias="REPLY_SYNC_STABILITY_SCANS", ge=1, le=10)
    max_replies_per_run: int = Field(default=50, alias="REPLY_SYNC_MAX_REPLIES_PER_RUN", ge=1, le=500)
    registry_path: Path = Field(
        default=Path(".data/reply-sync/thread-registry.json"),
        alias="REPLY_SYNC_REGISTRY_PATH",
    )
    cache_path: Path = Field(
        default=Path(".data/reply-sync/reply-cache.json"),
        alias="REPLY_SYNC_CACHE_PATH",
    )
    history_path: Path = Field(
        default=Path(".data/reply-sync/reply-history.json"),
        alias="REPLY_SYNC_HISTORY_PATH",
    )
    lock_path: Path = Field(
        default=Path(".data/reply-sync/automation.lock"),
        alias="REPLY_SYNC_LOCK_PATH",
    )
    temp_folder: Path = Field(
        default=Path(".data/reply-sync/temp"),
        alias="REPLY_SYNC_TEMP_FOLDER",
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("flows")
    @classmethod
    def validate_flows(cls, value: str) -> str:
        parse_reply_sync_flows(value)
        return value

    @property
    def flow_list(self) -> list[str]:
        return parse_reply_sync_flows(self.flows)


def parse_reply_sync_flows(value: str) -> list[str]:
    flows: list[str] = []
    for flow in [item.strip().lower() for item in value.replace(",", " ").split() if item.strip()]:
        if flow not in VALID_REPLY_SYNC_FLOWS:
            raise ValueError("REPLY_SYNC_FLOWS must contain only forward and/or reverse")
        if flow not in flows:
            flows.append(flow)
    if not flows:
        raise ValueError("REPLY_SYNC_FLOWS must include at least one flow")
    return flows


def _resolved(path: Path) -> Path:
    return path if path.is_absolute() else APP_ROOT / path


@lru_cache
def get_reply_sync_settings() -> ReplySyncSettings:
    settings = ReplySyncSettings()
    settings.registry_path = _resolved(settings.registry_path)
    settings.cache_path = _resolved(settings.cache_path)
    settings.history_path = _resolved(settings.history_path)
    settings.lock_path = _resolved(settings.lock_path)
    settings.temp_folder = _resolved(settings.temp_folder)
    return settings
