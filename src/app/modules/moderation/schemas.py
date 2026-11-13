"""Typed DTOs and constants for user reporting and moderation workflows."""

import uuid
from dataclasses import dataclass
from datetime import datetime

REPORT_CATEGORIES: dict[str, str] = {
    "HARASSMENT": "آزار و اذیت",
    "SPAM": "هرزنامه و تبلیغات",
    "INAPPROPRIATE_CONTENT": "محتوای نامناسب",
    "SCAM_FRAUD": "کلاهبرداری یا سوءاستفاده",
    "UNDERAGE_SUSPICION": "احتمال زیر ۱۸ سال",
    "OTHER": "سایر موارد",
}

REPORT_STATUSES = frozenset({"PENDING", "REVIEWED", "ACTIONED", "DISMISSED"})
MODERATION_ACTION_TYPES = frozenset(
    {"WARNING", "TEMPORARY_SUSPENSION", "PERMANENT_BAN", "CHAT_RESTRICTION"}
)

MAX_REPORT_DESCRIPTION_LENGTH = 500
REPORT_COOLDOWN_MINUTES = 10
REPORT_EVIDENCE_MESSAGE_LIMIT = 10


@dataclass(frozen=True)
class ReportSubmissionResult:
    """Privacy-safe result returned after registering a user abuse report."""

    report_id: int
    status: str
    category: str
    reported_public_id: str
    chat_session_id: uuid.UUID | None
    evidence_holds_created: int
    created_at: datetime
