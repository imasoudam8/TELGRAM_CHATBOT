# Staging Environment Readiness & Real Telegram UAT Runbook

This runbook governs controlled deployment, operational verification, and **two-account User Acceptance Testing (UAT)** of the **Anonymous Telegram Matching & Chat Bot** in an **isolated staging environment** using real Telegram client accounts.

> **Scope & Truthfulness Rule:** Automated fake-transport integration tests (`tests/integration/test_telegram_user_journeys_integration.py`) validate `aiogram` routing, FSM transitions, database invariants, and Redis coordination against PostgreSQL 16 and Redis 7. They do **not** replace human UAT on real Telegram clients. Never mark manual UAT scenarios as passed without actual human execution on real Telegram accounts, and never deploy staging configurations or tokens to production.

---

## 1. Automated vs. Manual Verification Status Legend

Operators and release reviewers MUST keep these states strictly separate:

| Status Label | Meaning |
|---|---|
| `AUTOMATED_PASSED` | Verified automatically in pytest/CI over PostgreSQL 16, Redis 7, and `RecordingTelegramSession`. |
| `STAGING_CONFIG_VERIFIED` | Validated structurally (`Settings`, Compose config, healthcheck CLI, polling lock, secret guards) with synthetic non-prod inputs. |
| `NOT_EXECUTED` | Human manual Telegram test case not yet executed on a live staging bot. |
| `BLOCKED` | Cannot execute until prerequisites (VPS host, dedicated staging `@BotFather` token, two independent Telegram test accounts) are provisioned. |
| `PASS` / `FAIL` | Executed by human operators on real Telegram accounts with observed evidence recorded in **Actual result**. |

---

## 2. Staging Deployment & Isolation Runbook

### 2.1 Non-Negotiable Isolation Invariants
- **Dedicated Staging Bot Token:** Create a separate bot via `@BotFather` (e.g., `@your_project_staging_bot`). **Never** reuse the production `BOT_TOKEN`. Concurrent polling on the same bot token causes `TelegramConflictError` and leaks live traffic.
- **No Shared `.env` File:** Ensure there is no `.env` file carrying development or production secrets in the staging working directory, because base `docker-compose.yml` references `.env` (`required: false`). Always invoke Compose with explicit `--env-file .env.staging`.
- **Isolated Containers, Volumes, & Internal Network** (`docker-compose.staging.yml`):
  - **Containers:** `matching_bot_staging_postgres`, `matching_bot_staging_redis`, `matching_bot_staging_app`
  - **Persistent Volumes:** `matching_bot_staging_postgres_data`, `matching_bot_staging_redis_data`
  - **Network:** `matching_bot_staging_internal` (bridge, **zero published Postgres/Redis ports**)
- **Isolated Database & Redis:** `POSTGRES_DB=matching_bot_staging`, `REDIS_DB=1`, `--requirepass "$REDIS_PASSWORD"`, `--maxmemory-policy noeviction`.
- **Runtime Hardening & Guards:**
  - Non-root container user (`appuser`, UID `10001`), `read_only: true` rootfs with `/tmp` tmpfs, `cap_drop: [ALL]`, `no-new-privileges:true`, `restart: unless-stopped`, `init: true`, `stop_grace_period: 30s`.
  - Strict `APP_ENV=staging` validation (`Settings`): enforces `REQUIRE_MIGRATIONS_CURRENT=true`, verifies `bot.get_me()` on startup (fast-fail on `TelegramUnauthorizedError`, bounded retry on transient network/rate-limit errors), rejects `EXAMPLE`/`PLACEHOLDER`/`CHANGEME`/`YOUR_` bot tokens, rejects weak/default DB or Redis passwords (`< 12` chars), and requires `POSTGRES_ECHO=false`.
  - Distributed Redis polling lock (`telegram:polling_lock:<bot_id>`) prevents duplicate bot instances from starting concurrent polling loops.

