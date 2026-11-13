"""Moderation SQLAlchemy models and domain module exports."""

from importlib import import_module
from typing import Any

from src.app.modules.moderation.models import ModerationAction, Report
from src.app.modules.moderation.schemas import (
    MODERATION_ACTION_TYPES,
    REPORT_CATEGORIES,
    REPORT_STATUSES,
    ReportSubmissionResult,
)

__all__ = [
    "MODERATION_ACTION_TYPES",
    "REPORT_CATEGORIES",
    "REPORT_STATUSES",
    "AccountRestrictionStatus",
    "ModerationAction",
    "Report",
    "ReportService",
    "ReportSubmissionResult",
    "evaluate_user_restrictions_in_session",
    "format_account_restriction_message",
    "get_active_chat_restriction_in_session",
    "has_active_chat_restriction_in_session",
    "reconcile_user_suspension_in_session",
]

_LAZY_EXPORTS: dict[str, str] = {
    "AccountRestrictionStatus": "src.app.modules.moderation.enforcement",
    "evaluate_user_restrictions_in_session": "src.app.modules.moderation.enforcement",
    "format_account_restriction_message": "src.app.modules.moderation.enforcement",
    "get_active_chat_restriction_in_session": "src.app.modules.moderation.enforcement",
    "has_active_chat_restriction_in_session": "src.app.modules.moderation.enforcement",
    "reconcile_user_suspension_in_session": "src.app.modules.moderation.enforcement",
    "ReportService": "src.app.modules.moderation.service",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is not None:
        return getattr(import_module(module_path), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
