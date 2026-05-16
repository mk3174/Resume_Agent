"""SQLite-backed sliding-window rate limiter.

Counts how many `applied` records exist in the last hour and returns whether
another submit is allowed. The cap comes from settings.apply.rate_limit_per_hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from config_loader import load_settings
from store.db import session
from store.models import Application


def hourly_used() -> int:
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    with session() as s:
        n = s.scalar(
            select(func.count(Application.id))
            .where(Application.status == "applied")
            .where(Application.updated_at >= one_hour_ago)
        )
        return int(n or 0)


def remaining() -> int:
    cap = int(load_settings()["apply"].get("rate_limit_per_hour", 8))
    return max(0, cap - hourly_used())


def can_apply() -> bool:
    return remaining() > 0
