# Staging Readiness & Real-World Telegram UAT Runbook

This runbook governs controlled **User Acceptance Testing (UAT)** of the **Anonymous Telegram Matching & Chat Bot** in an **isolated staging environment** using two real Telegram client accounts.

> **Important Scope Boundary:** Automated fake-transport tests (`tests/integration/test_telegram_user_journeys_integration.py`) validate `aiogram` routing, FSM transitions, database invariants, and Redis coordination against PostgreSQL 16 and Redis 7. They do **not** replace human UAT on real Telegram clients. Do **not** deploy staging configurations to production or reuse production tokens/databases.

---

## 1. Automated vs. Manual Verification Status Legend

Operators and release reviewers MUST keep these states strictly separate:

| Status Label | Meaning |
|---|---|
| `AUTOMATED_PASSED` | Verified automatically in pytest/CI over PostgreSQL 16, Redis 7, and `RecordingTelegramSession`. |
| `STAGING_CONFIG_VERIFIED` | Validated structurally (`Settings`, Compose config, healthcheck CLI, secret guards) with synthetic non-prod inputs. |
| `NOT_EXECUTED` | Human manual Telegram test case not yet performed on a live staging bot. |
| `BLOCKED_NO_INFRA` | Cannot run until a live staging VPS/host, staging `@BotFather` token, or real test accounts are provisioned. |
| `MANUAL_PASSED` / `MANUAL_FAILED` | Executed by a human operator on real Telegram accounts with observed evidence recorded. |

---

## 2. Staging Setup & Isolation Guide

### 2.1 Isolation Invariants (Non-Negotiable)
- **Dedicated Telegram Bot:** Create a separate bot via `@BotFather` (e.g., `@your_project_staging_bot`). **Never** use the production `BOT_TOKEN` in staging because long-polling on two instances with the same token causes `TelegramConflictError` and leaks live traffic.
- **Dedicated Containers, Volumes, & Network:** `docker-compose.staging.yml` isolates every resource from dev and production:
  - Containers: `matching_bot_staging_postgres`, `matching_bot_staging_redis`, `matching_bot_staging_app`
  - Volumes: `matching_bot_staging_postgres_data`, `matching_bot_staging_redis_data`
  - Network: `matching_bot_staging_internal` (bridge, **no public PostgreSQL or Redis ports**)
- **Dedicated Database & Redis Auth:** `POSTGRES_DB=matching_bot_staging`, `REDIS_DB=1`, `--requirepass`, `--maxmemory-policy noeviction`.
- **Strict Staging Guards (`Settings`):** `APP_ENV=staging` automatically enables `REQUIRE_MIGRATIONS_CURRENT=true`, verifies `bot.get_me()` on startup, rejects `EXAMPLE`/`PLACEHOLDER`/`CHANGEME`/`YOUR_` tokens, rejects weak/default DB or Redis passwords (`< 12` chars), and requires `POSTGRES_ECHO=false`.

### 2.2 Staging Environment Template (`.env.staging.example`)
Create `.env.staging` on the staging host from `.env.staging.example` (never commit `.env.staging`):

```bash
cp .env.staging.example .env.staging
chmod 600 .env.staging
# Replace all PLACEHOLDER_* values with dedicated staging-only values:
# - BOT_TOKEN (from staging @BotFather bot)
# - POSTGRES_PASSWORD (>= 12 chars, unique to staging)
# - REDIS_PASSWORD (>= 12 chars, unique to staging)
```

### 2.3 Reproducible Staging Startup, Migration, & Health Verification

```bash
# 1. Validate merged Compose configuration (never touches production files)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml config > /dev/null

# 2. Start PostgreSQL 16 and Redis 7 staging services first
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d --wait postgres redis

# 3. Build staging bot image and apply forward-only Alembic migrations
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml build bot
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml run --rm \
  --entrypoint ./scripts/run_migrations.sh bot

# 4. Start staging bot container
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d bot

# 5. Verify health, Alembic head, and clean startup logs (zero secrets exposed)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml ps
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python -m src.app.infrastructure.healthcheck --check-migrations
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml logs --tail=50 bot
```

