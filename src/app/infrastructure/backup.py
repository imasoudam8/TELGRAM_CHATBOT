"""PostgreSQL native backup, integrity verification, encryption, retention, and restore tools."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.app.config.settings import Settings
from src.app.infrastructure.database.migrations import get_expected_alembic_heads
from src.app.infrastructure.logging.logger import SensitiveDataFilter

logger = logging.getLogger(__name__)

SUPPORTED_POSTGRES_MAJOR = 16
BACKUP_FILENAME_RE = re.compile(r"^(?P<db>[A-Za-z0-9_.-]+)_(?P<ts>\d{8}T\d{6}Z)\.dump(?:\.gpg)?$")
PG_VERSION_RE = re.compile(r"\b(\d+)(?:\.\d+)*\b")


@dataclass(frozen=True)
class BackupResult:
    """Metadata produced by a verified PostgreSQL backup run."""

    backup_path: Path
    checksum_path: Path
    sha256: str
    size_bytes: int
    encrypted: bool
    toc_entries: int
    offsite_status: str
    remote_target: str | None
    pruned_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BackupVerificationResult:
    """Result of verifying backup integrity and archive table of contents."""

    backup_path: Path
    size_bytes: int
    sha256: str
    checksum_verified: bool
    encrypted: bool
    toc_entries: int


@dataclass(frozen=True)
class RestoreVerificationResult:
    """Result of restoring a backup into a target PostgreSQL database and verifying schema."""

    backup_path: Path
    target_db: str
    alembic_revisions: tuple[str, ...]
    tables_count: int
    triggers_count: int
    users_count: int


def _make_scrubber(
    settings: Settings,
    extra_secrets: list[str] | None = None,
) -> SensitiveDataFilter:
    secrets = [
        settings.BOT_TOKEN.get_secret_value(),
        settings.POSTGRES_PASSWORD.get_secret_value(),
    ]
    if settings.REDIS_PASSWORD:
        secrets.append(settings.REDIS_PASSWORD.get_secret_value())
    if extra_secrets:
        secrets.extend(s for s in extra_secrets if s)
    return SensitiveDataFilter(additional_secrets=secrets)


def _pg_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = settings.POSTGRES_PASSWORD.get_secret_value()
    return env


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_sha256_sidecar(backup_path: Path) -> tuple[Path, str]:
    digest = _compute_sha256(backup_path)
    checksum_path = Path(f"{backup_path}.sha256")
    checksum_path.write_text(f"{digest}  {backup_path.name}\n", encoding="utf-8")
    os.chmod(checksum_path, 0o600)
    return checksum_path, digest


def _read_passphrase(
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
) -> str | None:
    if passphrase:
        return passphrase
    if passphrase_file is not None:
        if not passphrase_file.exists():
            raise FileNotFoundError(f"GPG passphrase file not found: {passphrase_file}")
        secret = passphrase_file.read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError("GPG passphrase file is empty.")
        return secret
    return None


def _run_subprocess(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    scrubber: SensitiveDataFilter,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        err = scrubber.redact((result.stderr or result.stdout or "").strip())
        safe_cmd = scrubber.redact(" ".join(cmd))
        raise RuntimeError(
            f"Command failed (exit={result.returncode}) [{safe_cmd}]: {err or 'no stderr'}"
        )
    return result


def parse_pg_tool_major_version(version_output: str) -> int:
    """Extract the PostgreSQL major version integer from '--version' CLI output."""
    text = version_output.strip()
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", text)
    if match:
        return int(match.group(1))
    fallback = PG_VERSION_RE.search(text)
    if fallback:
        return int(fallback.group(1))
    raise ValueError(f"Could not parse PostgreSQL tool major version from: {text!r}")


def get_pg_tool_major_version(tool_name: str) -> int:
    """Return the installed major version for pg_dump, pg_restore, or psql."""
    if shutil.which(tool_name) is None:
        raise RuntimeError(f"'{tool_name}' binary is not installed in PATH.")
    res = subprocess.run(
        [tool_name, "--version"],
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to query '{tool_name} --version': {res.stderr.strip()}")
    return parse_pg_tool_major_version(res.stdout or res.stderr)


def get_pg_server_major_version(
    settings: Settings,
    *,
    database: str | None = None,
    scrubber: SensitiveDataFilter | None = None,
) -> int:
    """Query the target PostgreSQL server major version via server_version_num."""
    if shutil.which("psql") is None:
        raise RuntimeError("'psql' binary is not installed in PATH.")
    active_scrubber = scrubber or _make_scrubber(settings)
    target_db = database or settings.POSTGRES_DB
    res = _run_subprocess(
        [
            "psql",
            "--host",
            settings.POSTGRES_HOST,
            "--port",
            str(settings.POSTGRES_PORT),
            "--username",
            settings.POSTGRES_USER,
            "--dbname",
            target_db,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SHOW server_version_num;",
        ],
        env=_pg_env(settings),
        scrubber=active_scrubber,
    )
    raw_num = res.stdout.strip().splitlines()[-1].strip()
    if not raw_num.isdigit():
        raise RuntimeError(f"Unexpected PostgreSQL server_version_num output: {raw_num!r}")
    return int(raw_num) // 10000


def assert_pg_tools_compatible(
    settings: Settings,
    *,
    tools: tuple[str, ...] = ("pg_dump", "pg_restore"),
    database: str | None = None,
    scrubber: SensitiveDataFilter | None = None,
) -> dict[str, int]:
    """Verify PostgreSQL client tools are compatible with the target PostgreSQL server."""
    server_major = get_pg_server_major_version(
        settings,
        database=database,
        scrubber=scrubber,
    )
    versions: dict[str, int] = {"server": server_major}
    for tool in tools:
        client_major = get_pg_tool_major_version(tool)
        versions[tool] = client_major
        if client_major > server_major:
            raise RuntimeError(
                f"PostgreSQL client compatibility error: '{tool}' major version {client_major} "
                f"is newer than target PostgreSQL server major version {server_major} "
                "(which causes incompatible SQL such as 'SET transaction_timeout = 0;'). "
                f"Install PostgreSQL {server_major} client utilities."
            )
    return versions


def encrypt_file_gpg(
    source_path: Path,
    *,
    recipient: str | None = None,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    scrubber: SensitiveDataFilter | None = None,
) -> Path:
    """Encrypt source_path using GPG asymmetric recipient or symmetric passphrase-fd."""
    if shutil.which("gpg") is None:
        raise RuntimeError("GPG binary ('gpg') is required for backup encryption.")

    resolved_pass = _read_passphrase(passphrase, passphrase_file)
    if not recipient and not resolved_pass:
        raise ValueError("Provide either GPG recipient or passphrase/passphrase_file.")

    active_scrubber = scrubber or SensitiveDataFilter(
        additional_secrets=[resolved_pass] if resolved_pass else None
    )
    encrypted_path = Path(f"{source_path}.gpg")

    if recipient:
        cmd = [
            "gpg",
            "--batch",
            "--yes",
            "--trust-model",
            "always",
            "--encrypt",
            "--recipient",
            recipient,
            "--output",
            str(encrypted_path),
            str(source_path),
        ]
        _run_subprocess(cmd, scrubber=active_scrubber)
    else:
        assert resolved_pass is not None
        with tempfile.TemporaryDirectory(prefix="gpg_home_") as gpg_home:
            os.chmod(gpg_home, 0o700)
            cmd = [
                "gpg",
                "--homedir",
                gpg_home,
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--output",
                str(encrypted_path),
                str(source_path),
            ]
            _run_subprocess(cmd, stdin_text=f"{resolved_pass}\n", scrubber=active_scrubber)

    if not encrypted_path.exists() or encrypted_path.stat().st_size == 0:
        raise RuntimeError("GPG encryption produced an empty or missing output file.")
    os.chmod(encrypted_path, 0o600)
    return encrypted_path


def decrypt_file_gpg(
    encrypted_path: Path,
    output_path: Path,
    *,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    scrubber: SensitiveDataFilter | None = None,
) -> Path:
    """Decrypt a .gpg backup file cleanly using gpg-agent or loopback passphrase-fd."""
    if shutil.which("gpg") is None:
        raise RuntimeError("GPG binary ('gpg') is required for backup decryption.")

    resolved_pass = _read_passphrase(passphrase, passphrase_file)
    active_scrubber = scrubber or SensitiveDataFilter(
        additional_secrets=[resolved_pass] if resolved_pass else None
    )

    if resolved_pass is not None:
        with tempfile.TemporaryDirectory(prefix="gpg_home_") as gpg_home:
            os.chmod(gpg_home, 0o700)
            cmd = [
                "gpg",
                "--homedir",
                gpg_home,
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--decrypt",
                "--output",
                str(output_path),
                str(encrypted_path),
            ]
            _run_subprocess(cmd, stdin_text=f"{resolved_pass}\n", scrubber=active_scrubber)
    else:
        cmd = [
            "gpg",
            "--batch",
            "--yes",
            "--decrypt",
            "--output",
            str(output_path),
            str(encrypted_path),
        ]
        _run_subprocess(cmd, scrubber=active_scrubber)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Decrypted backup output file is empty or missing.")
    os.chmod(output_path, 0o600)
    return output_path


def verify_backup_file(
    backup_path: Path,
    *,
    checksum_path: Path | None = None,
    require_checksum: bool = False,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    settings: Settings | None = None,
) -> BackupVerificationResult:
    """Verify backup exists, is non-empty, passes SHA-256 sidecar, and has valid pg_restore TOC."""
    if not backup_path.exists() or not backup_path.is_file():
        raise FileNotFoundError(f"Backup file does not exist: {backup_path}")

    size_bytes = backup_path.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"Backup file is empty (0 bytes): {backup_path}")

    actual_sha256 = _compute_sha256(backup_path)
    resolved_checksum = checksum_path or Path(f"{backup_path}.sha256")
    checksum_verified = False

    if resolved_checksum.exists():
        raw_line = resolved_checksum.read_text(encoding="utf-8").strip()
        if not raw_line:
            raise ValueError(f"Checksum file is empty: {resolved_checksum}")
        expected_hash = raw_line.split()[0].lower()
        if expected_hash != actual_sha256.lower():
            raise ValueError(
                f"Checksum mismatch for {backup_path.name}: "
                f"expected={expected_hash}, actual={actual_sha256}"
            )
        checksum_verified = True
    elif require_checksum:
        raise FileNotFoundError(f"Required SHA-256 checksum file missing: {resolved_checksum}")

    if shutil.which("pg_restore") is None:
        raise RuntimeError("'pg_restore' binary is not installed in PATH.")

    resolved_pass = _read_passphrase(passphrase, passphrase_file)
    scrubber = (
        _make_scrubber(settings, [resolved_pass] if resolved_pass else None)
        if settings
        else SensitiveDataFilter(additional_secrets=[resolved_pass] if resolved_pass else None)
    )

    is_encrypted = backup_path.suffix == ".gpg"
    if is_encrypted:
        with tempfile.TemporaryDirectory(prefix="pg_verify_") as tmpdir:
            decrypted = Path(tmpdir) / backup_path.stem
            decrypt_file_gpg(
                backup_path,
                decrypted,
                passphrase=resolved_pass,
                scrubber=scrubber,
            )
            res = _run_subprocess(["pg_restore", "--list", str(decrypted)], scrubber=scrubber)
    else:
        res = _run_subprocess(["pg_restore", "--list", str(backup_path)], scrubber=scrubber)

    toc_lines = [
        line.strip()
        for line in res.stdout.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]
    if not toc_lines:
        raise ValueError(f"Backup archive has an empty table of contents: {backup_path}")

    return BackupVerificationResult(
        backup_path=backup_path,
        size_bytes=size_bytes,
        sha256=actual_sha256,
        checksum_verified=checksum_verified,
        encrypted=is_encrypted,
        toc_entries=len(toc_lines),
    )


def parse_backup_timestamp(path: Path) -> datetime | None:
    """Parse UTC timestamp from standardized backup filenames."""
    match = BACKUP_FILENAME_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def prune_backup_retention(
    backup_dir: Path,
    *,
    db_name: str,
    daily_keep: int = 7,
    monthly_keep: int = 1,
    protected_paths: set[Path] | None = None,
) -> tuple[Path, ...]:
    """Prune backup files honoring N daily backups plus M monthly snapshots."""
    if daily_keep < 1 or monthly_keep < 0:
        raise ValueError("daily_keep must be >= 1 and monthly_keep must be >= 0.")

    protected = {p.resolve() for p in (protected_paths or set())}
    candidates: list[tuple[datetime, Path]] = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        if not (
            entry.name.startswith(f"{db_name}_")
            and (entry.name.endswith(".dump") or entry.name.endswith(".dump.gpg"))
        ):
            continue
        ts = parse_backup_timestamp(entry)
        if ts is not None:
            candidates.append((ts, entry))

    candidates.sort(key=lambda item: item[0], reverse=True)

    keep_paths: set[Path] = set(protected)

    # 1. Keep latest snapshot per distinct calendar day up to daily_keep days
    kept_days: set[str] = set()
    for ts, entry in candidates:
        day_key = ts.strftime("%Y-%m-%d")
        if day_key not in kept_days:
            if len(kept_days) < daily_keep:
                kept_days.add(day_key)
                keep_paths.add(entry.resolve())
        elif day_key in kept_days and entry.resolve() in protected:
            keep_paths.add(entry.resolve())

    # 2. Keep latest snapshot per distinct calendar month up to monthly_keep months
    if monthly_keep > 0:
        kept_months: set[str] = set()
        for ts, entry in candidates:
            month_key = ts.strftime("%Y-%m")
            if month_key not in kept_months and len(kept_months) < monthly_keep:
                kept_months.add(month_key)
                keep_paths.add(entry.resolve())

    pruned: list[Path] = []
    for _, entry in candidates:
        if entry.resolve() not in keep_paths:
            entry.unlink(missing_ok=True)
            pruned.append(entry)
            sha_sidecar = Path(f"{entry}.sha256")
            if sha_sidecar.exists():
                sha_sidecar.unlink(missing_ok=True)
                pruned.append(sha_sidecar)

    return tuple(pruned)


def _upload_offsite(
    backup_path: Path,
    checksum_path: Path,
    *,
    remote_target: str,
    remote_command: str,
    scrubber: SensitiveDataFilter,
) -> None:
    """Upload verified backup and SHA-256 sidecar to configured off-server storage."""
    tool = remote_command.strip().lower()
    if tool == "rclone":
        if shutil.which("rclone") is None:
            raise RuntimeError("'rclone' is configured for off-server backup but not installed.")
        dest = remote_target.rstrip("/")
        _run_subprocess(
            ["rclone", "copyto", str(backup_path), f"{dest}/{backup_path.name}"], scrubber=scrubber
        )
        _run_subprocess(
            ["rclone", "copyto", str(checksum_path), f"{dest}/{checksum_path.name}"],
            scrubber=scrubber,
        )
    elif tool == "scp":
        if shutil.which("scp") is None:
            raise RuntimeError("'scp' is configured for off-server backup but not installed.")
        _run_subprocess(
            ["scp", "-q", str(backup_path), str(checksum_path), remote_target], scrubber=scrubber
        )
    elif tool == "aws":
        if shutil.which("aws") is None:
            raise RuntimeError("'aws' CLI is configured for off-server backup but not installed.")
        dest = remote_target.rstrip("/")
        _run_subprocess(
            ["aws", "s3", "cp", str(backup_path), f"{dest}/{backup_path.name}"], scrubber=scrubber
        )
        _run_subprocess(
            ["aws", "s3", "cp", str(checksum_path), f"{dest}/{checksum_path.name}"],
            scrubber=scrubber,
        )
    else:
        raise ValueError(
            f"Unsupported BACKUP_REMOTE_COMMAND '{remote_command}'. Use rclone, scp, or aws."
        )


def create_postgres_backup(
    settings: Settings,
    *,
    backup_dir: Path,
    gpg_recipient: str | None = None,
    gpg_passphrase: str | None = None,
    gpg_passphrase_file: Path | None = None,
    daily_keep: int = 7,
    monthly_keep: int = 1,
    remote_target: str | None = None,
    remote_command: str = "rclone",
    require_remote: bool = False,
) -> BackupResult:
    """Execute pg_dump, optional GPG encryption, verification, retention prune, and offsite copy."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError("'pg_dump' binary is not installed in PATH.")

    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    resolved_pass = _read_passphrase(gpg_passphrase, gpg_passphrase_file)
    scrubber = _make_scrubber(settings, [resolved_pass] if resolved_pass else None)

    assert_pg_tools_compatible(
        settings,
        tools=("pg_dump", "pg_restore"),
        database=settings.POSTGRES_DB,
        scrubber=scrubber,
    )

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    raw_dump_path = backup_dir / f"{settings.POSTGRES_DB}_{timestamp}.dump"

    dump_cmd = [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        "--host",
        settings.POSTGRES_HOST,
        "--port",
        str(settings.POSTGRES_PORT),
        "--username",
        settings.POSTGRES_USER,
        "--file",
        str(raw_dump_path),
        settings.POSTGRES_DB,
    ]
    _run_subprocess(dump_cmd, env=_pg_env(settings), scrubber=scrubber)
    if not raw_dump_path.exists() or raw_dump_path.stat().st_size == 0:
        raise RuntimeError("pg_dump finished but backup file is empty or missing.")
    os.chmod(raw_dump_path, 0o600)

    final_path = raw_dump_path
    encrypted = False
    if gpg_recipient or resolved_pass:
        try:
            encrypted_path = encrypt_file_gpg(
                raw_dump_path,
                recipient=gpg_recipient,
                passphrase=resolved_pass,
                scrubber=scrubber,
            )
            final_path = encrypted_path
            encrypted = True
        finally:
            raw_dump_path.unlink(missing_ok=True)

    checksum_path, sha256 = _write_sha256_sidecar(final_path)
    verification = verify_backup_file(
        final_path,
        checksum_path=checksum_path,
        require_checksum=True,
        passphrase=resolved_pass,
        settings=settings,
    )

    clean_remote = remote_target.strip() if remote_target else ""
    if clean_remote:
        _upload_offsite(
            final_path,
            checksum_path,
            remote_target=clean_remote,
            remote_command=remote_command,
            scrubber=scrubber,
        )
        offsite_status = "uploaded"
    else:
        if require_remote:
            raise RuntimeError(
                "Off-server backup destination is required (--require-remote) "
                "but BACKUP_REMOTE_TARGET is not configured."
            )
        offsite_status = "skipped_unconfigured"

    pruned = prune_backup_retention(
        backup_dir,
        db_name=settings.POSTGRES_DB,
        daily_keep=daily_keep,
        monthly_keep=monthly_keep,
        protected_paths={final_path},
    )

    return BackupResult(
        backup_path=final_path,
        checksum_path=checksum_path,
        sha256=sha256,
        size_bytes=verification.size_bytes,
        encrypted=encrypted,
        toc_entries=verification.toc_entries,
        offsite_status=offsite_status,
        remote_target=clean_remote or None,
        pruned_files=pruned,
    )