### 2.2 Staging Environment Setup (`.env.staging`)
Prepare `.env.staging` on the isolated staging host from `.env.staging.example` (gitignored via `.env*`; never commit):

```bash
cp .env.staging.example .env.staging
chmod 600 .env.staging
```

Edit `.env.staging` and replace all `PLACEHOLDER_*` values with staging-only values:
- `BOT_TOKEN`: Dedicated staging `@BotFather` token (must NOT contain `EXAMPLE`, `PLACEHOLDER`, `CHANGEME`, or `YOUR_`)
- `POSTGRES_PASSWORD`: Unique staging password (>= 12 characters)
- `REDIS_PASSWORD`: Unique staging password (>= 12 characters)

### 2.3 Startup Procedure (Ordered & Verified)

```bash
# 1. Validate merged Compose configuration (read-only config check)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml config > /dev/null

# 2. Start PostgreSQL 16 and Redis 7 first and wait for healthchecks
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d --wait postgres redis

# 3. Build staging bot image and apply forward-only Alembic migrations
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml build bot
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml run --rm \
  --entrypoint ./scripts/run_migrations.sh bot

# 4. Start the staging bot container
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d bot
```

### 2.4 Health & Persistent Volume Verification Commands

```bash
# Verify container statuses (postgres, redis, bot must all show healthy)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml ps

# Run deep readiness CLI inside bot container (PostgreSQL SELECT 1 + Alembic head + Redis PING)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python -m src.app.infrastructure.healthcheck --check-migrations

# Query internal loopback HTTP liveness (/health) and readiness (/ready) inside bot container
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode()); print(urllib.request.urlopen('http://127.0.0.1:8080/ready').read().decode())"

# Confirm zero temporary media files accumulate in container /tmp
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  find /tmp -maxdepth 2
```

### 2.5 Graceful Shutdown & Restart Persistence Procedure
Use `stop` (sends `SIGTERM` with `stop_grace_period: 30s`, allows in-flight updates up to 10s, releases Redis polling lock, closes DB/Redis pools cleanly) — **do NOT pass `-v`** unless intentionally wiping staging volumes:

```bash
# Graceful stop (preserves staging PostgreSQL and Redis volumes)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml stop -t 30 bot

# Full stack pause without removing persistent volumes
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml stop -t 30

# Start back up and confirm persisted staging data remains intact
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT count(*) FROM users;"
```

### 2.6 Out-of-Band Staging `SUPER_ADMIN` Bootstrap
After Operator Account **A** completes `/start` + `18+` declaration in the staging bot:

```bash
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python scripts/bootstrap_admin.py --telegram-id <STAGING_OPERATOR_A_TG_ID> --reason "Staging UAT SUPER_ADMIN"
```

### 2.7 Log Inspection & Secret Hygiene
All logs emit structured JSON to stdout with automatic `SensitiveDataFilter` redaction and `10m x 5` rotation:

```bash
# Follow recent staging bot logs
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml logs -f --tail=100 bot

# Filter warnings/errors and verify no raw BOT_TOKEN or passwords appear
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml logs --tail=300 bot \
  | grep -E '"level": "(WARNING|ERROR|CRITICAL)"|Acquired distributed|Connected to Telegram API|Polling stopped'
```

Expected healthy startup markers:
- `"Database schema matches Alembic head."`
- `"Connected to Telegram API as @<staging_bot> (id=...)"`
- `"Acquired distributed Telegram polling lock (bot_id=...)."`
- `"All dependencies healthy. Starting Telegram long polling..."`

### 2.8 Staging Migration, Rollback Guidance & Safe Update Procedure

#### Safe Staging Update Procedure
1. **Take a verified staging backup first** (Section 2.9).
2. **Pull target commit cleanly:** `git pull --ff-only origin main`
3. **Build updated image:**
   ```bash
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml build bot
   ```
4. **Stop running bot gracefully** (`SIGTERM` finishes active updates and frees `telegram:polling_lock:<bot_id>`):
   ```bash
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml stop -t 30 bot
   ```
