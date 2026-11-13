"""Profile discovery and preferences module."""

from src.app.modules.discovery.schemas import (
    ALLOWED_ACTIVITY_WINDOWS_HOURS,
    RELAXABLE_FIELD_KEYS,
    DiscoveryPageDTO,
    PublicProfileDTO,
    UserPreferencesDTO,
    format_relative_activity,
)
from src.app.modules.discovery.service import (
    DiscoveryService,
    preferences_model_to_dto,
    profile_matches_preferences,
    validate_preferences_inputs,
)

__all__ = [
    "ALLOWED_ACTIVITY_WINDOWS_HOURS",
    "RELAXABLE_FIELD_KEYS",
    "DiscoveryPageDTO",
    "DiscoveryService",
    "PublicProfileDTO",
    "UserPreferencesDTO",
    "format_relative_activity",
    "preferences_model_to_dto",
    "profile_matches_preferences",
    "validate_preferences_inputs",
]
