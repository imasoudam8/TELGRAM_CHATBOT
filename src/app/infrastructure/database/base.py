"""SQLAlchemy Declarative Base and timezone-aware utilities."""

from datetime import date, datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # noqa: UP017
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, CheckConstraint, DateTime
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


@compiles(BigInteger, "sqlite")
def _sqlite_compile_bigint(type_: BigInteger, compiler: object, **kw: object) -> str:
    return "INTEGER"


_orig_get_column_default_string = SQLiteDDLCompiler.get_column_default_string


def _custom_sqlite_get_column_default_string(self: SQLiteDDLCompiler, column: object) -> str | None:
    server_def = getattr(column, "server_default", None)
    if server_def is not None and hasattr(server_def, "arg"):
        sql = str(server_def.arg)
        if "ARRAY[" in sql:
            return "'[]'"
        if "jsonb" in sql:
            return "'{}'"
        if "gen_random_uuid" in sql:
            return "'00000000-0000-0000-0000-000000000000'"
        if "now()" in sql or "clock_timestamp()" in sql:
            return "CURRENT_TIMESTAMP"
    return _orig_get_column_default_string(self, column)


SQLiteDDLCompiler.get_column_default_string = _custom_sqlite_get_column_default_string


@compiles(CheckConstraint, "sqlite")
def _sqlite_skip_pg_interval_checks(
    element: CheckConstraint,
    compiler: str,
    **kw: object,
) -> str | None:
    sqltext = str(element.sqltext)
    if "INTERVAL" in sqltext or "jsonb_" in sqltext:
        return None
    return compiler.visit_check_constraint(element, **kw)


@compiles(JSONB, "sqlite")
def _sqlite_compile_jsonb(element: JSONB, compiler: object, **kw: object) -> str:
    return "JSON"


@compiles(ARRAY, "sqlite")
def _sqlite_compile_array(element: ARRAY, compiler: object, **kw: object) -> str:
    return "JSON"


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