5. **Apply forward-only migrations:**
   ```bash
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml run --rm \
     --entrypoint ./scripts/run_migrations.sh bot
   ```
6. **Start updated bot and re-verify health & logs:**
   ```bash
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d bot
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
     python -m src.app.infrastructure.healthcheck --check-migrations
   docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml logs --tail=50 bot
   ```

#### Migration & Rollback Guidance
- **Application Rollback (backward-compatible schema):** Stop bot (`stop -t 30 bot`), check out the prior verified Git SHA, rebuild (`build bot`), and restart (`up -d bot`).
- **Schema Rollback (manual DBA / backup-restore only):** Never run blind `alembic downgrade` against stateful databases because financial ledger triggers, audit tables, or columns may be dropped. Ship a **forward-fix migration** or restore a verified pre-update backup (`restore_postgres.py`). See `docs/operations/deployment-and-recovery.md` Section 4.

### 2.9 Staging Backup & Restore Verification Commands
Run staging backups against the isolated staging PostgreSQL container (verify `POSTGRES_DB=matching_bot_staging` first):

```bash
# 1. Create and verify an encrypted or checksummed staging backup inside a secure host directory
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python scripts/backup_postgres.py --backup-dir /tmp/staging_backups

# 2. Verify archive checksum and pg_restore table of contents without restoring
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python scripts/restore_postgres.py /tmp/staging_backups/matching_bot_staging_<TIMESTAMP>.dump --verify-only

# 3. Restore drill into an isolated disposable staging drill database, then drop drill DB
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python scripts/restore_postgres.py /tmp/staging_backups/matching_bot_staging_<TIMESTAMP>.dump \
    --target-db staging_restore_drill --create-target-db
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T postgres \
  psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE staging_restore_drill;"
```

### 2.10 Common Startup Failure Troubleshooting

| Symptom / Exit Code | Root Cause | Remediation |
|---|---|---|
| Exit code `1`: `Configuration error ... Staging environment cannot use example or placeholder bot tokens` | `PLACEHOLDER_` / `EXAMPLE` marker left in `.env.staging` `BOT_TOKEN` | Set a real dedicated staging `@BotFather` token in `.env.staging`. |
| Exit code `1`: `Configuration error ... trivial or default database passwords` or `REDIS_PASSWORD` | DB or Redis password `< 12` chars, missing, or matching dev defaults | Set unique `>= 12`-char `POSTGRES_PASSWORD` and `REDIS_PASSWORD` in `.env.staging`. |
| Exit code `1`: `Telegram Bot API authentication failure` (`TelegramUnauthorizedError`) | Invalid, mistyped, or revoked staging `BOT_TOKEN` | Verify staging token in `@BotFather` and restart `bot`. Never expose raw token in logs/tickets. |
| Exit code `2`: `Polling lock conflict` (`PollingConflictError`) | A second bot container/process is already polling the same `BOT_TOKEN` against this Redis | Stop the duplicate runner cleanly (`stop -t 30 bot`); wait up to 30s if an old process crashed ungracefully. |
| Runtime log: `TelegramConflictError` | An external machine or stale Telegram webhook is polling/receiving updates for the staging token | Verify only the staging container uses this staging token; check `getWebhookInfo` if a prior webhook was set manually. |
| Exit code `2`: `Database migration mismatch` | Schema behind `alembic head` | Run `docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml run --rm --entrypoint ./scripts/run_migrations.sh bot`. |
| Wrong DB/credentials picked up unexpectedly | Stray `.env` file present in staging directory | Remove `.env` from staging directory and always pass `--env-file .env.staging`. |

---

## 3. Two-Account Real Telegram UAT Execution Checklist (14 Scenarios)

