"""Runtime configuration, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    portal_url: str
    portal_email: str
    portal_password: str
    # The portal allows ~120 requests/minute across all endpoints. Stay under it.
    portal_requests_per_minute: int
    # How long the in-memory meter snapshot is considered fresh.
    index_ttl_seconds: int
    # How long a meter's readings response is cached.
    readings_ttl_seconds: int
    request_timeout_seconds: int
    # Load the fleet snapshot at startup instead of on the first request.
    warm_on_startup: bool


def load_settings() -> Settings:
    return Settings(
        portal_url=os.getenv("PORTAL_URL", "https://urja-ops.flockenergy.tech").rstrip("/"),
        portal_email=os.getenv("PORTAL_EMAIL", "operator@urja.local"),
        portal_password=os.getenv("PORTAL_PASSWORD", "urja-ops-2026"),
        portal_requests_per_minute=_env_int("PORTAL_REQUESTS_PER_MINUTE", 100),
        index_ttl_seconds=_env_int("INDEX_TTL_SECONDS", 600),
        readings_ttl_seconds=_env_int("READINGS_TTL_SECONDS", 300),
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 20),
        warm_on_startup=os.getenv("WARM_ON_STARTUP", "1").lower() not in ("0", "false", "no"),
    )
