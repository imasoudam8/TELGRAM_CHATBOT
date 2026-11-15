# Deployment, Backup, Recovery & Operational Runbook

This runbook covers local setup, production deployment prerequisites, migrations, encrypted backups, health checks, rollback rules, and initial `SUPER_ADMIN` bootstrapping for the **Anonymous Telegram Matching & Chat Bot**.

All user-facing workflows, matchmaking, anonymous chat, and moderation occur exclusively inside Telegram (`aiogram`). Do not expose PostgreSQL or Redis ports publicly.

---

## 1. Local Deployment

```bash
cp .env.example .env
# Set BOT_TOKEN in .env (obtain from @BotFather)

# Option A: Docker Compose (recommended local stack)
docker compose up --build -d
docker compose ps
docker compose logs -f bot

# Option B: Local Python 3.12 virtualenv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
python -m src.app.infrastructure.healthcheck --check-migrations
python -m src.app.main
```

---

## 2. Production Deployment Prerequisites *(Manual Operator Actions Required)*

Before running production workloads:
1. **Provision VPS / Host**: Linux host with Docker (`>= 24.0`), Docker Compose v2 (`>= 2.20`), `postgresql-client` (PG 16), `gnupg`, and an off-server sync tool (`rclone`, `aws`, or `scp`).
2. **Prepare Production Secrets** *(never commit to Git)*:
   - `APP_ENV=production`
   - `BOT_TOKEN`: Live `@BotFather` token (must not contain `EXAMPLE`, `TEST`, `PLACEHOLDER`, `CHANGEME`, or `YOUR_`)
   - `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD` (minimum 12 characters, non-trivial)
   - `REDIS_PASSWORD` (required in production, minimum 12 characters, non-trivial)
   - `POSTGRES_ECHO=false`
3. **Configure Off-Server Backup Storage**:
   - Configure `BACKUP_GPG_RECIPIENT` or `BACKUP_GPG_PASSPHRASE_FILE` (`chmod 0600`)
   - Configure `BACKUP_REMOTE_TARGET` (e.g., S3 bucket via `rclone` or remote SSH host) and verify remote upload credentials.
4. **GitHub Actions Deployment Secrets** *(only if using `.github/workflows/deploy.yml`)*:
   - Configure repository/environment secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_DEPLOY_PATH`, optional `VPS_PORT`.
   - Deployments in `deploy.yml` run **only** on manual `workflow_dispatch` with confirmation `DEPLOY`.

---

## 3. Environment Configuration Reference

| Variable | Required in Prod | Default | Purpose |
|---|---|---|---|
| `APP_ENV` | Yes (`production`) | `development` | Enables strict credential & migration checks |
| `BOT_TOKEN` | Yes | *(none)* | `@BotFather` token (`SecretStr`, masked in logs) |
| `TIMEZONE` | Yes | `Asia/Tehran` | IANA business timezone for daily coin quota reset |
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` | Yes | `postgres`/`5432`/`matching_bot` | PostgreSQL 16 connection parameters |
| `POSTGRES_PASSWORD` | Yes (>=12 chars) | *(rejected if weak)* | Database password (`SecretStr`) |
| `POSTGRES_ECHO` | No (`false`) | `false` | Must remain `false` in production |
| `REDIS_HOST` / `PORT` / `DB` | Yes | `redis`/`6379`/`0` | Redis 7 coordination & FSM store |
| `REDIS_PASSWORD` | Yes (>=12 chars) | *(none)* | Required Redis password in production |
| `REQUIRE_MIGRATIONS_CURRENT` | No | `true` in prod/staging | Blocks bot startup if schema != Alembic head |
| `HEALTH_SERVER_ENABLED` | No | `false` (`true` in prod Compose) | Starts loopback HTTP `/health` & `/ready` |
| `HEALTH_HOST` / `HEALTH_PORT` | No | `127.0.0.1` / `8080` | Bind address for internal readiness endpoint |
| `LOG_LEVEL` / `LOG_FORMAT` | No | `INFO` / `json` | Structured JSON logging with secret redaction |
| `LOG_FILE_PATH` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | No | `None` / `10MiB` / `5` | Optional rotating file log for non-Docker hosts |
| `BACKUP_DIR` / `BACKUP_GPG_*` / `BACKUP_REMOTE_*` | Recommended | `backups` | Backup location, GPG encryption, off-server target |

---

## 4. Database Migration Readiness & Rollback Distinction

### Applying Migrations
- Forward-only upgrades use Alembic:
  ```bash
  ./scripts/run_migrations.sh
  # or via Compose:
  docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --entrypoint ./scripts/run_migrations.sh bot
  ```
- In `production` and `staging`, `src.app.main` verifies `alembic_version == head` during startup and refuses to boot if migrations are missing or incomplete.
- Deployment scripts **never** run destructive database commands automatically.

