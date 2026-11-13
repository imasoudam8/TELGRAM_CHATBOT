"""Schemas and DTOs for profile discovery and matching preferences."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

RELAXABLE_FIELD_KEYS = frozenset({"gender", "age", "city", "recent_activity"})
ALLOWED_ACTIVITY_WINDOWS_HOURS = frozenset({1, 24, 168, 720})


def format_relative_activity(last_activity_at: datetime, now: datetime | None = None) -> str:
    """Format last_activity_at as privacy-safe relative activity wording in Persian.

    Never claims real-time Telegram online status.
    """
    ref_now = now or datetime.now(UTC)
    ts = (
        last_activity_at
        if last_activity_at.tzinfo is not None
        else last_activity_at.replace(tzinfo=UTC)
    )
    delta = ref_now - ts
    if delta <= timedelta(minutes=15):
        return "فعال در دقایق اخیر"
    if delta <= timedelta(hours=1):
        return "فعال در یک ساعت گذشته"
    if delta <= timedelta(hours=24):
        return "فعال در ۲۴ ساعت گذشته"
    if delta <= timedelta(days=7):
        return "فعال در هفته گذشته"
    if delta <= timedelta(days=30):
        return "فعال در ماه گذشته"
    return "کمتر فعال در روزهای اخیر"


@dataclass(frozen=True)
class PublicProfileDTO:
    """Public profile representation strictly isolated from Telegram numeric IDs."""

    user_id: int
    public_id: str
    display_name: str
    age: int
    gender: str
    city: str | None
    bio: str | None
    last_activity_at: datetime
    in_active_chat: bool = False


@dataclass
class UserPreferencesDTO:
    """Normalized representation of user discovery/matchmaking preferences."""

    user_id: int
    preferred_genders: list[str] = field(default_factory=list)
    min_age: int = 18
    max_age: int = 99
    preferred_cities: list[str] = field(default_factory=list)
    recent_activity_hours: int | None = None
    relaxable_fields: set[str] = field(default_factory=set)

    @property
    def has_active_filters(self) -> bool:
        """Return True if any narrowing filter beyond defaults is configured."""
        return bool(
            self.preferred_genders
            or self.min_age > 18
            or self.max_age < 99
            or self.preferred_cities
            or self.recent_activity_hours is not None
        )

    @property
    def has_relaxable_active_filters(self) -> bool:
        """Return True only when user explicitly marked at least one active filter as relaxable."""
        if not self.relaxable_fields:
            return False
        if "gender" in self.relaxable_fields and self.preferred_genders:
            return True
        if "age" in self.relaxable_fields and (self.min_age > 18 or self.max_age < 99):
            return True
        if "city" in self.relaxable_fields and self.preferred_cities:
            return True
        if "recent_activity" in self.relaxable_fields and self.recent_activity_hours is not None:
            return True
        return False


@dataclass(frozen=True)
class DiscoveryPageDTO:
    """Paginated profile discovery response."""

    items: list[PublicProfileDTO]
    total_count: int
    offset: int
    limit: int
    has_next: bool
    has_prev: bool
    used_relaxed: bool = False
    relaxed_available: bool = False
    relaxed_count: int = 0
