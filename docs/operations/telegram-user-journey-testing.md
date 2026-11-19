# Telegram Bot User-Journey Integration Testing

This document explains how end-to-end Telegram Bot user journeys are tested against live **PostgreSQL 16** and **Redis 7** using a deterministic in-memory fake Telegram Bot API transport — without requiring a real Telegram bot token or making external network requests to `api.telegram.org`.

---

## 1. Test Architecture & Fake Telegram Transport

Unlike unit tests that invoke handler coroutines directly with `MagicMock` objects over SQLite, the user-journey integration suite executes the **real bot interaction stack**:

```text
Synthetic Telegram Update
        │
        ▼
Dispatcher (create_dispatcher)
  ├── StructuredLoggingMiddleware
  ├── MemoryStorage FSM (FSMContext per user/chat)
  ├── Router Precedence (onboarding → moderation → admin → chat[ActiveChatFilter] → profile → discovery → matching → direct_requests → connections → wallet)
  ├── Handlers & Keyboard Builders
  └── Domain Services ──▶ Live PostgreSQL 16 (disposable migrated DB) + Live Redis 7
        │
        ▼
Bot(session=RecordingTelegramSession) ──▶ Captured Outgoing Bot API Calls
```

Key components in `tests/helpers/telegram_harness.py`:
- **`RecordingTelegramSession(BaseSession)`**: Subclasses `aiogram.client.session.base.BaseSession` and overrides `make_request(...)`. All outgoing `SendMessage`, `EditMessageText`, `EditMessageReplyMarkup`, `AnswerCallbackQuery`, `SendPhoto`, `SendVideo`, `SendVoice`, `SendSticker`, `SendAnimation`, `DeleteMessage`, and `GetMe` calls undergo real `aiogram` serialization/validation, are stored in memory (`calls`, `sent_messages`, `chat_messages`), and return authentic `aiogram.types.Message` / `bool` responses with zero HTTP I/O.
- **`RecordingTelegramSession.fail_chat_ids`**: Configurable per-chat failure set used to simulate Telegram delivery errors (e.g. blocked bot or network rejection) and verify atomic rollback / first-message flag reversion.
- **`TelegramTestHarness`**: Instantiates `Bot(..., parse_mode=ParseMode.HTML)` + `create_dispatcher(settings, db_manager, redis_manager)` and exposes helpers to send commands/text/media updates, press inline buttons directly from rendered bot messages, inject forged/unauthorized callbacks, replay duplicate updates, and assert zero Telegram ID or phone number leakage (`assert_no_privacy_leak`).

---

## 2. Simulating Telegram Updates

Tests interact with the bot as multiple concurrent Telegram users (`710...` numeric IDs) through realistic `aiogram.types.Update` payloads:
- **Commands & Reply Menu Taps**: `send_command(tg_id, "/start")` attaches real `bot_command` `MessageEntity` metadata; `send_text(tg_id, BTN_...)` simulates pressing persistent reply keyboard buttons or sending plain text/FSM inputs.
- **Inline Callback Queries**: `press_button(tg_id, callback_data=...)` locates the actual `InlineKeyboardMarkup` message delivered to `tg_id`'s chat and emits a `CallbackQuery` bound to that stored `Message` (or explicitly tests forged/stale callbacks with `allow_missing_from_history=True`).
- **Media & Oversized Payloads**: `send_media(...)` constructs `PhotoSize`, `Voice`, `Video` (with configurable `file_size`), `Sticker`, `Animation`, `Document`, and `Audio` updates to verify MIME/type validation, `< 10 MiB` video enforcement, and caption HTML/safety filtering.
- **Duplicate Updates & Delivery Failures**: Replaying identical callbacks or setting `fail_chat_ids` verifies idempotent settlement, direct-request decision locking, and zero coin loss on delivery failure.

---

## 3. Running Tests Locally and in CI

All user-journey integration tests require only local/CI PostgreSQL 16 and Redis 7 (they automatically skip when local Postgres is unreachable outside CI, and run deterministically with randomized ordering). **No real `@BotFather` token is required.**

```bash
# Run only the Telegram bot user-journey integration suite
pytest -v tests/integration/test_telegram_user_journeys_integration.py

# Run full lint, format, compilation, and normal + randomized test passes
ruff check .
ruff format --check .
python -m compileall -q src tests scripts migrations
pytest -v
pytest -v --random-order-seed=12345
pytest -v --random-order-seed=67890
pytest -v --random-order-seed=99999
```

In `.github/workflows/ci.yml`, the `Lint, Compile & Test` job provisions `postgres:16-alpine` and `redis:7-alpine` services, sets a synthetic `BOT_TOKEN`, and executes the full suite across standard and all three randomized seed passes.

---

## 4. Covered User Journeys (`A`–`G`)

