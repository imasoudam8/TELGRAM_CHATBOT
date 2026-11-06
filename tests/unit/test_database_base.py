"""Unit tests for SQLAlchemy base, mixins, and timezone conversions."""

from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # noqa: UP017
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.app.infrastructure.database.base import (
    Base,
    TimestampMixin,
    get_business_date,
    now_utc,
    to_business_tz,
)


class SampleModel(Base, TimestampMixin):
    """Temporary test model verifying Base and TimestampMixin behavior."""

    __tablename__ = "test_sample_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)


def test_now_utc_is_timezone_aware() -> None:
    """Verify now_utc returns timezone-aware UTC datetime."""
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.tzinfo == UTC


def test_to_business_tz_conversion() -> None:
    """Verify UTC to Asia/Tehran timezone conversion."""
    tehran_tz = ZoneInfo("Asia/Tehran")
    # 2026-03-21 21:00:00 UTC is 2026-03-22 00:30:00 in Tehran (+03:30)
    utc_dt = datetime(2026, 3, 21, 21, 0, 0, tzinfo=UTC)
    tehran_dt = to_business_tz(utc_dt, tehran_tz)

    assert tehran_dt.year == 2026
    assert tehran_dt.month == 3
    assert tehran_dt.day == 22
    assert tehran_dt.hour == 0
    assert tehran_dt.minute == 30


def test_get_business_date() -> None:
    """Verify business date boundary extraction."""
    tehran_tz = ZoneInfo("Asia/Tehran")
    utc_night = datetime(2026, 6, 15, 22, 0, 0, tzinfo=UTC)
    b_date = get_business_date(utc_night, tehran_tz)
    # At 22:00 UTC, Tehran is on next day (01:30)
    assert b_date.day == 16


async def test_sample_model_timestamps(sqlite_session: AsyncSession) -> None:
    """Verify model persistence with automatic UTC timestamp creation."""
    item = SampleModel(name="test_item")
    sqlite_session.add(item)
    await sqlite_session.flush()

    assert item.id is not None
    assert item.created_at is not None
    assert item.updated_at is not None
    assert item.created_at.tzinfo is not None