def restore_and_verify_backup(
    settings: Settings,
    *,
    backup_path: Path,
    target_db: str,
    create_target_db: bool = False,
    allow_existing_target: bool = False,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    verify_alembic_head: bool = True,
) -> RestoreVerificationResult:
    """Verify backup integrity, restore to target_db via pg_restore, and validate schema."""
    if shutil.which("pg_restore") is None or shutil.which("psql") is None:
        raise RuntimeError("'pg_restore' and 'psql' binaries must be installed in PATH.")

    if target_db == settings.POSTGRES_DB and not allow_existing_target:
        raise ValueError(
            "Refusing to restore directly over active database without "
            "--confirm-overwrite / allow_existing_target=True."
        )

    resolved_pass = _read_passphrase(passphrase, passphrase_file)
    scrubber = _make_scrubber(settings, [resolved_pass] if resolved_pass else None)

    assert_pg_tools_compatible(
        settings,
        tools=("pg_restore",),
        database="postgres" if create_target_db else target_db,
        scrubber=scrubber,
    )

    verification = verify_backup_file(
        backup_path,
        require_checksum=Path(f"{backup_path}.sha256").exists(),
        passphrase=resolved_pass,
        settings=settings,
    )

    env = _pg_env(settings)
    if create_target_db:
        safe_db = target_db.replace('"', '""')
        _run_subprocess(
            [
                "psql",
                "--host",
                settings.POSTGRES_HOST,
                "--port",
                str(settings.POSTGRES_PORT),
                "--username",
                settings.POSTGRES_USER,
                "--dbname",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f'CREATE DATABASE "{safe_db}";',
            ],
            env=env,
            scrubber=scrubber,
        )

    def _exec_restore(archive_file: Path) -> None:
        cmd = [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--host",
            settings.POSTGRES_HOST,
            "--port",
            str(settings.POSTGRES_PORT),
            "--username",
            settings.POSTGRES_USER,
            "--dbname",
            target_db,
        ]
        if allow_existing_target and not create_target_db:
            cmd.extend(["--clean", "--if-exists"])
        cmd.append(str(archive_file))
        _run_subprocess(cmd, env=env, scrubber=scrubber)

    if verification.encrypted:
        with tempfile.TemporaryDirectory(prefix="pg_restore_") as tmpdir:
            decrypted = Path(tmpdir) / backup_path.stem
            decrypt_file_gpg(
                backup_path,
                decrypted,
                passphrase=resolved_pass,
                scrubber=scrubber,
            )
            _exec_restore(decrypted)
    else:
        _exec_restore(backup_path)

    # Validate restored schema: alembic_version, tables, triggers, users table readable
    def _psql_scalar(sql: str) -> str:
        res = _run_subprocess(
            [
                "psql",
                "--host",
                settings.POSTGRES_HOST,
                "--port",
                str(settings.POSTGRES_PORT),
                "--username",
                settings.POSTGRES_USER,
                "--dbname",
                target_db,
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            env=env,
            scrubber=scrubber,
        )
        return res.stdout.strip()

    rev_out = _psql_scalar("SELECT version_num FROM alembic_version ORDER BY version_num;")
    revisions = tuple(r.strip() for r in rev_out.splitlines() if r.strip())
    if not revisions:
        raise RuntimeError("Restored database is missing alembic_version records.")
    if verify_alembic_head:
        expected_heads = get_expected_alembic_heads()
        if revisions != expected_heads:
            raise RuntimeError(
                f"Restored Alembic revision {revisions} does not match expected {expected_heads}."
            )

    tables_count = int(
        _psql_scalar(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
        )
    )
    triggers_count = int(
        _psql_scalar(
            "SELECT count(*) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND t.tgisinternal = false;"
        )
    )
    users_count = int(_psql_scalar("SELECT count(*) FROM users;"))

    if tables_count < 18 or triggers_count < 7:
        raise RuntimeError(
            f"Restored database incomplete: tables={tables_count}, triggers={triggers_count}"
        )

    return RestoreVerificationResult(
        backup_path=backup_path,
        target_db=target_db,
        alembic_revisions=revisions,
        tables_count=tables_count,
        triggers_count=triggers_count,
        users_count=users_count,
    )


def backup_result_summary(res: BackupResult) -> dict[str, Any]:
    """Serialize BackupResult cleanly without sensitive paths/secrets."""
    return {
        "backup_file": str(res.backup_path),
        "checksum_file": str(res.checksum_path),
        "sha256": res.sha256,
        "size_bytes": res.size_bytes,
        "encrypted": res.encrypted,
        "toc_entries": res.toc_entries,
        "offsite_status": res.offsite_status,
        "pruned_count": len(res.pruned_files),
    }
