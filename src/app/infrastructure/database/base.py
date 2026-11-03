"""SQLAlchemy Declarative Base and timezone-aware utilities."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    __abstract__ = True


class TimestampMixin:
    """Reusable model mixin providing timezone-aware UTC created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


def now_utc() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_business_tz(utc_dt: datetime, business_tz: ZoneInfo) -> datetime:
    """Convert a UTC datetime to the business timezone.

    Ensures the source datetime is timezone-aware UTC before conversion.
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    return utc_dt.astimezone(business_tz)


def get_business_date(utc_dt: datetime, business_tz: ZoneInfo) -> date:
    """Extract the calendar date in the business timezone for a given UTC timestamp."""
    return to_business_tz(utc_dt, business_tz).date()