| Journey | Scope Verified via `Dispatcher` + PG16 + Redis 7 |
|---|---|
| **A. Registration & Onboarding** | `/start`, 18+ confirmation (`CALLBACK_AGE_CONFIRM`), underage decline (`CALLBACK_AGE_DECLINE`) locking menu with zero DB row created, pre-declaration menu/text denial, repeated `/start` clearing active wizard FSM state, duplicate confirmation idempotency, privacy against Telegram ID leakage. |
| **B. Profile Management** | Full 5-step creation wizard (`ProfileCreationSG`), invalid input retries, optional city/bio skipping, wizard cancellation, field edits (`display_name`, `age`, `gender`, `city`, `bio`), edit cancellation, HTML escaping, `INCOMPLETE`/`HIDDEN` visibility exclusion from discovery, cross-user edit isolation. |
| **C. Discovery & Matching** | Filter configuration via callbacks & FSM (`gender`, `age`, `city`, `relaxable_fields`), guarantee that strict filters are never silently relaxed, explicit relaxed opt-in browsing (`disc:browse:0:1`), strict pagination, lookup by `public_id`, Redis 7 matchmaking queue join/leave/pair, active-chat transition, `ALREADY_IN_CHAT` guard, and blocked-user exclusion. |
| **D. Direct Requests** | Sending from discovery card and by `public_id` (`DirectRequestSG`), recipient notification inline keyboard, duplicate pending rejection, unauthorized third-party callback rejection, accept/reject notifications, outgoing cancel (`dr:can:*`), 24-hour expiration check, duplicate accept safety. |
| **E. Chat Interactions & Routing** | Router precedence (`MAIN_MENU_BUTTONS` guard, `/start`, `ReportSG` vs `ActiveChatFilter` catch-all), bidirectional text & supported media relay (`photo`, `voice`, `video < 10 MiB`, `sticker`, `animation`), unsupported (`document`, `audio`) & `>= 10 MiB` video rejection, content filter (`https://`, `t.me/`, `@username`, phone), unauthorized `/end`/`chat:end:*`/`chat:blk:*` rejection, chat end cleanup & partner notice. |
| **F. Connections, Safety & Admin Guard** | Connections list/open (`conn:open:*`), asymmetric removal (`conn:rem:*`) preserving peer's view, abuse reporting from active chat (`rep:chat:*`) & profile (`rep:usr:*`) creating `Report` + `MessageEvidenceHold`, blocking via chat/connections/discovery terminating chat and stopping future requests, unauthorized `/admin` & `adm:*` callback denial. |
| **G. Wallet & Atomic Settlement** | Daily free allowance allocation via `BTN_WALLET` & `wallet:refresh`, failed delivery reverting first-message flag with 0 coins charged, single-sided chat ending with 0 charge, two-sided `RANDOM` (1 each) and `DIRECT` (2 requester / 0 recipient) settlement, insufficient-balance preservation (`is_charged=False`), duplicate settlement idempotency, ledger history pagination (`wallet:hist:*`). |

---

## 5. Known Limitations of Fake-Transport Testing

While the fake `BaseSession` exercises `aiogram` routing, FSM transitions, handlers, and live database/Redis state end-to-end, it intentionally does **not** simulate:
- Real Telegram Bot API network latency, TLS handshakes, HTTP `429 Retry-After` rate limiting, or webhook secret header verification from Telegram servers.
- Client-side rendering differences across Telegram iOS, Android, Desktop, and Web clients (RTL Persian text wrapping, inline keyboard button width truncation, reply keyboard persistence behavior, HTML tag rendering).
- Actual binary file upload/download streaming via Telegram CDN `file_id` handles across separate bot sessions.

---

## 6. Manual Telegram Client QA Scenarios (Pre-Launch Checklist)

Before switching to live production traffic, verify the following manually in a staging Telegram bot with two physical/desktop test accounts:
1. **Onboarding & Persian RTL Layout**: Run `/start` from a fresh account, verify inline 18+ prompt alignment, accept 18+, and confirm the 2-column persistent reply keyboard renders cleanly on mobile & desktop.
2. **Profile & Media Relay Check**: Complete both profiles and start a live chat; send real compressed video `< 10 MiB`, photo, voice note, sticker, and GIF between iOS/Android clients and confirm smooth playback without sender attribution.
3. **Oversized Video & Privacy Check**: Send a `>= 10 MiB` video (verify Persian size rejection without forwarding) and inspect profile cards / notifications on both accounts to confirm neither account sees the other's `@username`, phone number, or Telegram numeric ID.
4. **Live Termination & Moderation**: Tap `❌ پایان گفتگو` and `🚫 مسدود کردن مخاطب` on mobile; confirm reply keyboard reverts to the main menu for both participants and blocked partner disappears from discovery & connections.
5. **Wallet & Daily Reset Spot Check**: Inspect `🪙 کیف پول`, confirm daily free coin expiry timestamp renders in `Asia/Tehran` time, exchange two-sided messages, and verify balance deduction and `📜 تاریخچه تراکنش‌ها` pagination.
