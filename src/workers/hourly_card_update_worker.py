"""Durable, single-owner hourly refresh worker for sent Teams risk cards."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pymongo import ReturnDocument

from src.database.mongo import MongoManager
from src.clients.teams_conversation_client import TeamsConversationClient
from src.config.settings import get_settings
from src.renderers.teams_risk_card_renderer import TeamsRiskCardRenderer
from src.repositories._base import maybe_await, utcnow
from src.repositories.notification_repository import NotificationRepository
from src.repositories.risk_repository import RiskRepository
from src.services.generic_risk_view_builder import GenericRiskViewBuilder
from src.services.risk_service import RiskService
from src.services.risk_timing import RiskTimingService
from src.services.teams_message_update_service import (
    PermanentTeamsUpdateError,
    RetryableTeamsUpdateError,
    TeamsMessageUpdateService,
)

logger = logging.getLogger(__name__)


class WorkerLeaseRepository:
    """Mongo leases for one active scheduler and one bounded update cycle."""

    collection_name = "hourly_card_update_leases"
    heartbeat_lease_name = "hourly-card-update-heartbeat"

    def __init__(self, mongo: MongoManager) -> None:
        self.collection = mongo.collection(self.collection_name)

    async def acquire(self, lease_name: str, owner: str, checkpoint: datetime, *, seconds: int = 900) -> bool:
        """Acquire an expired cycle lease or renew this owner's same checkpoint."""
        now = utcnow()
        result = await maybe_await(self.collection.find_one_and_update(
            {"lease_name": lease_name, "$or": [
                {"expires_at": {"$lte": now}},
                {"owner": owner, "checkpoint": checkpoint},
            ]},
            {"$set": {"owner": owner, "checkpoint": checkpoint, "expires_at": now + timedelta(seconds=seconds),
                      "updated_at": now}},
            upsert=False,
            return_document=ReturnDocument.AFTER,
        ))
        if result is not None:
            return True
        try:
            await maybe_await(self.collection.insert_one({
                "lease_name": lease_name, "owner": owner, "checkpoint": checkpoint,
                "expires_at": now + timedelta(seconds=seconds), "updated_at": now,
            }))
            return True
        except Exception as exc:
            # A competing worker may create the unique lease between the
            # conditional update and insert. It owns this cycle, not an error.
            if exc.__class__.__name__ == "DuplicateKeyError":
                return False
            raise

    async def renew(self, lease_name: str, owner: str, checkpoint: datetime, *, seconds: int = 900) -> bool:
        now = utcnow()
        result = await maybe_await(self.collection.update_one(
            {"lease_name": lease_name, "owner": owner, "checkpoint": checkpoint,
             "expires_at": {"$gt": now}},
            {"$set": {"expires_at": now + timedelta(seconds=seconds), "updated_at": now}},
        ))
        return bool(getattr(result, "matched_count", 0))

    async def release(self, lease_name: str, owner: str) -> None:
        now = utcnow()
        await maybe_await(self.collection.update_one(
            {"lease_name": lease_name, "owner": owner},
            {"$set": {"expires_at": now, "updated_at": now}},
        ))

    async def claim_heartbeat_owner(self, owner: str, *, seconds: int, legacy_stale_seconds: int = 180) -> bool:
        """Claim the stable heartbeat row without an owner-dependent upsert.

        A legacy row without an expiry is only claimable after its
        ``updated_at`` is stale. We only insert if no row exists. A duplicate
        insert race is retried as a normal conditional update, so a restart
        cannot crash with E11000 against the unique lease-name index.
        """
        now = utcnow()
        legacy_stale_before = now - timedelta(seconds=legacy_stale_seconds)
        query = {
            "lease_name": self.heartbeat_lease_name,
            "$or": [
                {"owner": owner},
                {"expires_at": {"$lte": now}},
                {"ownership_expires_at": {"$lte": now}},
                {"$and": [
                    # ``None`` deliberately matches a null or absent field.
                    {"expires_at": None},
                    {"ownership_expires_at": None},
                    {"$or": [
                        {"updated_at": {"$lte": legacy_stale_before}},
                        {"updated_at": None},
                    ]},
                ]},
            ],
        }
        update = {"$set": {
            "owner": owner,
            "updated_at": now,
            "expires_at": now + timedelta(seconds=seconds),
            "ownership_expires_at": now + timedelta(seconds=seconds),
            "state": "starting",
        }, "$setOnInsert": {"created_at": now}}
        result = await maybe_await(self.collection.update_one(query, update, upsert=False))
        if getattr(result, "matched_count", 0):
            return True
        # Avoid repeatedly attempting an insert (and consequently producing
        # duplicate-key write errors) while a fresh owner is merely holding
        # the stable row. The conditional update above remains the ownership
        # compare-and-set; this read only distinguishes absent from standby.
        existing = await maybe_await(self.collection.find_one({"lease_name": self.heartbeat_lease_name}))
        if existing is not None:
            return False
        try:
            await maybe_await(self.collection.insert_one({
                "lease_name": self.heartbeat_lease_name,
                "owner": owner,
                "created_at": now,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=seconds),
                "ownership_expires_at": now + timedelta(seconds=seconds),
                "state": "starting",
            }))
            return True
        except Exception as exc:
            if exc.__class__.__name__ != "DuplicateKeyError":
                raise
        result = await maybe_await(self.collection.update_one(query, update, upsert=False))
        return bool(getattr(result, "matched_count", 0))

    async def heartbeat(self, owner: str, *, seconds: int) -> bool:
        """Renew liveness only while this worker still owns the heartbeat."""
        now = utcnow()
        result = await maybe_await(self.collection.update_one(
            {"lease_name": self.heartbeat_lease_name, "owner": owner,
             "$or": [{"expires_at": {"$gt": now}}, {"ownership_expires_at": {"$gt": now}}]},
            {"$set": {"updated_at": now, "expires_at": now + timedelta(seconds=seconds),
                      "ownership_expires_at": now + timedelta(seconds=seconds)}},
        ))
        return bool(getattr(result, "matched_count", 0))

    async def update_scheduler_state(self, owner: str, **values: Any) -> bool:
        """Publish diagnostics without allowing a stale process to overwrite them."""
        now = utcnow()
        values.update({"scheduler_updated_at": now, "updated_at": now})
        result = await maybe_await(self.collection.update_one(
            {"lease_name": self.heartbeat_lease_name, "owner": owner,
             "$or": [{"expires_at": {"$gt": now}}, {"ownership_expires_at": {"$gt": now}}]},
            {"$set": values},
        ))
        return bool(getattr(result, "matched_count", 0))

    async def release_heartbeat_owner(self, owner: str) -> None:
        now = utcnow()
        await maybe_await(self.collection.update_one(
            {"lease_name": self.heartbeat_lease_name, "owner": owner},
            {"$set": {"expires_at": now, "ownership_expires_at": now, "updated_at": now, "state": "stopped"}},
        ))

    async def heartbeat_ownership_status(self, owner: str, *, legacy_stale_seconds: int) -> str:
        """Classify ownership for rate-limited standby diagnostics only."""
        now = utcnow()
        record = await maybe_await(self.collection.find_one({"lease_name": self.heartbeat_lease_name}))
        if record is None:
            return "missing"
        if record.get("owner") == owner:
            return "owned"
        expires_at = self._as_utc(record.get("expires_at"))
        compatibility_expires_at = self._as_utc(record.get("ownership_expires_at"))
        if (expires_at and expires_at > now) or (compatibility_expires_at and compatibility_expires_at > now):
            return "active_owner"
        if expires_at is None and compatibility_expires_at is None:
            updated_at = self._as_utc(record.get("updated_at"))
            stale_before = now - timedelta(seconds=legacy_stale_seconds)
            return "legacy_stale" if updated_at is None or updated_at <= stale_before else "legacy_fresh"
        return "expired_owner"

    @staticmethod
    def _as_utc(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    async def last_completed_checkpoint(self) -> datetime | None:
        record = await maybe_await(self.collection.find_one({"lease_name": "hourly-card-update-run"}))
        value = record.get("last_completed_checkpoint") if record else None
        return value if isinstance(value, datetime) else None

    async def complete_cycle(self, owner: str, checkpoint: datetime) -> None:
        await maybe_await(self.collection.update_one(
            {"lease_name": "hourly-card-update-run", "owner": owner, "checkpoint": checkpoint},
            {"$set": {"last_completed_checkpoint": checkpoint, "last_completed_at": utcnow()}},
        ))


class HourlyCardUpdateWorker:
    """Refresh original cards once per UTC checkpoint, with bounded retries."""

    lease_name = "hourly-card-update-run"

    def __init__(
        self, repository: NotificationRepository, updater: TeamsMessageUpdateService, leases: WorkerLeaseRepository,
        *, owner: str | None = None, batch_size: int = 100, heartbeat_seconds: int = 30,
        heartbeat_lease_seconds: int = 90, legacy_heartbeat_stale_seconds: int = 180,
        owner_marker_path: str | Path = "/tmp/hourly-card-updater-owner", now_provider: Callable[[], datetime] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.updater = updater
        self.leases = leases
        self.owner = owner or f"{os.uname().nodename}:{os.getpid()}:{uuid4()}"
        self.batch_size = batch_size
        self.heartbeat_seconds = heartbeat_seconds
        self.heartbeat_lease_seconds = heartbeat_lease_seconds
        self.legacy_heartbeat_stale_seconds = legacy_heartbeat_stale_seconds
        self.owner_marker_path = Path(owner_marker_path)
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_provider or time.monotonic
        self._owns_heartbeat = False
        self._last_completed_checkpoint: datetime | None = None
        self._next_retry_at: datetime | None = None
        self._cycle_completed = False
        self._cycle_in_progress = False
        self._running_checkpoint: datetime | None = None
        self._last_logged_schedule: tuple[datetime, datetime | None] | None = None
        self._last_ownership_diagnostic_at = float("-inf")

    async def run_once(self, now: datetime | None = None) -> int:
        """Process one checkpoint; failures of one card never stop the batch."""
        checkpoint = RiskTimingService.hour_checkpoint(now or self._now())
        self._cycle_completed = False
        if not await self.leases.acquire(self.lease_name, self.owner, checkpoint):
            logger.info("hourly_worker_lease_not_acquired", extra={"checkpoint": checkpoint.isoformat()})
            return 0
        updated = failed = 0
        started = self._monotonic()
        self._cycle_in_progress = True
        self._running_checkpoint = checkpoint
        logger.info("hourly_card_update_cycle_started", extra={"checkpoint": checkpoint.isoformat(), "owner": self.owner})
        try:
            candidates = await self.repository.list_eligible_updates(checkpoint, limit=self.batch_size)
            for candidate in candidates:
                notification_id = candidate.get("notification_id")
                if not isinstance(notification_id, str):
                    continue
                renew = getattr(self.leases, "renew", None)
                if renew is not None:
                    await renew(self.lease_name, self.owner, checkpoint)
                claimed = await self.repository.claim_update(notification_id, checkpoint, self.owner)
                if claimed is None:
                    continue
                try:
                    card = await self.updater.update_notification(claimed)
                except PermanentTeamsUpdateError as exc:
                    failed += 1
                    await self.repository.mark_update_failure(notification_id, checkpoint, self._safe_error(exc),
                                                              permanent=True, retry_at=None, owner=self.owner)
                except RetryableTeamsUpdateError as exc:
                    failed += 1
                    retry_at = utcnow() + timedelta(seconds=self._backoff_seconds(claimed, exc.retry_after))
                    await self.repository.mark_update_failure(notification_id, checkpoint, self._safe_error(exc),
                                                              permanent=False, retry_at=retry_at, owner=self.owner)
                    logger.info("hourly_worker_retry_scheduled", extra={"checkpoint": checkpoint.isoformat(),
                                "notification_id": notification_id, "retry_next_at": retry_at.isoformat()})
                except Exception as exc:  # Keep one bad message from stopping the batch.
                    failed += 1
                    retry_at = utcnow() + timedelta(seconds=self._backoff_seconds(claimed, None))
                    await self.repository.mark_update_failure(notification_id, checkpoint, self._safe_error(exc),
                                                              permanent=False, retry_at=retry_at, owner=self.owner)
                    logger.warning("hourly_card_update_failed", extra={"checkpoint": checkpoint.isoformat(),
                                   "notification_id": notification_id, "error_type": type(exc).__name__})
                else:
                    await self._mark_update_success(notification_id, checkpoint, card)
                    updated += 1
            self._cycle_completed = True
            complete = getattr(self.leases, "complete_cycle", None)
            if complete is not None:
                await complete(self.owner, checkpoint)
            logger.info("hourly_card_update_completed", extra={"checkpoint": checkpoint.isoformat(), "updated": updated,
                        "failed": failed, "duration_ms": int((self._monotonic() - started) * 1000)})
            return updated
        finally:
            self._cycle_in_progress = False
            self._running_checkpoint = None
            await self.leases.release(self.lease_name, self.owner)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Run a short, cancellation-aware UTC scheduler and independent heartbeat."""
        stop_event = stop_event or asyncio.Event()
        logger.info("hourly_worker_started", extra={"owner": self.owner})
        self._clear_local_owner_marker()
        await self._load_last_completed_checkpoint()
        # A restart in the same UTC hour must rediscover persisted retryable
        # notifications; otherwise their retry deadline would be skipped until
        # the next hourly checkpoint.
        await self._refresh_retry_deadline(RiskTimingService.hour_checkpoint(self._now()))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(stop_event))
        try:
            while not stop_event.is_set():
                await self._maintain_heartbeat()
                if self._owns_heartbeat:
                    try:
                        await self._scheduler_tick(self._now())
                    except Exception as exc:
                        logger.exception("hourly_card_update_failed", extra={"owner": self.owner,
                                         "error_type": type(exc).__name__})
                timeout = self._next_wait_seconds(self._now())
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            if self._owns_heartbeat:
                try:
                    await self.leases.release_heartbeat_owner(self.owner)
                except Exception:
                    logger.warning("hourly_worker_shutdown_release_failed", exc_info=True)
            self._clear_local_owner_marker()
            logger.info("hourly_worker_shutdown", extra={"owner": self.owner})

    async def _load_last_completed_checkpoint(self) -> None:
        getter = getattr(self.leases, "last_completed_checkpoint", None)
        if getter is None:
            return
        try:
            self._last_completed_checkpoint = await getter()
        except Exception:
            logger.warning("hourly_worker_checkpoint_load_failed", exc_info=True)

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                await self._maintain_heartbeat()

    async def _maintain_heartbeat(self) -> None:
        try:
            if self._owns_heartbeat:
                self._owns_heartbeat = await self.leases.heartbeat(self.owner, seconds=self.heartbeat_lease_seconds)
            else:
                self._owns_heartbeat = await self.leases.claim_heartbeat_owner(
                    self.owner, seconds=self.heartbeat_lease_seconds,
                    legacy_stale_seconds=self.legacy_heartbeat_stale_seconds,
                )
            if self._owns_heartbeat:
                self._write_local_owner_marker()
                logger.debug("hourly_worker_heartbeat_updated", extra={"owner": self.owner})
                if self._cycle_in_progress:
                    # A bounded Teams update yields to the event loop. Publish
                    # that it is still actively running so health checks do not
                    # mistake a long batch for a stalled scheduler.
                    try:
                        await self.leases.update_scheduler_state(
                            self.owner, state="running", current_checkpoint=self._running_checkpoint,
                        )
                    except Exception:
                        logger.warning("hourly_worker_scheduler_state_failed", exc_info=True)
            else:
                self._clear_local_owner_marker()
                await self._log_ownership_not_acquired()
        except Exception as exc:
            self._owns_heartbeat = False
            self._clear_local_owner_marker()
            logger.warning("hourly_worker_heartbeat_failed", extra={"owner": self.owner,
                           "error_type": type(exc).__name__}, exc_info=True)

    async def _log_ownership_not_acquired(self) -> None:
        """Emit bounded standby diagnostics rather than one log every tick."""
        now_monotonic = self._monotonic()
        if now_monotonic - self._last_ownership_diagnostic_at < max(300, self.heartbeat_seconds * 3):
            return
        self._last_ownership_diagnostic_at = now_monotonic
        state = "unknown"
        try:
            state = await self.leases.heartbeat_ownership_status(
                self.owner, legacy_stale_seconds=self.legacy_heartbeat_stale_seconds
            )
        except Exception:
            logger.warning("hourly_worker_ownership_diagnostic_failed", exc_info=True)
        logger.warning("hourly_worker_lease_not_acquired", extra={"owner": self.owner,
                       "lease_name": WorkerLeaseRepository.heartbeat_lease_name, "ownership_state": state,
                       "retry_in_seconds": self.heartbeat_seconds})

    def _write_local_owner_marker(self) -> None:
        """Let Docker's in-container health check identify this worker, not a peer."""
        try:
            self.owner_marker_path.parent.mkdir(parents=True, exist_ok=True)
            self.owner_marker_path.write_text(self.owner, encoding="utf-8")
        except OSError:
            logger.warning("hourly_worker_owner_marker_write_failed", exc_info=True)

    def _clear_local_owner_marker(self) -> None:
        try:
            if self.owner_marker_path.exists() and self.owner_marker_path.read_text(encoding="utf-8").strip() == self.owner:
                self.owner_marker_path.unlink()
        except OSError:
            logger.warning("hourly_worker_owner_marker_clear_failed", exc_info=True)

    async def _scheduler_tick(self, now: datetime) -> None:
        checkpoint = RiskTimingService.hour_checkpoint(now)
        recovered = self._last_completed_checkpoint and checkpoint > self._last_completed_checkpoint + timedelta(hours=1)
        due_retry = self._next_retry_at is not None and self._next_retry_at <= now
        if checkpoint != self._last_completed_checkpoint or due_retry:
            if recovered:
                logger.info("hourly_worker_recovered", extra={"owner": self.owner,
                            "checkpoint": checkpoint.isoformat(), "last_completed_checkpoint": self._last_completed_checkpoint.isoformat()})
            await self.run_once(checkpoint)
            if self._cycle_completed:
                self._last_completed_checkpoint = checkpoint
                await self._refresh_retry_deadline(checkpoint)
        next_checkpoint = checkpoint + timedelta(hours=1)
        await self.leases.update_scheduler_state(
            self.owner, state="waiting", current_checkpoint=checkpoint,
            last_completed_checkpoint=self._last_completed_checkpoint, next_checkpoint=next_checkpoint,
            next_retry_at=self._next_retry_at,
        )
        schedule_key = (next_checkpoint, self._next_retry_at)
        if schedule_key != self._last_logged_schedule:
            logger.info("hourly_worker_next_checkpoint", extra={"owner": self.owner,
                        "checkpoint": checkpoint.isoformat(), "next_checkpoint": next_checkpoint.isoformat(),
                        "retry_next_at": self._next_retry_at.isoformat() if self._next_retry_at else None})
            self._last_logged_schedule = schedule_key

    async def _refresh_retry_deadline(self, checkpoint: datetime) -> None:
        getter = getattr(self.repository, "next_retry_at", None)
        if getter is None:
            self._next_retry_at = None
            return
        try:
            self._next_retry_at = await getter(checkpoint)
        except Exception:
            self._next_retry_at = None
            logger.warning("hourly_worker_retry_lookup_failed", exc_info=True)

    async def _mark_update_success(self, notification_id: str, checkpoint: datetime, card: dict[str, Any]) -> None:
        """Pass claim ownership to the production repository.

        The fallback preserves compatibility with small legacy test doubles
        whose deliberately minimal method signature predates claim fencing.
        """
        marker = self.repository.mark_update_success
        try:
            await marker(notification_id, checkpoint, card, owner=self.owner)
        except TypeError as exc:
            if "unexpected keyword argument 'owner'" not in str(exc):
                raise
            await marker(notification_id, checkpoint, card)

    def _next_wait_seconds(self, now: datetime) -> float:
        # Short monotonic asyncio waits make OS/Docker suspension harmless: the
        # next wake recomputes UTC rather than trusting an old long sleep.
        wait = float(self.heartbeat_seconds)
        if self._next_retry_at is not None:
            wait = min(wait, max(1.0, (self._next_retry_at - now).total_seconds()))
        return wait

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return f"{type(exc).__name__}: Teams card update failed"

    @staticmethod
    def _backoff_seconds(notification: dict, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 3600)
        failures = int(notification.get("failure_count") or 0) + 1
        base = min(300, 2 ** min(failures, 8))
        return base + random.uniform(0, min(30, base / 2))


async def main() -> None:
    # Keep importing this SDK-dependent factory at process startup so unit
    # tests can exercise the worker state machine without Bot Framework extras.
    from src.bot.adapter import create_adapter
    from src.utils.logger import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.hourly_card_update_worker_enabled:
        logger.warning("hourly_card_update_worker_disabled")
        return
    mongo = MongoManager(settings)
    await mongo.start()
    try:
        adapter = create_adapter(settings)
        renderer = TeamsRiskCardRenderer(GenericRiskViewBuilder())
        updater = TeamsMessageUpdateService(
            RiskService(RiskRepository(mongo=mongo)), renderer, TeamsConversationClient(adapter, settings.microsoft_app_id),
            update_timeout_seconds=settings.teams_message_update_timeout_seconds,
        )
        worker = HourlyCardUpdateWorker(
            NotificationRepository(mongo=mongo), updater, WorkerLeaseRepository(mongo),
            batch_size=settings.hourly_card_update_batch_size,
            heartbeat_seconds=settings.hourly_card_update_heartbeat_seconds,
            heartbeat_lease_seconds=settings.hourly_card_update_lease_seconds,
            legacy_heartbeat_stale_seconds=settings.hourly_card_update_legacy_heartbeat_stale_seconds,
            owner_marker_path=settings.hourly_card_update_owner_marker_path,
        )
        await worker.run_forever()
    finally:
        await mongo.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
