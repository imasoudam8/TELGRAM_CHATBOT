"""Custom aiogram filter checking if the acting user is in an active chat."""

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.chat.session_service import ChatService


class ActiveChatFilter(BaseFilter):
    """Matches incoming messages when the sender is currently in an active chat."""

    async def __call__(
        self,
        message: Message,
        db_manager: DatabaseManager | None = None,
        settings: Settings | None = None,
        **kwargs: Any,
    ) -> bool | dict[str, Any]:
        if message.from_user is None:
            return False

        if message.text and message.text.startswith("/start"):
            return False

        resolved_settings = settings or get_settings()
        resolved_db = db_manager or get_database_manager(resolved_settings)
        chat_service = ChatService(resolved_db)
        active_chat = await chat_service.get_active_chat_by_telegram_id(message.from_user.id)
        if active_chat is None:
            return False

        return {"active_chat": active_chat}
