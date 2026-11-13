"""Random matchmaking module."""

from src.app.modules.matching.service import (
    QUEUE_TTL_SECONDS,
    RECENT_PAIR_WINDOW_HOURS,
    MatchResult,
    RandomMatchService,
    is_mutual_match,
)

__all__ = [
    "QUEUE_TTL_SECONDS",
    "RECENT_PAIR_WINDOW_HOURS",
    "MatchResult",
    "RandomMatchService",
    "is_mutual_match",
]