### 2.4 Out-of-Band Staging `SUPER_ADMIN` Bootstrap
After Operator Account **A** completes `/start` + `18+` declaration in the staging bot:

```bash
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T bot \
  python scripts/bootstrap_admin.py --telegram-id <STAGING_OPERATOR_A_TG_ID> --reason "Staging UAT SUPER_ADMIN"
```

---

## 3. Two-Account Manual Telegram UAT Runbook & Checklist

**Prerequisites:**
- Isolated staging stack running and healthy (`healthcheck --check-migrations` exits `0`).
- **Account A** (e.g., Male profile, `27`, city `تهران`) on Telegram Mobile/Desktop.
- **Account B** (e.g., Female profile, `25`, city `تهران`) on a separate Telegram account/device.
- Optional **Account C** (normal user for third-party privacy/isolation & Support/Moderator RBAC checks).
- Note: Automated coverage in `tests/integration/test_telegram_user_journeys_integration.py` already passed (`AUTOMATED_PASSED`); the manual checklist below records `NOT_EXECUTED` until run by a human operator on real Telegram clients.

### A. Registration & Onboarding
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-A1 | Send `/start` from Account A and tap **زیر ۱۸ سال هستم** (`age_decline`). Try tapping menu buttons (`👤 پروفایل من`, `🪙 کیف پول`). | Menu remains locked with 18+ warning; no account row created in staging DB. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-A2 | Send `/start` on Accounts A & B and tap **تأیید سن بالای ۱۸ سال**. Inspect Persian RTL rendering and persistent 2-column reply keyboard. | Both accounts receive anonymous `usr_...` IDs, RTL text wraps cleanly without broken HTML tags, main menu buttons work. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-A3 | On Account A, start profile wizard (`👤 پروفایل من` -> `ایجاد پروفایل`), enter invalid name `"A"` and invalid age `"17"` / `"abc"`, then send `/start` mid-wizard. | Validation errors display cleanly in Persian; `/start` resets FSM state cleanly without creating duplicate users. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-A4 | Complete profiles for Account A and Account B (including optional city/bio). | Both profiles reach `COMPLETED` + `PUBLIC`; neither card shows Telegram ID or phone number. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### B. Discovery & Matching
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-B1 | On Account A, open `🔎 مشاهده پروفایلها` -> `⚙️ تنظیم فیلترها`. Set filter matching Account B, then set a non-matching city (e.g. `مشهد`) with `city` locked (strict). | Strict filter returns empty **without** silently relaxing to Account B. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-B2 | Mark `city` as relaxable (`قابل انعطاف`), browse again, then tap the explicit relaxed-search button. | Strict browse shows count of relaxed suggestions; relaxed results appear **only** after explicit button tap. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-B3 | Test `🎲 گفتگوی تصادفی` with only Account A in queue, then join with Account B. | Account A waits alone (never self-matched); when B joins, both enter active chat with active-chat keyboard. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-B4 | While A & B are in active chat, try tapping `🎲 گفتگوی تصادفی` or main menu buttons from Account A. | Blocked from entering a second simultaneous chat; main menu taps do not leak to partner. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### C. Direct Chat Requests
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-C1 | From discovery card or `✉️ درخواست مستقیم`, Account A sends request to Account B. Account B taps `❌ رد درخواست`. | Account B receives notification with Accept/Reject buttons; rejection notifies Account A cleanly. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-C2 | Account A sends a second request and cancels via `📤 درخواست‌های ارسالی`, then sends a fresh request and Account B taps `✅ پذیرش درخواست`. | Cancelled request cannot be accepted; accepted request starts `DIRECT` active chat for A & B. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-C3 | Verify 24h expiration rule (if tested with aged DB row in staging) and ensure Account C cannot manipulate/accept B's request. | Expired requests transition to `EXPIRED` and cannot open chat; third-party acceptance denied. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### D. Active Chat Lifecycle
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-D1 | Exchange Persian and English text messages between A and B in both directions (plus HTML characters like `<test>`). | Messages relay instantly without sender attribution; HTML characters render safely escaped. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-D2 | End chat from Account A (`❌ پایان گفتگو` or `/end` or inline end button). | Both A and B return to main menu keyboard; Account B receives partner-left notification; both can browse discovery again. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### E. Media Relay & Policy Enforcement
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-E1 | In active chat, send **photo**, **voice note**, **video (< 10 MiB)**, **sticker**, and **GIF/animation**. | All 5 supported media types relay cleanly via Telegram `file_id` and play on the recipient's real device; zero media files saved inside staging container (`/app`, `/tmp`). | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-E2 | Send **video >= 10 MiB**, **audio file (MP3)**, and **document (PDF/ZIP)**. | Video `>= 10 MiB` rejected with Persian size limit message; `audio` and `document` rejected cleanly as unsupported in anonymous chat; nothing forwarded to partner. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-E3 | Simulate delivery failure (have Account B temporarily stop/block the staging bot, then Account A sends a message; afterwards B unblocks bot). | Account A sees clean delivery-failure notice without crash; first-message flag reverts and 0 coins are deducted. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### F. Privacy, Content Filtering & Safety
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-F1 | Inspect every profile card, direct request card, match alert, and chat message on Accounts A & B. | Neither account can see the other's Telegram numeric ID, `@username`, phone number, or forwardable sender link. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-F2 | In active chat, attempt to send `https://example.com`, `t.me/testuser`, `@myhandle`, and `09123456789`. | All four are blocked by `content_filter` before relay with Persian warning. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-F3 | Test reporting (`🚨 گزارش تخلف`), connection removal (`👥 ارتباطهای من`), and blocking (`🚫 مسدود کردن کاربر`). | Report stored with evidence hold; connection removal is asymmetric; blocking terminates active chat and blocks future discovery/requests/matching. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### G. Wallet & Atomic Settlement (Staging-Only Coins)
> **Unresolved Business Rule Notice:** The current implementation preserves the documented deferred-settlement behavior when a participant has insufficient coins: active chat remains usable, `ChatSession.is_charged` stays `False`, and zero settlement debits occur rather than mid-chat lockouts. **Do NOT alter this policy during UAT; document product-owner feedback in the Go/No-Go decision.**

| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-G1 | Open `🪙 کیف پول` on both accounts; verify `DAILY_FREE_COINS_QUOTA` and Tehran midnight expiry timestamp. | Daily free coins allocated once per Tehran date; `🔄 بروزرسانی` does not double-allocate. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-G2 | Complete a two-sided `RANDOM` chat (both send >= 1 message) and a two-sided `DIRECT` chat. Inspect balances and `📜 تاریخچه تراکنش‌ها`. | `RANDOM`: 1 coin each after both send; `DIRECT`: 2 coins from requester only, 0 from recipient; single-sided chat charges 0. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-G3 | Verify a zero-balance staging user can still exchange messages without negative balance (`purchased_coins >= 0`) and flag product feedback on insufficient-balance behavior. | Zero negative balances; `is_charged=False` preserved; product decision logged in Go/No-Go report. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### H. Admin Panel & RBAC
| ID | Scenario & Steps | Expected Result | Automated Status | Manual Status |
|---|---|---|---|---|
| UAT-H1 | Normal user (Account B) runs `/admin`. | Access denied cleanly (`⛔ شما دسترسی به پنل مدیریت ندارید.`). | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-H2 | Bootstrapped `SUPER_ADMIN` (Account A) opens `/admin`, assigns `SUPPORT` to Account C and `MODERATOR` to another test account, and verifies financial (`coin_adjust`) controls. | `SUPER_ADMIN` accesses all operations including coin adjustment; `SUPPORT` and `MODERATOR` see only their RBAC-permitted panels and are blocked from financial coin adjustments. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |
| UAT-H3 | Review an open UAT report inside `/admin`, apply a test warning, and inspect audit logs. | Report status updates and immutable `admin_audit_logs` record is persisted without exposing private media outside hold scope. | `AUTOMATED_PASSED` | `[ ] NOT_EXECUTED` |

### I. Telegram Client Compatibility Matrix
*(Only mark `MANUAL_PASSED` or `MANUAL_FAILED` after testing with an actual client build.)*

| Client Platform | Client Version Tested | RTL Persian Layout | Reply & Inline Keyboards | Media Relay (`file_id`) | Status |
|---|---|---|---|---|---|
| **Telegram Android** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram iOS** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram Desktop** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |
| **Telegram Web (A/K)** | *(pending)* | *(pending)* | *(pending)* | *(pending)* | `NOT_EXECUTED` |

