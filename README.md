# Anonymous Telegram Matching & Chat Bot

> **TASK 001** — Project Foundation & Engineering Infrastructure (Scaffolding Phase)

A typed, asynchronous Python foundation for the **Anonymous Telegram Matching & Chat Bot**. This codebase provides centralized configuration, asynchronous PostgreSQL database access with SQLAlchemy 2.x and Alembic migrations, Redis caching & coordination, structured logging with automatic secret redaction, and a multi-stage Docker container build.

*Note: This repository represents foundation scaffolding and engineering infrastructure. It does not implement product features (such as user registration or chat routing) and is not yet designated for production deployment.*

---

## Architecture & Technology Stack

| Component | Technology | Version | Purpose |
| :--- | :--- | :--- | :--- |
| **Language** | Python | `3.12+` (pinned `3.12`) | Modern, typed asynchronous runtime |
| **Bot Framework** | aiogram | `3.x` | Asynchronous Telegram Bot API framework |
| **Relational Database** | PostgreSQL | `16` | Persistent relational store |
| **ORM & Driver** | SQLAlchemy & asyncpg | `2.0+` & `0.30+` | Async declarative base, connection pooling |
| **Database Migrations** | Alembic | `1.14+` | Version-controlled schema migrations |
| **In-Memory Store** | Redis | `7` | Ephemeral state & coordination |
| **Configuration** | Pydantic Settings | `2.7+` | Centralized typed environment validation |
| **Testing** | pytest & pytest-asyncio | `8.3+` | Unit & integration test execution |
| **Linter / Formatter** | Ruff | `0.8+` | Code hygiene and style enforcement |
| **Orchestration** | Docker & Docker Compose | Multi-stage | Isolated execution environment |
| **Continuous Integration** | GitHub Actions | Workflows | Automated lint, format check, compile, test |

---

## Directory Layout

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml              # GitHub Actions CI workflow (services: postgres, redis)
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
│       │   ├── filters/        # Custom message filters
│       │   ├── handlers/       # Bot update routers (smoke test /start)
│       │   ├── keyboards/      # Keyboard markup scaffolding
│       │   └── middlewares/    # Structured event logging middleware
│       ├── config/             # Centralized Pydantic Settings
│       │   └── settings.py
│       ├── infrastructure/     # Infrastructure adapters
│       │   ├── database/       # Async SQLAlchemy engine & base
│       │   ├── healthcheck.py  # Health monitoring logic
│       │   ├── lifecycle.py    # Strict startup ordering & graceful shutdown
│       │   ├── logging/        # Structured JSON & sensitive data masking
│       │   ├── redis/          # Async Redis client pool & lifecycle
│       │   └── telegram/       # Bot & Dispatcher factory
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
│   ├── integration/            # DB, Redis, Dispatcher & live services tests
│   └── unit/                   # Config, logging, health, handlers & lifecycle tests
├── .env.example                # Safe environment variable documentation
├── .gitignore                  # VCS exclusion rules
├── .python-version             # Pinned Python version (3.12)
├── alembic.ini                 # Alembic CLI configuration
├── Dockerfile                  # Canonical multi-stage container build
├── docker-compose.yml          # Local orchestration (PostgreSQL + Redis + Bot)
├── pyproject.toml              # Build metadata, dependencies & tool configs
└── README.md
```

---

## Prerequisites

- **Python 3.12** or higher
- **Git**
- Optional: **Docker** (`>= 24.0`) and **Docker Compose** (`>= 2.20`)
- Optional: `uv` (recommended for fast virtual environment management)

---

## Local Development Installation

### 1. Create and Activate Virtual Environment

Using standard Python:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Or using `uv`:
```bash
uv venv .venv --python 3.12
source .venv/bin/activate
```

### 2. Install Dependencies

Install the project in editable mode including development dependencies:
```bash
pip install -e ".[dev]"
```
*(Or with uv: `uv pip install -e ".[dev]"`)`*

Package building is managed via Hatchling and configured in `pyproject.toml` with `[tool.hatch.build.targets.wheel] packages = ["src/app"]`.

### 3. Environment Configuration

Copy the example configuration file:
```bash
cp .env.example .env
```

Open `.env` and set your Telegram Bot token from [@BotFather](https://t.me/BotFather):
```env
BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
```

Configure local PostgreSQL and Redis credentials matching your development services.

---

## Application Lifecycle & Running the Bot

The application enforces a strict, ordered async lifespan:
1. Load and validate settings via Pydantic.
2. Configure logging with sensitive credential redaction.
3. Initialize PostgreSQL engine and connection pool.
4. Verify PostgreSQL connectivity via `SELECT 1`.
5. Initialize Redis connection pool.
6. Verify Redis connectivity via `PING`.
7. Instantiate Bot client and Dispatcher.
8. Start long polling only after all dependencies are verified healthy.
9. On any startup failure, previously initialized resources are closed cleanly.
10. On shutdown or cancellation (`SIGINT`/`SIGTERM`), the bot session, Redis pool, and PostgreSQL pool are gracefully closed in reverse order.

### Run Database Migrations
```bash
alembic upgrade head
```

### Run Health Check
Verify dependency connectivity directly via CLI:
```bash
python -m src.app.infrastructure.healthcheck
```

### Start the Bot
```bash
python -m src.app.main
```

---

## Docker & Docker Compose

The repository utilizes one canonical Dockerfile at the repository root (`Dockerfile`), featuring a multi-stage build, minimal attack surface (`python:3.12-slim-bookworm`), and an unprivileged non-root user (`appuser`, UID `10001`).

### Start Services via Docker Compose
```bash
docker compose up --build -d
```

### Inspect Container Status & Health
```bash
docker compose ps
```

### View Application Logs
```bash
docker compose logs -f bot
```

### Stop Services
```bash
docker compose down
```
*(Use `docker compose down -v` to delete persistent database and Redis volumes).*

---

## Code Quality, Linting & Testing

### Run Automated Tests
```bash
pytest -v
```

### Run Ruff Linter
```bash
ruff check .
```

### Run Ruff Formatter Check
```bash
ruff format --check .
```

### Auto-Format Code
```bash
ruff format .
```

### Validate Bytecode Compilation
```bash
python -m compileall src tests
```

---

## Troubleshooting Common Issues

1. **`pip install .[dev]` fails with "Unable to determine which files to ship"**:
   - Ensure `[tool.hatch.build.targets.wheel] packages = ["src/app"]` is present in `pyproject.toml`.
2. **Bot terminates on startup with `Configuration error: BOT_TOKEN`**:
   - The bot token is required and must match Telegram's `<bot_id>:<token_secret>` pattern. Verify `.env` is configured.
3. **Database connection failure on startup**:
   - The bot will not start polling if PostgreSQL is unreachable. Check that your database container is healthy via `pg_isready` or `python -m src.app.infrastructure.healthcheck`.
4. **Redis connection failure on startup**:
   - Ensure Redis is running and reachable on the configured host and port.
5. **Partial initialization cleanup**:
   - If Redis is unavailable while PostgreSQL is reachable, the application logs the failure and automatically closes the PostgreSQL connection pool before exiting.
