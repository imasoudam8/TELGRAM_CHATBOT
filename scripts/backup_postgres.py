#!/usr/bin/env python3
"""Operational PostgreSQL backup CLI with verification, GPG encryption, and retention pruning."""

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
    backup_result_summary,
    create_postgres_backup,
)
from src.app.infrastructure.logging.logger import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Create and verify a PostgreSQL custom-format backup."""
    parser = argparse.ArgumentParser(description="Create and verify PostgreSQL backup")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(os.environ.get("BACKUP_DIR", "backups")),
        help="Directory to store backups (permissions set to 0700)",
    )
    parser.add_argument(
        "--daily-keep",
        type=int,
        default=int(os.environ.get("BACKUP_DAILY_RETENTION", "7")),
        help="Number of daily backups to retain (default: 7)",
    )
    parser.add_argument(
        "--monthly-keep",
        type=int,
        default=int(os.environ.get("BACKUP_MONTHLY_RETENTION", "1")),
        help="Number of monthly backups to retain (default: 1)",
    )
    parser.add_argument(
        "--gpg-recipient",
        default=os.environ.get("BACKUP_GPG_RECIPIENT") or None,
        help="Optional GPG public key recipient for encrypted backups",
    )
    parser.add_argument(
        "--gpg-passphrase-file",
        type=Path,
        default=(
            Path(os.environ["BACKUP_GPG_PASSPHRASE_FILE"])
            if os.environ.get("BACKUP_GPG_PASSPHRASE_FILE")
            else None
        ),
        help="Optional path to file containing symmetric GPG encryption passphrase",
    )
    parser.add_argument(
        "--remote-target",
        default=os.environ.get("BACKUP_REMOTE_TARGET") or None,
        help="Optional off-server destination (e.g., s3://bucket/path or user@host:/backups)",
    )
    parser.add_argument(
        "--remote-command",
        default=os.environ.get("BACKUP_REMOTE_COMMAND", "rclone"),
        help="Off-server upload tool: rclone, scp, or aws (default: rclone)",
    )
    parser.add_argument(
        "--require-remote",
        action="store_true",
        help="Fail if off-server backup destination is not configured",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    try:
        res = create_postgres_backup(
            settings,
            backup_dir=args.backup_dir,
            gpg_recipient=args.gpg_recipient,
            gpg_passphrase=os.environ.get("BACKUP_GPG_PASSPHRASE") or None,
            gpg_passphrase_file=args.gpg_passphrase_file,
            daily_keep=args.daily_keep,
            monthly_keep=args.monthly_keep,
            remote_target=args.remote_target,
            remote_command=args.remote_command,
            require_remote=args.require_remote,
        )
        print(json.dumps(backup_result_summary(res), indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
