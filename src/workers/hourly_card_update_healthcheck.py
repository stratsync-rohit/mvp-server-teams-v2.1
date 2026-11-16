"""Docker health check for the persisted hourly-worker liveness and scheduler state."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from src.config.settings import get_settings


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def health_diagnostic(
    record: dict[str, Any] | None, now: datetime, *, heartbeat_seconds: int, expected_owner: str | None = None,
) -> tuple[bool, str]:
    """Validate liveness separately from completed-card-update progress.

    A normal worker writes a heartbeat every 30 seconds.  Three intervals plus
    one bounded operation (minimum 120 seconds) tolerate Docker/host clock
    skew and brief infrastructure delays, while a stale scheduler state catches
    a loop that is alive but no longer evaluating UTC checkpoints.
    """
    if not record:
        return False, "hourly worker heartbeat record is missing"
    if expected_owner is not None and record.get("owner") != expected_owner:
        return False, "hourly worker is standby; another owner holds the heartbeat"
    now = _as_utc(now) or datetime.now(timezone.utc)
    heartbeat_limit = max(120, heartbeat_seconds * 3 + 30)
    updated_at = _as_utc(record.get("updated_at"))
    if updated_at is None:
        return False, "hourly worker heartbeat has no valid updated_at"
    if updated_at < now - timedelta(seconds=heartbeat_limit):
        return False, f"hourly worker heartbeat is stale (age={(now - updated_at).total_seconds():.0f}s)"
    expires_at = _as_utc(record.get("expires_at")) or _as_utc(record.get("ownership_expires_at"))
    if expected_owner is not None and expires_at is None:
        return False, "hourly worker owns an unfenced legacy heartbeat record"
    if expires_at is not None and expires_at < now - timedelta(seconds=10):
        return False, "hourly worker ownership lease expired"
    state = record.get("state")
    scheduler_at = _as_utc(record.get("scheduler_updated_at"))
    # Docker grants a 120 second start period. During that period the worker
    # has a fresh heartbeat but has not necessarily published a scheduler tick.
    if state != "starting":
        if scheduler_at is None:
            return False, "hourly worker scheduler state is missing"
        if scheduler_at < now - timedelta(seconds=heartbeat_limit):
            return False, f"hourly worker scheduler is stalled (age={(now - scheduler_at).total_seconds():.0f}s)"
    return True, "hourly worker heartbeat and scheduler are healthy"


def _read_expected_owner(path: str) -> str | None:
    try:
        owner = Path(path).read_text(encoding="utf-8").strip()
        return owner or None
    except OSError:
        return None


async def main() -> int:
    settings = get_settings()
    if not settings.hourly_card_update_worker_enabled or not settings.mongodb_url:
        print("hourly worker health check is not configured", file=sys.stderr)
        return 1
    client = AsyncIOMotorClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
        connectTimeoutMS=settings.mongodb_connect_timeout_ms,
        socketTimeoutMS=settings.mongodb_socket_timeout_ms,
        tz_aware=True,
    )
    try:
        record = await client[settings.mongodb_database]["hourly_card_update_leases"].find_one({
            "lease_name": "hourly-card-update-heartbeat",
        })
        expected_owner = _read_expected_owner(settings.hourly_card_update_owner_marker_path)
        if expected_owner is None:
            print("hourly worker is standby; local heartbeat ownership has not been acquired", file=sys.stderr)
            return 1
        healthy, diagnostic = health_diagnostic(
            record, datetime.now(timezone.utc), heartbeat_seconds=settings.hourly_card_update_heartbeat_seconds,
            expected_owner=expected_owner,
        )
        stream = sys.stdout if healthy else sys.stderr
        print(diagnostic, file=stream)
        return 0 if healthy else 1
    except Exception as exc:
        # Distinguishes unavailable Mongo from a persisted worker failure in
        # Docker's health-check output without exposing connection credentials.
        print(f"hourly worker health check MongoDB connectivity failure: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
