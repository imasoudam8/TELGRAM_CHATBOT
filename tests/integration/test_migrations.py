"""Integration tests for Alembic migration environment and PostgreSQL DDL generation."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).parent.parent.parent


def test_alembic_script_directory_discovery() -> None:
    """Verify that Alembic locates the migrations directory and valid revision scripts."""
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    script_dir = ScriptDirectory.from_config(alembic_cfg)

    head = script_dir.get_current_head()
    assert head is not None, "Alembic head revision is missing"
    assert head == "e0d35e865fec", f"Unexpected Alembic head revision: {head}"

    revision_script = script_dir.get_revision(head)
    assert revision_script is not None
    assert "initial_schema" in revision_script.doc


def test_alembic_offline_sql_generation(tmp_path: Path) -> None:
    """Verify that Alembic offline mode renders complete PostgreSQL DDL statements."""
    from alembic import command

    output_sql_file = tmp_path / "migration.sql"
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    # Execute offline migration command redirecting stdout to output file
    with open(output_sql_file, "w") as f:
        with pytest.MonkeyPatch.context() as m:
            m.setattr("sys.stdout", f)
            command.upgrade(alembic_cfg, "head", sql=True)

    sql_content = output_sql_file.read_text()
    assert "CREATE TABLE users" in sql_content
    assert "CREATE TABLE user_profiles" in sql_content
    assert "CREATE TABLE chat_sessions" in sql_content
    assert "CREATE TABLE wallet_ledgers" in sql_content
    assert "CREATE TRIGGER trg_wallet_ledgers_append_only" in sql_content
    assert "CREATE TRIGGER trg_users_declaration_guard" in sql_content
    assert "CREATE TRIGGER trg_chat_messages_guard" in sql_content
    assert "CREATE TRIGGER trg_message_evidence_holds_guard" in sql_content
