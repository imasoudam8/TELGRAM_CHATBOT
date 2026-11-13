"""Typed DTOs and domain exceptions for administration, RBAC, moderation, and config."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class AdminPermissionError(PermissionError):
    """Raised when an operator lacks required active admin privileges or role."""


class AdminValidationError(ValueError):
    """Raised when admin input fails schema, boundary, or reason validation."""


class AdminNotFoundError(LookupError):
    """Raised when a referenced target user, report, config, or block record does not exist."""


class AdminConflictError(RuntimeError):
    """Raised on concurrent state conflicts or forbidden self/last-admin transitions."""


@dataclass(frozen=True)
class AdminActorDTO:
    """Verified active admin actor context resolved from DB."""

    user_id: int
    public_id: str
    role: str
    is_active: bool


@dataclass(frozen=True)
class AdminModerationSummaryDTO:
    """Compact moderation action record displayed safely in user inspection."""

    id: int
    action_type: str
    reason: str
    expires_at: datetime | None
    created_at: datetime
    report_id: int | None


@dataclass(frozen=True)
class AdminUserDetailDTO:
    """Sanitized administrative inspection view of a user without exposing Telegram ID."""

    user_id: int
    public_id: str
    account_status: str
    age_18_plus_declared: bool
    created_at: datetime
    last_activity_at: datetime
    suspended_until: datetime | None
    suspension_reason: str | None
    is_chat_restricted: bool
    chat_restriction_reason: str | None
    chat_restriction_expires_at: datetime | None
    admin_role: str | None
    admin_is_active: bool | None
    display_name: str | None
    age: int | None
    gender: str | None
    city: str | None
    profile_completion_status: str | None
    profile_visibility: str | None
    in_active_chat: bool
    reports_against_count: int
    reports_submitted_count: int
    recent_actions: tuple[AdminModerationSummaryDTO, ...]


@dataclass(frozen=True)
class EvidenceHoldMetadataDTO:
    """Privacy-safe metadata for an evidence hold record (zero plaintext message body)."""

    id: int
    message_id: int | None
    chat_session_id: uuid.UUID
    message_content_hash: str
    message_type: str | None
    hold_type: str
    status: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class ReportQueueItemDTO:
    """Summary entry for a report in the moderation queue."""

    id: int
    category: str
    status: str
    reporter_public_id: str
    reported_public_id: str
    chat_session_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True)
class ReportQueuePageDTO:
    """Paginated slice of moderation queue reports."""

    items: tuple[ReportQueueItemDTO, ...]
    total_count: int
    offset: int
    limit: int
    has_prev: bool
    has_next: bool
    status_filter: str


@dataclass(frozen=True)
class ReportDetailDTO:
    """Full moderation triage view for a report with evidence metadata only."""

    id: int
    category: str
    status: str
    description: str | None
    reporter_public_id: str
    reported_public_id: str
    reported_account_status: str
    reported_is_chat_restricted: bool
    chat_session_id: uuid.UUID | None
    reported_message_id: int | None
    moderator_public_id: str | None
    resolution_notes: str | None
    created_at: datetime
    reviewed_at: datetime | None
    evidence_holds: tuple[EvidenceHoldMetadataDTO, ...]


@dataclass(frozen=True)
class ModerationOutcomeDTO:
    """Result of executing a moderation action or restoration against a user."""

    action_id: int | None
    action_type: str
    target_user_id: int
    target_public_id: str
    target_status: str
    is_chat_restricted: bool
    expires_at: datetime | None
    audit_log_id: int
    terminated_chat_id: uuid.UUID | None = None
    target_telegram_id: int | None = None
    terminated_partner_telegram_id: int | None = None
    requires_queue_removal: bool = False


@dataclass(frozen=True)
class AdminBlockOutcomeDTO:
    """Result of a moderation-initiated block or unblock between two users."""

    blocked: bool
    blocker_public_id: str
    blocked_public_id: str
    audit_log_id: int
    terminated_chat_id: uuid.UUID | None = None
    participant_telegram_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AdminWalletOpDTO:
    """Result of a SUPER_ADMIN wallet grant or debit including audit log lineage."""

    target_public_id: str
    operation: str
    amount: int
    purchased_coins_after: int
    free_coins_after: int
    total_spendable_after: int
    ledger_ids: tuple[int, ...]
    audit_log_id: int | None
    already_applied: bool = False


@dataclass(frozen=True)
class SystemConfigItemDTO:
    """Representation of an allowlisted system configuration entry."""

    key: str
    category: str
    field_name: str
    persisted_value: dict[str, Any] | None
    effective_int_value: int
    min_value: int
    max_value: int
    label_persian: str
    description: str
    updated_at: datetime | None


@dataclass(frozen=True)
class AdminAuditLogDTO:
    """Formatted entry from immutable admin_audit_logs."""

    id: int
    admin_public_id: str
    action: str
    target_type: str
    target_id: str
    payload_before: dict[str, Any] | None
    payload_after: dict[str, Any] | None
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class AdminAuditLogPageDTO:
    """Paginated audit log entries."""

    items: tuple[AdminAuditLogDTO, ...]
    total_count: int
    offset: int
    limit: int
    has_prev: bool
    has_next: bool


@dataclass(frozen=True)
class AdminRoleDTO:
    """Assigned internal operator record."""

    user_id: int
    public_id: str
    role: str
    is_active: bool
    assigned_by_public_id: str | None
    updated_at: datetime