**Prerequisites before starting Scenario 1:**
- Isolated staging stack is running and `healthcheck --check-migrations` exits `0`.
- Dedicated staging `@BotFather` bot is reachable inside Telegram (`get_me()` logged `@<staging_bot>`).
- **Account A** and **Account B** are two independent real Telegram accounts on real Telegram clients.
- Optional **Account C** available for secondary RBAC (`SUPPORT` / `MODERATOR`) and privacy isolation checks.
- All 14 scenarios below start with status `NOT_EXECUTED` (currently `BLOCKED` until live staging infra + credentials + testers are provisioned). Record real observations verbatim during UAT.

---

### Scenario 1: New User Registration
- **Automated Baseline:** `AUTOMATED_PASSED` (`test_telegram_user_journeys_integration.py`)
- **Preconditions:** Neither Account A nor Account B has started the staging bot before (or DB is freshly migrated).
- **Actions for Account A:**
  1. Send `/start` to the staging bot.
  2. First tap `❌ زیر ۱۸ سال هستم`, then attempt tapping `👤 پروفایل من`.
  3. Send `/start` again and tap `✅ تأیید میکنم، ۱۸ سال یا بیشتر دارم`.
- **Actions for Account B:**
  1. Send `/start` to the staging bot.
  2. Tap `✅ تأیید میکنم، ۱۸ سال یا بیشتر دارم`.
- **Expected result:** Under-18 decline locks menu access without creating an active adult user; 18+ confirmation assigns distinct anonymous `usr_...` public IDs and displays the 2-column Persian main menu reply keyboard (`🎲 گفتگوی تصادفی`, `🔎 مشاهده پروفایلها`, `✉️ درخواست مستقیم`, `👥 ارتباطهای من`, `👤 پروفایل من`, `🪙 کیف پول`, `🆘 راهنما و پشتیبانی`) cleanly in RTL with zero Telegram IDs or phone numbers requested or leaked.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 2: Profile Creation and Editing
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B have completed 18+ onboarding (Scenario 1).
- **Actions for Account A:**
  1. Tap `👤 پروفایل من` -> `✨ ایجاد پروفایل من`.
  2. Enter invalid name `"A"` and invalid age `"17"`, observe validation prompts, then complete valid name (e.g., `"آرش"`), age (`27`), gender (`👨 مرد`), city (`"تهران"`), bio (`"تست استیجینگ"`), and tap `✅ تأیید و ثبت نهایی`.
  3. Reopen `👤 پروفایل من`, tap `✏️ ویرایش شهر` -> enter `"اصفهان"`, and `✏️ ویرایش درباره من` -> enter `"متن ویرایش‌شده"`.
- **Actions for Account B:**
  1. Tap `👤 پروفایل من` -> `✨ ایجاد پروفایل من`.
  2. Create valid profile (name `"سارا"`, age `25`, gender `👩 زن`, city `"تهران"`, skip or enter bio) and confirm.
- **Expected result:** Invalid inputs show clear Persian validation feedback; completed profiles save cleanly (`COMPLETED` + `PUBLIC`); edits update Account A's displayed city and bio immediately upon saving without creating duplicate user rows.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 3: Profile Discovery and Filters
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Both Account A (city `اصفهان`) and Account B (city `تهران`) have completed public profiles.
- **Actions for Account A:**
  1. Tap `🔎 مشاهده پروفایلها` -> open filter preferences (`🎛 تنظیم فیلترهای گفتگو` / preferences).
  2. Set gender filter matching Account B (`👩 زن`), city `"تهران"`, with `city` set strict (locked). Browse discovery cards.
  3. Change city filter to `"تبریز"` (strict) and browse; verify empty result without silent relaxation.
  4. Toggle city filter to relaxable (`قابل انعطاف`), browse again, and tap the explicit relaxed-search button.
- **Actions for Account B:**
  1. Keep Account B's profile public and idle outside active chat.
  2. Also test `🔎 مشاهده پروفایلها` from Account B to confirm Account A appears with updated city `"اصفهان"`.
