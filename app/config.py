"""Runtime configuration for one grading point.

Each container/process gets its own ``SITE_ID`` and ``DATABASE_PATH``.  Two
services therefore never share a SQLite file unless explicitly configured to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    return value


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    site_id: str
    database_path: str
    default_target_base_url: str | None
    max_unscored: int | None
    seed_count: int
    seed_prefix: str
    score_min: float
    score_max: float
    http_timeout: float
    recovery_enabled: bool
    recovery_interval_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        site_id = os.getenv("SITE_ID", "point-a").strip() or "point-a"
        default_prefix = "".join(part[:1].upper() for part in site_id.replace("_", "-").split("-") if part)[:2] or "P"
        return cls(
            site_id=site_id,
            database_path=os.getenv("DATABASE_PATH", "./data/grading.db"),
            default_target_base_url=(os.getenv("DEFAULT_TARGET_BASE_URL") or "").strip() or None,
            # None/empty means unlimited.  Compose gives point-b a small demo quota.
            max_unscored=_get_int("MAX_UNSCORED", None),
            seed_count=_get_int("SEED_COUNT", 8) or 0,
            seed_prefix=(os.getenv("SEED_PREFIX") or default_prefix).strip(),
            score_min=_get_float("SCORE_MIN", 0.0),
            score_max=_get_float("SCORE_MAX", 100.0),
            http_timeout=_get_float("HTTP_TIMEOUT", 5.0),
            recovery_enabled=_get_bool("RECOVERY_ENABLED", True),
            recovery_interval_seconds=_get_float("RECOVERY_INTERVAL_SECONDS", 3.0),
        )