---

## 4. Operational Readiness & Known Staging Limitations

- **FSM Storage Mode:** `Dispatcher()` currently uses `aiogram` `MemoryStorage`. Active chats, wallets, direct requests, connections, moderation records, and the matchmaking queue persist in PostgreSQL 16 + Redis 7 across container restarts, **but** an in-progress multi-step wizard (`ProfileCreationSG`, `DirectRequestSG`, `ReportSG`, `AdminPanelSG`) resets if the bot restarts mid-input; users recover cleanly by sending `/start`.
- **Telegram Rate Limits (`429 Retry-After`) & Network Glitches:** `execute_telegram_send_with_retry` retries once for `TelegramRetryAfter <= 5s` and transient `TelegramNetworkError`, and logs permanent rejections (`TelegramForbiddenError`, `TelegramBadRequest`) cleanly without raising or leaking message text or bot tokens.
- **No Permanent Media Storage:** All media relays forward Telegram `file_id` handles directly; verify on the staging container that no media files accumulate on disk (`docker exec matching_bot_staging_app find /tmp -maxdepth 2`).
- **Log Redaction:** `SensitiveDataFilter` automatically masks `BOT_TOKEN`, DB passwords, Redis passwords, and bearer tokens in stdout JSON logs.

---

## 5. Staging Cleanup & Reset Procedure

After UAT concludes (or to reset state between UAT rounds) — **staging containers/volumes ONLY**:

```bash
# 1. Flush staging Redis matchmaking queue (safe without wiping Postgres)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml exec -T redis \
  redis-cli DEL matching:queue

# 2. Full staging teardown (DESTROYS STAGING TEST DATA ONLY — verify container/volume names first!)
docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml down -v

# 3. Revoke or rotate the staging @BotFather token if UAT testers will no longer participate
```

---

## 6. Defect Reporting Template

```markdown
### Staging UAT Defect Report
- **Defect ID:** UAT-DEF-___
- **Scenario ID:** (e.g., UAT-E1)
- **Severity:** Critical / High / Medium / Low
- **Client & OS:** (e.g., Telegram Android 11.x / iOS 11.x / Desktop 5.x / Web)
- **Commit SHA Tested:** `<git_sha>`
- **Account Roles Involved:** (e.g., User A / User B / Support / Moderator / Super Admin)
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

## 7. UAT Acceptance Criteria & Go/No-Go Report Template

### Staging UAT Sign-Off Criteria
1. All automated CI checks (`ruff`, `compileall`, PostgreSQL 16 + Redis 7 integration & randomized tests, Compose config validation) pass on the exact commit SHA deployed to staging.
2. Zero Critical or High severity defects open across **A–H**.
3. Zero privacy leaks (no Telegram ID, `@username`, or phone number exposure) observed on any client.
4. All supported media types relay via `file_id` with `< 10 MiB` enforcement and zero disk media retention.
5. Product decision recorded for the unresolved insufficient-balance settlement policy before any production go-live.

### Go / No-Go Report Template

```markdown
# Staging UAT Go / No-Go Decision Report
- **Staging Commit SHA:** `<git_sha>`
- **Staging Bot Handle:** `@<staging_bot_username>` *(not production)*
- **UAT Window:** YYYY-MM-DD to YYYY-MM-DD
- **Operators / Testers:** <names/roles>
- **Client Platforms Executed:** Android [ ], iOS [ ], Desktop [ ], Web [ ]
- **Automated Suite Status:** `AUTOMATED_PASSED` / `FAILED`
- **Manual UAT Summary (A–I):**
  - Passed: __ / __
  - Failed: __ (list UAT-DEF IDs)
  - Not Executed: __
- **Product Decision on Unresolved Insufficient-Balance Policy:**
  - [ ] Keep deferred settlement (`is_charged=False`, chat stays open)
  - [ ] Change policy in follow-up task (specify requirement)
- **Recommendation:** **GO FOR NEXT PHASE** / **NO-GO (BLOCKED)**
- **Blocking Items (if No-Go):** ...
```