- **Expected result:** Strict non-matching filters return empty **without** silently expanding results; when city is marked relaxable, strict results show relaxed-suggestion availability and reveal relaxed candidates **only** after explicit operator tap. No Telegram `@username`, phone number, or numeric ID appears on any profile card.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 4: Random Matching
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B have compatible matching filters, completed profiles, and are NOT in active chats.
- **Actions for Account A:**
  1. Tap `🎲 گفتگوی تصادفی` (`match:join` if needed) while Account B is not in queue.
  2. Confirm Account A stays waiting (with `❌ لغو جستجو` inline option) and never self-matches.
  3. After matching with Account B, try tapping `🎲 گفتگوی تصادفی` or `🔎 مشاهده پروفایلها` to verify second-chat prevention while active.
- **Actions for Account B:**
  1. Tap `🎲 گفتگوی تصادفی` while Account A is waiting.
  2. Confirm match notification and active-chat keyboard (`❌ پایان گفتگو`, `🚫 مسدود کردن مخاطب`) appear.
- **Expected result:** Account A waits alone without self-match; when Account B joins, both pair into a `RANDOM` active chat session with partner's anonymized card and active-chat controls; simultaneous second chat entry is blocked cleanly.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 5: Direct Chat Request
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Neither Account A nor Account B is in an active chat; both have Account B's `usr_...` public ID visible in discovery.
- **Actions for Account A:**
  1. Send a direct chat request to Account B from discovery card OR via `✉️ درخواست مستقیم` -> `🆔 ارسال درخواست با شناسه عمومی` using Account B's `usr_...`.
  2. Open `✉️ درخواست مستقیم` -> `📤 درخواست‌های ارسالی` -> cancel it with `❌ لغو درخواست`, then send a fresh direct request to Account B.
- **Actions for Account B:**
  1. Observe incoming notification for the cancelled request (confirm trying to accept a cancelled request fails gracefully) and incoming notification for the fresh request with `✅ پذیرش و شروع گفتگو` and `❌ رد درخواست`.
- **Expected result:** Outgoing cancellation invalidates the first request; fresh request delivers Account A's anonymized public card (`usr_...`) and Accept/Reject inline buttons to Account B with 24h expiry notice.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 6: Accepting and Rejecting Requests
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Pending direct request from Account A is in Account B's inbox (from Scenario 5).
- **Actions for Account A:**
  1. Wait for Account B's rejection first, observe notification, then send another direct request to Account B.
  2. Wait for Account B's acceptance on the second request.
- **Actions for Account B:**
  1. On first request, tap `❌ رد درخواست`.
  2. On second request (either from notification or `✉️ درخواست مستقیم` -> `📥 درخواست‌های دریافتی`), tap `✅ پذیرش و شروع گفتگو`.
- **Expected result:** Rejection notifies Account A cleanly without opening a chat; acceptance opens a `DIRECT` active chat session for both Account A and Account B and switches both keyboards to active-chat mode (`❌ پایان گفتگو`, `🚫 مسدود کردن مخاطب`).
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 7: Chat Messaging
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B are paired in an active anonymous chat (from Scenario 6).
- **Actions for Account A:**
  1. Send Persian text `"سلام، تست استیجینگ!"`, English text `"Hello staging!"`, and HTML characters `"<script>alert(1)</script> & <b>tag</b>"`.
  2. Receive Account B's reply.
- **Actions for Account B:**
  1. Verify all three messages arrive verbatim without sender username/ID attribution and with HTML characters safely escaped.
  2. Reply `"پیام شما دریافت شد ✅"` to Account A.
- **Expected result:** Bidirectional relay works instantaneously via Telegram Bot API without sender Telegram attribution; special HTML characters render safely escaped without crashing message formatting. Keep chat open or end before Scenario 8 depending on preferred flow (if kept open, continue straight into Scenario 8).
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 8: Text and Supported Media Exchange
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Active anonymous chat between Account A and Account B; staging container `/tmp` initially clean (`find /tmp -maxdepth 2`).
- **Actions for Account A:**
  1. Send supported media: a **photo**, a **voice note**, a **video (< 10 MiB)**, a **sticker**, and a **GIF/animation**.
  2. Attempt sending disallowed/oversized media: a **video >= 10 MiB** (or verify boundary), an **audio file (MP3)**, and a **document (PDF/ZIP)**.
  3. End chat after verification using `❌ پایان گفتگو` (or `/end`).
