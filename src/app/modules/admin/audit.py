"""Transactional append-only audit logging service for sensitive admin operations."""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import now_utc
from src.app.modules.admin.models import AdminAuditLog
from src.app.modules.admin.schemas import AdminValidationError

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "bot_token",
        "password",
        "postgres_password",
        "redis_password",
        "secret",
        "plaintext",
        "message_text",
        "raw_message",
    }
)


def _sanitize_audit_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    sanitized: dict[str, Any] = {}
    for key, val in payload.items():
        lower_key = str(key).strip().lower()
        if lower_key in FORBIDDEN_PAYLOAD_KEYS:
            sanitized[str(key)] = "[REDACTED]"
        elif isinstance(val, dict):
            sanitized[str(key)] = _sanitize_audit_payload(val)
        elif isinstance(val, datetime):
            sanitized[str(key)] = val.isoformat()
        else:
            sanitized[str(key)] = val
    return sanitized


async def record_audit_log_in_session(
    session: AsyncSession,
    *,
    admin_user_id: int,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    payload_before: dict[str, Any] | None = None,
    payload_after: dict[str, Any] | None = None,
    utc_now: datetime | None = None,
) -> AdminAuditLog:
    """Append an AdminAuditLog record inside the caller's active transaction.

    Because the audit row is flushed within the same session transaction, any failure
    to insert the audit entry forces the entire business mutation to roll back cleanly.
    """
    clean_reason = reason.strip() if reason else ""
    if not clean_reason:
        raise AdminValidationError("ذکر دلیل برای ثبت عملیات در گزارش ممیزی الزامی است.")

    clean_action = action.strip().upper()[:50]
    clean_target_type = target_type.strip().upper()[:30]
    clean_target_id = str(target_id).strip()[:64]
    if not clean_action or not clean_target_type or not clean_target_id:
        raise AdminValidationError("مشخصات عملیات ممیزی ناقص است.")

    entry = AdminAuditLog(
        admin_user_id=admin_user_id,
        action=clean_action,
        target_type=clean_target_type,
        target_id=clean_target_id,
        payload_before=_sanitize_audit_payload(payload_before),
        payload_after=_sanitize_audit_payload(payload_after),
        reason=clean_reason,
        created_at=utc_now or now_utc(),
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry
