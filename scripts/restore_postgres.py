#!/usr/bin/env python3
"""Operational PostgreSQL backup verification & safe restore drill CLI."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.app.config.settings import get_settings  # noqa: E402
from src.app.infrastructure.backup import (  # noqa: E402
    restore_and_verify_backup,
    verify_backup_file,
)
from src.app.infrastructure.logging.logger import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Verify backup archive integrity or perform verified restore into target DB."""
    parser = argparse.ArgumentParser(description="Verify or restore a PostgreSQL backup")
    parser.add_argument("backup_file", type=Path, help="Path to .dump or .dump.gpg backup file")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify checksum, decryption (if encrypted), and pg_restore TOC without restoring",
    )
    parser.add_argument(
        "--target-db",
        default=None,
        help="Target database name for restore (recommended: isolated drill DB)",
    )
    parser.add_argument(
        "--create-target-db",
        action="store_true",
        help="Create target database before restoring (for isolated restore drills)",
    )
    parser.add_argument(
        "--confirm-overwrite",
        action="store_true",
        help="Explicit confirmation required to restore onto the active primary database",
    )
    parser.add_argument(
        "--skip-head-check",
        action="store_true",
        help="Skip verifying restored alembic_version matches current repository head",
    )
    parser.add_argument(
        "--gpg-passphrase-file",
        type=Path,
        default=(
            Path(os.environ["BACKUP_GPG_PASSPHRASE_FILE"])
            if os.environ.get("BACKUP_GPG_PASSPHRASE_FILE")
            else None
        ),
        help="Optional path to file containing GPG passphrase",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    passphrase = os.environ.get("BACKUP_GPG_PASSPHRASE") or None

    try:
        if args.verify_only:
            v = verify_backup_file(
                args.backup_file,
                require_checksum=Path(f"{args.backup_file}.sha256").exists(),
                passphrase=passphrase,
                passphrase_file=args.gpg_passphrase_file,
                settings=settings,
            )
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "backup_file": str(v.backup_path),
                        "size_bytes": v.size_bytes,
                        "sha256": v.sha256,
                        "checksum_verified": v.checksum_verified,
                        "encrypted": v.encrypted,
                        "toc_entries": v.toc_entries,
                    },
                    indent=2,
                )
            )
            return 0

        if not args.target_db:
            parser.error("--target-db is required unless --verify-only is specified.")

        r = restore_and_verify_backup(
            settings,
            backup_path=args.backup_file,
            target_db=args.target_db,
            create_target_db=args.create_target_db,
            allow_existing_target=args.confirm_overwrite,
            passphrase=passphrase,
            passphrase_file=args.gpg_passphrase_file,
            verify_alembic_head=not args.skip_head_check,
        )
        print(
            json.dumps(
                {
                    "status": "restored_and_verified",
                    "backup_file": str(r.backup_path),
                    "target_db": r.target_db,
                    "alembic_revisions": list(r.alembic_revisions),
                    "tables_count": r.tables_count,
                    "triggers_count": r.triggers_count,
                    "users_count": r.users_count,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
