"""Fake Telegram Bot API transport session and Dispatcher update harness for integration tests.

Never calls real Telegram HTTP endpoints and never requires a real Telegram bot token.
All outgoing Bot API calls are recorded in memory so tests inspect exact messages,
inline keyboards, callback answers, media payloads, and privacy guarantees.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendAnimation,
    SendMessage,
    SendPhoto,
    SendSticker,
    SendVideo,
    SendVoice,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    Animation,
    Audio,
    CallbackQuery,
    Chat,
    Document,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Sticker,
    Update,
    Video,
    Voice,
)
from aiogram.types import (
    User as TgUser,
)

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.infrastructure.telegram.bot import create_dispatcher


@dataclass
class RecordedTelegramCall:
    """In-memory record of an outgoing Telegram Bot API method call."""

    method: str
    chat_id: int | None = None
    message_id: int | None = None
    text: str | None = None
    caption: str | None = None
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None
    callback_query_id: str | None = None
    callback_text: str | None = None
    show_alert: bool = False
    media_type: str | None = None
    file_id: str | None = None


class RecordingTelegramSession(BaseSession):
    """Custom aiogram BaseSession capturing outgoing Bot API requests with zero network I/O."""

    def __init__(self, bot_user_id: int = 999999000, bot_username: str = "test_match_bot") -> None:
        super().__init__()
        self.bot_user = TgUser(
            id=bot_user_id,
            is_bot=True,
            first_name="AnonymousMatchingBot",
            username=bot_username,
        )
        self._next_message_id = 1000
        self.calls: list[RecordedTelegramCall] = []
        self.sent_messages: dict[tuple[int, int], Message] = {}
        self.chat_messages: dict[int, list[Message]] = {}
        self.fail_chat_ids: set[int] = set()

    def _allocate_message_id(self) -> int:
        mid = self._next_message_id
        self._next_message_id += 1
        return mid

    def _store_message(self, msg: Message) -> Message:
        key = (msg.chat.id, msg.message_id)
        self.sent_messages[key] = msg
        history = self.chat_messages.setdefault(msg.chat.id, [])
        for idx, existing in enumerate(history):
            if existing.message_id == msg.message_id:
                history[idx] = msg
                return msg
        history.append(msg)
        return msg

    @staticmethod
    def _coerce_chat_id(raw_chat_id: int | str) -> int:
        return int(raw_chat_id)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> Any:
        if isinstance(method, GetMe):
            return self.bot_user

        if isinstance(method, AnswerCallbackQuery):
            self.calls.append(
                RecordedTelegramCall(
                    method="answerCallbackQuery",
                    callback_query_id=method.callback_query_id,
                    callback_text=method.text,
                    show_alert=bool(method.show_alert),
                )
            )
            return True

        if isinstance(method, DeleteMessage):
            chat_id = self._coerce_chat_id(method.chat_id)
            self.calls.append(
                RecordedTelegramCall(
                    method="deleteMessage",
                    chat_id=chat_id,
                    message_id=method.message_id,
                )
            )
            return True

        if isinstance(method, SendMessage):
            chat_id = self._coerce_chat_id(method.chat_id)
            if chat_id in self.fail_chat_ids:
                raise RuntimeError(f"Simulated Telegram delivery failure for chat {chat_id}")
            mid = self._allocate_message_id()
            inline_kb = (
                method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None
            )
            msg = Message(
                message_id=mid,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                from_user=self.bot_user,
                text=method.text,
                reply_markup=inline_kb,
            )
            self._store_message(msg)
            self.calls.append(
                RecordedTelegramCall(
                    method="sendMessage",
                    chat_id=chat_id,
                    message_id=mid,
                    text=method.text,
                    reply_markup=method.reply_markup,
                )
            )
            return msg

        if isinstance(method, EditMessageText):
            chat_id = self._coerce_chat_id(method.chat_id or 0)
            mid = int(method.message_id or 0)
            inline_kb = (
                method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None
            )
            existing = self.sent_messages.get((chat_id, mid))
            if existing is not None:
                updated = existing.model_copy(
                    update={"text": method.text, "reply_markup": inline_kb}
                )
            else:
                updated = Message(
                    message_id=mid or self._allocate_message_id(),
                    date=datetime.now(UTC),
                    chat=Chat(id=chat_id, type="private"),
                    from_user=self.bot_user,
                    text=method.text,
                    reply_markup=inline_kb,
                )
            self._store_message(updated)
            self.calls.append(
                RecordedTelegramCall(
                    method="editMessageText",
                    chat_id=chat_id,
                    message_id=updated.message_id,
                    text=method.text,
                    reply_markup=method.reply_markup,
                )
            )
            return updated

        if isinstance(method, EditMessageReplyMarkup):
            chat_id = self._coerce_chat_id(method.chat_id or 0)
            mid = int(method.message_id or 0)
            inline_kb = (
                method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None
            )
            existing = self.sent_messages.get((chat_id, mid))
            if existing is not None:
                updated = existing.model_copy(update={"reply_markup": inline_kb})
            else:
                updated = Message(
                    message_id=mid or self._allocate_message_id(),
                    date=datetime.now(UTC),
                    chat=Chat(id=chat_id, type="private"),
                    from_user=self.bot_user,
                    text="",
                    reply_markup=inline_kb,
                )
            self._store_message(updated)
            self.calls.append(
                RecordedTelegramCall(
                    method="editMessageReplyMarkup",
                    chat_id=chat_id,
                    message_id=updated.message_id,
                    reply_markup=method.reply_markup,
                )
            )
            return updated

        if isinstance(method, (SendPhoto, SendVideo, SendVoice, SendSticker, SendAnimation)):
            chat_id = self._coerce_chat_id(method.chat_id)
            if chat_id in self.fail_chat_ids:
                raise RuntimeError(f"Simulated Telegram media delivery failure for chat {chat_id}")
            mid = self._allocate_message_id()
            caption = getattr(method, "caption", None)
            inline_kb = (
                method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None
            )
            msg_kwargs: dict[str, Any] = {
                "message_id": mid,
                "date": datetime.now(UTC),
                "chat": Chat(id=chat_id, type="private"),
                "from_user": self.bot_user,
                "caption": caption,
                "reply_markup": inline_kb,
            }
            if isinstance(method, SendPhoto):
                media_type = "PHOTO"
                fid = str(method.photo)
                msg_kwargs["photo"] = [
                    PhotoSize(file_id=fid, file_unique_id=f"u_{fid}", width=640, height=480)
                ]
            elif isinstance(method, SendVideo):
                media_type = "VIDEO"
                fid = str(method.video)
                msg_kwargs["video"] = Video(
                    file_id=fid, file_unique_id=f"u_{fid}", width=640, height=480, duration=5
                )
            elif isinstance(method, SendVoice):
                media_type = "VOICE"
                fid = str(method.voice)
                msg_kwargs["voice"] = Voice(file_id=fid, file_unique_id=f"u_{fid}", duration=4)
            elif isinstance(method, SendSticker):
                media_type = "STICKER"
                fid = str(method.sticker)
                msg_kwargs["sticker"] = Sticker(
                    file_id=fid,
                    file_unique_id=f"u_{fid}",
                    type="regular",
                    width=512,
                    height=512,
                    is_animated=False,
                    is_video=False,
                )
            else:
                media_type = "ANIMATION"
                fid = str(method.animation)
                msg_kwargs["animation"] = Animation(
                    file_id=fid, file_unique_id=f"u_{fid}", width=320, height=240, duration=2
                )

            msg = Message(**msg_kwargs)
            self._store_message(msg)
            self.calls.append(
                RecordedTelegramCall(
                    method=method.__api_method__,
                    chat_id=chat_id,
                    message_id=mid,
                    caption=caption,
                    reply_markup=method.reply_markup,
                    media_type=media_type,
                    file_id=fid,
                )
            )
            return msg

        raise NotImplementedError(
            f"RecordingTelegramSession does not handle Bot API method: {type(method).__name__}"
        )

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""

    async def close(self) -> None:
        return None


class TelegramTestHarness:
    """Drives real Dispatcher + routers + middlewares using realistic Telegram Update objects."""

    def __init__(
        self,
        *,
        settings: Settings,
        db_manager: DatabaseManager,
        redis_manager: RedisManager | None = None,
    ) -> None:
        self.settings = settings
        self.db_manager = db_manager
        self.redis_manager = redis_manager
        self.session = RecordingTelegramSession()
        self.bot = Bot(
            token=settings.BOT_TOKEN.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=self.session,
        )
        self.dp: Dispatcher = create_dispatcher(
            settings,
            db_manager=db_manager,
            redis_manager=redis_manager,
        )
        self._next_update_id = 1
        self._next_incoming_msg_id = 50000
        self._next_cb_id = 1

    def _alloc_update_id(self) -> int:
        uid = self._next_update_id
        self._next_update_id += 1
        return uid

    def _alloc_incoming_msg_id(self) -> int:
        mid = self._next_incoming_msg_id
        self._next_incoming_msg_id += 1
        return mid

    def _alloc_cb_id(self) -> str:
        cid = f"cbq_{self._next_cb_id}"
        self._next_cb_id += 1
        return cid

    @property
    def calls(self) -> list[RecordedTelegramCall]:
        return self.session.calls

    def calls_for(self, chat_id: int) -> list[RecordedTelegramCall]:
        return [c for c in self.session.calls if c.chat_id == chat_id]

    def text_calls_for(self, chat_id: int) -> list[RecordedTelegramCall]:
        return [
            c
            for c in self.session.calls
            if c.chat_id == chat_id and c.method in {"sendMessage", "editMessageText"}
        ]

    def latest_text_for(self, chat_id: int) -> str:
        texts = self.text_calls_for(chat_id)
        assert texts, f"Expected at least one text message sent to chat_id={chat_id}"
        return texts[-1].text or ""

    def find_button(
        self,
        chat_id: int,
        *,
        prefix: str | None = None,
        exact: str | None = None,
    ) -> tuple[Message, str]:
        """Locate the most recent inline button sent to chat_id matching prefix or exact."""
        for msg in reversed(self.session.chat_messages.get(chat_id, [])):
            kb = msg.reply_markup
            if not isinstance(kb, InlineKeyboardMarkup):
                continue
            for row in kb.inline_keyboard:
                for btn in row:
                    if not btn.callback_data:
                        continue
                    if exact is not None and btn.callback_data == exact:
                        return msg, btn.callback_data
                    if prefix is not None and btn.callback_data.startswith(prefix):
                        return msg, btn.callback_data
        raise AssertionError(
            f"Inline button not found for chat_id={chat_id} (prefix={prefix!r}, exact={exact!r})"
        )

    async def feed_raw_update(self, update: Update) -> list[RecordedTelegramCall]:
        start_idx = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        return self.session.calls[start_idx:]

    def build_text_update(
        self,
        tg_id: int,
        text: str,
        *,
        update_id: int | None = None,
        first_name: str = "User",
        is_command: bool = False,
    ) -> Update:
        entities = None
        if is_command or text.startswith("/"):
            cmd_len = len(text.split()[0])
            entities = [MessageEntity(type="bot_command", offset=0, length=cmd_len)]
        msg = Message(
            message_id=self._alloc_incoming_msg_id(),
            date=datetime.now(UTC),
            chat=Chat(id=tg_id, type="private"),
            from_user=TgUser(id=tg_id, is_bot=False, first_name=first_name),
            text=text,
            entities=entities,
        )
        return Update(update_id=update_id or self._alloc_update_id(), message=msg)

    async def send_text(
        self,
        tg_id: int,
        text: str,
        *,
        update_id: int | None = None,
        first_name: str = "User",
    ) -> list[RecordedTelegramCall]:
        upd = self.build_text_update(
            tg_id,
            text,
            update_id=update_id,
            first_name=first_name,
            is_command=text.startswith("/"),
        )
        return await self.feed_raw_update(upd)

    async def send_command(
        self,
        tg_id: int,
        command_text: str,
        *,
        update_id: int | None = None,
        first_name: str = "User",
    ) -> list[RecordedTelegramCall]:
        upd = self.build_text_update(
            tg_id,
            command_text,
            update_id=update_id,
            first_name=first_name,
            is_command=True,
        )
        return await self.feed_raw_update(upd)

    async def send_media(
        self,
        tg_id: int,
        *,
        photo_id: str | None = None,
        voice_id: str | None = None,
        video_id: str | None = None,
        video_size: int | None = None,
        sticker_id: str | None = None,
        animation_id: str | None = None,
        document_id: str | None = None,
        audio_id: str | None = None,
        caption: str | None = None,
        first_name: str = "User",
    ) -> list[RecordedTelegramCall]:
        msg_kwargs: dict[str, Any] = {
            "message_id": self._alloc_incoming_msg_id(),
            "date": datetime.now(UTC),
            "chat": Chat(id=tg_id, type="private"),
            "from_user": TgUser(id=tg_id, is_bot=False, first_name=first_name),
            "caption": caption,
        }
        if photo_id is not None:
            msg_kwargs["photo"] = [
                PhotoSize(
                    file_id=photo_id,
                    file_unique_id=f"u_{photo_id}",
                    width=800,
                    height=600,
                    file_size=120_000,
                )
            ]
        if voice_id is not None:
            msg_kwargs["voice"] = Voice(
                file_id=voice_id,
                file_unique_id=f"u_{voice_id}",
                duration=5,
                file_size=18_000,
            )
        if video_id is not None:
            msg_kwargs["video"] = Video(
                file_id=video_id,
                file_unique_id=f"u_{video_id}",
                width=640,
                height=360,
                duration=8,
                file_size=video_size if video_size is not None else 500_000,
            )
        if sticker_id is not None:
            msg_kwargs["sticker"] = Sticker(
                file_id=sticker_id,
                file_unique_id=f"u_{sticker_id}",
                type="regular",
                width=512,
                height=512,
                is_animated=False,
                is_video=False,
                file_size=20_000,
            )
        if animation_id is not None:
            msg_kwargs["animation"] = Animation(
                file_id=animation_id,
                file_unique_id=f"u_{animation_id}",
                width=320,
                height=240,
                duration=3,
                file_size=80_000,
            )
        if document_id is not None:
            msg_kwargs["document"] = Document(
                file_id=document_id,
                file_unique_id=f"u_{document_id}",
                file_name="payload.pdf",
                file_size=40_000,
            )
        if audio_id is not None:
            msg_kwargs["audio"] = Audio(
                file_id=audio_id,
                file_unique_id=f"u_{audio_id}",
                duration=15,
                file_size=100_000,
            )

        upd = Update(update_id=self._alloc_update_id(), message=Message(**msg_kwargs))
        return await self.feed_raw_update(upd)

    async def press_button(
        self,
        tg_id: int,
        *,
        callback_data: str | None = None,
        prefix: str | None = None,
        message: Message | None = None,
        allow_missing_from_history: bool = False,
        update_id: int | None = None,
        first_name: str = "User",
    ) -> list[RecordedTelegramCall]:
        resolved_data = callback_data
        target_msg = message

        if resolved_data is None and prefix is not None:
            target_msg, resolved_data = self.find_button(tg_id, prefix=prefix)
        elif resolved_data is not None and target_msg is None:
            try:
                target_msg, _ = self.find_button(tg_id, exact=resolved_data)
            except AssertionError:
                if not allow_missing_from_history:
                    raise
                history = self.session.chat_messages.get(tg_id, [])
                target_msg = (
                    history[-1]
                    if history
                    else Message(
                        message_id=self._alloc_incoming_msg_id(),
                        date=datetime.now(UTC),
                        chat=Chat(id=tg_id, type="private"),
                        from_user=self.session.bot_user,
                        text="synthetic-origin",
                    )
                )

        assert resolved_data is not None
        assert target_msg is not None

        cbq = CallbackQuery(
            id=self._alloc_cb_id(),
            from_user=TgUser(id=tg_id, is_bot=False, first_name=first_name),
            chat_instance=f"ci_{tg_id}",
            message=target_msg,
            data=resolved_data,
        )
        upd = Update(update_id=update_id or self._alloc_update_id(), callback_query=cbq)
        return await self.feed_raw_update(upd)

    def assert_no_privacy_leak(
        self,
        telegram_ids: Iterable[int],
        phone_numbers: Iterable[str] = (),
    ) -> None:
        """Verify no raw Telegram numeric ID or phone number appears in any outgoing payload."""
        forbidden = [str(tid) for tid in telegram_ids] + [p for p in phone_numbers if p]
        for call in self.session.calls:
            for field in (call.text, call.caption, call.callback_text):
                if not field:
                    continue
                for token in forbidden:
                    assert token not in field, (
                        f"Privacy leak detected! Protected token {token!r} found in "
                        f"{call.method} to chat_id={call.chat_id}: {field!r}"
                    )

    async def close(self) -> None:
        await self.bot.session.close()
