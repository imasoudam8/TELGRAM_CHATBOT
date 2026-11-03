"""Unit tests for bot handlers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message, User

from src.app.bot.handlers.base import handle_start_smoke_test


@pytest.mark.asyncio
async def test_start_smoke_test_handler_sends_smoke_message() -> None:
    """Verify /start handler replies with system status message."""
    mock_message = MagicMock(spec=Message)
    mock_user = MagicMock(spec=User)
    mock_user.id = 123456789
    mock_message.from_user = mock_user
    mock_message.answer = AsyncMock()

    await handle_start_smoke_test(mock_message)

    mock_message.answer.assert_awaited_once()
    args, _ = mock_message.answer.call_args
    assert "System Online" in args[0]
    assert "health-check mode" in args[0]
