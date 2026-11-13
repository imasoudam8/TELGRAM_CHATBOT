"""Unit tests for TASK007 bot routers, HTML escaping, active chat relay, and error handling."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, PhotoSize, Video
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.app.bot.handlers.base import get_base_router
from src.app.bot.handlers.chat import handle_block_partner_callback, handle_chat_relay_message
from src.app.bot.handlers.direct_requests import handle_dr_decision_callback
from src.app.bot.keyboards.chat import CB_CHAT_BLK_PREFIX, CB_DR_ACCEPT_PREFIX
from src.app.bot.keyboards.onboarding import (
    BTN_DIRECT_REQUEST,
    BTN_MY_CONNECTIONS,
    BTN_RANDOM_CHAT,
    BTN_VIEW_PROFILES,
)
from src.app.config.settings import Settings
from src.app.modules.chat.models import ActiveChatParticipant, ChatMessage, ChatSession
from src.app.modules.chat.session_service import VIDEO_SIZE_LIMIT_BYTES
from src.app.modules.discovery.schemas import PublicProfileDTO
from src.app.modules.notifications.service import format_public_profile_card
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.service import UserService


class SQLiteDBWrapper:
    """SQLite DatabaseManager test adapter."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise


def _make_message(
    telegram_id: int,
    text: str | None = None,
    *,
    video_size: int | None = None,
    photo_id: str | None = None,
    has_document: bool = False,
    bot: MagicMock | None = None,
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.voice = None
    msg.video = None
    msg.sticker = None
    msg.animation = None
    msg.audio = None
    msg.document = MagicMock() if has_document else None

    if video_size is not None:
        vid = MagicMock(spec=Video)
        vid.file_id = "video_file_123"
        vid.file_size = video_size
        vid.duration = 10
        msg.video = vid

    if photo_id is not None:
        ps = MagicMock(spec=PhotoSize)
        ps.file_id = photo_id
        ps.file_size = 12345
        msg.photo = [ps]

    tg_user = MagicMock(spec=TelegramUser)
    tg_user.id = telegram_id
    tg_user.is_bot = False
    msg.from_user = tg_user
    msg.answer = AsyncMock()
    msg.bot = bot or _make_bot()
    return msg


def _make_callback(telegram_id: int, data: str, bot: MagicMock | None = None) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    tg_user = MagicMock(spec=TelegramUser)
    tg_user.id = telegram_id
    tg_user.is_bot = False
    cb.from_user = tg_user
    cb.answer = AsyncMock()
    cb.bot = bot or _make_bot()

    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_video = AsyncMock()
    bot.send_voice = AsyncMock()
    bot.send_sticker = AsyncMock()
    bot.send_animation = AsyncMock()
    return bot


def test_base_router_mounts_all_task007_routers() -> None:
    """Verify all domain sub-routers are included cleanly in base router."""
    router = get_base_router()
    sub_names = {sub.name for sub in router.sub_routers}
    assert {
        "onboarding",
        "chat",
        "profile",
        "discovery",
        "matching",
        "direct_requests",
        "connections",
        "wallet",
    }.issubset(sub_names)


def test_public_profile_card_html_escapes_and_hides_telegram_id() -> None:
    """Verify user-supplied profile fields are HTML-escaped and Telegram IDs never appear."""
    dto = PublicProfileDTO(
        user_id=987654321,
        public_id="usr_safe12345678",
        display_name="<b>Hack</b> & Test",
        age=25,
        gender="MALE",
        city="<i>Tehran</i>",
        bio="Hello <script> & </script>",
        last_activity_at=datetime.now(UTC),
    )
    rendered = format_public_profile_card(dto)
    assert "&lt;b&gt;Hack&lt;/b&gt; &amp; Test" in rendered
    assert "&lt;i&gt;Tehran&lt;/i&gt;" in rendered
    assert "&lt;script&gt; &amp; &lt;/script&gt;" in rendered
    assert "987654321" not in rendered
    assert "usr_safe12345678" in rendered


@pytest.mark.asyncio
async def test_chat_relay_validation_html_escaping_video_limit_and_delivery_failure(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify text relay escaping, filter rejection, video ceiling, and delivery resilience."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    prof_svc = ProfileService(db)

    u1, _ = await user_svc.register_adult_user(600001, test_settings.business_timezone)
    u2, _ = await user_svc.register_adult_user(600002, test_settings.business_timezone)
    await prof_svc.create_profile(
        600001, ProfileData(display_name="کاربر یک", age=25, gender="MALE")
    )
    await prof_svc.create_profile(
        600002, ProfileData(display_name="کاربر دو", age=24, gender="FEMALE")
    )

    chat_id = uuid.uuid4()
    async with db.session() as session:
        session.add(
            ChatSession(
                id=chat_id,
                chat_type="RANDOM",
                participant_one_id=u1.id,
                participant_two_id=u2.id,
                status="ACTIVE",
                is_charged=False,
            )
        )
        await session.flush()
        session.add_all(
            [
                ActiveChatParticipant(user_id=u1.id, chat_id=chat_id),
                ActiveChatParticipant(user_id=u2.id, chat_id=chat_id),
            ]
        )

    # 1. Valid text with HTML chars is escaped & sets participant_one_sent_msg without charging
    bot = _make_bot()
    msg_text = _make_message(600001, text="سلام <خوبی> & <b>تست</b>", bot=bot)
    await handle_chat_relay_message(msg_text, db_manager=db, settings=test_settings)
    bot.send_message.assert_awaited_once()
    sent_kwargs = bot.send_message.call_args.kwargs
    assert sent_kwargs["chat_id"] == 600002
    assert sent_kwargs["text"] == "سلام &lt;خوبی&gt; &amp; &lt;b&gt;تست&lt;/b&gt;"

    async with db.session() as session:
        chat_mid = (
            await session.execute(select(ChatSession).where(ChatSession.id == chat_id))
        ).scalar_one()
        assert chat_mid.participant_one_sent_msg is True
        assert chat_mid.participant_two_sent_msg is False
        assert chat_mid.is_charged is False

    # 2. Content filter rejects @username without relaying
    bot.send_message.reset_mock()
    msg_unsafe = _make_message(600001, text="آیدیم @ali_1234 هست", bot=bot)
    await handle_chat_relay_message(msg_unsafe, db_manager=db, settings=test_settings)
    bot.send_message.assert_not_awaited()
    assert "مجاز نیست" in msg_unsafe.answer.call_args[0][0]

    # 3. Video >= 10 MiB is rejected cleanly
    msg_big_vid = _make_message(600001, video_size=VIDEO_SIZE_LIMIT_BYTES, bot=bot)
    await handle_chat_relay_message(msg_big_vid, db_manager=db, settings=test_settings)
    bot.send_video.assert_not_awaited()
    assert "۱۰ مگابایت" in msg_big_vid.answer.call_args[0][0]

    # 4. Unsupported document payload is rejected gracefully without corrupting chat
    msg_doc = _make_message(600001, has_document=True, bot=bot)
    await handle_chat_relay_message(msg_doc, db_manager=db, settings=test_settings)
    assert "پشتیبانی نمی‌شود" in msg_doc.answer.call_args[0][0]

    # 5. Photo relay uses telegram file_id, succeeds, and triggers chat settlement
    msg_photo = _make_message(600002, photo_id="photo_file_999", bot=bot)
    await handle_chat_relay_message(msg_photo, db_manager=db, settings=test_settings)
    bot.send_photo.assert_awaited_once_with(chat_id=600001, photo="photo_file_999", caption=None)

    # 6. Telegram delivery failure handled gracefully without corrupting active chat state
    failing_bot = _make_bot()
    failing_bot.send_message.side_effect = RuntimeError("Telegram Forbidden: user blocked bot")
    msg_fail = _make_message(600001, text="پیام تست خطای تلگرام", bot=failing_bot)
    await handle_chat_relay_message(msg_fail, db_manager=db, settings=test_settings)
    assert "انجام نشد" in msg_fail.answer.call_args[0][0]

    # Verify chat session remains ACTIVE, settled exactly once, both sent_msg flags True
    async with db.session() as session:
        chat_row = (
            await session.execute(select(ChatSession).where(ChatSession.id == chat_id))
        ).scalar_one()
        assert chat_row.status == "ACTIVE"
        assert chat_row.is_charged is True
        assert chat_row.charged_at is not None
        assert chat_row.participant_one_sent_msg is True
        assert chat_row.participant_two_sent_msg is True

        msgs = (
            (await session.execute(select(ChatMessage).where(ChatMessage.chat_id == chat_id)))
            .scalars()
            .all()
        )
        assert len(msgs) == 3  # escaped text, photo, and delivery-fail attempt metadata


@pytest.mark.asyncio
async def test_unauthorized_callback_is_rejected_and_answered(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify cross-user chat block / invalid request callbacks acknowledge and fail safely."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    await user_svc.register_adult_user(700001, test_settings.business_timezone)

    state = MagicMock()
    state.clear = AsyncMock()

    # Non-existent chat UUID block callback
    cb_blk = _make_callback(700001, f"{CB_CHAT_BLK_PREFIX}{uuid.uuid4().hex}")
    await handle_block_partner_callback(cb_blk, state=state, db_manager=db, settings=test_settings)
    cb_blk.answer.assert_awaited_once()

    # Non-existent direct request decision callback
    cb_dr = _make_callback(700001, f"{CB_DR_ACCEPT_PREFIX}999999")
    await handle_dr_decision_callback(cb_dr, db_manager=db, settings=test_settings)
    cb_dr.answer.assert_awaited_once()


def test_menu_button_labels_present_in_onboarding_constants() -> None:
    """Sanity check exact Persian button constants match router wiring."""
    assert BTN_RANDOM_CHAT == "🎲 گفتگوی تصادفی"
    assert BTN_VIEW_PROFILES == "🔎 مشاهده پروفایلها"
    assert BTN_DIRECT_REQUEST == "✉️ درخواست مستقیم"
    assert BTN_MY_CONNECTIONS == "👥 ارتباطهای من"