- **Actions for Account B:**
  1. Verify all 5 supported media types arrive and play cleanly via relayed Telegram `file_id`.
  2. Confirm none of the rejected media (oversized video, MP3 audio, PDF/ZIP document) reach Account B.
  3. Confirm end-of-chat notification returns Account B to the Persian main menu keyboard.
- **Expected result:** All 5 supported media types relay seamlessly via Telegram `file_id`; `>= 10 MiB` video and unsupported types (`audio`, `document`) are rejected with clear Persian warnings and never forwarded; host/container check (`find /tmp -maxdepth 2`) confirms zero media files stored permanently on server disk.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 9: Blocking and Reporting
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B have completed at least one chat session (so connection/history exists); start a new active chat or open connection detail for testing report & block.
- **Actions for Account A:**
  1. Inside an active chat (or connection card), tap `🚨 گزارش تخلف`, choose a report category, and submit with optional description.
  2. Afterwards, tap `🚫 مسدود کردن مخاطب` (or `🚫 مسدود کردن` from inline/connection card).
  3. Try browsing `🔎 مشاهده پروفایلها` and joining `🎲 گفتگوی تصادفی` while Account B also tries.
- **Actions for Account B:**
  1. Observe clean chat termination notice (`⚠️ گفتگوی فعال شما پایان یافت.`) without receiving notice of who reported/blocked beyond neutral termination.
  2. Attempt sending a direct request to Account A by `usr_...`.
- **Expected result:** Abuse report saves cleanly in staging DB with evidence hold; block immediately terminates active chat, notifies partner neutrally, removes mutual discovery/matching visibility, and rejects direct chat requests in either direction. *(After Scenario 9 & 10 testing—or before repeating Account A<->B tests—clear the test block via admin/SQL in staging or use Account C as needed.)*
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 10: Connection History
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B previously chatted (if blocked in Scenario 9, verify pre-block or use Account B <-> Account C for asymmetric removal).
- **Actions for Account A:**
  1. Tap `👥 ارتباطهای من`.
  2. Open the partner's entry -> inspect anonymized profile card (`👤 ...`), request button, `🗑 حذف از ارتباط‌ها`, `🚫 مسدود کردن`, `🚨 گزارش تخلف`.
  3. Tap `🗑 حذف از ارتباط‌ها` on Account A's side.
- **Actions for Account B:**
  1. Tap `👥 ارتباطهای من` both before and after Account A removes the connection.
- **Expected result:** Completed chat partner appears in `👥 ارتباطهای من` with anonymized details (`usr_...`, display name, age); removing from Account A's list deletes it **asymmetrically** from Account A's list only while Account B's connection view remains intact.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 11: Wallet and Chat Coins Behavior
> **Product Rule Constraint:** Preserve the documented deferred-settlement behavior when a participant has insufficient coins: active chat stays open, `ChatSession.is_charged` remains `False`, and zero debits occur (never force negative balances or alter policy during staging UAT).

- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A and Account B have unblocked access to chat; observe starting balances via `🪙 کیف پول`.
- **Actions for Account A:**
  1. Tap `🪙 کیف پول`, note daily free quota (`DAILY_FREE_COINS_QUOTA=4`), expiry time (Tehran midnight), purchased coins, and tap `🔄 بروزرسانی موجودی` twice (confirm no double allocation).
  2. Complete a two-sided `RANDOM` chat with Account B (both send >= 1 message), then inspect `🪙 کیف پول` -> `📜 تاریخچه تراکنش‌ها`.
  3. Initiate and complete a two-sided `DIRECT` chat with Account B (Account A = requester), then inspect `📜 تاریخچه تراکنش‌ها`.
  4. Also perform one single-sided chat (Account A sends, Account B sends 0 messages, then ends chat) and check balances.
