"""Admin SQLAlchemy models and domain module exports."""

from importlib import import_module
from typing import Any

from src.app.modules.admin.models import AdminAuditLog, AdminUser, SystemConfiguration
from src.app.modules.admin.schemas import (
    AdminConflictError,
    AdminNotFoundError,
    AdminPermissionError,
    AdminValidationError,
)

__all__ = [
    "ALLOWLISTED_SYSTEM_CONFIGS",
    "ROLE_LABELS_PERSIAN",
    "ROLE_MODERATOR",
    "ROLE_SUPER_ADMIN",
    "ROLE_SUPPORT",
    "VALID_ADMIN_ROLES",
    "AdminAuditLog",
    "AdminConfigService",
    "AdminConflictError",
    "AdminNotFoundError",
    "AdminPermissionError",
    "AdminService",
    "AdminUser",
    "AdminValidationError",
    "SystemConfiguration",
    "get_effective_chat_tariffs_in_session",
    "get_effective_config_int_in_session",
    "get_effective_direct_request_expiry_hours_in_session",
    "get_effective_message_retention_days_in_session",
    "get_effective_video_size_limit_in_session",
    "record_audit_log_in_session",
    "require_admin_actor_in_session",
    "resolve_active_admin_in_session",
    "validate_config_payload",
]

_LAZY_EXPORTS: dict[str, str] = {
    "record_audit_log_in_session": "src.app.modules.admin.audit",
    "ALLOWLISTED_SYSTEM_CONFIGS": "src.app.modules.admin.config_service",
    "AdminConfigService": "src.app.modules.admin.config_service",
    "get_effective_chat_tariffs_in_session": "src.app.modules.admin.config_service",
    "get_effective_config_int_in_session": "src.app.modules.admin.config_service",
    "get_effective_direct_request_expiry_hours_in_session": "src.app.modules.admin.config_service",
    "get_effective_message_retention_days_in_session": "src.app.modules.admin.config_service",
    "get_effective_video_size_limit_in_session": "src.app.modules.admin.config_service",
    "validate_config_payload": "src.app.modules.admin.config_service",
    "ROLE_LABELS_PERSIAN": "src.app.modules.admin.rbac",
    "ROLE_MODERATOR": "src.app.modules.admin.rbac",
    "ROLE_SUPER_ADMIN": "src.app.modules.admin.rbac",
    "ROLE_SUPPORT": "src.app.modules.admin.rbac",
    "VALID_ADMIN_ROLES": "src.app.modules.admin.rbac",
    "require_admin_actor_in_session": "src.app.modules.admin.rbac",
    "resolve_active_admin_in_session": "src.app.modules.admin.rbac",
    "AdminService": "src.app.modules.admin.service",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is not None:
        return getattr(import_module(module_path), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
