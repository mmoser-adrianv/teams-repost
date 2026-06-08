from functools import lru_cache
from pathlib import Path
import tempfile

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_GRAPH_SCOPES = ("ChannelMessage.Read.All", "ChannelMessage.Send")


class Settings(BaseSettings):
    azure_tenant_id: str = Field(..., alias="AZURE_TENANT_ID")
    azure_client_id: str = Field(..., alias="AZURE_CLIENT_ID")
    azure_client_secret: str | None = Field(default=None, alias="AZURE_CLIENT_SECRET")
    redirect_uri: str = Field(default="http://localhost:8000/auth/callback", alias="REDIRECT_URI")
    graph_base_url: str = Field(default="https://graph.microsoft.com/v1.0", alias="GRAPH_BASE_URL")
    graph_scopes: str = Field(default=" ".join(DEFAULT_GRAPH_SCOPES), alias="GRAPH_SCOPES")
    source_team_id: str | None = Field(default=None, alias="SOURCE_TEAM_ID")
    source_channel_id: str | None = Field(default=None, alias="SOURCE_CHANNEL_ID")
    destination_team_id: str | None = Field(default=None, alias="DESTINATION_TEAM_ID")
    destination_channel_id: str | None = Field(default=None, alias="DESTINATION_CHANNEL_ID")
    repost_history_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "teams-repost" / "repost-history.json",
        alias="REPOST_HISTORY_PATH",
    )
    post_cache_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "teams-repost" / "post-cache.json",
        alias="POST_CACHE_PATH",
    )
    exception_list_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "teams-repost" / "exception-list.json",
        alias="EXCEPTION_LIST_PATH",
    )
    post_list_limit: int = Field(default=25, alias="POST_LIST_LIMIT", ge=1, le=100)
    post_cache_max_refresh_pages: int = Field(default=10, alias="POST_CACHE_MAX_REFRESH_PAGES", ge=1, le=100)
    max_file_size_mb: int = Field(default=25, alias="MAX_FILE_SIZE_MB", ge=1)
    temp_folder: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "teams-repost", alias="TEMP_FOLDER")
    session_secret: str = Field(default="dev-only-change-me", alias="SESSION_SECRET")
    link_original_on_copy_failure: bool = Field(default=False, alias="LINK_ORIGINAL_ON_COPY_FAILURE")
    graph_request_timeout_seconds: float = Field(default=60.0, alias="GRAPH_REQUEST_TIMEOUT_SECONDS")
    graph_max_retries: int = Field(default=3, alias="GRAPH_MAX_RETRIES", ge=0)
    try_inline_hosted_contents: bool = Field(default=True, alias="TRY_INLINE_HOSTED_CONTENTS")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_translation_model: str = Field(default="gpt-5.5", alias="OPENAI_TRANSLATION_MODEL")
    openai_translation_target: str = Field(default="zh-Hans", alias="OPENAI_TRANSLATION_TARGET")
    openai_request_timeout_seconds: float = Field(default=60.0, alias="OPENAI_REQUEST_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("graph_scopes")
    @classmethod
    def validate_graph_scopes(cls, value: str) -> str:
        if not parse_graph_scopes(value):
            raise ValueError("GRAPH_SCOPES must include at least one scope")
        return value

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @property
    def graph_scope_list(self) -> list[str]:
        return parse_graph_scopes(self.graph_scopes)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.temp_folder = _app_relative_path(settings.temp_folder)
    settings.repost_history_path = _app_relative_path(settings.repost_history_path)
    settings.post_cache_path = _app_relative_path(settings.post_cache_path)
    settings.exception_list_path = _app_relative_path(settings.exception_list_path)
    settings.temp_folder.mkdir(parents=True, exist_ok=True)
    settings.repost_history_path.parent.mkdir(parents=True, exist_ok=True)
    settings.post_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.exception_list_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def _app_relative_path(path: Path) -> Path:
    return path if path.is_absolute() else APP_ROOT / path


def parse_graph_scopes(value: str) -> list[str]:
    return [scope.strip() for scope in value.replace(",", " ").split() if scope.strip()]