- **Actions for Account B:**
  1. Tap `🪙 کیف پول` -> `📜 تاریخچه تراکنش‌ها` before and after both the `RANDOM` and `DIRECT` chats.
- **Expected result:** `DAILY_FREE_COINS_QUOTA` allocates at most once per Tehran business date; two-sided `RANDOM` chat debits 1 coin each atomically; two-sided `DIRECT` chat debits 2 coins from requester (Account A) only and 0 from recipient (Account B); single-sided chat charges 0; balances never become negative and zero-balance users do not trigger negative balances.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 12: Admin Permissions and Moderation
- **Automated Baseline:** `AUTOMATED_PASSED`
- **Preconditions:** Account A promoted to `SUPER_ADMIN` out-of-band via `scripts/bootstrap_admin.py` (Section 2.6); Account B is a regular non-admin user; optional Account C (or B promoted temporarily) used for role boundary check; open abuse report from Scenario 9 exists.
- **Actions for Account A (`SUPER_ADMIN`):**
  1. Run `/admin`.
  2. Inspect admin panel: review the open report from Scenario 9, apply a warning or inspect target user, view audit logs, and test coin adjustment (`coin_adjust`).
  3. Assign `SUPPORT` or `MODERATOR` role to Account C (if available) via `/admin`.
- **Actions for Account B (regular user) / Account C (`SUPPORT` or `MODERATOR`):**
  1. Regular user (Account B): send `/admin`.
  2. If Account C has `SUPPORT`/`MODERATOR`: send `/admin` and verify role-permitted actions vs denied financial/super-admin operations (`coin_adjust`, role management).
- **Expected result:** Normal non-admin Account B receives clean access-denied response (`⛔ شما دسترسی به پنل مدیریت ندارید.`); `SUPER_ADMIN` accesses all moderation, RBAC, audit, and coin adjustment panels; `SUPPORT`/`MODERATOR` see only RBAC-permitted functions and cannot perform coin adjustments; immutable `admin_audit_logs` entry records every admin action with reason.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 13: Bot Restart and User Recovery
- **Automated Baseline:** `AUTOMATED_PASSED` (`test_polling_lock_integration.py`, `test_lifecycle.py`)
- **Preconditions:** Account A and Account B have saved profiles/connections/wallet ledger in PostgreSQL; start a fresh `RANDOM` or `DIRECT` active chat between A & B AND note Account A is in active chat (or start a profile edit wizard mid-step).
- **Actions for Account A:**
  1. Before restart: verify an active chat session or saved profile/wallet balance exists; optionally leave Account A mid-edit on `✏️ ویرایش درباره من`.
  2. Operator gracefully restarts the staging bot:
     `docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml stop -t 30 bot && docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d bot`
  3. After bot becomes healthy again, send text from Account A (and if wizard was open mid-step, send `/start` to recover cleanly), open `👤 پروفایل من` and `🪙 کیف پول`.
- **Actions for Account B:**
  1. Send a chat message after bot restart completes and verify relay continues cleanly with persisted DB active chat state.
- **Expected result:** `telegram:polling_lock:<bot_id>` releases cleanly during `SIGTERM` shutdown and re-acquires immediately on startup (verified in logs without `PollingConflictError`); persisted PostgreSQL profiles, wallets, connections, and active chats survive container restart intact; any interrupted in-memory FSM wizard recovers cleanly when the user sends `/start`.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

### Scenario 14: Privacy and Identity Protection
- **Automated Baseline:** `AUTOMATED_PASSED` (`test_content_filter_and_discovery.py`, `test_telegram_user_journeys_integration.py`)
- **Preconditions:** Account A and Account B have active Telegram `@username`s on their accounts; open active chat between Account A and Account B.
- **Actions for Account A:**
  1. Inspect every UI surface received from Account B (discovery card, direct request alert, random match alert, connection detail, forwarded chat messages).
  2. Inside active chat, attempt sending:
     - `https://example.com`
     - `t.me/testuser`
     - `@myhandle`
     - `09123456789`
