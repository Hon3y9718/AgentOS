"""Application configuration.

Role: single source of truth for env-derived config. No `os.getenv` anywhere else
(scripts/check_layering.sh enforces this in CI).
Called by: main.py, db/session.py, and anything needing config. Calls nothing internal.
Gotcha: provider keys are Optional — a missing key disables that provider at the
registry level later, it must never crash the process.
See: docs/ARCHITECTURE.md#configuration
"""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App-wide configuration loaded from the environment / .env file.

    Raises:
        pydantic_core.ValidationError: at construction time if a required
            field (database_url) is missing or malformed.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    # WHY required, no default, like database_url (not Optional like the
    # provider keys below): a missing signing secret must crash startup, not
    # silently issue tokens no one can later verify were signed with the
    # value the operator intended. Reused for JWT signing AND fastapi-users'
    # reset-password/verification token secrets (app/core/auth/manager.py) —
    # those two flows aren't exposed by any endpoint yet, but
    # BaseUserManager requires the properties to exist regardless. One
    # secret for three purposes is an MVP simplification; split them if
    # password reset ever ships for real, so rotating one doesn't invalidate
    # the others.
    secret_key: str
    # WHY default here but not on database_url: local dev shouldn't need a value
    # to get readable console output; prod sets LOG_LEVEL explicitly.
    log_level: str = "INFO"

    # Individually optional: ARCHITECTURE.md requires a provider with no key
    # configured to show up as `available: false`, not crash startup.
    #
    # WHY AliasChoices with a second, non-canonical name on three of these:
    # this repo's real .env (not .env.example) predates the provider names
    # settling and still uses OPEN_AI_API_KEY / CLAUD_API_KEY / GOOGLE_API_KEY
    # — confirmed directly by the user in chat, since Claude is denied `Read`
    # on `.env` itself (by design, per the scaffolding session). The
    # canonical name is listed first and is what `.env.example` documents;
    # the legacy alias exists only so this specific file keeps working
    # without being rewritten by hand.
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "OPEN_AI_API_KEY")
    )
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CLAUD_API_KEY")
    )
    together_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )

    # WHY a flag, not always-on: app/core/llm/registry.py's live model
    # refresh makes real HTTP calls to every configured provider. Without
    # this, `make test` would fire those calls (possibly billed) whenever a
    # developer's real .env happens to be loaded — tests/conftest.py forces
    # this to False before importing app.main, independent of what's in any
    # given developer's .env.
    enable_live_model_refresh: bool = True


# WHY module-level (not a lazy factory): importing this module is the startup
# path. If required config is missing, this raises immediately on `import
# app.config` — before uvicorn binds a port — instead of on the first request
# that happens to touch settings.
settings = Settings()


def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Exists as a function (not a bare import) so tests can override it via
    FastAPI's `app.dependency_overrides[get_settings]`.
    """
    return settings
