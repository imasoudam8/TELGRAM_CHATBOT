"""Centralized registry importing all SQLAlchemy ORM models."""

from src.app.infrastructure.database.base import Base
from src.app.modules.admin.models import AdminAuditLog, AdminUser, SystemConfiguration
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    DirectRequest,
    MessageEvidenceHold,
    UserBlock,
    UserConnection,
)
from src.app.modules.moderation.models import ModerationAction, Report
from src.app.modules.users.models import User, UserPreferences, UserProfile
from src.app.modules.wallet.models import (
    Wallet,
    WalletDailyAllowance,
    WalletLedger,
)

__all__ = [
    "ActiveChatParticipant",
    "AdminAuditLog",
    "AdminUser",
    "Base",
    "ChatMessage",
    "ChatSession",
    "DirectRequest",
    "MessageEvidenceHold",
    "ModerationAction",
    "Report",
    "SystemConfiguration",
    "User",
    "UserBlock",
    "UserConnection",
    "UserPreferences",
    "UserProfile",
    "Wallet",
    "WalletDailyAllowance",
    "WalletLedger",
]
