# Anonymous Telegram Matching & Chat Bot

> **TASK 001** — Project Foundation & Engineering Infrastructure

A robust, production-grade asynchronous Python foundation for the **Anonymous Telegram Matching & Chat Bot**. This project is engineered with strict type hints, centralized Pydantic Settings, asynchronous database access via SQLAlchemy 2.x and asyncpg, Alembic migrations, Redis caching & coordination, structured logging with automatic secret redaction, and a fully containerized Docker Compose environment.

---

## Architecture & Technology Stack

| Component | Technology | Version | Purpose |
| :--- | :--- | :--- | :--- |
| **Language** | Python | `3.12+` (pinned `3.12`) | Modern, typed asynchronous runtime |
| **Bot Framework** | aiogram | `3.x` | Asynchronous Telegram Bot API framework |
| **Relational Database** | PostgreSQL | `16` | Source of truth for persistent data |
| **ORM & Driver** | SQLAlchemy & asyncpg | `2.0+` & `0.30+` | Async declarative base, connection pooling |
| **Database Migrations** | Alembic | `1.14+` | Version-controlled schema migrations |
| **In-Memory Store** | Redis | `7` | Temporary session state & rate coordination |
| **Configuration** | Pydantic Settings | `2.7+` | Centralized typed environment validation |
| **Testing** | pytest & pytest-asyncio | `8.3+` | Unit & integration test execution |
| **Linter / Formatter** | Ruff | `0.8+` | Fast code style & type hygiene enforcement |
| **Orchestration** | Docker & Docker Compose | Multi-stage | Isolated, reproducible execution |
| **Continuous Integration** | GitHub Actions | Workflows | Automated lint, format check, compile, test |

---

## Directory Structure

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml              # GitHub Actions CI pipeline
├── docker/
│   └── Dockerfile              # Container definition for bot service
├── migrations/                 # Alembic async migration environment
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── scripts/
│   ├── healthcheck.py          # Standalone CLI health checker
│   ├── run_bot.sh              # Container startup script (migrations + polling)
│   └── run_migrations.sh      # Standalone migration execution script
├── src/
│   └── app/
│       ├── bot/                # Telegram bot presentation layer
│       │   ├── filters/        # Custom aiogram message filters
│       │   ├── handlers/       # Bot update routers (smoke test /start)
│       │   ├── keyboards/      # Inline & reply keyboard markups
│       │   └── middlewares/    # Privacy-preserving event logging
│       ├── config/             # Centralized Pydantic Settings
│       │   └── settings.py
│       ├── infrastructure/     # External boundary adapters
│       │   ├── database/       # Async SQLAlchemy engine & base
│       │   ├── healthcheck.py  # Health monitoring logic
│       │   ├── logging/        # Structured JSON & sensitive data masking
│       │   ├── redis/          # Async Redis client pool & lifecycle
│       │   └── telegram/       # Bot & Dispatcher initialization
│       ├── modules/            # Modular domain boundaries
│       │   ├── admin/
│       │   ├── chat/
│       │   ├── discovery/
│       │   ├── matching/
│       │   ├── moderation/
│       │   ├── notifications/
│       │   ├── payments/
│       │   ├── profiles/
│       │   ├── users/
│       │   └── wallet/
│       └── main.py             # Application bootstrap & graceful shutdown
├── tests/
│   ├── conftest.py             # Test fixtures & isolation mocks
│   ├── integration/            # DB, Redis & Dispatcher lifecycle tests
│   └── unit/                   # Config, logging, health & handlers tests
├── .env.example                # Safe environment variable documentation
├── .gitignore                  # VCS exclusion rules
├── .python-version             # Pinned Python version (3.12)
├── alembic.ini                 # Alembic CLI configuration
├── Dockerfile                  # Multi-stage production container build
├── docker-compose.yml          # Local orchestration (PostgreSQL + Redis + Bot)
├── pyproject.toml              # Build metadata, dependencies & tool configs
└── README.md
```

---

## Prerequisites

Ensure the following tools are installed on your workstation:

- **Python 3.12** or higher (e.g., via `pyenv` or official installers)
- **Docker** (`>= 24.0`) and **Docker Compose** (`>= 2.20`)
- **Git**
- Optional: `uv` (recommended for rapid dependency management)

---

## Quickstart (Docker Compose)

For a fully automated local development setup that launches PostgreSQL, Redis, runs database migrations, and boots the bot:

1. **Clone the repository and enter directory**:
   ```bash
   git clone <repository_url>
   cd <repository_name>
   ```

2. **Configure your environment**:
   ```bash
   cp .env.example .env
   ```
   Open `.env` and set your real Telegram Bot token from [@BotFather](https://t.me/BotFather):
   ```env
   BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
   ```

3. **Start services with Docker Compose**:
   ```bash
   docker compose up --build -d
   ```

4. **Verify container health and status**:
   ```bash
   docker compose ps
   ```

5. **Tail application logs**:
   ```bash
   docker compose logs -f bot
   ```

6. **Stop services**:
   ```bash
   docker compose down
   ```
   *(To wipe persistent database and cache volumes, pass `-v`: `docker compose down -v`)*

---

## Local Development (Without Docker)

If you prefer to run the application directly in a Python virtual environment:

### 1. Create Virtual Environment and Install Dependencies

Using `uv` (recommended):
```bash
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Or using standard `pip`:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### 2. Configure Environment

```bash
cp .env.example .env
```
Ensure your local PostgreSQL and Redis instances are running and configured in `.env`.

### 3. Run Database Migrations

Apply database migrations:
```bash
alembic upgrade head
```

To create a new revision when models change in future tasks:
```bash
alembic revision --autogenerate -m "describe_migration"
```

### 4. Run Health Check Verification

Validate that database and Redis connections are live:
```bash
python -m src.app.infrastructure.healthcheck
```
Output:
```json
{
  "status": "healthy",
  "database": {
    "status": "healthy",
    "engine": "postgresql+asyncpg"
  },
  "redis": {
    "status": "healthy"
  }
}
```

### 5. Launch Bot Long Polling

```bash
python -m src.app.main
```

---

## Code Quality, Linting & Testing

All validation commands run offline without requiring external network access or a real Telegram Bot API connection.

### Run Unit and Integration Tests

```bash
pytest
```
Run with verbose output and coverage:
```bash
pytest -v
```

### Code Formatting and Linting with Ruff

Check for lint violations:
```bash
ruff check .
```

Check code formatting compliance:
```bash
ruff format --check .
```

Automatically format code:
```bash
ruff format .
```

Validate Python bytecode compilation:
```bash
python -m compileall src tests
```

---

## Operational Details & Security Policies

1. **Zero Secret Leakage**:
   - The application registers a custom `SensitiveDataFilter` that intercepts and scrubs bot tokens, database connection URIs, and Redis passwords from stdout and log entries.
   - All secrets in settings use Pydantic's `SecretStr` type.
2. **Network Security**:
   - `docker-compose.yml` runs PostgreSQL and Redis strictly on an internal bridge network (`bot_internal`). No database or cache ports are exposed publicly to the host.
3. **Non-Root Containers**:
   - The `Dockerfile` creates and runs as an unprivileged user `appuser` (UID `10001`).
4. **Timezone & Timestamps**:
   - All database timestamps are timezone-aware UTC (`TIMESTAMP WITH TIME ZONE`).
   - Business calculations utilize the configured business timezone (`Asia/Tehran`).
5. **Alembic Over CreateAll**:
   - `create_all()` is forbidden in production code; all schema evolutions must be tracked in `migrations/versions/`.
