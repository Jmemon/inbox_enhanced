from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# server/app/config.py -> parents[2] is the repo root; falls back gracefully if .env is absent (Railway injects env vars directly).
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")
    session_secret: str = Field(alias="SESSION_SECRET")
    encryption_key: str = Field(alias="ENCRYPTION_KEY")
    session_ttl_seconds: int = Field(default=60 * 60 * 24 * 30, alias="SESSION_TTL_SECONDS")
    google_client_id: str = Field(alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(alias="GOOGLE_REDIRECT_URI")
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")
    # --- OpenRouter (LLM classifier + draft preview) ---
    # We call LLMs through OpenRouter's OpenAI-compatible API so the model is a
    # swappable config string (any OpenRouter-hosted provider). Set
    # OPENROUTER_API_KEY in the Railway worker service before the deploy that
    # ships bucket changes — workers boot fine without it but every
    # classify/preview call returns 401 and falls through to "no fit".
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
    # OpenRouter model ids are provider-prefixed (e.g. "anthropic/claude-haiku-4-5",
    # "openai/gpt-4o-mini", "google/gemini-flash-1.5"). Omitting the prefix 404s.
    llm_classify_model: str = Field(default="anthropic/claude-haiku-4-5", alias="LLM_CLASSIFY_MODEL")
    # Model used for the task-extraction LLM stage (Phase 2A task engine).
    # Kept separate from LLM_CLASSIFY_MODEL so extraction quality/cost can be
    # tuned independently of inbox bucket classification.
    llm_extract_model: str = Field(default="anthropic/claude-sonnet-4.5", alias="LLM_EXTRACT_MODEL")
    # Process-wide cap on concurrent in-flight LLM calls. One semaphore is
    # shared by classification + draft-preview, so a 200-thread full sync and a
    # user-triggered preview can't both push 16 concurrently.
    llm_concurrency: int = Field(default=16, alias="LLM_CONCURRENCY")
    # Minimum extraction confidence (0-100) required to auto-apply an extracted
    # task without user confirmation.
    task_apply_confidence: int = Field(default=75, alias="TASK_APPLY_CONFIDENCE")
    # Minimum confidence (0-100) required to auto-link an extracted task to an
    # existing thread/task rather than creating a new one.
    task_link_confidence: int = Field(default=60, alias="TASK_LINK_CONFIDENCE")
    # On Railway, ENV is "production"; locally unset → development.
    env: str = Field(default="development", alias="ENV")

    @property
    def cookie_secure(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