### Application Rollback vs. Database Rollback
| Action | Scope | Safe Automatically? | Procedure |
|---|---|---|---|
| **Application Rollback** | Reverts Python bot code / Docker image tag (`sha-...`) | **Yes** (when schema changes are backward-compatible) | Re-deploy previous image tag: `BOT_IMAGE=<prev_tag> docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps bot` |
| **Database Rollback** | Modifies persistent schema or data (`alembic downgrade` or backup restore) | **NO — Manual DBA operation only** | Never blindly run `alembic downgrade` in production (it drops tables/columns/triggers and destroys financial & audit data). Either ship a **forward-fix migration** or restore a verified pre-deploy PostgreSQL backup into a clean DB and cut over cleanly. |

---

## 5. Backup & Recovery Policy

### Retention Policy
- **7 daily** verified backups + **1 monthly** archive (`--daily-keep 7 --monthly-keep 1`).
- Backup directory permissions are enforced to `0700`; backup and `.sha256` files are `0600`.
- Database passwords are passed via `PGPASSWORD` in subprocess environment only (never CLI flags or logs). GPG passphrases are passed via `--passphrase-fd 0`.

### Creating & Verifying an Encrypted Off-Server Backup
```bash
export BACKUP_GPG_PASSPHRASE_FILE=/run/secrets/backup_gpg_passphrase
export BACKUP_REMOTE_TARGET=s3://your-offsite-bucket/matching_bot
export BACKUP_REMOTE_COMMAND=rclone

./scripts/backup_postgres.py --backup-dir /var/backups/matching_bot --require-remote
```
*Note: Without `BACKUP_REMOTE_TARGET`, `backup_postgres.py` records `"offsite_status": "skipped_unconfigured"` (or exits non-zero when `--require-remote` is set) rather than claiming off-server redundancy.*

### Verifying Existing Backup Files
Detects 0-byte files, SHA-256 mismatches, decryption failures, or corrupt/truncated `pg_restore` archives:
```bash
./scripts/restore_postgres.py /var/backups/matching_bot/matching_bot_YYYYMMDDTHHMMSSZ.dump.gpg --verify-only
```

### Monthly Restore Drill *(Required Operational Cadence)*
Once per month, restore the latest off-server encrypted backup into a **disposable** PostgreSQL 16 database and verify Alembic head, tables (`>=18`), and immutability triggers (`>=7`):
```bash
./scripts/restore_postgres.py /var/backups/matching_bot/matching_bot_YYYYMMDDTHHMMSSZ.dump.gpg \
  --target-db restore_drill_$(date +%Y%m%d) \
  --create-target-db
# Inspect counts, then drop the disposable drill database:
psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE restore_drill_$(date +%Y%m%d);"
```

---

## 6. Health Checks, Logging & Troubleshooting

- **Container & CLI Readiness Check**:
  ```bash
  python -m src.app.infrastructure.healthcheck --check-migrations
  ```
- **Internal Loopback HTTP Endpoints** (when `HEALTH_SERVER_ENABLED=true`):
  - `GET http://127.0.0.1:8080/health` — Process liveness (`200 OK`)
  - `GET http://127.0.0.1:8080/ready` — Live PostgreSQL (`SELECT 1`), Alembic head, and Redis (`PING`) readiness (`200 OK` or `503 Service Unavailable`)
- **Log Rotation**:
  - Compose `json-file` driver caps logs at `10m` × `5` files per service (`postgres`, `redis`, `bot`).
  - `SensitiveDataFilter` redacts bot tokens, DB passwords, Redis passwords, and bearer tokens across log messages, exception tracebacks, and `extra` fields.
- **Common Failures**:
  - *Exit code `1` on startup (`Configuration error`)*: Missing/weak `BOT_TOKEN`, `POSTGRES_PASSWORD`, or `REDIS_PASSWORD` in `APP_ENV=production`.
  - *Exit code `2` on startup (`Infrastructure or startup readiness failure`)*: PostgreSQL/Redis unreachable or `alembic_version` behind repository head. Run `./scripts/run_migrations.sh`.
  - *Redis eviction errors*: Never switch Redis away from `--maxmemory-policy noeviction`. Increase `maxmemory` if needed.

---

## 7. Initial `SUPER_ADMIN` Bootstrap *(Manual Post-Migration Action)*

No Telegram numeric IDs are hardcoded in the repository. After migrations succeed, promote the first operator account out-of-band after that user starts the bot once (`/start` + age confirmation):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T bot \
  python scripts/bootstrap_admin.py --telegram-id <OPERATOR_TELEGRAM_ID> --reason "Initial SUPER_ADMIN bootstrap"
```
*(Or identify the operator by `--public-id usr_...`.)*

---

## 8. Post-Deployment Verification Checklist

1. `docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` shows `postgres`, `redis`, and `bot` as `(healthy)`.
2. `docker compose ... exec -T bot python -m src.app.infrastructure.healthcheck --check-migrations` exits `0` with `"status": "healthy"`.
3. `docker compose ... logs --tail=50 bot` shows successful Telegram API `@bot` connection and clean JSON logs with zero unredacted secrets.
4. Verify SIGTERM graceful shutdown behavior during maintenance (`stop_grace_period: 30s`).
5. Confirm initial backup (`backup_postgres.py --require-remote`) succeeds and uploads to off-server storage.
6. Confirm bootstrap `SUPER_ADMIN` can open `/admin` inside Telegram.
