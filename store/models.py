"""SQLAlchemy ORM models. SQLite by default; swap connection string for Postgres."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stable_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    company: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    semantic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ats_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimates_present: Mapped[bool] = mapped_column(default=False)
    barriers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