- **Actions for Account B:**
  1. Inspect every UI surface received from Account A (including forwarded chat messages/media).
  2. Confirm whether any blocked contact-sharing/URL messages from Account A arrived, and confirm Account A's Telegram `@username`, numeric Telegram ID, or profile link never appears anywhere.
- **Expected result:** All four prohibited contact/link patterns are blocked by `content_filter` before relay with Persian warnings; zero Telegram numeric IDs, `@username` handles, phone numbers, or forwardable sender headers appear anywhere on either real Telegram client.
- **Actual result:** *(fill during live UAT)*
- **Status:** `NOT_EXECUTED` — `[ ] PASS  [ ] FAIL  [ ] BLOCKED`

---

## 4. Telegram Client Compatibility Matrix
*(Only mark `PASS` or `FAIL` after testing with an actual client build.)*

| Client Platform | Client Version Tested | RTL Persian Layout | Reply & Inline Keyboards | Media Relay (`file_id`) | Status |
|---|---|---|---|---|---|
| **Telegram Android** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram iOS** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram Desktop** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram Web (A/K)** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |

---

## 5. Defect Reporting Template

```markdown
### Staging UAT Defect Report
- **Defect ID:** UAT-DEF-___
- **Scenario Number:** (1–14)
- **Severity:** Critical / High / Medium / Low
- **Client & OS:** (e.g., Telegram Android 11.x / iOS 11.x / Desktop 5.x / Web)
- **Commit SHA Tested:** `<git_sha>`
- **Account Roles Involved:** (e.g., Account A / Account B / Account C / Super Admin)
- **Steps to Reproduce:**
  1. ...
  2. ...
- **Expected Result:** ...
- **Actual Result:** ...
- **Sanitized Log Excerpt (no tokens/passwords/personal IDs):**
  ```json
  ...
  ```
```

---

## 6. UAT Acceptance Criteria & Go / No-Go Sign-Off

### Staging UAT Sign-Off Criteria
1. All automated CI checks (`ruff`, `compileall`, PostgreSQL 16 + Redis 7 integration & randomized tests, Compose config validation) pass on the exact commit SHA deployed to staging.
2. All 14 UAT scenarios executed on two independent real Telegram accounts with **zero** open Critical or High severity defects.
3. Zero privacy leaks (no Telegram ID, `@username`, or phone number exposure) observed across tested Telegram clients.
4. All supported media types relay via `file_id` with `< 10 MiB` enforcement and zero disk media retention (`/tmp` clean).
5. Persistent PostgreSQL/Redis state and Redis polling lock clean release/re-acquisition confirmed across container restart (Scenario 13).
6. Product decision recorded for the deferred insufficient-balance settlement policy before any production go-live.

### Go / No-Go Report Template

```markdown
# Staging UAT Go / No-Go Decision Report
- **Staging Commit SHA:** `<git_sha>`
- **Staging Bot Handle:** `@<staging_bot_username>` *(not production)*
- **UAT Window:** YYYY-MM-DD to YYYY-MM-DD
- **Operators / Testers:** <names/roles>
- **Client Platforms Executed:** Android [ ], iOS [ ], Desktop [ ], Web [ ]
- **Automated Suite Status:** `AUTOMATED_PASSED` / `FAILED`
- **Manual UAT Summary (Scenarios 1–14):**
  - Passed: __ / 14
  - Failed: __ (list UAT-DEF IDs)
  - Blocked / Not Executed: __
- **Product Decision on Unresolved Insufficient-Balance Policy:**
  - [ ] Keep deferred settlement (`is_charged=False`, chat stays open)
  - [ ] Change policy in follow-up task (specify requirement)
- **Recommendation:** **GO FOR NEXT PHASE** / **NO-GO (BLOCKED)**
- **Blocking Items (if No-Go):** ...
```
