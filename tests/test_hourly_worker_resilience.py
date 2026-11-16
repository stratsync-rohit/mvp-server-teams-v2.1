"""Regression coverage for worker liveness, ownership, and UTC scheduling."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import src.workers.hourly_card_update_worker as worker_module
from src.workers.hourly_card_update_healthcheck import health_diagnostic
from src.workers.hourly_card_update_worker import HourlyCardUpdateWorker, WorkerLeaseRepository


class _HeartbeatCollection:
    """Small CAS-aware fake for heartbeat ownership regression tests."""

    def __init__(self, document):
        self.document = document
        self.inserts = 0

    async def update_one(self, query, update, *, upsert=False):
        assert upsert is False
        if self.document is not None and self._matches(self.document, query):
            self.document.update(update["$set"])
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def insert_one(self, document):
        self.inserts += 1
        if self.document is None:
            self.document = dict(document)
            return
        class DuplicateKeyError(Exception):
            pass
        raise DuplicateKeyError("E11000 duplicate key")

    def find_one(self, query):
        return self.document if self.document and self.document.get("lease_name") == query.get("lease_name") else None

    @classmethod
    def _matches(cls, record, query):
        return all(cls._matches_item(record, key, value) for key, value in query.items())

    @classmethod
    def _matches_item(cls, record, key, value):
        if key == "$or":
            return any(cls._matches(record, clause) for clause in value)
        if key == "$and":
            return all(cls._matches(record, clause) for clause in value)
        actual = record.get(key)
        if value is None:
            return actual is None
        if not isinstance(value, dict):
            return actual == value
        if "$lte" in value:
            return isinstance(actual, datetime) and actual <= value["$lte"]
        if "$gt" in value:
            return isinstance(actual, datetime) and actual > value["$gt"]
        return False


class _Mongo:
    def __init__(self, collection):
        self._collection = collection

    def collection(self, name):
        assert name == "hourly_card_update_leases"
        return self._collection


@pytest.mark.asyncio
async def test_stale_legacy_heartbeat_is_migrated_without_owner_upsert_or_e11000(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)
    collection = _HeartbeatCollection({
        "lease_name": "hourly-card-update-heartbeat", "owner": "old-worker",
        "updated_at": now - timedelta(seconds=181),
    })
    leases = WorkerLeaseRepository(_Mongo(collection))

    assert await leases.claim_heartbeat_owner("new-worker", seconds=90, legacy_stale_seconds=180) is True
    assert collection.inserts == 0
    assert collection.document["owner"] == "new-worker"
    assert collection.document["expires_at"] > now


@pytest.mark.asyncio
async def test_stale_legacy_heartbeat_with_null_expiry_is_migrated(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)
    collection = _HeartbeatCollection({
        "lease_name": "hourly-card-update-heartbeat", "owner": "old-worker",
        "updated_at": now - timedelta(seconds=181), "expires_at": None,
    })

    assert await WorkerLeaseRepository(_Mongo(collection)).claim_heartbeat_owner(
        "new-worker", seconds=90, legacy_stale_seconds=180
    )
    assert collection.document["owner"] == "new-worker"


@pytest.mark.asyncio
async def test_fresh_legacy_heartbeat_is_not_taken_over(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)
    collection = _HeartbeatCollection({
        "lease_name": "hourly-card-update-heartbeat", "owner": "old-worker",
        "updated_at": now - timedelta(seconds=30),
    })
    leases = WorkerLeaseRepository(_Mongo(collection))

    assert await leases.claim_heartbeat_owner("new-worker", seconds=90, legacy_stale_seconds=180) is False
    assert collection.inserts == 0
    assert collection.document["owner"] == "old-worker"


@pytest.mark.asyncio
async def test_new_owner_can_recover_after_expiry_and_then_renew_and_release(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)
    collection = _HeartbeatCollection({
        "lease_name": "hourly-card-update-heartbeat", "owner": "old-worker",
        "updated_at": now, "expires_at": now - timedelta(seconds=1),
    })
    leases = WorkerLeaseRepository(_Mongo(collection))

    assert await leases.claim_heartbeat_owner("new-worker", seconds=90, legacy_stale_seconds=180)
    assert await leases.heartbeat("new-worker", seconds=90)
    await leases.release_heartbeat_owner("new-worker")

    assert collection.document["owner"] == "new-worker"
    assert collection.document["expires_at"] == now


@pytest.mark.asyncio
async def test_concurrent_new_worker_cannot_take_over_active_owner(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)
    collection = _HeartbeatCollection({
        "lease_name": "hourly-card-update-heartbeat", "owner": "first-worker",
        "updated_at": now, "expires_at": now + timedelta(seconds=90),
    })
    leases = WorkerLeaseRepository(_Mongo(collection))

    assert not await leases.claim_heartbeat_owner("second-worker", seconds=90, legacy_stale_seconds=180)
    assert collection.document["owner"] == "first-worker"


@pytest.mark.asyncio
async def test_heartbeat_insert_race_does_not_propagate_duplicate_key_error(monkeypatch):
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_module, "utcnow", lambda: now)

    class RaceCollection(_HeartbeatCollection):
        def __init__(self):
            super().__init__(None)
            self._first_read = True

        def find_one(self, query):
            if self._first_read:
                self._first_read = False
                # A competing worker creates this document just after our
                # absent-row read and before the insert attempt.
                self.document = {
                    "lease_name": "hourly-card-update-heartbeat", "owner": "other-worker",
                    "updated_at": now, "expires_at": now + timedelta(seconds=90),
                }
                return None
            return super().find_one(query)

    assert not await WorkerLeaseRepository(_Mongo(RaceCollection())).claim_heartbeat_owner(
        "new-worker", seconds=90, legacy_stale_seconds=180
    )


class _SchedulerLeases:
    def __init__(self):
        self.states = []

    async def update_scheduler_state(self, owner, **values):
        self.states.append((owner, values))
        return True


class _UnusedRepository:
    async def next_retry_at(self, checkpoint):
        return None


@pytest.mark.asyncio
async def test_scheduler_runs_latest_checkpoint_once_and_recovers_without_replaying_hours():
    leases = _SchedulerLeases()
    worker = HourlyCardUpdateWorker(_UnusedRepository(), object(), leases, owner="one")
    runs = []

    async def run_once(checkpoint):
        runs.append(checkpoint)
        worker._cycle_completed = True
        return 0

    worker.run_once = run_once  # type: ignore[method-assign]
    worker._last_completed_checkpoint = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    latest = datetime(2026, 1, 2, 14, 37, tzinfo=timezone.utc)

    await worker._scheduler_tick(latest)
    await worker._scheduler_tick(latest + timedelta(seconds=30))

    assert runs == [datetime(2026, 1, 2, 14, tzinfo=timezone.utc)]
    assert leases.states[-1][1]["next_checkpoint"] == datetime(2026, 1, 2, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_restart_rediscovers_retry_deadline_for_the_current_checkpoint():
    current = datetime(2026, 1, 2, 14, 10, tzinfo=timezone.utc)

    class Leases:
        async def last_completed_checkpoint(self):
            return datetime(2026, 1, 2, 14, tzinfo=timezone.utc)

    class Repository:
        async def next_retry_at(self, checkpoint):
            assert checkpoint == datetime(2026, 1, 2, 14, tzinfo=timezone.utc)
            return current + timedelta(seconds=20)

    worker = HourlyCardUpdateWorker(Repository(), object(), Leases(), now_provider=lambda: current)
    await worker._load_last_completed_checkpoint()
    await worker._refresh_retry_deadline(worker._last_completed_checkpoint)

    assert worker._next_retry_at == current + timedelta(seconds=20)


def test_healthcheck_accepts_idle_worker_but_rejects_stale_scheduler():
    now = datetime(2026, 1, 2, 14, tzinfo=timezone.utc)
    healthy, _ = health_diagnostic({
        "updated_at": now - timedelta(seconds=31),
        "ownership_expires_at": now + timedelta(seconds=59),
        "scheduler_updated_at": now - timedelta(seconds=31),
        "state": "waiting",
    }, now, heartbeat_seconds=30)
    assert healthy

    healthy, diagnostic = health_diagnostic({
        "updated_at": now - timedelta(seconds=31),
        "ownership_expires_at": now + timedelta(seconds=59),
        "scheduler_updated_at": now - timedelta(minutes=3),
        "state": "waiting",
    }, now, heartbeat_seconds=30)
    assert not healthy
    assert "stalled" in diagnostic


def test_healthcheck_rejects_standby_worker_even_when_another_owner_is_fresh():
    now = datetime(2026, 1, 2, 14, tzinfo=timezone.utc)
    record = {
        "owner": "active-worker",
        "updated_at": now - timedelta(seconds=5),
        "expires_at": now + timedelta(seconds=85),
        "scheduler_updated_at": now - timedelta(seconds=5),
        "state": "waiting",
    }

    healthy, diagnostic = health_diagnostic(record, now, heartbeat_seconds=30, expected_owner="standby-worker")

    assert not healthy
    assert "standby" in diagnostic
