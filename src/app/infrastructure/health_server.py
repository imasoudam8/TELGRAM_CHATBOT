"""Lightweight internal HTTP health and readiness endpoint for container/VPS probing."""

import asyncio
import json
import logging

from src.app.config.settings import Settings
from src.app.infrastructure.healthcheck import run_health_checks

logger = logging.getLogger(__name__)


class HealthHttpServer:
    """Minimal asyncio HTTP/1.1 server exposing /health (liveness) and /ready (readiness)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int | None:
        """Return the bound TCP port once started."""
        if self._server is None or not self._server.sockets:
            return None
        sockname = self._server.sockets[0].getsockname()
        return int(sockname[1])

    async def start(self) -> None:
        """Start listening on HEALTH_HOST:HEALTH_PORT."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._settings.HEALTH_HOST,
            port=self._settings.HEALTH_PORT,
        )
        logger.info(
            "Internal health endpoint listening on http://%s:%s",
            self._settings.HEALTH_HOST,
            self.bound_port,
        )

    async def stop(self) -> None:
        """Close the HTTP health server cleanly."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Internal health endpoint stopped.")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            request_line = raw_request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = request_line.split(" ")
            method = parts[0].upper() if len(parts) >= 1 else "GET"
            path = parts[1].split("?", 1)[0] if len(parts) >= 2 else "/"

            if method not in {"GET", "HEAD"}:
                await self._write_response(
                    writer,
                    405,
                    "Method Not Allowed",
                    {"status": "error", "error": "Method not allowed"},
                    head_only=False,
                )
                return

            if path in {"/health", "/livez"}:
                await self._write_response(
                    writer,
                    200,
                    "OK",
                    {"status": "alive", "service": "telegram-matching-bot"},
                    head_only=(method == "HEAD"),
                )
                return

            if path in {"/ready", "/healthz"}:
                is_healthy, details = await run_health_checks(
                    self._settings,
                    check_migrations=self._settings.effective_require_migrations_current,
                    timeout_seconds=5.0,
                )
                status_code = 200 if is_healthy else 503
                reason = "OK" if is_healthy else "Service Unavailable"
                await self._write_response(
                    writer,
                    status_code,
                    reason,
                    details,
                    head_only=(method == "HEAD"),
                )
                return

            await self._write_response(
                writer,
                404,
                "Not Found",
                {"status": "not_found"},
                head_only=(method == "HEAD"),
            )
        except Exception:
            try:
                await self._write_response(
                    writer,
                    500,
                    "Internal Server Error",
                    {"status": "unhealthy"},
                    head_only=False,
                )
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        status_code: int,
        reason: str,
        payload: dict[str, object],
        *,
        head_only: bool,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers)
        if not head_only:
            writer.write(body)
        await writer.drain()
