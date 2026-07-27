"""Application configuration, globals, and constants."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: Literal["dev", "production"] = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    timezone_name: str = "Asia/Jerusalem"

    database_path: str = "./noam_coach.db"
    storage_dir: str = "./storage"
    photo_retention_days: int = 30

    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0
    # Optional guard: if set, the running bot's @username must match this on
    # startup, so a wrong/reused token (e.g. an unrelated bot) is caught early.
    expected_bot_username: str = ""
    # Local Bot API Server support (lifts the 50MB bot download limit so large
    # Apple Health ZIPs can be sent in chat). Leave empty to use Telegram cloud.
    telegram_base_url: str = ""
    telegram_base_file_url: str = ""
    telegram_read_timeout: float = 120.0
    telegram_write_timeout: float = 120.0
    telegram_connect_timeout: float = 30.0
    telegram_pool_timeout: float = 30.0
    telegram_connection_pool_size: int = 16
    telegram_get_updates_read_timeout: float = 45.0
    telegram_get_updates_write_timeout: float = 30.0
    telegram_get_updates_connect_timeout: float = 15.0
    telegram_get_updates_pool_timeout: float = 15.0
    telegram_get_updates_connection_pool_size: int = 4

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"

    # Debugging aid: when true, every place that would normally EDIT an
    # existing Telegram message instead SENDS A NEW MESSAGE with the same
    # content, leaving prior screens visible so the full conversation
    # history can be scrolled and inspected. Off by default -- production
    # behavior (edit-in-place) is unaffected unless explicitly enabled.
    debug_append_only_messages: bool = False

    # Continuous HealthKit/Shortcuts/Watch ingestion is optional and disabled
    # by default.  Retrospective ZIP/XML imports remain available.
    enable_healthkit_api: bool = False
    healthkit_api_token: str = ""
    enable_local_health_path_import: bool = True
    # Semicolon-separated absolute roots. Empty means the current Windows
    # user's home directory (Desktop/Downloads/Documents are below it).
    local_health_import_allowed_roots: str = ""
    local_health_import_recursive: bool = False
    health_import_max_local_mb: int = 4096
    public_base_url: str = ""
    admin_chat_id: int = 0
    # Secret used to sign Mini App access tokens. Falls back to the bot token
    # if unset (still per-deployment secret), but a dedicated value is better.
    mini_app_secret: str = ""
    mini_app_login_ttl_seconds: int = 300
    mini_app_token_ttl_seconds: int = 3600

    # Public API and import safety limits.
    api_max_body_bytes: int = 5 * 1024 * 1024
    api_rate_limit_requests_per_minute: int = 120
    readiness_min_free_mb: int = 256
    health_import_max_upload_mb: int = 300
    health_import_max_xml_mb: int = 4096
    health_import_max_zip_members: int = 100_000
    health_import_max_compression_ratio: float = 250.0

    default_calories: int = 2100
    default_protein: int = 150
    default_steps: int = 8000

    # Routine learning + proactive coaching
    routine_window_days: int = 45
    health_retention_days: int = 548  # ~18 months kept from Apple Health export
    analytics_retention_days: int = 180
    audit_retention_days: int = 365
    job_state_retention_days: int = 90
    approval_retention_days: int = 90
    # product_events is the canonical interaction trace, read by trace_reader,
    # session_trace, session_review and turn_context. It grows at roughly
    # 1,150 rows per hour of active use and was the only high-volume table
    # with no retention at all. The window is deliberately long: purging it
    # aggressively would break historical reconstruction, which is the whole
    # point of the table.
    product_events_retention_days: int = 365
    intraday_calorie_trigger: int = 650  # first "next meals" nudge threshold

    # Proactive messaging: useful coaching without notification fatigue.
    proactive_daily_limit: int = 4
    proactive_min_gap_minutes: int = 75
    proactive_quiet_start: str = "22:30"
    proactive_quiet_end: str = "07:00"
    proactive_max_attempts: int = 3
    proactive_claim_ttl_minutes: int = 20

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_runtime(self) -> None:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN חסר בקובץ .env")
        if not self.telegram_allowed_user_id:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_ID חסר בקובץ .env")
        if self.app_env == "production":
            if self.enable_healthkit_api and len(self.healthkit_api_token) < 32:
                raise RuntimeError("HEALTHKIT_API_TOKEN חייב להכיל לפחות 32 תווים כאשר ENABLE_HEALTHKIT_API=true")
            if not self.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY נדרש במצב production")
            if self.public_base_url and not self.public_base_url.startswith("https://"):
                raise RuntimeError("PUBLIC_BASE_URL חייב להתחיל ב-https:// במצב production")
            if self.public_base_url and len(self.mini_app_secret) < 32:
                raise RuntimeError(
                    "MINI_APP_SECRET עצמאי של לפחות 32 תווים נדרש כאשר Mini App פעיל"
                )
            if self.mini_app_secret == self.telegram_bot_token:
                raise RuntimeError("MINI_APP_SECRET חייב להיות שונה מ-TELEGRAM_BOT_TOKEN")
        try:
            ZoneInfo(self.timezone_name)
        except Exception as exc:
            raise RuntimeError(f"TIMEZONE_NAME אינו תקין: {self.timezone_name}") from exc
        for name, value in (
            ("PROACTIVE_QUIET_START", self.proactive_quiet_start),
            ("PROACTIVE_QUIET_END", self.proactive_quiet_end),
        ):
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError as exc:
                raise RuntimeError(f"{name} חייב להיות בפורמט HH:MM") from exc
        if self.api_max_body_bytes < 1024:
            raise RuntimeError("API_MAX_BODY_BYTES קטן מדי")
        if self.api_rate_limit_requests_per_minute <= 0:
            raise RuntimeError("API_RATE_LIMIT_REQUESTS_PER_MINUTE חייב להיות חיובי")
        if min(
            self.telegram_read_timeout,
            self.telegram_write_timeout,
            self.telegram_connect_timeout,
            self.telegram_pool_timeout,
            self.telegram_get_updates_read_timeout,
            self.telegram_get_updates_write_timeout,
            self.telegram_get_updates_connect_timeout,
            self.telegram_get_updates_pool_timeout,
        ) <= 0:
            raise RuntimeError("הגדרות timeout של Telegram חייבות להיות חיוביות")
        if self.telegram_connection_pool_size < 1 or self.telegram_get_updates_connection_pool_size < 1:
            raise RuntimeError("גודל connection pool של Telegram חייב להיות חיובי")
        if self.proactive_daily_limit < 0 or self.proactive_max_attempts < 1:
            raise RuntimeError("הגדרות ההתראות אינן תקינות")
        if (
            self.health_import_max_upload_mb <= 0
            or self.health_import_max_local_mb <= 0
            or self.health_import_max_xml_mb <= 0
            or self.health_import_max_zip_members <= 0
            or self.health_import_max_compression_ratio <= 1
        ):
            raise RuntimeError("מגבלות קובצי Health אינן תקינות")
        if self.enable_local_health_path_import:
            try:
                from noam_coach.services.local_health_path import allowed_roots_from_text

                allowed_roots_from_text(self.local_health_import_allowed_roots)
            except ValueError as exc:
                raise RuntimeError(f"LOCAL_HEALTH_IMPORT_ALLOWED_ROOTS אינו תקין: {exc}") from exc
        if (
            min(
                self.photo_retention_days,
                self.health_retention_days,
                self.analytics_retention_days,
                self.audit_retention_days,
                self.job_state_retention_days,
                self.approval_retention_days,
                self.product_events_retention_days,
            )
            <= 0
        ):
            raise RuntimeError("תקופות שמירת המידע חייבות להיות חיוביות")
        secrets_in_use = [
            value
            for value in (
                self.telegram_bot_token,
                self.healthkit_api_token,
                self.mini_app_secret,
            )
            if value
        ]
        if len(secrets_in_use) != len(set(secrets_in_use)):
            raise RuntimeError("כל secret/token חייב להיות ייחודי")


def assert_safe_database_path(path: str | None) -> Path:
    """Startup-boundary guard for the runtime database location.

    Called explicitly by the real runtime entrypoint (before the DB is opened)
    and by the preflight command — never on plain import or generic Settings
    construction, so unit tests that don't cross the startup boundary keep
    working, and importing a module never creates a directory or touches disk.

    Rejects the dangerous defaults that let a stray ``noam_coach.db`` appear in
    the repository root or an unresolved location:

    * empty / missing path  -> fail (no silent fallback to ``./noam_coach.db``)
    * relative path         -> fail (must be explicitly absolute)
    * ``.`` / ``..`` segments left in the path -> fail (must be fully resolved)

    An absolute, normalized path is accepted regardless of *where* it points, so
    temporary absolute DB paths used by tests remain valid — the canonical
    ``data/noam_coach.db`` is not hard-coded as the only permissible location.
    Returns the resolved :class:`Path`. Does **not** create anything.
    """
    if path is None or not str(path).strip():
        raise RuntimeError(
            "DATABASE_PATH חסר: יש להגדיר נתיב מסד נתונים מוחלט ומפורש "
            "(לדוגמה C:\\coach_bot\\noam-coach\\data\\noam_coach.db). "
            "אין fallback ל-./noam_coach.db."
        )
    raw = str(path).strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(
            f"DATABASE_PATH חייב להיות נתיב מוחלט, לא יחסי: {raw!r}. "
            "נתיב יחסי עלול ליצור noam_coach.db בשורש הריפוזיטורי."
        )
    # Reject un-normalized paths (stray '.'/'..' segments) so the resolved
    # runtime location is unambiguous. resolve(strict=False) does not touch disk
    # for a non-existent file and does not create the parent directory.
    resolved = candidate.resolve(strict=False)
    if any(part in (".", "..") for part in candidate.parts):
        raise RuntimeError(
            f"DATABASE_PATH חייב להיות מפורש ומנורמל (ללא '.'/'..'): {raw!r} "
            f"→ {resolved}"
        )
    return resolved


try:
    APP_VERSION = (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() or "0.0.0")
except FileNotFoundError:
    APP_VERSION = "0.0.0"
SETTINGS = Settings()
TZ = ZoneInfo(SETTINGS.timezone_name)

class _SecretRedactionFilter(logging.Filter):
    """Strip Telegram bot token / API keys from any log line.

    httpx logs the full request URL, which for Telegram includes the bot token
    (``api.telegram.org/bot<TOKEN>/...``). This filter redacts the configured
    secrets from every record so a token never reaches the terminal or a file.
    """

    def __init__(self, secrets_to_redact: list[str]) -> None:
        super().__init__()
        # Only redact non-trivial secrets to avoid masking unrelated text.
        self._secrets = [s for s in secrets_to_redact if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - never let logging itself crash
                return True
            redacted = message
            for secret in self._secrets:
                if secret in redacted:
                    redacted = redacted.replace(secret, "***REDACTED***")
            if redacted != message:
                record.msg = redacted
                record.args = ()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("noam_coach")

# Telegram bot token leaks through httpx request URLs; quiet those libraries and
# redact secrets defensively across all handlers (terminal + files).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
_redaction_filter = _SecretRedactionFilter(
    [
        SETTINGS.telegram_bot_token,
        SETTINGS.openai_api_key,
        SETTINGS.healthkit_api_token,
        SETTINGS.mini_app_secret,
    ]
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_redaction_filter)


@dataclass
class RuntimeState:
    db_ready: bool = False
    telegram_ready: bool = False
    shutting_down: bool = False
    startup_error: str | None = None


RUNTIME_STATE = RuntimeState()

OPENAI_CLIENT = AsyncOpenAI(api_key=SETTINGS.openai_api_key) if SETTINGS.openai_api_key else None
